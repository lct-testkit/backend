"""Периодическая проверка SLA (new_spec §4.10).

Раз в 15 минут сканирует незакрытые сделки с активным таймером (`sla_state`
не `paused` — статусы типа `parked` таймер останавливают, см.
`app/modules/crm/service.py::_apply_sla_for_status`) и пересчитывает
`sla_state` по доле прошедшего времени: `status_changed_at` — момент входа в
статус, `sla_due_at` — абсолютный дедлайн, значит
`(now - status_changed_at) / (sla_due_at - status_changed_at)` и есть доля
израсходованного срока. Пороги из раздела 4.10: предупреждение владельцу —
`warn_threshold_pct` правила SLA статуса из опубликованного снимка воронки (75%,
если правила уже нет: сделку перенесли с сохранением срока), 100% — владельцу
и руководителю, 150% — тоже breached (отдельного состояния «эскалировано» в
перечне `sla_state` нет, поэтому дальнейшая эскалация видна по количеству
уведомлённых, а не по значению поля).

Частичный индекс `ix_deals_sla_due_open` (только незакрытые сделки) держит
запрос быстрым независимо от общего объёма — тот же приём, что и в
`app/modules/workflow/tasks.py` для мастера сопоставления.
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
from app.modules.crm.models import Deal, SlaState
from app.modules.identity.models import User
from app.modules.notification.service import NotificationPriority, get_notification_service
from app.modules.workflow.models import Workflow

logger = structlog.get_logger(__name__)

WARN_THRESHOLD = 0.75
BREACH_THRESHOLD = 1.0
ESCALATE_THRESHOLD = 1.5


async def _warn_thresholds(
    session: AsyncSession, workflow_ids: set[uuid.UUID]
) -> dict[tuple[uuid.UUID, str], float]:
    """Порог предупреждения по правилам SLA: (воронка, id статуса) → доля срока.
    Сделки живут по снимку воронки на момент публикации, поэтому и правила берутся
    из него, а не из живых `sla_rules` черновика."""
    rows = await session.execute(
        select(Workflow.id, Workflow.published_graph).where(Workflow.id.in_(workflow_ids))
    )
    return {
        (workflow_id, rule["status_id"]): rule["warn_threshold_pct"] / 100
        for workflow_id, graph in rows.all()
        for rule in (graph or {}).get("sla_rules", [])
    }


async def sweep_sla_breaches(ctx: dict[str, Any]) -> dict[str, int]:
    now = dt.datetime.now(dt.UTC)
    warned = breached = escalated = 0

    async with session_scope() as session:
        deals = (
            (
                await session.execute(
                    select(Deal).where(
                        Deal.sla_due_at.is_not(None),
                        Deal.closed_at.is_(None),
                        Deal.deleted_at.is_(None),
                        Deal.sla_state != SlaState.PAUSED.value,
                    )
                )
            )
            .scalars()
            .all()
        )

        warn_thresholds = await _warn_thresholds(session, {deal.workflow_id for deal in deals})

        for deal in deals:
            total = (deal.sla_due_at - deal.status_changed_at).total_seconds()
            if total <= 0:
                continue
            fraction = (now - deal.status_changed_at).total_seconds() / total
            warn_threshold = warn_thresholds.get((deal.workflow_id, str(deal.status_id)))

            if fraction >= BREACH_THRESHOLD:
                new_state = SlaState.BREACHED.value
            elif fraction >= (WARN_THRESHOLD if warn_threshold is None else warn_threshold):
                new_state = SlaState.WARNING.value
            else:
                new_state = SlaState.OK.value

            if new_state == deal.sla_state:
                continue
            deal.sla_state = new_state

            if new_state == SlaState.WARNING.value:
                warned += 1
                await get_notification_service().notify_user(
                    session,
                    recipient_id=deal.owner_id,
                    template_code="DEAL_SLA_WARNING",
                    priority=NotificationPriority.NORMAL,
                    entity_type="deal",
                    entity_id=deal.id,
                )
            elif new_state == SlaState.BREACHED.value:
                breached += 1
                recipients = {deal.owner_id}
                owner = await session.get(User, deal.owner_id)
                if owner and owner.manager_id:
                    recipients.add(owner.manager_id)
                if fraction >= ESCALATE_THRESHOLD:
                    escalated += 1
                for recipient_id in recipients:
                    await get_notification_service().notify_user(
                        session,
                        recipient_id=recipient_id,
                        template_code="DEAL_SLA_BREACHED",
                        priority=NotificationPriority.HIGH,
                        entity_type="deal",
                        entity_id=deal.id,
                    )

    background_tasks_total.labels(task="sweep_sla_breaches", result="success").inc()
    if warned or breached:
        logger.info("sla_sweep_completed", warned=warned, breached=breached, escalated=escalated)
    return {"warned": warned, "breached": breached, "escalated": escalated}
