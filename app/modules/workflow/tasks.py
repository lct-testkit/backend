"""Периодическая докрутка переноса сделок при архивировании статуса.

`WorkflowService.archive_status` обрабатывает первую партию синхронно в
пределах HTTP-запроса (раздел 6.5 требует «переводятся батчами по 100» —
именно поэтому одна партия и есть бюджет запроса). Если сделок больше,
задача остаётся `running`, а остаток докручивает эта периодическая задача.

Сделано именно периодическим сканом, а не постановкой продолжения в очередь
из `archive_status`: запись `status_mapping_jobs` создаётся в той же
транзакции, что и HTTP-запрос, и коммитится только после того, как хендлер
вернёт ответ. Если бы `archive_status` сразу ставил задачу в очередь arq,
воркер мог бы забрать её и прочитать `status_mapping_jobs` раньше, чем
транзакция закоммитится — то есть до того, как строка вообще станет видна
другому соединению. Периодический скан снимает эту гонку и заодно даёт
восстановление после падения воркера: недокрученная задача останется
`running`, и следующий проход подхватит её заново.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import session_scope
from app.core.metrics import background_tasks_total
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.crm.service import get_deal_status_service
from app.modules.workflow.models import MappingJobStatus, StatusMappingJob, WorkflowStatus
from app.modules.workflow.service import MAPPING_BATCH_SIZE, invalidate_workflow_cache

logger = structlog.get_logger(__name__)


async def _process_one_batch(session: AsyncSession, job: StatusMappingJob) -> None:
    rules = job.mapping_rules
    target_status_id = uuid.UUID(rules["target_status_id"])
    fallback_raw = rules.get("fallback_status_id")

    result = await get_deal_status_service().migrate_batch(
        session,
        from_status_id=job.from_status_id,
        target_status_id=target_status_id,
        fallback_status_id=uuid.UUID(fallback_raw) if fallback_raw else None,
        sla_mode=rules.get("sla_mode", "recalculate"),
        batch_size=MAPPING_BATCH_SIZE,
    )
    job.processed_count += result.processed
    job.failed_count += result.failed

    if result.has_more:
        return

    job.status = MappingJobStatus.COMPLETED.value
    job.finished_at = dt.datetime.now(dt.UTC)
    job.report = {"processed": job.processed_count, "failed": job.failed_count}

    status = await session.get(WorkflowStatus, job.from_status_id)
    if status is not None:
        status.is_archived = True
        status.archived_at = job.finished_at
        status.replaced_by_status_id = target_status_id
        await invalidate_workflow_cache(status.workflow_id)

    await AuditService(session).record(
        AuditAction.STATUS_MAPPING_COMPLETED,
        entity_type="status_mapping_job",
        entity_id=job.id,
        changes={"processed": job.processed_count, "failed": job.failed_count},
    )


async def sweep_status_mapping_jobs(ctx: dict[str, Any]) -> dict[str, int]:
    """Обрабатывает по одной партии для каждой незавершённой задачи сопоставления."""
    touched = 0
    async with session_scope() as session:
        jobs = list(
            (
                await session.execute(
                    select(StatusMappingJob).where(
                        StatusMappingJob.status == MappingJobStatus.RUNNING.value
                    )
                )
            )
            .scalars()
            .all()
        )
        for job in jobs:
            await _process_one_batch(session, job)
            touched += 1

    background_tasks_total.labels(task="sweep_status_mapping_jobs", result="success").inc()
    if touched:
        logger.info("status_mapping_jobs_swept", jobs_touched=touched)
    return {"jobs_touched": touched}
