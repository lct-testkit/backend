"""Сборщик исполнения удаления/обезличивания (new_spec §4.8.4 шаг 5).

Раньше `ERASURE_EXECUTED` существовал только как значение `AuditAction` и ни
разу не вызывался: `grace_until` вычислялся роутером для ответа API и нигде
не сохранялся, сборщика не было вовсе. `identity.erasure_service.
ErasureExecutionService.execute` закрывает половину «что» — этот файл
закрывает «когда».

Каждая заявка исполняется в своей собственной транзакции (`session_scope()`
на заявку, а не одна на весь тик), а не в одной общей на весь список, как
`crm.tasks.sweep_sla_breaches` — там мутация одного поля не может провалиться
частично, здесь исполнение делает вызов в Keycloak, загрузку акта в S3 и
несколько мутаций подряд: если оно упадёт на середине одной заявки, откат не
должен задевать уже исполненные соседние. Тот же принцип «батчами, каждый в
своей транзакции с чекпоинтом», что new_spec §4.11 описывает для миграции
статусов при архивировании.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import structlog
from sqlalchemy import select

from app.core.db import session_scope
from app.core.metrics import background_tasks_total
from app.modules.identity.erasure_service import ErasureExecutionService
from app.modules.identity.models import DataErasureRequest, ErasureStatus

logger = structlog.get_logger(__name__)


async def sweep_erasure_requests(ctx: dict[str, Any]) -> dict[str, int]:
    now = dt.datetime.now(dt.UTC)
    async with session_scope() as session:
        due_ids = list(
            (
                await session.execute(
                    select(DataErasureRequest.id).where(
                        DataErasureRequest.status.in_(
                            [ErasureStatus.PENDING.value, ErasureStatus.APPROVED.value]
                        ),
                        DataErasureRequest.grace_until.is_not(None),
                        DataErasureRequest.grace_until <= now,
                    )
                )
            )
            .scalars()
            .all()
        )

    executed = blocked = failed = 0
    for request_id in due_ids:
        try:
            async with session_scope() as session:
                request = await session.get(DataErasureRequest, request_id)
                if request is None:
                    continue
                result = await ErasureExecutionService(session).execute(request)
            if result["executed"]:
                executed += 1
            else:
                blocked += 1
        except Exception:  # noqa: BLE001 — одна проблемная заявка не должна ронять сборщик
            failed += 1
            logger.exception("erasure_execution_failed", request_id=str(request_id))

    background_tasks_total.labels(task="sweep_erasure_requests", result="success").inc()
    if executed or blocked or failed:
        logger.info("erasure_sweep_completed", executed=executed, blocked=blocked, failed=failed)
    return {"executed": executed, "blocked": blocked, "failed": failed}
