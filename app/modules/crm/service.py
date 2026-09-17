"""Сервисный слой сделок (раздел 6.6, new_spec §4.9-§4.10).

Три вещи стоит понимать перед тем, как трогать этот файл:

* **Переход по статусу читает `workflow.published_graph`, а не живые таблицы**
  `workflow_statuses`/`workflow_transitions`. Это тот же принцип, что описан в
  `app/modules/workflow/service.py`: администратор может править черновик
  опубликованной воронки, не затрагивая уже идущие сделки — они обязаны жить
  по снимку до следующей публикации. Поэтому вся логика перехода работает с
  JSON-структурой из `get_cached_published_graph`, а не с ORM-моделями
  `WorkflowStatus`/`WorkflowTransition`.
* **Организации, контакты, причины отказа и продукты — из спринта 4.**
  `Deal.organization_id`, `Deal.contact_id`, `Deal.loss_reason_id` и
  `DealProduct.product_id` хранятся без FK (см. `app/modules/crm/models.py`).
  Здесь это означает: сервис не может провалидировать их существование и не
  должен пытаться — это станет возможным, когда появится каталог.
* **Этот файл — граница модуля crm для workflow и identity.** Протоколы
  `OwnershipService`/`DealStatusService` были контрактами-заглушками до этого
  спринта; теперь у них есть настоящая реализация (`RealOwnershipService`,
  `RealDealStatusService`), зарегистрированная по умолчанию. Сигнатуры не
  менялись, поэтому `app/modules/workflow/service.py` и
  `app/modules/identity/admin_service.py` не потребовали правок.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

import structlog
from sqlalchemy import ColumnElement, Select, exists, false, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    AppError,
    ErrorCode,
    FieldError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
    VersionConflictError,
)
from app.core.permissions import DealScope, deal_scope_for
from app.core.redis_client import RECENT_MAX_ITEMS, get_redis, key_recent
from app.core.security import Principal
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.crm.models import (
    OPEN_TASK_STATUSES,
    Deal,
    DealComment,
    DealCommentRevision,
    DealEvent,
    DealEventType,
    DealParticipant,
    DealProduct,
    DealStatusHistory,
    HistoryReason,
    Priority,
    SlaState,
    Task,
    TaskStatus,
)
from app.modules.identity.models import Team, User
from app.modules.identity.service import IdentityService
from app.modules.integration.service import get_outbox_service
from app.modules.notification.service import NotificationPriority, get_notification_service
from app.modules.workflow import dsl
from app.modules.workflow.models import (
    TERMINAL_TYPES,
    StatusType,
    Workflow,
    WorkflowState,
    WorkflowStatus,
)
from app.modules.workflow.service import get_cached_published_graph

logger = structlog.get_logger(__name__)

_TERMINAL_TYPE_VALUES = {t.value for t in TERMINAL_TYPES}


# =============================================================================
# Контракт для identity: сколько держит пользователь (раздел 4.7, 4.8.3)
# =============================================================================


@dataclass(slots=True)
class UserWorkload:
    """Что держит пользователь на момент увольнения или удаления."""

    supported: bool = False
    active_deals: int = 0
    critical_deals: list[dict[str, Any]] = field(default_factory=list)
    open_tasks: int = 0
    running_imports: int = 0
    running_reports: int = 0

    @property
    def is_empty(self) -> bool:
        return not (
            self.active_deals or self.open_tasks or self.running_imports or self.running_reports
        )


@runtime_checkable
class OwnershipService(Protocol):
    async def collect_workload(
        self, session: AsyncSession, user_id: uuid.UUID
    ) -> UserWorkload: ...

    async def reassign_all(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        *,
        successor_id: uuid.UUID,
        reason: str,
    ) -> list[uuid.UUID]:
        """Меняет владельца всех сделок, возвращая их идентификаторы."""

    async def mark_owner_unavailable(
        self, session: AsyncSession, user_id: uuid.UUID, *, unavailable: bool
    ) -> int:
        """Ставит или снимает `owner_unavailable` при блокировке (§4.5)."""


class NullOwnershipService:
    """Оставлена для тестов и сред, где модуль сделок не подключён."""

    async def collect_workload(self, session: AsyncSession, user_id: uuid.UUID) -> UserWorkload:
        return UserWorkload(supported=False)

    async def reassign_all(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        *,
        successor_id: uuid.UUID,
        reason: str,
    ) -> list[uuid.UUID]:
        return []

    async def mark_owner_unavailable(
        self, session: AsyncSession, user_id: uuid.UUID, *, unavailable: bool
    ) -> int:
        return 0


async def _reassign_owner(
    session: AsyncSession,
    deal: Deal,
    successor_id: uuid.UUID,
    *,
    reason: str,
    actor_id: uuid.UUID | None,
) -> bool:
    """Общая механика переноса владельца — используется и `/reassign`,
    и массовой передачей дел при увольнении (new_spec §4.7)."""
    if deal.owner_id == successor_id:
        return False
    old_owner = deal.owner_id
    deal.owner_id = successor_id
    deal.owner_unavailable = False
    deal.version += 1
    session.add(
        DealEvent(
            deal_id=deal.id,
            event_type=DealEventType.OWNER_CHANGED.value,
            actor_id=actor_id,
            payload={
                "old_owner_id": str(old_owner),
                "new_owner_id": str(successor_id),
                "reason": reason,
            },
        )
    )
    return True


class RealOwnershipService:
    """Реализация контракта `OwnershipService` поверх настоящих сделок."""

    async def collect_workload(self, session: AsyncSession, user_id: uuid.UUID) -> UserWorkload:
        active_count = await session.scalar(
            select(func.count())
            .select_from(Deal)
            .where(Deal.owner_id == user_id, Deal.deleted_at.is_(None), Deal.closed_at.is_(None))
        )
        critical_rows = (
            await session.execute(
                select(Deal.id, Deal.number, Deal.title, Deal.status_id, Deal.priority)
                .where(
                    Deal.owner_id == user_id,
                    Deal.deleted_at.is_(None),
                    Deal.closed_at.is_(None),
                )
                .order_by(Deal.updated_at.desc())
                .limit(20)
            )
        ).all()
        critical_deals = [
            {
                "id": str(row.id),
                "number": row.number,
                "title": row.title,
                "status_id": str(row.status_id),
                "priority": row.priority,
            }
            for row in critical_rows
        ]
        open_tasks = await session.scalar(
            select(func.count())
            .select_from(Task)
            .where(
                Task.assignee_id == user_id,
                Task.status.in_(OPEN_TASK_STATUSES),
                Task.deleted_at.is_(None),
            )
        )
        # Импорты и отчёты появятся в спринтах 5-6: до тех пор в системе
        # физически не может быть ни одного запущенного — 0 здесь честен,
        # а не «не знаем».
        return UserWorkload(
            supported=True,
            active_deals=int(active_count or 0),
            critical_deals=critical_deals,
            open_tasks=int(open_tasks or 0),
            running_imports=0,
            running_reports=0,
        )

    async def reassign_all(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        *,
        successor_id: uuid.UUID,
        reason: str,
    ) -> list[uuid.UUID]:
        deals = (
            (
                await session.execute(
                    select(Deal).where(Deal.owner_id == user_id, Deal.deleted_at.is_(None))
                )
            )
            .scalars()
            .all()
        )
        changed_ids: list[uuid.UUID] = []
        for deal in deals:
            if await _reassign_owner(
                session, deal, successor_id, reason=reason, actor_id=user_id
            ):
                changed_ids.append(deal.id)

        open_tasks = (
            (
                await session.execute(
                    select(Task).where(
                        Task.assignee_id == user_id,
                        Task.status.in_(OPEN_TASK_STATUSES),
                        Task.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        for task in open_tasks:
            task.assignee_id = successor_id

        if changed_ids:
            await session.flush()
            await AuditService(session).record(
                AuditAction.DEAL_REASSIGNED_BULK,
                entity_type="user",
                entity_id=user_id,
                changes={
                    "deal_ids": {"old": None, "new": [str(i) for i in changed_ids]},
                    "successor_id": {"old": None, "new": str(successor_id)},
                    "reason": {"old": None, "new": reason},
                },
            )
        return changed_ids

    async def mark_owner_unavailable(
        self, session: AsyncSession, user_id: uuid.UUID, *, unavailable: bool
    ) -> int:
        result = await session.execute(
            update(Deal)
            .where(Deal.owner_id == user_id, Deal.deleted_at.is_(None))
            .values(owner_unavailable=unavailable)
        )
        return result.rowcount or 0


_ownership_service: OwnershipService = RealOwnershipService()


def register_ownership_service(service: OwnershipService) -> None:
    global _ownership_service
    _ownership_service = service


def get_ownership_service() -> OwnershipService:
    return _ownership_service


# =============================================================================
# Контракт для workflow: архивирование статуса с переносом сделок (раздел 6.5)
# =============================================================================


@dataclass(slots=True)
class StatusWorkload:
    supported: bool = False
    active_count: int = 0
    problem_deals: list[dict[str, Any]] = field(default_factory=list)
    sla_affected: int = 0


@dataclass(slots=True)
class MappingBatchResult:
    processed: int = 0
    failed: int = 0
    has_more: bool = False


@runtime_checkable
class DealStatusService(Protocol):
    async def status_workload(
        self, session: AsyncSession, status_id: uuid.UUID
    ) -> StatusWorkload: ...

    async def migrate_batch(
        self,
        session: AsyncSession,
        *,
        from_status_id: uuid.UUID,
        target_status_id: uuid.UUID,
        fallback_status_id: uuid.UUID | None,
        sla_mode: str,
        batch_size: int,
    ) -> MappingBatchResult: ...


class NullDealStatusService:
    async def status_workload(
        self, session: AsyncSession, status_id: uuid.UUID
    ) -> StatusWorkload:
        return StatusWorkload(supported=False)

    async def migrate_batch(
        self,
        session: AsyncSession,
        *,
        from_status_id: uuid.UUID,
        target_status_id: uuid.UUID,
        fallback_status_id: uuid.UUID | None,
        sla_mode: str,
        batch_size: int,
    ) -> MappingBatchResult:
        return MappingBatchResult(has_more=False)


def _json_safe(value: Any) -> Any:
    """JSONB-колонки (`audit_log.changes`, `deal_events.payload`) пишутся через
    обычный `json.dumps` без кастомного энкодера (`app/core/db.py` не задаёт
    `json_serializer`) — `UUID`/`Decimal`/`date` обязаны прийти уже строкой,
    иначе `flush()` упадёт `TypeError` прямо в момент INSERT."""
    if isinstance(value, dt.date | dt.datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID | Decimal):
        return str(value)
    return value


def _field_present(deal: Deal, field_name: str) -> bool:
    """Проверка `required_fields` целевого статуса при миграции (раздел 4.11
    п.4: «какие обязательные поля целевого статуса у них не заполнены»)."""
    if field_name.startswith("custom_fields."):
        return bool(deal.custom_fields.get(field_name.removeprefix("custom_fields.")))
    return getattr(deal, field_name, None) is not None


class RealDealStatusService:
    """Реализация контракта `DealStatusService` для мастера сопоставления."""

    async def status_workload(
        self, session: AsyncSession, status_id: uuid.UUID
    ) -> StatusWorkload:
        active_count = await session.scalar(
            select(func.count())
            .select_from(Deal)
            .where(Deal.status_id == status_id, Deal.deleted_at.is_(None))
        )
        sla_affected = await session.scalar(
            select(func.count())
            .select_from(Deal)
            .where(
                Deal.status_id == status_id,
                Deal.deleted_at.is_(None),
                Deal.sla_due_at.is_not(None),
            )
        )
        # `problem_deals` в превью пуст: целевой статус ещё не выбран на этом
        # шаге мастера (раздел 4.11 п.2-3 идут после `status_impact`), значит
        # required_fields сверять не с чем. Список проблемных сделок строится
        # по факту в `migrate_batch`, когда цель уже известна.
        return StatusWorkload(
            supported=True,
            active_count=int(active_count or 0),
            problem_deals=[],
            sla_affected=int(sla_affected or 0),
        )

    async def migrate_batch(
        self,
        session: AsyncSession,
        *,
        from_status_id: uuid.UUID,
        target_status_id: uuid.UUID,
        fallback_status_id: uuid.UUID | None,
        sla_mode: str,
        batch_size: int,
    ) -> MappingBatchResult:
        rows = (
            (
                await session.execute(
                    select(Deal)
                    .where(Deal.status_id == from_status_id, Deal.deleted_at.is_(None))
                    .limit(batch_size)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return MappingBatchResult(processed=0, failed=0, has_more=False)

        target = await session.get(WorkflowStatus, target_status_id)
        if target is None:
            raise NotFoundError("Статус воронки", target_status_id)
        fallback = (
            await session.get(WorkflowStatus, fallback_status_id) if fallback_status_id else None
        )

        graph: dict[str, Any] | None = None
        if sla_mode == "recalculate":
            workflow = await session.get(Workflow, target.workflow_id)
            if workflow is not None:
                try:
                    graph = await get_cached_published_graph(workflow)
                except AppError:
                    graph = None

        now = dt.datetime.now(dt.UTC)
        processed = failed = 0
        for deal in rows:
            missing = [f for f in (target.required_fields or []) if not _field_present(deal, f)]
            destination = target
            if missing:
                if fallback is None:
                    failed += 1
                    continue
                destination = fallback

            previous_status_id = deal.status_id
            deal.status_id = destination.id
            deal.status_changed_at = now
            deal.version += 1

            if sla_mode == "reset":
                deal.sla_due_at = None
                deal.sla_state = SlaState.OK.value
            elif sla_mode == "recalculate" and graph is not None:
                status_view = next(
                    (s for s in graph["statuses"] if s["id"] == str(destination.id)),
                    {"id": str(destination.id), "type": destination.type},
                )
                _apply_sla_for_status(deal, graph, status_view, now)
            # "keep" — sla_due_at/sla_state переносятся как есть.

            session.add(
                DealStatusHistory(
                    deal_id=deal.id,
                    from_status_id=previous_status_id,
                    to_status_id=destination.id,
                    changed_by=None,
                    reason=HistoryReason.WORKFLOW_MIGRATION.value,
                    comment="Автоматический перенос при архивировании статуса воронки",
                    changed_at=now,
                )
            )
            session.add(
                DealEvent(
                    deal_id=deal.id,
                    event_type=DealEventType.WORKFLOW_MIGRATION.value,
                    actor_id=None,
                    payload={
                        "from_status_id": str(from_status_id),
                        "to_status_id": str(destination.id),
                        "used_fallback": destination is fallback,
                    },
                )
            )
            session.add(
                DealComment(
                    deal_id=deal.id,
                    author_id=None,
                    body=(
                        f"Статус перенесён автоматически: «{from_status_id}» архивирован, "
                        f"сделка перенесена на «{destination.code}»"
                    ),
                    is_system=True,
                )
            )
            processed += 1

        await session.flush()
        return MappingBatchResult(
            processed=processed, failed=failed, has_more=len(rows) == batch_size
        )


_deal_status_service: DealStatusService = RealDealStatusService()


def register_deal_status_service(service: DealStatusService) -> None:
    global _deal_status_service
    _deal_status_service = service


def get_deal_status_service() -> DealStatusService:
    return _deal_status_service


# =============================================================================
# SLA: расчёт срока с учётом рабочих дней (new_spec §4.10)
# =============================================================================


def is_business_day(day: dt.date) -> bool:
    """Понедельник-пятница. Справочник `holidays` — каталог спринта 4/5:
    когда он появится, это единственное место, которое нужно расширить."""
    return day.weekday() < 5


def compute_sla_due_at(
    start: dt.datetime, duration: dt.timedelta, *, count_business_days: bool
) -> dt.datetime:
    """Считает дедлайн. В режиме рабочих дней время на выходных «замирает»:
    остаток срока переносится на понедельник 00:00 того же часового пояса.
    """
    if not count_business_days:
        return start + duration
    if duration <= dt.timedelta(0):
        return start

    current = start
    remaining = duration
    while remaining > dt.timedelta(0):
        day_end = dt.datetime.combine(
            current.date() + dt.timedelta(days=1), dt.time.min, tzinfo=current.tzinfo
        )
        if is_business_day(current.date()):
            available_today = day_end - current
            if remaining <= available_today:
                return current + remaining
            remaining -= available_today
        current = day_end
    return current


def _apply_sla_for_status(
    deal: Deal, graph: dict[str, Any], status_view: dict[str, Any], entered_at: dt.datetime
) -> None:
    """Ставит `sla_due_at`/`sla_state` для статуса, в который сделка входит.

    `parked` замораживает таймер безусловно — своё правило SLA у него не
    имеет смысла (new_spec §4.10 «Пауза SLA»). Накопленное время пока сделка
    была в parked учитывает вызывающая сторона при выходе из него
    (`DealService.transition`), а не эта функция.
    """
    if status_view.get("type") == StatusType.PARKED.value:
        deal.sla_due_at = None
        deal.sla_state = SlaState.PAUSED.value
        return

    rule = next(
        (r for r in graph.get("sla_rules", []) if r["status_id"] == status_view["id"]),
        None,
    )
    if rule is None:
        deal.sla_due_at = None
        deal.sla_state = SlaState.OK.value
        return

    duration = dt.timedelta(seconds=rule["max_duration_seconds"])
    deal.sla_due_at = compute_sla_due_at(
        entered_at, duration, count_business_days=rule["count_business_days"]
    )
    deal.sla_state = SlaState.OK.value


# =============================================================================
# Проекция сделки для DSL (раздел 8)
# =============================================================================


async def build_deal_context(session: AsyncSession, deal: Deal) -> dict[str, Any]:
    """Собирает контекст для `dsl.evaluate`/`dsl.resolve_field`.

    `attachments` — пустой словарь: модуль files (спринт 4) ещё не
    существует, а DSL по конструкции не падает на отсутствующем поле (раздел
    8), значит `attachments.contract` просто не выполнится, а не уронит
    переход с 500.
    """
    open_tasks = await session.scalar(
        select(func.count())
        .select_from(Task)
        .where(
            Task.deal_id == deal.id,
            Task.status.in_(OPEN_TASK_STATUSES),
            Task.deleted_at.is_(None),
        )
    )
    products_count = await session.scalar(
        select(func.count()).select_from(DealProduct).where(DealProduct.deal_id == deal.id)
    )
    return {
        "amount": deal.amount,
        "currency": deal.currency,
        "owner_id": str(deal.owner_id),
        "organization_id": str(deal.organization_id) if deal.organization_id else None,
        "contact_id": str(deal.contact_id) if deal.contact_id else None,
        "priority": deal.priority,
        "loss_reason_id": str(deal.loss_reason_id) if deal.loss_reason_id else None,
        "expected_close_date": deal.expected_close_date,
        "signature_status": deal.signature_status,
        "students_planned": deal.students_planned,
        "custom_fields": deal.custom_fields or {},
        "attachments": {},
        "tasks": {"open_count": int(open_tasks or 0)},
        "products": {"count": int(products_count or 0)},
    }


# =============================================================================
# Скоуп сделок (раздел 4 / permissions.DealScope)
# =============================================================================


async def deal_scope_clause(
    session: AsyncSession, principal: Principal
) -> ColumnElement[bool] | None:
    """`None` — без ограничения (ADMIN). Иначе — булево выражение на `Deal`."""
    scope = deal_scope_for(principal.role)
    if scope is DealScope.ALL:
        return None
    if scope is DealScope.NONE:
        return false()
    if scope is DealScope.SOURCE:
        return Deal.created_by == principal.user_id
    if scope is DealScope.TEAM:
        member_ids: set[uuid.UUID] = {principal.user_id}
        if principal.team_id is not None:
            member_ids.update(await IdentityService(session).team_member_ids(principal.team_id))
        return Deal.owner_id.in_(member_ids)
    # OWN — KAM: владелец либо участник.
    participant_exists = (
        select(DealParticipant.id)
        .where(DealParticipant.deal_id == Deal.id, DealParticipant.user_id == principal.user_id)
        .exists()
    )
    return or_(Deal.owner_id == principal.user_id, participant_exists)


async def deal_in_scope(session: AsyncSession, principal: Principal, deal: Deal) -> bool:
    clause = await deal_scope_clause(session, principal)
    if clause is None:
        return True
    result = await session.scalar(select(exists(select(Deal.id).where(Deal.id == deal.id, clause))))
    return bool(result)


async def touch_recent(
    user_id: uuid.UUID, *, entity_type: str, entity_id: uuid.UUID, title: str
) -> None:
    """`recent:{user_id}` ZSET — «кэш действий пользователя» раздела 3.4."""
    member = json.dumps(
        {"type": entity_type, "id": str(entity_id), "title": title}, ensure_ascii=False
    )
    try:
        client = get_redis()
        score = dt.datetime.now(dt.UTC).timestamp()
        await client.zadd(key_recent(user_id), {member: score})
        await client.zremrangebyrank(key_recent(user_id), 0, -(RECENT_MAX_ITEMS + 1))
    except Exception:  # noqa: BLE001 — кэш не источник истины
        pass


# =============================================================================
# Фильтры списков
# =============================================================================


@dataclass(slots=True)
class DealFilters:
    status_id: uuid.UUID | None = None
    workflow_id: uuid.UUID | None = None
    deal_type: str | None = None
    organization_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    product_id: uuid.UUID | None = None
    priority: str | None = None
    sla_state: str | None = None
    created_from: dt.datetime | None = None
    created_to: dt.datetime | None = None
    closed_from: dt.datetime | None = None
    closed_to: dt.datetime | None = None
    q: str | None = None


@dataclass(slots=True)
class TaskFilters:
    deal_id: uuid.UUID | None = None
    assignee_id: uuid.UUID | None = None
    status: str | None = None
    priority: str | None = None
    due_before: dt.datetime | None = None
    overdue: bool = False


# =============================================================================
# DealService
# =============================================================================

#: Поля, которые можно задать через `PATCH /api/deals/{id}`. `owner_id` —
#: только через `/reassign` (свои права и уведомления), `status_id`/
#: `workflow_id` — только через `/transition`.
_PATCHABLE_FIELDS = frozenset(
    {
        "title",
        "amount",
        "currency",
        "students_planned",
        "expected_close_date",
        "priority",
        "organization_id",
        "contact_id",
        "source",
    }
)

#: Поля, которые можно закрыть через `fields` при переходе (new_spec §4.9
#: п.5 — заполнение `required_fields` целевого статуса вместе с переходом).
_TRANSITION_DIRECT_FIELDS = frozenset(
    {"amount", "currency", "expected_close_date", "loss_reason_id", "students_planned", "title"}
)


@dataclass(slots=True)
class TransitionConditionView:
    field: str
    op: str
    expected: Any
    actual: Any
    satisfied: bool


@dataclass(slots=True)
class TransitionAvailability:
    id: uuid.UUID
    name: str
    to_status_id: uuid.UUID
    requires_comment: bool
    role_allowed: bool
    satisfied: bool
    conditions: list[TransitionConditionView]
    actions: list[dict[str, Any]]


class DealService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    # --- Скоуп и выборка ---------------------------------------------------

    async def list_query(self, principal: Principal, filters: DealFilters) -> Select[tuple[Deal]]:
        stmt = select(Deal).where(Deal.deleted_at.is_(None))
        clause = await deal_scope_clause(self._session, principal)
        if clause is not None:
            stmt = stmt.where(clause)

        if filters.status_id:
            stmt = stmt.where(Deal.status_id == filters.status_id)
        if filters.workflow_id:
            stmt = stmt.where(Deal.workflow_id == filters.workflow_id)
        if filters.deal_type:
            stmt = stmt.where(Deal.deal_type == filters.deal_type)
        if filters.organization_id:
            stmt = stmt.where(Deal.organization_id == filters.organization_id)
        if filters.contact_id:
            stmt = stmt.where(Deal.contact_id == filters.contact_id)
        if filters.owner_id:
            stmt = stmt.where(Deal.owner_id == filters.owner_id)
        if filters.priority:
            stmt = stmt.where(Deal.priority == filters.priority)
        if filters.sla_state:
            stmt = stmt.where(Deal.sla_state == filters.sla_state)
        if filters.created_from:
            stmt = stmt.where(Deal.created_at >= filters.created_from)
        if filters.created_to:
            stmt = stmt.where(Deal.created_at < filters.created_to)
        if filters.closed_from:
            stmt = stmt.where(Deal.closed_at >= filters.closed_from)
        if filters.closed_to:
            stmt = stmt.where(Deal.closed_at < filters.closed_to)
        if filters.product_id:
            stmt = stmt.where(
                select(DealProduct.id)
                .where(DealProduct.deal_id == Deal.id, DealProduct.product_id == filters.product_id)
                .exists()
            )
        if filters.q:
            pattern = f"%{filters.q.strip()}%"
            stmt = stmt.where(Deal.title.ilike(pattern) | Deal.number.ilike(pattern))
        return stmt

    async def get_or_404(self, deal_id: uuid.UUID, principal: Principal) -> Deal:
        deal = await self._session.get(Deal, deal_id)
        if deal is None or deal.deleted_at is not None:
            raise NotFoundError("Сделка", deal_id)
        if not await deal_in_scope(self._session, principal, deal):
            # Чужая сделка — 404, а не 403: раздел 3.2 требует не подтверждать
            # даже факт существования объекта вне скоупа.
            raise NotFoundError("Сделка", deal_id)
        return deal

    # --- Создание ------------------------------------------------------------

    async def _resolve_workflow(
        self, deal_type: str, workflow_id: uuid.UUID | None
    ) -> Workflow:
        if workflow_id is not None:
            workflow = await self._session.get(Workflow, workflow_id)
            if workflow is None:
                raise NotFoundError("Воронка", workflow_id)
        else:
            workflow = (
                await self._session.execute(
                    select(Workflow).where(
                        Workflow.deal_type == deal_type,
                        Workflow.is_default.is_(True),
                        Workflow.state == WorkflowState.PUBLISHED.value,
                    )
                )
            ).scalar_one_or_none()
            if workflow is None:
                raise AppError(
                    ErrorCode.VALIDATION,
                    f"Нет опубликованной воронки по умолчанию для типа {deal_type!r}",
                )

        if workflow.state != WorkflowState.PUBLISHED.value:
            raise ValidationError(
                "Воронка не опубликована",
                [FieldError(field="workflow_id", reason="воронка не опубликована")],
            )
        if workflow.deal_type != deal_type:
            raise ValidationError(
                "Тип воронки не совпадает с типом сделки",
                [FieldError(field="workflow_id", reason="deal_type не совпадает")],
            )
        return workflow

    async def _next_number(self) -> str:
        seq = await self._session.scalar(text("SELECT nextval('deal_number_seq')"))
        year = dt.datetime.now(dt.UTC).year
        return f"D-{year}-{int(seq):06d}"

    async def create(self, principal: Principal, payload: Any) -> Deal:
        workflow = await self._resolve_workflow(payload.deal_type, payload.workflow_id)
        graph = await get_cached_published_graph(workflow)
        initial = next(
            (s for s in graph["statuses"] if s["type"] == StatusType.INITIAL.value), None
        )
        if initial is None:
            raise AppError(ErrorCode.VALIDATION, "У воронки нет начального статуса")

        number = await self._next_number()
        now = dt.datetime.now(dt.UTC)
        deal = Deal(
            number=number,
            title=payload.title,
            deal_type=payload.deal_type,
            workflow_id=workflow.id,
            status_id=uuid.UUID(initial["id"]),
            organization_id=payload.organization_id,
            contact_id=payload.contact_id,
            owner_id=payload.owner_id or principal.user_id,
            created_by=principal.user_id,
            amount=payload.amount,
            currency=payload.currency or "RUB",
            students_planned=payload.students_planned,
            expected_close_date=payload.expected_close_date,
            status_changed_at=now,
            priority=payload.priority or Priority.NORMAL.value,
            custom_fields=payload.custom_fields or {},
            source=payload.source,
            external_ids=payload.external_ids or {},
        )
        self._session.add(deal)
        await self._session.flush()

        _apply_sla_for_status(deal, graph, initial, now)

        for item in payload.products:
            self._session.add(
                DealProduct(
                    deal_id=deal.id,
                    product_id=item.product_id,
                    quantity=item.quantity,
                    price=item.price,
                    discount_pct=item.discount_pct,
                    total=item.total,
                )
            )

        self._session.add(
            DealStatusHistory(
                deal_id=deal.id,
                from_status_id=None,
                to_status_id=deal.status_id,
                changed_by=principal.user_id,
                reason=HistoryReason.MANUAL.value,
                sla_state_at_change=deal.sla_state,
                changed_at=now,
            )
        )
        self._session.add(
            DealEvent(
                deal_id=deal.id,
                event_type=DealEventType.CREATED.value,
                actor_id=principal.user_id,
                payload={"source": payload.source},
            )
        )
        await self._session.flush()

        await self._audit.record(
            AuditAction.DEAL_CREATED,
            entity_type="deal",
            entity_id=deal.id,
            changes={
                "title": {"old": None, "new": deal.title},
                "deal_type": {"old": None, "new": deal.deal_type},
                "owner_id": {"old": None, "new": str(deal.owner_id)},
            },
        )
        return deal

    # --- Обновление ------------------------------------------------------

    async def update(self, deal: Deal, payload: Any, *, expected_version: int) -> Deal:
        if deal.version != expected_version:
            raise VersionConflictError(deal.version, {"title": deal.title})
        if deal.deleted_at is not None:
            raise AppError(ErrorCode.DEAL_NOT_ACTIVE, "Сделка удалена")

        data = payload.model_dump(exclude_unset=True)
        changes: dict[str, dict[str, Any]] = {}

        if "custom_fields" in data:
            new_custom = data.pop("custom_fields")
            if new_custom is not None:
                merged = {**deal.custom_fields, **new_custom}
                if merged != deal.custom_fields:
                    changes["custom_fields"] = {"old": deal.custom_fields, "new": merged}
                    deal.custom_fields = merged

        for key, value in data.items():
            if key not in _PATCHABLE_FIELDS:
                continue
            old = getattr(deal, key)
            old_cmp = _json_safe(old)
            new_cmp = _json_safe(value)
            if old_cmp == new_cmp:
                continue
            changes[key] = {"old": old_cmp, "new": new_cmp}
            setattr(deal, key, value)

        if not changes:
            return deal

        deal.version += 1
        await self._session.flush()
        await self._audit.record(
            AuditAction.DEAL_UPDATED, entity_type="deal", entity_id=deal.id, changes=changes
        )
        return deal

    # --- Переходы ------------------------------------------------------------

    async def _load_graph(self, deal: Deal) -> tuple[Workflow, dict[str, Any]]:
        workflow = await self._session.get(Workflow, deal.workflow_id)
        if workflow is None:
            raise NotFoundError("Воронка", deal.workflow_id)
        graph = await get_cached_published_graph(workflow)
        return workflow, graph

    async def available_transitions(
        self, deal: Deal, principal: Principal
    ) -> list[TransitionAvailability]:
        _workflow, graph = await self._load_graph(deal)
        context = await build_deal_context(self._session, deal)

        results: list[TransitionAvailability] = []
        for t in graph["transitions"]:
            if t["from_status_id"] != str(deal.status_id):
                continue
            allowed_roles = t.get("allowed_roles") or []
            role_ok = not allowed_roles or principal.role in allowed_roles or principal.is_admin

            evaluation = dsl.evaluate(t["conditions"], context)
            conditions_view = [
                TransitionConditionView(
                    field=str(leaf.get("field")),
                    op=str(leaf.get("op")),
                    expected=leaf.get("value"),
                    actual=dsl.resolve_field(str(leaf.get("field")), context),
                    satisfied=dsl.evaluate(leaf, context).ok,
                )
                for leaf in dsl.flatten_leaves(t["conditions"])
            ]
            results.append(
                TransitionAvailability(
                    id=uuid.UUID(t["id"]),
                    name=t["name"],
                    to_status_id=uuid.UUID(t["to_status_id"]),
                    requires_comment=bool(t["requires_comment"]),
                    role_allowed=role_ok,
                    satisfied=role_ok and evaluation.ok,
                    conditions=conditions_view,
                    actions=list(t.get("actions") or []),
                )
            )
        return results

    def _apply_transition_field(self, deal: Deal, key: str, value: Any) -> None:
        if key.startswith("custom_fields."):
            sub = key.removeprefix("custom_fields.")
            deal.custom_fields = {**deal.custom_fields, sub: value}
            return
        if key not in _TRANSITION_DIRECT_FIELDS:
            raise ValidationError(
                f"Поле {key!r} нельзя задать через переход",
                [FieldError(field=key, reason="недопустимое поле")],
            )
        if value is not None:
            if key == "loss_reason_id":
                value = uuid.UUID(str(value))
            elif key == "expected_close_date" and isinstance(value, str):
                value = dt.date.fromisoformat(value)
            elif key == "amount":
                value = Decimal(str(value))
        setattr(deal, key, value)

    async def transition(
        self,
        deal: Deal,
        principal: Principal,
        *,
        to_status_id: uuid.UUID,
        comment: str | None,
        fields: dict[str, Any],
        expected_version: int,
    ) -> Deal:
        if deal.version != expected_version:
            raise VersionConflictError(deal.version, {"status_id": str(deal.status_id)})
        if deal.deleted_at is not None or deal.closed_at is not None:
            raise AppError(ErrorCode.DEAL_NOT_ACTIVE, "Сделка закрыта или удалена")

        workflow, graph = await self._load_graph(deal)
        statuses_by_id = {s["id"]: s for s in graph["statuses"]}
        transition = next(
            (
                t
                for t in graph["transitions"]
                if t["from_status_id"] == str(deal.status_id)
                and t["to_status_id"] == str(to_status_id)
            ),
            None,
        )
        if transition is None:
            raise AppError(
                ErrorCode.TRANSITION_CONDITIONS,
                "Переход из текущего статуса в целевой не найден в опубликованном графе",
                extra={"from_status_id": str(deal.status_id), "to_status_id": str(to_status_id)},
            )

        allowed_roles = transition.get("allowed_roles") or []
        if allowed_roles and principal.role not in allowed_roles and not principal.is_admin:
            raise AppError(
                ErrorCode.TRANSITION_FORBIDDEN,
                "Переход запрещён для этой роли",
                extra={"allowed_roles": allowed_roles},
            )

        if transition["requires_comment"] and not (comment and comment.strip()):
            raise AppError(
                ErrorCode.TRANSITION_COMMENT_REQUIRED, "Для этого перехода обязателен комментарий"
            )

        for key, value in (fields or {}).items():
            self._apply_transition_field(deal, key, value)

        context = await build_deal_context(self._session, deal)
        evaluation = dsl.evaluate(transition["conditions"], context)
        if not evaluation.ok:
            raise AppError(
                ErrorCode.TRANSITION_CONDITIONS,
                "Переход недоступен: условия не выполнены",
                extra={"unmet": [item.as_dict() for item in evaluation.unmet]},
            )

        to_status = statuses_by_id.get(str(to_status_id))
        if to_status is None:
            raise NotFoundError("Статус воронки", to_status_id)
        from_status = statuses_by_id.get(str(deal.status_id))

        now = dt.datetime.now(dt.UTC)
        duration_in_prev = now - deal.status_changed_at
        if from_status is not None and from_status["type"] == StatusType.PARKED.value:
            # new_spec §4.10: время в parked не считается против SLA, но
            # накапливается отдельно для отчётности.
            deal.sla_paused_total = deal.sla_paused_total + duration_in_prev

        previous_status_id = deal.status_id
        deal.status_id = uuid.UUID(to_status["id"])
        deal.status_changed_at = now
        deal.version += 1
        _apply_sla_for_status(deal, graph, to_status, now)

        if to_status["type"] in _TERMINAL_TYPE_VALUES:
            deal.closed_at = now

        self._session.add(
            DealStatusHistory(
                deal_id=deal.id,
                from_status_id=previous_status_id,
                to_status_id=deal.status_id,
                changed_by=principal.user_id,
                transition_id=uuid.UUID(transition["id"]),
                reason=HistoryReason.MANUAL.value,
                comment=comment,
                duration_in_prev=duration_in_prev,
                sla_state_at_change=deal.sla_state,
                changed_at=now,
            )
        )
        if comment:
            self._session.add(
                DealComment(
                    deal_id=deal.id, author_id=principal.user_id, body=comment, is_system=False
                )
            )
        await self._session.flush()

        await self._audit.record(
            AuditAction.DEAL_STATUS_CHANGED,
            entity_type="deal",
            entity_id=deal.id,
            changes={"status_id": {"old": str(previous_status_id), "new": str(deal.status_id)}},
        )
        if deal.closed_at is not None:
            await self._audit.record(
                AuditAction.DEAL_CLOSED,
                entity_type="deal",
                entity_id=deal.id,
                changes={"status_code": {"old": None, "new": to_status["code"]}},
            )

        await self._run_actions(deal, transition.get("actions") or [], principal, now)
        return deal

    # --- Действия перехода (раздел 8 DSL) -----------------------------------

    async def _run_actions(
        self, deal: Deal, actions: list[dict[str, Any]], principal: Principal, now: dt.datetime
    ) -> None:
        for action in actions:
            kind = action.get("type")
            if kind == dsl.ActionType.CREATE_TASK.value:
                await self._run_create_task(deal, action, principal, now)
            elif kind == dsl.ActionType.NOTIFY.value:
                await self._run_notify(deal, action, principal)
            elif kind == dsl.ActionType.INTEGRATION_EVENT.value:
                await get_outbox_service().publish(
                    self._session,
                    aggregate_type="deal",
                    aggregate_id=deal.id,
                    event_type=action.get("event_code", "UNKNOWN"),
                    payload=action.get("payload"),
                )
            elif kind == dsl.ActionType.REQUEST_SIGNATURE.value:
                # Модуль ПЭП — спринт 8: пока честно логируем, что действие
                # сработало бы, но подпись не запрашивается.
                logger.info(
                    "request_signature_action_stub",
                    deal_id=str(deal.id),
                    template=action.get("template"),
                )
            else:
                logger.warning("unknown_transition_action", action_type=kind, deal_id=str(deal.id))

    async def _run_create_task(
        self, deal: Deal, action: dict[str, Any], principal: Principal, now: dt.datetime
    ) -> None:
        assignee_id = await self._resolve_action_assignee(deal, action, principal)
        if assignee_id is None:
            logger.warning("create_task_action_no_assignee", deal_id=str(deal.id))
            return
        due_days = action.get("due_days")
        task = Task(
            deal_id=deal.id,
            title=action["title"],
            assignee_id=assignee_id,
            created_by=principal.user_id,
            due_at=(now + dt.timedelta(days=due_days)) if due_days else None,
            priority=action.get("priority", Priority.NORMAL.value),
        )
        self._session.add(task)
        await self._session.flush()
        await self._audit.record(
            AuditAction.TASK_CREATED,
            entity_type="task",
            entity_id=task.id,
            changes={
                "deal_id": {"old": None, "new": str(deal.id)},
                "assignee_id": {"old": None, "new": str(assignee_id)},
            },
        )

    async def _resolve_action_assignee(
        self, deal: Deal, action: dict[str, Any], principal: Principal
    ) -> uuid.UUID | None:
        assignee = action.get("assignee")
        if assignee == "owner":
            return deal.owner_id
        if assignee == "initiator":
            return principal.user_id
        if assignee == "manager":
            owner = await self._session.get(User, deal.owner_id)
            return owner.manager_id if owner else None

        role = action.get("assignee_role")
        if role:
            owner = await self._session.get(User, deal.owner_id)
            if owner and owner.role == role:
                return owner.id
            if role == "HEAD" and owner and owner.manager_id:
                return owner.manager_id
        return None

    async def _run_notify(
        self, deal: Deal, action: dict[str, Any], principal: Principal
    ) -> None:
        recipients = action.get("recipients") or ["owner"]
        resolved: set[uuid.UUID] = set()
        for recipient in recipients:
            if recipient == "owner":
                resolved.add(deal.owner_id)
            elif recipient == "initiator":
                resolved.add(principal.user_id)
            elif recipient == "manager":
                owner = await self._session.get(User, deal.owner_id)
                if owner and owner.manager_id:
                    resolved.add(owner.manager_id)
            elif recipient == "team_head":
                owner = await self._session.get(User, deal.owner_id)
                if owner and owner.team_id:
                    team = await self._session.get(Team, owner.team_id)
                    if team and team.head_id:
                        resolved.add(team.head_id)
            elif recipient == "participants":
                rows = (
                    await self._session.execute(
                        select(DealParticipant.user_id).where(DealParticipant.deal_id == deal.id)
                    )
                ).scalars().all()
                resolved.update(rows)
            # "contact" пропускаем: контакты — каталог спринта 4.

        for user_id in resolved:
            await get_notification_service().notify_user(
                self._session,
                recipient_id=user_id,
                template_code=action.get("event_code", "DEAL_EVENT"),
                priority=NotificationPriority.NORMAL,
                entity_type="deal",
                entity_id=deal.id,
            )

    # --- Назначение ответственного ------------------------------------------

    async def reassign(
        self, deal: Deal, principal: Principal, *, owner_id: uuid.UUID, reason: str,
        expected_version: int,
    ) -> Deal:
        if deal.version != expected_version:
            raise VersionConflictError(deal.version, {"owner_id": str(deal.owner_id)})
        if deal.owner_id == owner_id:
            raise ValidationError(
                "Новый ответственный совпадает с текущим",
                [FieldError(field="owner_id", reason="совпадает с текущим владельцем")],
            )
        new_owner = await self._session.get(User, owner_id)
        if new_owner is None:
            raise NotFoundError("Пользователь", owner_id)

        old_owner = deal.owner_id
        await _reassign_owner(
            self._session, deal, owner_id, reason=reason, actor_id=principal.user_id
        )
        self._session.add(
            DealComment(
                deal_id=deal.id,
                author_id=principal.user_id,
                body=f"Ответственный изменён (причина: {reason})",
                is_system=True,
            )
        )
        await self._session.flush()
        await self._audit.record(
            AuditAction.DEAL_OWNER_CHANGED,
            entity_type="deal",
            entity_id=deal.id,
            changes={
                "owner_id": {"old": str(old_owner), "new": str(owner_id)},
                "reason": {"old": None, "new": reason},
            },
        )
        await get_notification_service().notify_user(
            self._session,
            recipient_id=owner_id,
            template_code="DEAL_REASSIGNED",
            priority=NotificationPriority.NORMAL,
            entity_type="deal",
            entity_id=deal.id,
        )
        return deal

    async def bulk_reassign(
        self,
        principal: Principal,
        *,
        deal_ids: list[uuid.UUID],
        successor_id: uuid.UUID,
        reason: str,
    ) -> int:
        successor = await self._session.get(User, successor_id)
        if successor is None:
            raise NotFoundError("Пользователь", successor_id)

        stmt = select(Deal).where(Deal.id.in_(deal_ids), Deal.deleted_at.is_(None))
        clause = await deal_scope_clause(self._session, principal)
        if clause is not None:
            stmt = stmt.where(clause)
        deals = (await self._session.execute(stmt)).scalars().all()

        changed_ids: list[uuid.UUID] = []
        for deal in deals:
            if await _reassign_owner(
                self._session, deal, successor_id, reason=reason, actor_id=principal.user_id
            ):
                changed_ids.append(deal.id)

        if not changed_ids:
            return 0

        await self._session.flush()
        await self._audit.record(
            AuditAction.DEAL_REASSIGNED_BULK,
            entity_type="deal",
            changes={
                "deal_ids": {"old": None, "new": [str(i) for i in changed_ids]},
                "successor_id": {"old": None, "new": str(successor_id)},
                "reason": {"old": None, "new": reason},
            },
        )
        return len(changed_ids)

    # --- История ---------------------------------------------------------

    async def history(
        self, deal_id: uuid.UUID
    ) -> tuple[list[DealStatusHistory], list[DealEvent]]:
        statuses = (
            (
                await self._session.execute(
                    select(DealStatusHistory)
                    .where(DealStatusHistory.deal_id == deal_id)
                    .order_by(DealStatusHistory.changed_at)
                )
            )
            .scalars()
            .all()
        )
        events = (
            (
                await self._session.execute(
                    select(DealEvent)
                    .where(DealEvent.deal_id == deal_id)
                    .order_by(DealEvent.created_at)
                )
            )
            .scalars()
            .all()
        )
        return list(statuses), list(events)

    # --- Счётчики карточки -------------------------------------------------

    async def load_products(self, deal_id: uuid.UUID) -> list[DealProduct]:
        rows = (
            await self._session.execute(
                select(DealProduct).where(DealProduct.deal_id == deal_id)
            )
        ).scalars().all()
        return list(rows)

    async def counters(self, deal_id: uuid.UUID) -> tuple[int, int]:
        open_tasks = await self._session.scalar(
            select(func.count())
            .select_from(Task)
            .where(
                Task.deal_id == deal_id,
                Task.status.in_(OPEN_TASK_STATUSES),
                Task.deleted_at.is_(None),
            )
        )
        comments = await self._session.scalar(
            select(func.count())
            .select_from(DealComment)
            .where(DealComment.deal_id == deal_id, DealComment.deleted_at.is_(None))
        )
        return int(open_tasks or 0), int(comments or 0)


# =============================================================================
# Комментарии (раздел 6.6)
# =============================================================================


class CommentService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    async def list(self, deal_id: uuid.UUID) -> list[DealComment]:
        rows = (
            await self._session.execute(
                select(DealComment)
                .where(DealComment.deal_id == deal_id, DealComment.deleted_at.is_(None))
                .order_by(DealComment.created_at)
            )
        ).scalars().all()
        return list(rows)

    async def get_or_404(self, comment_id: uuid.UUID) -> DealComment:
        comment = await self._session.get(DealComment, comment_id)
        if comment is None or comment.deleted_at is not None:
            raise NotFoundError("Комментарий", comment_id)
        return comment

    async def create(
        self,
        deal: Deal,
        principal: Principal,
        *,
        body: str,
        parent_id: uuid.UUID | None,
        mentions: list[uuid.UUID],
        is_internal: bool,
    ) -> DealComment:
        if parent_id is not None:
            parent = await self._session.get(DealComment, parent_id)
            if parent is None or parent.deal_id != deal.id:
                raise NotFoundError("Комментарий", parent_id)

        comment = DealComment(
            deal_id=deal.id,
            author_id=principal.user_id,
            parent_id=parent_id,
            body=body,
            mentions=[str(m) for m in mentions],
            is_internal=is_internal,
        )
        self._session.add(comment)
        await self._session.flush()
        await self._audit.record(
            AuditAction.COMMENT_CREATED,
            entity_type="deal_comment",
            entity_id=comment.id,
            changes={"deal_id": {"old": None, "new": str(deal.id)}},
        )
        return comment

    async def update(self, comment: DealComment, principal: Principal, *, body: str) -> DealComment:
        if comment.is_system:
            raise AppError(ErrorCode.VALIDATION, "Системные комментарии нельзя редактировать")
        if comment.author_id != principal.user_id and not principal.is_admin:
            raise ForbiddenError("Редактировать можно только свой комментарий")

        self._session.add(
            DealCommentRevision(
                comment_id=comment.id, body=comment.body, edited_by=principal.user_id
            )
        )
        old_body = comment.body
        comment.body = body
        comment.edited_at = dt.datetime.now(dt.UTC)
        await self._session.flush()
        await self._audit.record(
            AuditAction.COMMENT_UPDATED,
            entity_type="deal_comment",
            entity_id=comment.id,
            changes={"body": {"old": old_body, "new": body}},
        )
        return comment

    async def delete(
        self, comment: DealComment, principal: Principal, *, reason: str | None
    ) -> None:
        if comment.is_system:
            raise AppError(ErrorCode.VALIDATION, "Системные комментарии нельзя удалить")
        if comment.author_id != principal.user_id and principal.role not in ("ADMIN", "HEAD"):
            raise ForbiddenError("Недостаточно прав для удаления комментария")

        comment.deleted_at = dt.datetime.now(dt.UTC)
        await self._session.flush()
        await self._audit.record(
            AuditAction.COMMENT_DELETED,
            entity_type="deal_comment",
            entity_id=comment.id,
            changes={"reason": {"old": None, "new": reason}},
        )


# =============================================================================
# Задачи (раздел 6.6)
# =============================================================================


class TaskService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    def list_query(
        self, filters: TaskFilters, scope_clause: ColumnElement[bool] | None
    ) -> Select[tuple[Task]]:
        stmt = select(Task).where(Task.deleted_at.is_(None))
        if scope_clause is not None:
            stmt = stmt.join(Deal, Deal.id == Task.deal_id).where(scope_clause)
        if filters.deal_id:
            stmt = stmt.where(Task.deal_id == filters.deal_id)
        if filters.assignee_id:
            stmt = stmt.where(Task.assignee_id == filters.assignee_id)
        if filters.status:
            stmt = stmt.where(Task.status == filters.status)
        if filters.priority:
            stmt = stmt.where(Task.priority == filters.priority)
        if filters.due_before:
            stmt = stmt.where(Task.due_at < filters.due_before)
        if filters.overdue:
            stmt = stmt.where(
                Task.due_at < dt.datetime.now(dt.UTC), Task.status.in_(OPEN_TASK_STATUSES)
            )
        return stmt

    async def get_or_404(self, task_id: uuid.UUID) -> Task:
        task = await self._session.get(Task, task_id)
        if task is None or task.deleted_at is not None:
            raise NotFoundError("Задача", task_id)
        return task

    async def create(
        self,
        principal: Principal,
        *,
        deal_id: uuid.UUID,
        title: str,
        description: str | None,
        assignee_id: uuid.UUID,
        due_at: dt.datetime | None,
        priority: str,
    ) -> Task:
        task = Task(
            deal_id=deal_id,
            title=title,
            description=description,
            assignee_id=assignee_id,
            created_by=principal.user_id,
            due_at=due_at,
            priority=priority,
        )
        self._session.add(task)
        await self._session.flush()
        await self._audit.record(
            AuditAction.TASK_CREATED,
            entity_type="task",
            entity_id=task.id,
            changes={"deal_id": {"old": None, "new": str(deal_id)}},
        )
        return task

    async def update(self, task: Task, payload: Any) -> Task:
        data = payload.model_dump(exclude_unset=True)
        changes: dict[str, dict[str, Any]] = {}
        for key, value in data.items():
            old = getattr(task, key)
            old_cmp = _json_safe(old)
            new_cmp = _json_safe(value)
            if old_cmp == new_cmp:
                continue
            changes[key] = {"old": old_cmp, "new": new_cmp}
            setattr(task, key, value)
            if key == "status" and value == TaskStatus.DONE.value and task.completed_at is None:
                task.completed_at = dt.datetime.now(dt.UTC)

        if not changes:
            return task
        await self._session.flush()
        await self._audit.record(
            AuditAction.TASK_UPDATED, entity_type="task", entity_id=task.id, changes=changes
        )
        return task

    async def complete(self, task: Task, principal: Principal) -> Task:
        if task.status == TaskStatus.DONE.value:
            return task
        task.status = TaskStatus.DONE.value
        task.completed_at = dt.datetime.now(dt.UTC)
        task.completed_by = principal.user_id
        await self._session.flush()
        await self._audit.record(
            AuditAction.TASK_COMPLETED,
            entity_type="task",
            entity_id=task.id,
            changes={"status": {"old": "open", "new": "done"}},
        )
        return task
