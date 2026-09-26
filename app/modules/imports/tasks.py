"""Периодическая докрутка импорта каталогов: батчи по 500 с чекпоинтом.

Тот же приём, что `workflow.tasks.sweep_status_mapping_jobs`: HTTP-хендлер
(`ImportService.start_apply`/`start_rollback`) только меняет статус задания в
рамках своей транзакции, построчную обработку продолжает эта задача. Чекпоинт —
набор строк `import_row_results` с уже проставленным `entity_id`/терминальным
статусом, поэтому падение воркера между партиями ничего не теряет.

За один тик задание получает столько партий, сколько влезает в бюджет времени (раньше — одна
партия в минуту: 100 000 строк занимали бы около трёх часов), и каждая партия идёт в своей
транзакции — коммит на границе партии и есть чекпоинт. Строка задания берётся под
`FOR UPDATE SKIP LOCKED`: если предыдущий тик ещё не закончил, следующий его не подхватывает —
раньше перекрывшиеся тики обрабатывали одни и те же строки дважды.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import structlog
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.metrics import background_tasks_total
from app.modules.imports.models import ImportJob, ImportJobStatus
from app.modules.imports.service import ImportService

logger = structlog.get_logger(__name__)

# Тик cron — раз в минуту; бюджет оставляет запас, чтобы следующий тик не наложился.
_TICK_BUDGET_SECONDS = 40.0


async def _job_ids(status: ImportJobStatus) -> list[uuid.UUID]:
    async with session_scope() as session:
        rows = await session.execute(
            select(ImportJob.id)
            .where(ImportJob.status == status.value)
            .order_by(ImportJob.created_at)
        )
        return list(rows.scalars().all())


async def _run_one_batch(job_id: uuid.UUID, status: ImportJobStatus, batch_size: int) -> str:
    """Одна партия одного задания в собственной транзакции. Возвращает `skip` (задание занято или
    сменило статус), `more` (есть ещё работа) или `done` (задание завершено)."""
    async with session_scope() as session:
        job = (
            await session.execute(
                select(ImportJob)
                .where(ImportJob.id == job_id, ImportJob.status == status.value)
                .with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if job is None:
            return "skip"
        service = ImportService(session)
        if status is ImportJobStatus.APPLYING:
            processed = await service.apply_batch(job, batch_size=batch_size)
            finished = await service.finalize_apply_if_done(job)
        else:
            processed = await service.rollback_batch(job, batch_size=batch_size)
            finished = await service.finalize_rollback_if_done(job)
        if finished:
            return "done"
        return "more" if processed else "done"


async def sweep_import_jobs(ctx: dict[str, Any]) -> dict[str, int]:
    settings = get_settings()
    deadline = time.monotonic() + _TICK_BUDGET_SECONDS
    counters = {"applied_batches": 0, "rollback_batches": 0, "completed": 0, "rolled_back": 0}

    for status, batch_key, done_key in (
        (ImportJobStatus.APPLYING, "applied_batches", "completed"),
        (ImportJobStatus.ROLLING_BACK, "rollback_batches", "rolled_back"),
    ):
        for job_id in await _job_ids(status):
            while time.monotonic() < deadline:
                outcome = await _run_one_batch(job_id, status, settings.import_batch_size)
                if outcome == "skip":
                    break
                counters[batch_key] += 1
                if outcome == "done":
                    counters[done_key] += 1
                    break

    background_tasks_total.labels(task="sweep_import_jobs", result="success").inc()
    if counters["applied_batches"] or counters["rollback_batches"]:
        logger.info("import_jobs_swept", **counters)
    return counters
