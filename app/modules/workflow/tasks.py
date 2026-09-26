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
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import session_scope
from app.core.metrics import background_tasks_total
from app.modules.workflow.models import MappingJobStatus, StatusMappingJob
from app.modules.workflow.service import WorkflowService

logger = structlog.get_logger(__name__)


async def _process_one_batch(session: AsyncSession, job: StatusMappingJob) -> None:
    """Одна партия задачи. Логика переноса и завершения — в `WorkflowService.advance_mapping_job`
    (тот же шаг, что делает первая, синхронная партия HTTP-запроса): курсор по id сделок,
    новый проход, если что-то переносилось, и `failed` без архивации статуса, если остались
    только застрявшие сделки."""
    await WorkflowService(session).advance_mapping_job(job)


async def sweep_status_mapping_jobs(ctx: dict[str, Any]) -> dict[str, int]:
    """Обрабатывает по одной партии для каждой незавершённой задачи сопоставления."""
    touched = 0
    async with session_scope() as session:
        # `skip_locked`: тик, начавшийся до окончания прошлого, не берёт задачи, которые тот
        # ещё обрабатывает, — иначе одну партию переносили бы дважды.
        jobs = list(
            (
                await session.execute(
                    select(StatusMappingJob)
                    .where(StatusMappingJob.status == MappingJobStatus.RUNNING.value)
                    .order_by(StatusMappingJob.created_at)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for job in jobs:
            job_id = str(job.id)  # после отката SAVEPOINT объект истечёт, а читать атрибуты нельзя
            try:
                # SAVEPOINT: сбой одной задачи не откатывает остальные и не заставляет воркер
                # каждую минуту повторять ту же ошибку на всей пачке.
                async with session.begin_nested():
                    await _process_one_batch(session, job)
            except Exception as exc:  # noqa: BLE001
                logger.exception("status_mapping_job_failed", job_id=job_id)
                job.status = MappingJobStatus.FAILED.value
                job.finished_at = dt.datetime.now(dt.UTC)
                job.error = f"{type(exc).__name__}: перенос остановлен, подробности в журнале"
            touched += 1

    background_tasks_total.labels(task="sweep_status_mapping_jobs", result="success").inc()
    if touched:
        logger.info("status_mapping_jobs_swept", jobs_touched=touched)
    return {"jobs_touched": touched}
