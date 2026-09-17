"""Периодическая докрутка импорта каталогов: батчи по 500 с чекпоинтом.

Тот же приём, что `workflow.tasks.sweep_status_mapping_jobs`: HTTP-хендлер
(`ImportService.start_apply`/`start_rollback`) только меняет статус задания в
рамках своей транзакции, построчную обработку продолжает эта задача — по
одной партии на задание за тик, чтобы ни одно задание не монополизировало
воркер и падение между тиками не теряло прогресс (чекпоинт — это набор строк
`import_row_results` с уже проставленным `entity_id`/терминальным статусом).
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.metrics import background_tasks_total
from app.modules.imports.models import ImportJob, ImportJobStatus
from app.modules.imports.service import ImportService

logger = structlog.get_logger(__name__)


async def sweep_import_jobs(ctx: dict[str, Any]) -> dict[str, int]:
    settings = get_settings()
    applied_batches = rollback_batches = completed = rolled_back = 0

    async with session_scope() as session:
        service = ImportService(session)

        applying = list(
            (
                await session.execute(
                    select(ImportJob).where(ImportJob.status == ImportJobStatus.APPLYING.value)
                )
            )
            .scalars()
            .all()
        )
        for job in applying:
            processed = await service.apply_batch(job, batch_size=settings.import_batch_size)
            if processed:
                applied_batches += 1
            if await service.finalize_apply_if_done(job):
                completed += 1

        rolling_back = list(
            (
                await session.execute(
                    select(ImportJob).where(ImportJob.status == ImportJobStatus.ROLLING_BACK.value)
                )
            )
            .scalars()
            .all()
        )
        for job in rolling_back:
            processed = await service.rollback_batch(job, batch_size=settings.import_batch_size)
            if processed:
                rollback_batches += 1
            if await service.finalize_rollback_if_done(job):
                rolled_back += 1

    background_tasks_total.labels(task="sweep_import_jobs", result="success").inc()
    if applied_batches or rollback_batches:
        logger.info(
            "import_jobs_swept",
            applied_batches=applied_batches,
            rollback_batches=rollback_batches,
            completed=completed,
            rolled_back=rolled_back,
        )
    return {
        "applied_batches": applied_batches,
        "rollback_batches": rollback_batches,
        "completed": completed,
        "rolled_back": rolled_back,
    }
