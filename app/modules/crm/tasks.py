"""Периодическая проверка SLA (new_spec §4.10).

Раз в 15 минут сканирует незакрытые сделки с активным таймером (`sla_state`
не `paused` — статусы типа `parked` таймер останавливают, см.
`app/modules/crm/service.py::_apply_sla_for_status`) и пересчитывает
`sla_state` по доле прошедшего времени: `status_changed_at` — момент входа в
статус, `sla_due_at` — абсолютный дедлайн, значит
`(now - status_changed_at) / (sla_due_at - status_changed_at)` и есть доля
израсходованного срока.

Пороги и адресаты берутся из правила SLA статуса в опубликованном снимке воронки (сделки
живут по снимку, а не по живым `sla_rules` черновика):

* предупреждение владельцу — `warn_threshold_pct` (75%, если правила уже нет: сделку перенесли
  с сохранением срока);
* 100% — нарушение, владельцу и руководителю;
* `escalate_threshold_pct` (по умолчанию 150%) — эскалация: `escalate_to_user_id` либо
  сотрудники роли `escalate_to_role` (для HEAD — руководитель команды владельца). Отдельного
  состояния «эскалировано» в перечне `sla_state` нет, поэтому факт эскалации хранится в
  `deals.sla_escalated_at` и повторно не шлётся;
* `channels` правила пробрасываются в уведомление; пустой список означает «не уведомлять».

Скан идёт партиями по возрастанию id (каждая — своя короткая транзакция) и выбирает только
сделки, у которых состояние ещё может измениться: нарушенные и уже эскалированные не читаются.
Частичный индекс `ix_deals_sla_due_open` (только незакрытые сделки) держит запрос быстрым
независимо от общего объёма — тот же приём, что и в `app/modules/workflow/tasks.py`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, NamedTuple

import structlog
from sqlalchemy import and_, func, not_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import session_scope
from app.core.metrics import sla_breaching_deals, sla_violations_total, track_task
from app.modules.crm.models import Deal, SlaState
from app.modules.identity.models import Team, User, UserStatus
from app.modules.notification.service import NotificationPriority, get_notification_service
from app.modules.workflow.models import Workflow

logger = structlog.get_logger(__name__)

WARN_THRESHOLD = 0.75
BREACH_THRESHOLD = 1.0
ESCALATE_THRESHOLD = 1.5

#: Сколько сделок сканируется за одну транзакцию: память и время удержания блокировок ограничены.
BATCH_SIZE = 500
#: Сколько сотрудников роли получает эскалацию, если получатель задан ролью, а не человеком.
MAX_ESCALATION_RECIPIENTS = 20

_DEFAULT_RULE: dict[str, Any] = {
    "warn_threshold_pct": WARN_THRESHOLD * 100,
    "escalate_threshold_pct": ESCALATE_THRESHOLD * 100,
    "escalate_to_role": None,
    "escalate_to_user_id": None,
    "channels": ["in_app"],
}


class _SlaSnapshots(NamedTuple):
    """Что воркеру нужно знать о воронках партии: правила и коды для меток метрики."""

    rules: dict[tuple[uuid.UUID, str], dict[str, Any]]
    #: (воронка, id статуса) → (код воронки, код статуса).
    codes: dict[tuple[uuid.UUID, str], tuple[str, str]]


async def _sla_snapshots(session: AsyncSession, workflow_ids: set[uuid.UUID]) -> _SlaSnapshots:
    """Правила SLA: (воронка, id статуса) → правило. Сделки живут по снимку воронки на момент
    публикации, поэтому и правила берутся из него, а не из живых `sla_rules` черновика. Поля,
    которых нет в старом снимке, добираются умолчаниями."""
    rows = await session.execute(
        select(Workflow.id, Workflow.code, Workflow.published_graph).where(
            Workflow.id.in_(workflow_ids)
        )
    )
    rules: dict[tuple[uuid.UUID, str], dict[str, Any]] = {}
    codes: dict[tuple[uuid.UUID, str], tuple[str, str]] = {}
    for workflow_id, workflow_code, graph in rows.all():
        for status in (graph or {}).get("statuses", []):
            codes[(workflow_id, status["id"])] = (workflow_code, status["code"])
        for rule in (graph or {}).get("sla_rules", []):
            rules[(workflow_id, rule["status_id"])] = {**_DEFAULT_RULE, **rule}
    return _SlaSnapshots(rules, codes)


async def _escalation_recipients(
    session: AsyncSession, deal: Deal, rule: dict[str, Any]
) -> set[uuid.UUID]:
    """Кому уходит эскалация: явный сотрудник правила, иначе сотрудники роли правила.

    Для роли HEAD это руководитель команды владельца (или его прямой руководитель) — а не все
    руководители компании. Неактивные сотрудники пропускаются."""
    user_id = rule.get("escalate_to_user_id")
    if user_id:
        target = await session.get(User, uuid.UUID(str(user_id)))
        return {target.id} if target is not None and target.is_active else set()

    role = rule.get("escalate_to_role")
    if not role:
        return set()
    owner = await session.get(User, deal.owner_id)
    if role == "HEAD" and owner is not None:
        if owner.team_id is not None:
            team = await session.get(Team, owner.team_id)
            if team is not None and team.head_id is not None:
                return {team.head_id}
        return {owner.manager_id} if owner.manager_id else set()
    rows = await session.scalars(
        select(User.id)
        .where(
            User.role == role,
            User.status == UserStatus.ACTIVE.value,
            User.deleted_at.is_(None),
        )
        .order_by(User.id)
        .limit(MAX_ESCALATION_RECIPIENTS)
    )
    return set(rows.all())


async def _notify(
    session: AsyncSession,
    recipient_id: uuid.UUID,
    deal: Deal,
    *,
    template_code: str,
    priority: str,
    channels: list[str],
    extra: dict[str, Any] | None = None,
) -> None:
    await get_notification_service().notify_user(
        session,
        recipient_id=recipient_id,
        template_code=template_code,
        priority=priority,
        entity_type="deal",
        entity_id=deal.id,
        payload={"channels": channels, **(extra or {})},
    )


async def _process_deal(
    session: AsyncSession,
    deal: Deal,
    now: dt.datetime,
    snapshots: _SlaSnapshots,
    counters: dict[str, int],
) -> None:
    total = (deal.sla_due_at - deal.status_changed_at).total_seconds()  # type: ignore[operator]
    if total <= 0:
        return
    fraction = (now - deal.status_changed_at).total_seconds() / total
    rule = snapshots.rules.get((deal.workflow_id, str(deal.status_id)))
    effective = rule or _DEFAULT_RULE
    warn_threshold = effective["warn_threshold_pct"] / 100
    escalate_threshold = effective["escalate_threshold_pct"] / 100
    channels: list[str] = list(effective.get("channels") or [])

    if fraction >= BREACH_THRESHOLD:
        new_state = SlaState.BREACHED.value
    elif fraction >= warn_threshold:
        new_state = SlaState.WARNING.value
    else:
        new_state = SlaState.OK.value

    if new_state != deal.sla_state:
        deal.sla_state = new_state
        if new_state == SlaState.WARNING.value:
            counters["warned"] += 1
            if channels:
                await _notify(
                    session,
                    deal.owner_id,
                    deal,
                    template_code="DEAL_SLA_WARNING",
                    priority=NotificationPriority.NORMAL,
                    channels=channels,
                )
        elif new_state == SlaState.BREACHED.value:
            counters["breached"] += 1
            # Вход в `breached` — одно нарушение: повторный проход по уже нарушенной сделке
            # состояние не меняет. Статус, которого нет в снимке (архивирован после публикации), —
            # `unknown`, а не id: число значений метки не должно расти вместе с числом статусов.
            workflow_code, status_code = snapshots.codes.get(
                (deal.workflow_id, str(deal.status_id)), ("unknown", "unknown")
            )
            sla_violations_total.labels(workflow=workflow_code, status=status_code).inc()
            recipients = {deal.owner_id}
            owner = await session.get(User, deal.owner_id)
            if owner and owner.manager_id:
                recipients.add(owner.manager_id)
            if channels:
                for recipient_id in recipients:
                    await _notify(
                        session,
                        recipient_id,
                        deal,
                        template_code="DEAL_SLA_BREACHED",
                        priority=NotificationPriority.HIGH,
                        channels=channels,
                    )

    # Эскалация не привязана к смене состояния: нарушение уже случилось (`breached`), а порог
    # эскалации проходят позже — на одном из следующих проходов. Раньше она проверялась только в
    # момент перехода в `breached` и практически никогда не срабатывала.
    if (
        new_state == SlaState.BREACHED.value
        and deal.sla_escalated_at is None
        and fraction >= escalate_threshold
    ):
        deal.sla_escalated_at = now
        counters["escalated"] += 1
        if channels:
            for recipient_id in await _escalation_recipients(session, deal, effective):
                await _notify(
                    session,
                    recipient_id,
                    deal,
                    template_code="DEAL_SLA_BREACHED",
                    priority=NotificationPriority.HIGH,
                    channels=channels,
                    extra={"escalated": True},
                )


async def _publish_sla_gauges() -> None:
    """Выставляет `crm_sla_breaching_deals{sla_state}` по всем открытым сделкам с таймером.

    Скан выше видит не всё (нарушенные и эскалированные пропускает), поэтому считаем отдельным
    агрегатом по открытым сделкам, уже после коммитов партий. Ряд выставляется для каждого
    состояния, в том числе нулём: иначе исчезнувшие нарушения висели бы в Prometheus старым
    значением. Сбой подсчёта метрики не должен ронять сам скан.
    """
    try:
        async with session_scope() as session:
            rows = await session.execute(
                select(Deal.sla_state, func.count(Deal.id))
                .where(
                    Deal.sla_due_at.is_not(None),
                    Deal.closed_at.is_(None),
                    Deal.deleted_at.is_(None),
                )
                .group_by(Deal.sla_state)
            )
            counts = dict(rows.tuples().all())
    except Exception:  # noqa: BLE001 — метрика вторична
        logger.warning("sla_gauges_failed", exc_info=True)
        return
    for state in SlaState:
        sla_breaching_deals.labels(sla_state=state.value).set(counts.get(state.value, 0))


@track_task
async def sweep_sla_breaches(ctx: dict[str, Any]) -> dict[str, int]:
    now = dt.datetime.now(dt.UTC)
    counters = {"warned": 0, "breached": 0, "escalated": 0}
    after_id: uuid.UUID | None = None

    while True:
        async with session_scope() as session:
            stmt = (
                select(Deal)
                .where(
                    Deal.sla_due_at.is_not(None),
                    Deal.closed_at.is_(None),
                    Deal.deleted_at.is_(None),
                    Deal.sla_state != SlaState.PAUSED.value,
                    # Нарушенные и уже эскалированные менять больше нечему.
                    not_(
                        and_(
                            Deal.sla_state == SlaState.BREACHED.value,
                            Deal.sla_escalated_at.is_not(None),
                        )
                    ),
                )
                .order_by(Deal.id)
                .limit(BATCH_SIZE)
                # Сделку, которую сейчас правит человек, не ждём: её возьмёт следующий проход.
                .with_for_update(skip_locked=True)
            )
            if after_id is not None:
                stmt = stmt.where(Deal.id > after_id)
            deals = list((await session.execute(stmt)).scalars().all())
            if not deals:
                break
            after_id = deals[-1].id
            snapshots = await _sla_snapshots(session, {deal.workflow_id for deal in deals})
            for deal in deals:
                await _process_deal(session, deal, now, snapshots, counters)
        if len(deals) < BATCH_SIZE:
            break

    await _publish_sla_gauges()
    if counters["warned"] or counters["breached"] or counters["escalated"]:
        logger.info("sla_sweep_completed", **counters)
    return counters
