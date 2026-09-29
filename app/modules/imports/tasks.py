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

import datetime as dt
import time
import uuid
from typing import Any

import structlog
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.metrics import track_task
from app.modules.imports.models import ImportJob, ImportJobStatus
from app.modules.imports.service import ImportService

logger = structlog.get_logger(__name__)

# Тик cron — раз в минуту; бюджет оставляет запас, чтобы следующий тик не наложился.
_TICK_BUDGET_SECONDS = 40.0

# Текст исключения в `last_error` не кладём: там бывает SQL и значения чужих строк (как и
# `_row_error_text`/`_GENERIC_ROW_ERROR` в `imports.service` для отдельной строки) — подробности
# только в структурированном логе выше, по `job_id` их легко найти.
_GENERIC_BATCH_ERROR = "Не удалось обработать партию импорта (подробности в журнале сервера)"


async def _job_ids(status: ImportJobStatus) -> list[uuid.UUID]:
    async with session_scope() as session:
        rows = await session.execute(
            select(ImportJob.id)
            .where(ImportJob.status == status.value)
            .order_by(ImportJob.created_at)
        )
        return list(rows.scalars().all())


async def _run_one_batch(job_id: uuid.UUID, status: ImportJobStatus, batch_size: int) -> str:
    """Одна партия одного задания в собственной транзакции. Возвращает `skip` (задание занято,
    сменило статус или упало — см. ниже), `more` (есть ещё работа) или `done` (задание
    завершено)."""
    try:
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
    except Exception:
        # Ошибка строки `apply_batch`/`rollback_batch` ловит сама (по SAVEPOINT'у — падает только
        # эта строка), сюда долетает лишь то, что сломалось вне построчного цикла: сам запрос
        # партии, `finalize_*_if_done`, обрыв соединения с БД. Раньше исключение просто улетало
        # через `sweep_import_jobs`/`track_task` (там только метрика и повторный raise) — статус
        # `import_jobs` оставался как был (`applying`/`rolling_back`) навсегда, а пользователь
        # никогда не узнавал, что импорт сломался. `session_scope` уже откатил транзакцию партии
        # выше — здесь отдельная, короткая, только на смену статуса.
        logger.exception("import_job_batch_failed", job_id=str(job_id), status=status.value)
        async with session_scope() as session:
            job = await session.get(ImportJob, job_id)
            # Статус мог уже уйти вперёд (другой тик успел завершить/откатить) — тогда трогать
            # его не нужно, иначе можно затереть terminал `completed`/`rolled_back` на `failed`.
            if job is not None and job.status == status.value:
                job.status = ImportJobStatus.FAILED.value
                job.finished_at = dt.datetime.now(dt.UTC)
                job.last_error = _GENERIC_BATCH_ERROR
        return "skip"


@track_task
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

    if counters["applied_batches"] or counters["rollback_batches"]:
        logger.info("import_jobs_swept", **counters)
    return counters
