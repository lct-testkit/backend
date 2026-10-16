"""Фоновые задачи отчётности (new_spec §3.4/§4.13).

* `render_report_job` (B8-11) — задача arq, которую `ReportJobService.create`
  ставит в очередь сразу после коммита (`enqueue_after_commit`): не ждать
  минутного тика, пока жюри смотрит на «зависший» тяжёлый отчёт. Захват —
  короткая транзакция `UPDATE ... WHERE status='queued' RETURNING`,
  рендер — отдельная транзакция без `FOR UPDATE` (строка уже уникально
  захвачена этим UPDATE). Модульный семафор ограничивает число одновременных
  рендеров ЭТОГО процесса воркера (раздел 4.13: «конкурентность воркеров
  ограничена семафором (10)»; несколько реплик воркера делят общий пул БД).
* `sweep_report_jobs` — теперь добор, а не основной рендер (DEBT-8):
  возвращает в `queued` задания `processing`, зависшие дольше
  `reports_stale_processing_minutes` (воркер упал посреди рендера), и
  переставляет в очередь arq `queued`-задания старше
  `reports_enqueue_retry_after_s` (постановка потерялась — сбой Redis). При
  `settings.reports_enqueue=false` (откат без деплоя) ведёт себя как раньше:
  сам рендерит пачку конкурентно (`asyncio.gather`), без arq вовсе.
* `expire_report_files` — раздел 4.13: «готовые файлы автоудаляются через
  7 дней». Удаляется файл, не запись `report_jobs` (история осталась,
  `file_id` — `ON DELETE SET NULL`, см. `reporting.models`).
* `refresh_report_materialized_views` — раздел 3.4/4.13: «материализованные
  представления с REFRESH CONCURRENTLY по расписанию».
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from typing import Any

import structlog
from sqlalchemy import func, select, text, update

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.metrics import reports_in_progress, track_task
from app.core.security import Principal, TokenClaims
from app.modules.files.models import File, FileStatus
from app.modules.files.service import delete_object_if_unreferenced
from app.modules.identity.models import User
from app.modules.reporting.models import ReportJob, ReportJobStatus
from app.modules.reporting.service import ReportJobService

logger = structlog.get_logger(__name__)

# B8-11: ограничивает одновременные рендеры ЭТОГО процесса воркера (модульный, не per-job) —
# создаётся лениво, чтобы размер читался из настроек в момент первого использования, а не при
# импорте модуля (важно для тестов, подменяющих `get_settings`).
_render_semaphore: asyncio.Semaphore | None = None


def _get_render_semaphore() -> asyncio.Semaphore:
    global _render_semaphore
    if _render_semaphore is None:
        _render_semaphore = asyncio.Semaphore(get_settings().reports_max_concurrent)
    return _render_semaphore


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


async def _claim_queued(session: Any, job_id: uuid.UUID) -> ReportJob | None:
    """Берёт задание из очереди под `FOR UPDATE SKIP LOCKED`.

    `processing` в БД не коммитится (генерация идёт одной транзакцией), поэтому «занято»
    выражено самой блокировкой строки: пересекающийся тик (cron раз в минуту, а отчёт может
    считаться дольше) пропускает уже взятое задание, а не строит тот же отчёт второй раз.
    Сбой воркера откатывает транзакцию, и задание само возвращается в очередь. `None` — задание
    уже взято другим тиком или обработано."""
    return (
        await session.execute(
            select(ReportJob)
            .where(ReportJob.id == job_id, ReportJob.status == ReportJobStatus.QUEUED.value)
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()


async def _generate_one(job_id: uuid.UUID) -> bool | None:
    """Рендерит один взятый в тик job. `True` — успех, `False` — ушёл в `failed`,
    `None` — уже забрано другим тиком/воркером (не считается ни успехом, ни сбоем).

    Отдельная функция (а не тело цикла) — чтобы `sweep_report_jobs` мог запустить
    несколько таких корутин конкурентно через `asyncio.gather`, а не одну за другой:
    каждая — со своей `session_scope()`/`AsyncSession`, так что параллельный рендер
    не делит одно соединение и не мешает друг другу через общий транзакционный стейт.
    """
    async with session_scope() as session:
        service = ReportJobService(session)
        job = await _claim_queued(session, job_id)
        if job is None:
            return None
        user = await session.get(User, job.requested_by)
        if user is None:
            await service.mark_failed(job, "Пользователь, запросивший отчёт, не найден")
            return False
        principal = _principal_for_worker(user)
        try:
            # `sweep_report_jobs` берёт за тик не больше `reports_max_concurrent` заданий, но
            # тики перекрываются, пока отчёт считается дольше минуты: тогда
            # `crm_reports_in_progress` честно покажет значение выше лимита.
            with reports_in_progress.track_inprogress():
                await service.generate(job, principal)
            return True
        except Exception:  # noqa: BLE001 — сбой одного отчёта не должен ронять тик
            # Клиент видит `report_jobs.error`: текст исключения (SQL, пути, данные) туда не
            # попадает. Подробности — в логе, по коду ошибки из сообщения.
            error_id = uuid.uuid4().hex[:12]
            logger.exception("report_generation_failed", job_id=str(job_id), error_id=error_id)
            # Неудачный flush внутри `generate()` мог оставить сессию в состоянии,
            # требующем отката, прежде чем ей снова можно пользоваться.
            await session.rollback()
            job = await _claim_queued(session, job_id)
            if job is not None:
                await service.mark_failed(
                    job, f"Не удалось сформировать отчёт. Код ошибки для поддержки: {error_id}"
                )
            return False


async def _render_claimed(job_id: uuid.UUID, requested_by: uuid.UUID) -> bool:
    """Рендерит задание, уже атомарно переведённое в `processing` (см. `render_report_job`) —
    без `FOR UPDATE`: строку уникально захватил атомарный `UPDATE ... RETURNING` выше, повторный
    захват здесь не нужен и не предохранил бы от того, от чего уже предохранил тот `UPDATE`."""
    async with session_scope() as session:
        service = ReportJobService(session)
        job = await session.get(ReportJob, job_id)
        if job is None:
            return False
        user = await session.get(User, requested_by)
        if user is None:
            await service.mark_failed(job, "Пользователь, запросивший отчёт, не найден")
            return False
        principal = _principal_for_worker(user)
        try:
            with reports_in_progress.track_inprogress():
                await service.generate(job, principal)
            return True
        except Exception:  # noqa: BLE001 — сбой одного отчёта не должен ронять задачу arq
            error_id = uuid.uuid4().hex[:12]
            logger.exception("report_generation_failed", job_id=str(job_id), error_id=error_id)
            await session.rollback()
            job = await session.get(ReportJob, job_id)
            if job is not None:
                await service.mark_failed(
                    job, f"Не удалось сформировать отчёт. Код ошибки для поддержки: {error_id}"
                )
            return False


@track_task
async def render_report_job(ctx: dict[str, Any], job_id: str) -> bool | None:
    """B8-11: задача arq, поставленная `ReportJobService.create` сразу после коммита.

    `None` — задание уже взято (дубль постановки, повторная доставка arq, добор опередил):
    не ошибка. Таймаут 600 с — заметно больше бюджета самого тяжёлого builder'а; превышение
    переводит задание в `failed`, а не оставляет «вечно processing»."""
    job_uuid = uuid.UUID(job_id)
    async with session_scope() as session:
        requested_by = await session.scalar(
            update(ReportJob)
            .where(ReportJob.id == job_uuid, ReportJob.status == ReportJobStatus.QUEUED.value)
            .values(status=ReportJobStatus.PROCESSING.value, started_at=dt.datetime.now(dt.UTC))
            .returning(ReportJob.requested_by)
        )
    if requested_by is None:
        return None

    async with _get_render_semaphore():
        try:
            return await asyncio.wait_for(_render_claimed(job_uuid, requested_by), timeout=600)
        except TimeoutError:
            async with session_scope() as session:
                job = await session.get(ReportJob, job_uuid)
                if job is not None and job.status == ReportJobStatus.PROCESSING.value:
                    await ReportJobService(session).mark_failed(
                        job, "Превышено время рендера отчёта"
                    )
            return False


@track_task
async def sweep_report_jobs(ctx: dict[str, Any]) -> dict[str, int]:
    settings = get_settings()
    now = dt.datetime.now(dt.UTC)

    async with session_scope() as session:
        # Воркер упал посреди рендера: `processing` навсегда не коммитится сам — возвращаем в
        # очередь, чтобы его забрал либо следующий добор (ниже), либо сам следующий тик при
        # `reports_enqueue=false`. Порог заметно больше таймаута `render_report_job` (600 с).
        stale_cutoff = now - dt.timedelta(minutes=settings.reports_stale_processing_minutes)
        requeued = (
            (
                await session.execute(
                    update(ReportJob)
                    .where(
                        ReportJob.status == ReportJobStatus.PROCESSING.value,
                        ReportJob.started_at.is_not(None),
                        ReportJob.started_at < stale_cutoff,
                    )
                    .values(status=ReportJobStatus.QUEUED.value, started_at=None)
                    .returning(ReportJob.id)
                )
            )
            .scalars()
            .all()
        )

        if not settings.reports_enqueue:
            # Откат «только cron»: без arq вовсе, тик сам рендерит пачку конкурентно —
            # поведение до B8-11.
            currently_processing = await session.scalar(
                select(func.count(ReportJob.id)).where(
                    ReportJob.status == ReportJobStatus.PROCESSING.value
                )
            )
            capacity = max(0, settings.reports_max_concurrent - (currently_processing or 0))
            job_ids = (
                []
                if capacity == 0
                else (
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
        else:
            # Добор: `queued` старше порога — постановка в arq потерялась (сбой Redis на
            # коммите) или задача ещё не добралась до воркера. Тот же `job_id`, что при
            # постановке из API: arq отбрасывает дубль, если оригинал всё же дошёл.
            retry_cutoff = now - dt.timedelta(seconds=settings.reports_enqueue_retry_after_s)
            job_ids = (
                (
                    await session.execute(
                        select(ReportJob.id).where(
                            ReportJob.status == ReportJobStatus.QUEUED.value,
                            ReportJob.created_at < retry_cutoff,
                        )
                    )
                )
                .scalars()
                .all()
            )

    if not settings.reports_enqueue:
        results = await asyncio.gather(*(_generate_one(job_id) for job_id in job_ids))
        processed = sum(1 for ok in results if ok is True)
        failed = sum(1 for ok in results if ok is False)
        if job_ids or requeued:
            logger.info(
                "report_jobs_swept", processed=processed, failed=failed, requeued=len(requeued)
            )
        return {"processed": processed, "failed": failed, "requeued": len(requeued)}

    pool = ctx.get("redis")
    enqueued = 0
    for job_id in job_ids:
        if pool is None:
            break
        await pool.enqueue_job("render_report_job", str(job_id), _job_id=f"report:{job_id}")
        enqueued += 1

    if job_ids or requeued:
        logger.info("report_jobs_swept", enqueued=enqueued, requeued=len(requeued))
    return {"processed": 0, "failed": 0, "enqueued": enqueued, "requeued": len(requeued)}


@track_task
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
                file.status = FileStatus.DELETED.value
                file.deleted_at = now
                file.refcount = max(0, file.refcount - 1)
                await session.flush()
                # Объект удаляется, только если на него не ссылается другая живая запись `files`:
                # дедупликация могла направить туда файл пользователя, и безусловное удаление
                # оставляло его `ready` без содержимого.
                await delete_object_if_unreferenced(
                    session, bucket=file.bucket, key=file.storage_key, excluding_file_id=file.id
                )
            job.file_id = None
            expired += 1
        if jobs:
            await session.flush()

    if expired:
        logger.info("report_files_expired", expired=expired)
    return {"expired": expired}


@track_task
async def refresh_report_materialized_views(ctx: dict[str, Any]) -> dict[str, int]:
    async with session_scope() as session:
        # REFRESH MATERIALIZED VIEW не грантится — только владельцу или
        # суперпользователю, а сессия работает под crm_app. Обёртка
        # `refresh_mv_deal_status_summary()` — SECURITY DEFINER, заведена
        # миграцией 0013_reporting_mv_refresh_definer.
        await session.execute(text("SELECT refresh_mv_deal_status_summary()"))
    return {"refreshed": 1}
