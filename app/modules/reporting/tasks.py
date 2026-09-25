"""Фоновые задачи отчётности (new_spec §3.4/§4.13).

* `sweep_report_jobs` — раздел 4.13: «конкурентность воркеров ограничена
  семафором (10)». Каждый job обрабатывается в собственной `session_scope()`
  (а не одной сессией на весь тик, как `imports.tasks.sweep_import_jobs`):
  там per-row ошибки перехватываются внутри самого сервиса и никогда не
  всплывают исключением, а здесь `ReportJobService.generate` намеренно
  исключения не глушит (см. её докстринг) — один job, "отравивший" сессию
  неудачным flush, не должен утащить за собой обработку соседних в этом же
  тике.
* `expire_report_files` — раздел 4.13: «готовые файлы автоудаляются через
  7 дней». Удаляется файл, не запись `report_jobs` (история осталась,
  `file_id` — `ON DELETE SET NULL`, см. `reporting.models`).
* `refresh_report_materialized_views` — раздел 3.4/4.13: «материализованные
  представления с REFRESH CONCURRENTLY по расписанию».
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import structlog
from sqlalchemy import func, select, text

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.metrics import background_tasks_total
from app.core.security import Principal, TokenClaims
from app.core.storage import delete_object
from app.modules.files.models import File, FileStatus
from app.modules.identity.models import User
from app.modules.reporting.models import ReportJob, ReportJobStatus
from app.modules.reporting.service import ReportJobService

logger = structlog.get_logger(__name__)


def _principal_for_worker(user: User) -> Principal:
    """Реконструирует `Principal` из строки `users` для фонового рендеринга —
    там нет ни HTTP-запроса, ни JWT, только `report_jobs.requested_by`.
    `RBAC`-скоуп (`deal_scope_clause`) читает только `role`/`user_id`/
    `team_id`, поэтому синтетических `claims` достаточно для корректной
    фильтрации; `raw.source` помечает их так, чтобы не притворяться
    настоящей браузерной сессией, если кто-то когда-нибудь распечатает их
    в логе."""
    return Principal(
        user_id=user.id,
        keycloak_id=user.keycloak_id or "",
        role=user.role,
        status=user.status,
        email=user.email,
        full_name=user.effective_name,
        team_id=user.team_id,
        manager_id=user.manager_id,
        perm_epoch=user.perm_epoch,
        session_id=None,
        consent_version=user.consent_version,
        must_change_password=False,
        claims=TokenClaims(
            subject=user.keycloak_id or str(user.id), raw={"source": "reporting_worker"}
        ),
    )


async def sweep_report_jobs(ctx: dict[str, Any]) -> dict[str, int]:
    settings = get_settings()

    async with session_scope() as session:
        currently_processing = await session.scalar(
            select(func.count(ReportJob.id)).where(
                ReportJob.status == ReportJobStatus.PROCESSING.value
            )
        )
        capacity = max(0, settings.reports_max_concurrent - (currently_processing or 0))
        if capacity == 0:
            return {"processed": 0, "failed": 0}
        job_ids = (
            (
                await session.execute(
                    select(ReportJob.id)
                    .where(ReportJob.status == ReportJobStatus.QUEUED.value)
                    .order_by(ReportJob.created_at)
                    .limit(capacity)
                )
            )
            .scalars()
            .all()
        )

    processed = failed = 0
    for job_id in job_ids:
        async with session_scope() as session:
            service = ReportJobService(session)
            job = await session.get(ReportJob, job_id)
            if job is None or job.status != ReportJobStatus.QUEUED.value:
                continue
            user = await session.get(User, job.requested_by)
            if user is None:
                await service.mark_failed(job, "Пользователь, запросивший отчёт, не найден")
                failed += 1
                continue
            principal = _principal_for_worker(user)
            try:
                await service.generate(job, principal)
                processed += 1
            except Exception as exc:  # noqa: BLE001 — сбой одного отчёта не должен ронять тик
                logger.exception("report_generation_failed", job_id=str(job_id))
                # Неудачный flush внутри `generate()` мог оставить сессию в
                # состоянии, требующем отката, прежде чем ей снова можно
                # пользоваться — см. докстринг модуля.
                await session.rollback()
                job = await session.get(ReportJob, job_id)
                await service.mark_failed(job, f"{type(exc).__name__}: {exc}")
                failed += 1

    background_tasks_total.labels(task="sweep_report_jobs", result="success").inc()
    if job_ids:
        logger.info("report_jobs_swept", processed=processed, failed=failed)
    return {"processed": processed, "failed": failed}


async def expire_report_files(ctx: dict[str, Any]) -> dict[str, int]:
    now = dt.datetime.now(dt.UTC)
    expired = 0
    async with session_scope() as session:
        jobs = (
            (
                await session.execute(
                    select(ReportJob).where(
                        ReportJob.status == ReportJobStatus.COMPLETED.value,
                        ReportJob.file_id.is_not(None),
                        ReportJob.expires_at.is_not(None),
                        ReportJob.expires_at < now,
                    )
                )
            )
            .scalars()
            .all()
        )
        for job in jobs:
            file = await session.get(File, job.file_id)
            if file is not None and file.status == FileStatus.READY.value:
                await delete_object(bucket=file.bucket, key=file.storage_key)
                file.status = FileStatus.DELETED.value
                file.deleted_at = now
                file.refcount = max(0, file.refcount - 1)
            job.file_id = None
            expired += 1
        if jobs:
            await session.flush()

    background_tasks_total.labels(task="expire_report_files", result="success").inc()
    if expired:
        logger.info("report_files_expired", expired=expired)
    return {"expired": expired}


async def refresh_report_materialized_views(ctx: dict[str, Any]) -> dict[str, int]:
    async with session_scope() as session:
        # REFRESH MATERIALIZED VIEW не грантится — только владельцу или
        # суперпользователю, а сессия работает под crm_app. Обёртка
        # `refresh_mv_deal_status_summary()` — SECURITY DEFINER, заведена
        # миграцией 0013_reporting_mv_refresh_definer.
        await session.execute(text("SELECT refresh_mv_deal_status_summary()"))
    background_tasks_total.labels(task="refresh_report_materialized_views", result="success").inc()
    return {"refreshed": 1}
