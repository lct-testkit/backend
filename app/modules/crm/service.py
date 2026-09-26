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
import zoneinfo
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

import structlog
from sqlalchemy import (
    ColumnElement,
    Date,
    DateTime,
    Numeric,
    Select,
    and_,
    exists,
    false,
    func,
    or_,
    select,
    text,
    true,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.core.db import run_after_commit
from app.core.errors import (
    AppError,
    ErrorCode,
    FieldError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
    VersionConflictError,
)
from app.core.optimistic import claim_version
from app.core.pagination import MAX_LIMIT, Cursor, keyset_after, keyset_before
from app.core.permissions import DealScope, Permission, deal_scope_for, has_permission
from app.core.redis_client import (
    RECENT_MAX_ITEMS,
    TTL_DEAL_CARD,
    get_redis,
    key_deal_card,
    key_recent,
)
from app.core.security import Principal
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService, defer_denied_audit
from app.modules.catalog.models import Contact, Holiday, LossReason, Organization, Product
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
    ParticipantRole,
    Priority,
    SlaState,
    Task,
    TaskStatus,
)
from app.modules.crm.models import (
    SignatureStatus as DealSignatureStatus,
)
from app.modules.files.models import Attachment
from app.modules.identity.models import Role, Team, User, UserStatus
from app.modules.identity.service import IdentityService
from app.modules.integration.service import get_outbox_service
from app.modules.notification.service import NotificationPriority, get_notification_service
from app.modules.signing.service import get_signing_service
from app.modules.workflow import dsl
from app.modules.workflow.models import (
    TERMINAL_TYPES,
    StatusType,
    Workflow,
    WorkflowState,
    WorkflowStatus,
    WorkflowTransition,
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
    async def collect_workload(self, session: AsyncSession, user_id: uuid.UUID) -> UserWorkload: ...

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
    bump_version: bool = True,
) -> bool:
    """Общая механика переноса владельца — используется и `/reassign`,
    и массовой передачей дел при увольнении (new_spec §4.7).

    `bump_version=False` — версию уже занял вызывающий (`claim_version`: атомарный UPDATE, а не
    «прочитанное + 1»), повторно её поднимать нельзя."""
    if deal.owner_id == successor_id:
        return False
    old_owner = deal.owner_id
    deal.owner_id = successor_id
    deal.owner_unavailable = False
    if bump_version:
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
        departing = await session.get(User, user_id)
        successor = await session.get(User, successor_id)
        departing_name = departing.effective_name if departing else str(user_id)
        successor_name = successor.effective_name if successor else str(successor_id)

        changed_ids: list[uuid.UUID] = []
        for deal in deals:
            if deal.owner_id == successor_id:
                continue
            # Версия растёт атомарно и относительно: параллельная правка сделки при увольнении не
            # должна откатить счётчик назад и «воскресить» кэш карточки устаревшей версии.
            await claim_version(session, deal)
            if await _reassign_owner(
                session, deal, successor_id, reason=reason, actor_id=user_id, bump_version=False
            ):
                changed_ids.append(deal.id)
                # new_spec §4.7 шаг 4: «в каждую сделку добавляется системный
                # комментарий «Ответственный изменён: Петров → Иванов
                # (причина: увольнение)»» — раньше это писал только
                # одиночный `/reassign`, массовая передача при увольнении
                # ограничивалась записью `DealEvent` без текста для истории.
                session.add(
                    DealComment(
                        deal_id=deal.id,
                        author_id=None,
                        body=(
                            f"Ответственный изменён: {departing_name} → "
                            f"{successor_name} (причина: {reason})"
                        ),
                        is_system=True,
                    )
                )

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


async def count_active_deals_for_contact(session: AsyncSession, contact_id: uuid.UUID) -> int:
    """Для блокера удаления контакта (new_spec §4.8.5): «действующий договор»

    Тот же критерий «активная», что `RealOwnershipService.collect_workload`
    использует для владельца — `closed_at IS NULL`, а не конкретный список
    статусов: набор нетерминальных статусов задаётся воркфлоу и меняется
    администратором, а `closed_at` проставляется независимо от того, какой
    именно статус довёл сделку до `won`/`lost` (раздел 4.9).
    """
    count = await session.scalar(
        select(func.count())
        .select_from(Deal)
        .where(Deal.contact_id == contact_id, Deal.deleted_at.is_(None), Deal.closed_at.is_(None))
    )
    return int(count or 0)


async def count_all_deals_for_contact(session: AsyncSession, contact_id: uuid.UUID) -> int:
    """Для режима «жёсткое удаление» (new_spec §4.8.2, режим C): разрешён,

    только если у сущности нет вообще ни одной зависимой записи — в отличие
    от блокера обычного удаления, здесь считаются и завершённые сделки тоже
    (`deals.contact_id` не имеет `ON DELETE CASCADE`, значит хоть одна такая
    запись делает жёсткое удаление невозможным, а не просто нежелательным).
    """
    count = await session.scalar(
        select(func.count()).select_from(Deal).where(Deal.contact_id == contact_id)
    )
    return int(count or 0)


async def count_active_deals_for_organization(
    session: AsyncSession, organization_id: uuid.UUID
) -> int:
    """Тот же блокер, что `count_active_deals_for_contact`, для организации

    (dop.md §11.8: ИП — субъект удаления/обезличивания наравне с контактом).
    """
    count = await session.scalar(
        select(func.count())
        .select_from(Deal)
        .where(
            Deal.organization_id == organization_id,
            Deal.deleted_at.is_(None),
            Deal.closed_at.is_(None),
        )
    )
    return int(count or 0)


async def count_all_deals_for_organization(
    session: AsyncSession, organization_id: uuid.UUID
) -> int:
    """Тот же принцип, что `count_all_deals_for_contact`, для организации."""
    count = await session.scalar(
        select(func.count()).select_from(Deal).where(Deal.organization_id == organization_id)
    )
    return int(count or 0)


# =============================================================================
# Контракт для workflow: архивирование статуса с переносом сделок (раздел 6.5)
# =============================================================================


@dataclass(slots=True)
class StatusWorkload:
    supported: bool = False
    active_count: int = 0
    #: Сделки без обязательных полей целевого статуса (не больше `PROBLEM_DEALS_LIMIT`).
    problem_deals: list[dict[str, Any]] = field(default_factory=list)
    problem_count: int = 0
    sla_affected: int = 0


#: Сколько проблемных сделок показывает превью архивирования: полный список нужен мастеру
#: как ориентир, а не как выгрузка.
PROBLEM_DEALS_LIMIT = 100


@dataclass(slots=True)
class MappingBatchResult:
    processed: int = 0
    failed: int = 0
    has_more: bool = False
    #: Последняя просмотренная сделка партии: курсор следующей (`after_id`). Не перенесённые
    #: сделки остаются в статусе, и без курсора каждая партия читала бы одни и те же строки.
    last_id: uuid.UUID | None = None


@runtime_checkable
class DealStatusService(Protocol):
    async def status_workload(
        self,
        session: AsyncSession,
        status_id: uuid.UUID,
        target_status_id: uuid.UUID | None = None,
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
        after_id: uuid.UUID | None = None,
    ) -> MappingBatchResult: ...


class NullDealStatusService:
    async def status_workload(
        self,
        session: AsyncSession,
        status_id: uuid.UUID,
        target_status_id: uuid.UUID | None = None,
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
        after_id: uuid.UUID | None = None,
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


def _field_missing_clause(field_name: str) -> ColumnElement[bool]:
    """SQL-двойник отрицания `_field_present`: у сделки этого поля нет."""
    if field_name.startswith("custom_fields."):
        value = Deal.custom_fields[field_name.removeprefix("custom_fields.")].as_string()
        return or_(value.is_(None), value == "")
    column = getattr(Deal, field_name, None)
    if not isinstance(column, InstrumentedAttribute):
        return true()  # `getattr(deal, имя, None)` для такого имени пуст
    return column.is_(None)


def _product_view(row: DealProduct) -> dict[str, Any]:
    """Строка продуктов сделки для `audit_log.changes` (JSONB без Decimal и UUID)."""
    return {
        "product_id": str(row.product_id),
        "quantity": row.quantity,
        "price": _json_safe(row.price),
        "discount_pct": _json_safe(row.discount_pct),
        "total": _json_safe(row.total),
        "stream_number": row.stream_number,
    }


def _field_present(deal: Deal, field_name: str) -> bool:
    """Проверка `required_fields` целевого статуса: при переходе сделки и при
    миграции (раздел 4.11 п.4: «какие обязательные поля целевого статуса у них
    не заполнены»). Заполненное поле — не пустое: `0` и `false` в
    пользовательском поле это значение, а не пропуск."""
    if field_name.startswith("custom_fields."):
        value = deal.custom_fields.get(field_name.removeprefix("custom_fields."))
        return value is not None and value != ""
    return getattr(deal, field_name, None) is not None


def _transition_field_value(deal: Deal, field_name: str) -> Any:
    """Текущее значение поля, которое переход может задать (для аудита «было → стало»)."""
    if field_name.startswith("custom_fields."):
        return deal.custom_fields.get(field_name.removeprefix("custom_fields."))
    return getattr(deal, field_name, None)


class RealDealStatusService:
    """Реализация контракта `DealStatusService` для мастера сопоставления."""

    async def status_workload(
        self,
        session: AsyncSession,
        status_id: uuid.UUID,
        target_status_id: uuid.UUID | None = None,
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
        workload = StatusWorkload(
            supported=True,
            active_count=int(active_count or 0),
            sla_affected=int(sla_affected or 0),
        )
        # Без целевого статуса (первый шаг мастера, раздел 4.11 п.2-3) сверять `required_fields`
        # не с чем: проблемные сделки считаются, только когда цель известна.
        if target_status_id is not None:
            await self._fill_problem_deals(session, workload, status_id, target_status_id)
        return workload

    async def _fill_problem_deals(
        self,
        session: AsyncSession,
        workload: StatusWorkload,
        status_id: uuid.UUID,
        target_status_id: uuid.UUID,
    ) -> None:
        """Сделки статуса, у которых нет обязательных полей целевого: их `migrate_batch`
        отправит в резервный статус. Отбор — в SQL, а не по всем сделкам в памяти."""
        target = await session.get(WorkflowStatus, target_status_id)
        required = list(target.required_fields or []) if target is not None else []
        if not required:
            return
        problem = select(Deal).where(
            Deal.status_id == status_id,
            Deal.deleted_at.is_(None),
            or_(*(_field_missing_clause(name) for name in required)),
        )
        workload.problem_count = int(
            await session.scalar(select(func.count()).select_from(problem.subquery())) or 0
        )
        rows = (
            (
                await session.execute(
                    problem.order_by(Deal.created_at, Deal.id).limit(PROBLEM_DEALS_LIMIT)
                )
            )
            .scalars()
            .all()
        )
        workload.problem_deals = [
            {
                "id": str(deal.id),
                "number": deal.number,
                "title": deal.title,
                "missing_fields": [name for name in required if not _field_present(deal, name)],
            }
            for deal in rows
        ]

    async def migrate_batch(
        self,
        session: AsyncSession,
        *,
        from_status_id: uuid.UUID,
        target_status_id: uuid.UUID,
        fallback_status_id: uuid.UUID | None,
        sla_mode: str,
        batch_size: int,
        after_id: uuid.UUID | None = None,
    ) -> MappingBatchResult:
        # Порядок по id и курсор `after_id`: сделка, которую перенести не удалось, остаётся в
        # статусе и без курсора попадала бы в каждую следующую партию — задача крутилась вечно.
        stmt = select(Deal).where(Deal.status_id == from_status_id, Deal.deleted_at.is_(None))
        if after_id is not None:
            stmt = stmt.where(Deal.id > after_id)
        rows = (await session.execute(stmt.order_by(Deal.id).limit(batch_size))).scalars().all()
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
            await claim_version(session, deal)
            deal.status_id = destination.id
            deal.status_changed_at = now
            deal.sla_escalated_at = None

            if sla_mode == "reset":
                deal.sla_due_at = None
                deal.sla_state = SlaState.OK.value
            elif sla_mode == "recalculate" and graph is not None:
                status_view = next(
                    (s for s in graph["statuses"] if s["id"] == str(destination.id)),
                    {"id": str(destination.id), "type": destination.type},
                )
                await apply_sla(session, deal, graph, status_view, now)
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
            processed=processed,
            failed=failed,
            has_more=len(rows) == batch_size,
            last_id=rows[-1].id,
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


#: Предел числа суток, которые расчёт срока проходит подряд: защита от календаря, в котором
#: нерабочими объявлены все дни, — без неё цикл не кончался бы.
_SLA_MAX_DAYS = 3660


@dataclass(slots=True, frozen=True)
class BusinessCalendar:
    """Рабочие сутки для расчёта SLA: часовой пояс, праздники и рабочие выходные.

    Границы суток считаются в часовом поясе владельца сделки, а не в UTC: иначе «конец дня»
    для менеджера из Владивостока наступал в 10 утра по его времени. Праздники и переносы
    берутся из справочника `holidays` (`is_working_day=True` — выходной, ставший рабочим)."""

    tz: dt.tzinfo = dt.UTC
    non_working: frozenset[dt.date] = frozenset()
    working: frozenset[dt.date] = frozenset()

    def is_business_day(self, day: dt.date) -> bool:
        if day in self.working:
            return True
        if day in self.non_working:
            return False
        return day.weekday() < 5


def is_business_day(day: dt.date, calendar: BusinessCalendar | None = None) -> bool:
    """Понедельник-пятница; с календарём — ещё и справочник праздников и переносов."""
    if calendar is None:
        return day.weekday() < 5
    return calendar.is_business_day(day)


def compute_sla_due_at(
    start: dt.datetime,
    duration: dt.timedelta,
    *,
    count_business_days: bool,
    calendar: BusinessCalendar | None = None,
) -> dt.datetime:
    """Считает дедлайн. В режиме рабочих дней время на выходных и праздниках «замирает»:
    остаток срока переносится на начало ближайших рабочих суток (00:00 в поясе календаря;
    без календаря — в поясе `start`).
    """
    if not count_business_days:
        return start + duration
    if duration <= dt.timedelta(0):
        return start

    out_tz = start.tzinfo or dt.UTC
    zone = calendar.tz if calendar is not None else out_tz
    current = start.astimezone(zone)
    remaining = duration
    for _ in range(_SLA_MAX_DAYS):
        day_end = dt.datetime.combine(
            current.date() + dt.timedelta(days=1), dt.time.min, tzinfo=zone
        )
        if is_business_day(current.date(), calendar):
            # Разность считается в UTC: вычитание «стенных» времён одного пояса ошибается на
            # сутках, где меняется смещение.
            available_today = day_end.astimezone(dt.UTC) - current.astimezone(dt.UTC)
            if remaining <= available_today:
                return (current.astimezone(dt.UTC) + remaining).astimezone(out_tz)
            remaining -= available_today
        current = day_end
    return current.astimezone(out_tz)


async def load_business_calendar(
    session: AsyncSession,
    start: dt.datetime,
    *,
    duration: dt.timedelta,
    tz_name: str | None,
) -> BusinessCalendar:
    """Календарь на окно, в которое заведомо уложится срок: праздники и переносы из `holidays`.

    Окно берётся с запасом (выходные и праздники удлиняют срок), а даты за его пределами
    считаются по обычному правилу «пн-пт»."""
    try:
        tz: dt.tzinfo = zoneinfo.ZoneInfo(tz_name or "Europe/Moscow")
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        tz = zoneinfo.ZoneInfo("Europe/Moscow")
    first = start.astimezone(tz).date()
    last = first + dt.timedelta(days=int(duration.total_seconds() // 86400) * 3 + 30)
    rows = await session.execute(
        select(Holiday.date, Holiday.is_working_day).where(
            Holiday.date >= first, Holiday.date <= last
        )
    )
    non_working: set[dt.date] = set()
    working: set[dt.date] = set()
    for day, is_working in rows.all():
        (working if is_working else non_working).add(day)
    return BusinessCalendar(tz=tz, non_working=frozenset(non_working), working=frozenset(working))


def _sla_rule(graph: dict[str, Any], status_id: str) -> dict[str, Any] | None:
    return next((r for r in graph.get("sla_rules", []) if r["status_id"] == status_id), None)


def _apply_sla_for_status(
    deal: Deal,
    graph: dict[str, Any],
    status_view: dict[str, Any],
    entered_at: dt.datetime,
    calendar: BusinessCalendar | None = None,
) -> None:
    """Ставит `sla_due_at`/`sla_state` для статуса, в который сделка входит.

    `parked` замораживает таймер безусловно — своё правило SLA у него не
    имеет смысла (new_spec §4.10 «Пауза SLA»). Накопленное время пока сделка
    была в parked учитывает вызывающая сторона при выходе из него
    (`DealService.transition`), а не эта функция.

    Эскалация относится к прежнему статусу: при входе в новый её отметка сбрасывается.
    `calendar` нужен только правилам в рабочих днях (`apply_sla` подгружает его из БД).
    """
    deal.sla_escalated_at = None
    if status_view.get("type") == StatusType.PARKED.value:
        deal.sla_due_at = None
        deal.sla_state = SlaState.PAUSED.value
        return

    rule = _sla_rule(graph, status_view["id"])
    if rule is None:
        deal.sla_due_at = None
        deal.sla_state = SlaState.OK.value
        return

    duration = dt.timedelta(seconds=rule["max_duration_seconds"])
    deal.sla_due_at = compute_sla_due_at(
        entered_at,
        duration,
        count_business_days=rule["count_business_days"],
        calendar=calendar,
    )
    deal.sla_state = SlaState.OK.value


async def apply_sla(
    session: AsyncSession,
    deal: Deal,
    graph: dict[str, Any],
    status_view: dict[str, Any],
    entered_at: dt.datetime,
) -> None:
    """`_apply_sla_for_status` с настоящим производственным календарём.

    Раньше рабочими считались пн-пт по UTC, а справочник праздников (`holidays`) расчёт не
    читал: срок «3 рабочих дня» пересекал майские праздники как обычные будни. Календарь
    грузится только для правил в рабочих днях и только для сделки, где он нужен."""
    rule = _sla_rule(graph, status_view["id"])
    calendar: BusinessCalendar | None = None
    if (
        rule is not None
        and rule.get("count_business_days")
        and status_view.get("type") != StatusType.PARKED.value
    ):
        tz_name = await session.scalar(select(User.timezone).where(User.id == deal.owner_id))
        calendar = await load_business_calendar(
            session,
            entered_at,
            duration=dt.timedelta(seconds=rule["max_duration_seconds"]),
            tz_name=tz_name,
        )
    _apply_sla_for_status(deal, graph, status_view, entered_at, calendar)


# =============================================================================
# Проекция сделки для DSL (раздел 8)
# =============================================================================


async def build_deal_context(session: AsyncSession, deal: Deal) -> dict[str, Any]:
    """Собирает контекст для `dsl.evaluate`/`dsl.resolve_field`.

    `attachments` — файлы, привязанные к сделке, по категориям: условие
    `attachments.contract exists` выполняется, когда у сделки есть хотя бы одно
    неудалённое вложение этой категории. Категории без вложений в словаре нет,
    а DSL по конструкции не падает на отсутствующем поле (раздел 8).
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
    attachments: dict[str, list[str]] = {}
    attachment_rows = await session.execute(
        select(Attachment.category, Attachment.file_id).where(
            Attachment.entity_type == "deal",
            Attachment.entity_id == deal.id,
            Attachment.deleted_at.is_(None),
        )
    )
    for category, file_id in attachment_rows.all():
        attachments.setdefault(category, []).append(str(file_id))
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
        "attachments": attachments,
        "tasks": {"open_count": int(open_tasks or 0)},
        "products": {"count": int(products_count or 0)},
    }


# =============================================================================
# Скоуп сделок (раздел 4 / permissions.DealScope)
# =============================================================================


async def head_scope_member_ids(session: AsyncSession, principal: Principal) -> set[uuid.UUID]:
    """Сотрудники, чьи сделки видит руководитель: он сам, его команда и подчинённые команды.

    Руководитель команды может не числиться её участником (`users.team_id` не выставляется при
    назначении `head_id`), поэтому кроме собственной команды берутся и команды, где он `head_id`:
    иначе руководитель «из коробки» не видел сделок своих менеджеров."""
    member_ids: set[uuid.UUID] = {principal.user_id}
    team_ids: set[uuid.UUID] = {principal.team_id} if principal.team_id is not None else set()
    led = await session.scalars(
        select(Team.id).where(Team.head_id == principal.user_id, Team.deleted_at.is_(None))
    )
    team_ids.update(led.all())
    identity = IdentityService(session)
    for team_id in team_ids:
        member_ids.update(await identity.team_member_ids(team_id))
    return member_ids


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
        return Deal.owner_id.in_(await head_scope_member_ids(session, principal))
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


async def deal_writable(session: AsyncSession, principal: Principal, deal: Deal) -> bool:
    """Может ли тот, кто сделку видит, её менять.

    Видимость KAM складывается из «владелец или любой участник», но участник с ролью
    `watcher` — наблюдатель: смотрит, а не правит. Раньше права записи проверялись только
    правом роли и скоупом, и наблюдатель мог править, переводить и закрывать сделку. Остальные
    роли (HEAD по команде, ADMIN, интеграция по источнику) видят сделку не как участники —
    их скоуп уже и есть право."""
    if deal_scope_for(principal.role) is not DealScope.OWN:
        return True
    if deal.owner_id == principal.user_id:
        return True
    working_participant = await session.scalar(
        select(
            exists().where(
                DealParticipant.deal_id == deal.id,
                DealParticipant.user_id == principal.user_id,
                DealParticipant.role_in_deal != ParticipantRole.WATCHER.value,
            )
        )
    )
    return bool(working_participant)


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


async def get_cached_deal_card(deal_id: uuid.UUID, version: int) -> dict[str, Any] | None:
    """`cache:deal:{id}:v{version}` (раздел 3.4/16). Версионный ключ вместо
    инвалидации по событию: любое изменение сделки бампает `version`, старый
    ключ просто осиротевает — ничего явно чистить не нужно."""
    try:
        raw = await get_redis().get(key_deal_card(deal_id, version))
    except Exception:  # noqa: BLE001 — кэш не источник истины
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


async def set_cached_deal_card(deal_id: uuid.UUID, version: int, payload: dict[str, Any]) -> None:
    try:
        await get_redis().setex(
            key_deal_card(deal_id, version), TTL_DEAL_CARD, json.dumps(payload, default=str)
        )
    except Exception:  # noqa: BLE001
        logger.warning("deal_card_cache_write_failed")


async def invalidate_deal_card(deal_id: uuid.UUID, version: int) -> None:
    try:
        await get_redis().delete(key_deal_card(deal_id, version))
    except Exception:  # noqa: BLE001 — кэш не источник истины
        logger.warning("deal_card_cache_invalidate_failed")


async def drop_deal_card_after_commit(session: AsyncSession, deal_id: uuid.UUID) -> None:
    """Комментарии и задачи не меняют `version` сделки, а значит, и ключ карточки, но
    меняют её счётчики: без сброса они устаревают до конца TTL. Сбрасываем после
    коммита — до него параллельное чтение успело бы закэшировать старые числа."""
    deal = await session.get(Deal, deal_id)
    if deal is not None:
        version = deal.version
        run_after_commit(session, lambda: invalidate_deal_card(deal_id, version))


# =============================================================================
# Фильтры списков
# =============================================================================


@dataclass(slots=True)
class DealFilters:
    status_ids: list[uuid.UUID] | None = None
    #: `True` — закрытые (won/lost/parked, есть `closed_at`), `False` — открытые.
    is_closed: bool | None = None
    workflow_id: uuid.UUID | None = None
    deal_type: str | None = None
    organization_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    product_id: uuid.UUID | None = None
    direction_id: uuid.UUID | None = None
    region_id: uuid.UUID | None = None
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


@dataclass(slots=True)
class HistoryPage:
    statuses: list[DealStatusHistory]
    events: list[DealEvent]
    next_statuses_cursor: str | None = None
    next_events_cursor: str | None = None


async def _ascending_page(
    session: AsyncSession,
    stmt: Select[Any],
    sort_column: Any,
    id_column: Any,
    *,
    limit: int | None,
    cursor: Cursor | None,
) -> tuple[list[Any], str | None]:
    """Выборка по возрастанию `(sort_column, id)`: строки после курсора, не больше `limit`
    (без него — все) и курсор следующей страницы, если она есть."""
    stmt = stmt.order_by(sort_column, id_column)
    if cursor is not None:
        stmt = stmt.where(keyset_after(sort_column, id_column, cursor))
    if limit is not None:
        stmt = stmt.limit(limit + 1)
    rows = list((await session.execute(stmt)).scalars().all())
    if limit is None or len(rows) <= limit:
        return rows, None
    rows = rows[:limit]
    return rows, Cursor(value=getattr(rows[-1], sort_column.key), id=rows[-1].id).encode()


#: Сортировка списка сделок (`sort=поле`, минус в начале — по убыванию). Строки без значения
#: (`amount`, `sla_due_at`, `expected_close_date` бывают пустыми) всегда в конце.
_DEAL_SORT_COLUMNS: dict[str, Any] = {
    "created_at": Deal.created_at,
    "updated_at": Deal.updated_at,
    "status_changed_at": Deal.status_changed_at,
    "number": Deal.number,
    "title": Deal.title,
    "amount": Deal.amount,
    "expected_close_date": Deal.expected_close_date,
    "sla_due_at": Deal.sla_due_at,
}
_DEFAULT_DEAL_SORT = "-created_at"


@dataclass(slots=True)
class DealSort:
    field: str
    descending: bool


def parse_deal_sort(raw: str | None) -> DealSort:
    value = raw or _DEFAULT_DEAL_SORT
    name = value.removeprefix("-")
    if name not in _DEAL_SORT_COLUMNS:
        allowed = ", ".join(_DEAL_SORT_COLUMNS)
        raise ValidationError(
            f"Сортировка по полю {name!r} не поддерживается; допустимы {allowed}",
            [FieldError(field="sort", reason=f"допустимы {allowed}; минус — по убыванию")],
        )
    return DealSort(field=name, descending=value.startswith("-"))


def _sort_cursor_value(column: Any, raw: Any) -> Any:
    """Значение курсора обратно в тип колонки: в курсоре (JSON) оно строка."""
    if raw is None:
        return None
    try:
        if isinstance(column.type, DateTime):
            return dt.datetime.fromisoformat(str(raw))
        if isinstance(column.type, Date):
            return dt.date.fromisoformat(str(raw))
        if isinstance(column.type, Numeric):
            return Decimal(str(raw))
    except (ValueError, ArithmeticError):
        raise ValidationError(
            "Курсор повреждён или устарел",
            [FieldError(field="cursor", reason="Некорректное значение курсора")],
        ) from None
    return raw


def apply_deal_order(stmt: Select[Any], sort: DealSort, cursor: Cursor | None) -> Select[Any]:
    """Порядок списка сделок и строки строго после курсора. Порядок по умолчанию (новые
    сверху) — кортежным сравнением, как везде; остальные — с пустыми значениями в конце."""
    if sort.field == "created_at" and sort.descending:
        stmt = stmt.order_by(Deal.created_at.desc(), Deal.id.desc())
        return (
            stmt if cursor is None else stmt.where(keyset_before(Deal.created_at, Deal.id, cursor))
        )

    column = _DEAL_SORT_COLUMNS[sort.field]
    if sort.descending:
        stmt = stmt.order_by(column.desc().nulls_last(), Deal.id.desc())
    else:
        stmt = stmt.order_by(column.asc().nulls_last(), Deal.id.asc())
    if cursor is None:
        return stmt

    value = _sort_cursor_value(column, cursor.value)
    beyond_id = Deal.id < cursor.id if sort.descending else Deal.id > cursor.id
    if value is None:  # курсор уже среди строк без значения
        return stmt.where(and_(column.is_(None), beyond_id))
    beyond = column < value if sort.descending else column > value
    return stmt.where(or_(beyond, and_(column == value, beyond_id), column.is_(None)))


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
#: Колонки сделки NOT NULL, которые PATCH принимает: очищать их нельзя.
_DEAL_NOT_NULL_FIELDS = ("title", "currency", "priority")

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
    conditions_tree: dict[str, Any] = field(default_factory=dict)


def _condition_tree_view(node: Any, context: dict[str, Any]) -> dict[str, Any]:
    """Дерево условий перехода с `satisfied` на каждом узле (и `actual` в листьях) — чтобы
    чек-листу было видно, что достаточно одного условия из `any`. Решает сервер, а не клиент
    (раздел 6.6). Пустое условие — пустой словарь."""
    if not isinstance(node, dict) or not node:
        return {}
    for group in ("all", "any"):
        if group in node:
            return {
                group: [_condition_tree_view(branch, context) for branch in node[group]],
                "satisfied": dsl.evaluate(node, context).ok,
            }
    return {
        **node,
        "actual": dsl.resolve_field(str(node.get("field")), context),
        "satisfied": dsl.evaluate(node, context).ok,
    }


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

        if filters.status_ids:
            stmt = stmt.where(Deal.status_id.in_(filters.status_ids))
        if filters.is_closed is not None:
            stmt = stmt.where(
                Deal.closed_at.is_not(None) if filters.is_closed else Deal.closed_at.is_(None)
            )
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
        if filters.direction_id:
            stmt = stmt.where(
                select(DealProduct.id)
                .join(Product, Product.id == DealProduct.product_id)
                .where(DealProduct.deal_id == Deal.id, Product.direction_id == filters.direction_id)
                .exists()
            )
        if filters.region_id:
            stmt = stmt.where(
                select(Organization.id)
                .where(
                    Organization.id == Deal.organization_id,
                    Organization.region_id == filters.region_id,
                )
                .exists()
            )
        if filters.q:
            pattern = f"%{filters.q.strip()}%"
            stmt = stmt.where(Deal.title.ilike(pattern) | Deal.number.ilike(pattern))
        return stmt

    async def party_names(
        self, principal: Principal, deals: Sequence[Deal]
    ) -> tuple[dict[uuid.UUID, str], dict[uuid.UUID, str]]:
        """Названия организаций и имена контактов сделок: по запросу на каждый тип, а не на
        каждую сделку. Контакт — ПДн: без права `contact:read` его имени нет."""
        organization_ids = {d.organization_id for d in deals if d.organization_id is not None}
        contact_ids = {d.contact_id for d in deals if d.contact_id is not None}
        organizations: dict[uuid.UUID, str] = {}
        contacts: dict[uuid.UUID, str] = {}
        if organization_ids and has_permission(principal.role, Permission.ORG_READ):
            rows = await self._session.execute(
                select(Organization.id, Organization.name).where(
                    Organization.id.in_(organization_ids)
                )
            )
            organizations = {row.id: row.name for row in rows}
        if contact_ids and has_permission(principal.role, Permission.CONTACT_READ):
            rows = await self._session.execute(
                select(
                    Contact.id, Contact.last_name, Contact.first_name, Contact.middle_name
                ).where(Contact.id.in_(contact_ids))
            )
            contacts = {
                row.id: " ".join(p for p in (row.last_name, row.first_name, row.middle_name) if p)
                for row in rows
            }
        return organizations, contacts

    async def count(self, stmt: Select[Any]) -> int:
        """Сколько строк в выборке — без порядка и курсора: `total` списка."""
        total = await self._session.scalar(
            select(func.count()).select_from(stmt.order_by(None).subquery())
        )
        return int(total or 0)

    async def get_or_404(
        self, deal_id: uuid.UUID, principal: Principal, *, write: bool = False
    ) -> Deal:
        """Сделка в скоупе вызывающего. `write=True` — для пишущих ручек: наблюдатель
        (участник `watcher`) сделку видит, но менять не может (403).

        Отказы (чужая сделка, запись наблюдателем) попадают в журнал аудита отдельной
        закоммиченной записью `ACCESS_DENIED`: исключение откатит транзакцию запроса, и
        обычная запись аудита пропала бы вместе с ней (`defer_denied_audit`)."""
        deal = await self._session.get(Deal, deal_id)
        if deal is None or deal.deleted_at is not None:
            raise NotFoundError("Сделка", deal_id)
        if not await deal_in_scope(self._session, principal, deal):
            defer_denied_audit(
                self._session, entity_type="deal", entity_id=deal.id, reason="out_of_scope"
            )
            # Чужая сделка — 404, а не 403: раздел 3.2 требует не подтверждать
            # даже факт существования объекта вне скоупа.
            raise NotFoundError("Сделка", deal_id)
        if write and not await deal_writable(self._session, principal, deal):
            defer_denied_audit(
                self._session, entity_type="deal", entity_id=deal.id, reason="watcher_read_only"
            )
            raise ForbiddenError("Наблюдатель видит сделку, но не может её менять")
        return deal

    # --- Создание ------------------------------------------------------------

    async def _resolve_workflow(self, deal_type: str, workflow_id: uuid.UUID | None) -> Workflow:
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

    async def _ensure_ref_exists(self, model: type[Any], ref_id: Any, label: str) -> None:
        """Раздел 21 DoD: ошибка по каталогу, а не голое `IntegrityError` от
        FK-ограничения, которое добавила миграция 0005 поверх колонок,
        оставшихся «голым» UUID со спринта 3 (см. `crm.models`)."""
        if ref_id is None:
            return
        found = await self._session.get(model, ref_id)
        if found is None or getattr(found, "deleted_at", None) is not None:
            raise NotFoundError(label, ref_id)

    async def _ensure_parties_in_scope(
        self,
        principal: Principal,
        *,
        organization_id: uuid.UUID | None,
        contact_id: uuid.UUID | None,
    ) -> None:
        """KAM и HEAD привязывают к сделке только то, что и так видят.

        Скоуп организаций и контактов строится через сделки («организации, к которым привязана
        хотя бы одна сделка»), поэтому проверки одного существования хватало, чтобы расширить
        собственный доступ: сделка с чужим `organization_id` делала организацию «своей», и её
        карточка, ПДн контактов и реквизиты становились читаемыми. Отказ — «не найдено», как и для
        несуществующей записи: наличие чужой организации не раскрывается. ADMIN не ограничен, а
        интеграция (вебхук сайта, импорт) привязывает контакты, которых заведомо ещё нет в её
        скоупе, — для неё проверка не применяется.

        Ничейное (организация без ответственного, контакт без организации, создателя и сделок)
        привязать можно: чужого портфеля там нет, а иначе менеджер не смог бы начать работу с
        вузом из общего реестра, пока руководитель не назначит ему ответственного."""
        if principal.role not in (Role.KAM.value, Role.HEAD.value):
            return
        from app.modules.catalog.service import contact_in_scope, organization_in_scope

        if organization_id is not None:
            organization = await self._session.get(Organization, organization_id)
            if (
                organization is not None
                and organization.owner_id is not None
                and not await organization_in_scope(self._session, principal, organization)
            ):
                raise NotFoundError("Организация", organization_id)
        if contact_id is not None:
            contact = await self._session.get(Contact, contact_id)
            if (
                contact is not None
                and not await contact_in_scope(self._session, principal, contact)
                and (
                    contact.organization_id is not None
                    or contact.created_by is not None
                    or await count_all_deals_for_contact(self._session, contact.id) > 0
                )
            ):
                raise NotFoundError("Контакт", contact_id)

    async def _resolve_owner(self, principal: Principal, owner_id: uuid.UUID | None) -> uuid.UUID:
        """Ответственный новой сделки: без `owner_id` или свой — сам вызывающий. Чужого
        назначает ADMIN и интеграция (вебхук CMS сам выбирает КАМа), HEAD — только из своей
        команды, KAM не назначает никого. Раньше `owner_id` принимался как есть: КАМ заводил
        сделки «на коллегу», а несуществующий id доходил до FK и давал 500.

        Тому, кому чужого назначать нельзя, отказ (403) одинаков для любого id — так по
        ответу не узнать, есть ли такой сотрудник."""
        if owner_id is None or owner_id == principal.user_id:
            return principal.user_id
        if principal.role == Role.HEAD.value:
            if owner_id not in await head_scope_member_ids(self._session, principal):
                defer_denied_audit(self._session, entity_type="user", reason="owner_outside_team")
                raise ForbiddenError(
                    "Назначить ответственным можно только сотрудника своей команды"
                )
        elif not (principal.is_admin or principal.role == Role.INTEGRATION.value):
            defer_denied_audit(self._session, entity_type="user", reason="assign_owner_forbidden")
            raise ForbiddenError("Заводить сделку на другого сотрудника может только руководитель")

        owner = await self._session.get(User, owner_id)
        if owner is None:
            raise NotFoundError("Пользователь", owner_id)
        if not owner.is_active:
            raise ValidationError(
                "Ответственным можно назначить только активного сотрудника",
                [FieldError(field="owner_id", reason="сотрудник неактивен")],
            )
        return owner.id

    async def create(
        self, principal: Principal, payload: Any, *, initial_status_code: str | None = None
    ) -> Deal:
        """`initial_status_code` — только для внутренних вызовов (импорт оплат, вебхук сайта):
        оплаченный заказ создаётся сразу в статусе «Оплата и договор оферты», а не в «Заявка с
        сайта». Из API он недоступен — клиент не выбирает стартовый статус. Нет такого статуса в
        воронке — начальный, как раньше."""
        workflow = await self._resolve_workflow(payload.deal_type, payload.workflow_id)
        graph = await get_cached_published_graph(workflow)
        initial = next(
            (s for s in graph["statuses"] if s["type"] == StatusType.INITIAL.value), None
        )
        if initial is None:
            raise AppError(ErrorCode.VALIDATION, "У воронки нет начального статуса")
        if initial_status_code is not None:
            chosen = next(
                (
                    s
                    for s in graph["statuses"]
                    if s["code"] == initial_status_code
                    and s["type"] in (StatusType.INITIAL.value, StatusType.INTERMEDIATE.value)
                ),
                None,
            )
            initial = chosen or initial

        owner_id = await self._resolve_owner(principal, payload.owner_id)
        await self._ensure_ref_exists(Organization, payload.organization_id, "Организация")
        await self._ensure_ref_exists(Contact, payload.contact_id, "Контакт")
        await self._ensure_parties_in_scope(
            principal, organization_id=payload.organization_id, contact_id=payload.contact_id
        )
        for item in payload.products:
            await self._ensure_ref_exists(Product, item.product_id, "Продукт")

        if principal.role == Role.INTEGRATION.value and (
            not payload.source or not payload.external_ids
        ):
            # Раздел 6.6: «если сделка создаётся интеграцией, источник и
            # внешние идентификаторы обязательны» — без них дедупликация и
            # обратная трассировка к системе-источнику (CMS/LMS/Bitrix)
            # ломаются на первом же повторе вебхука.
            raise ValidationError(
                "Для сделки, создаваемой интеграцией, обязательны source и external_ids",
                [
                    FieldError(field="source", reason="обязателен для источника INTEGRATION"),
                    FieldError(field="external_ids", reason="обязателен для источника INTEGRATION"),
                ],
            )

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
            owner_id=owner_id,
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
            order_number=getattr(payload, "order_number", None),
        )
        self._session.add(deal)
        await self._session.flush()

        await apply_sla(self._session, deal, graph, initial, now)

        for item in payload.products:
            self._session.add(
                DealProduct(
                    deal_id=deal.id,
                    product_id=item.product_id,
                    quantity=item.quantity,
                    price=item.price,
                    discount_pct=item.discount_pct,
                    total=item.total,
                    stream_number=item.stream_number,
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
        # Раздел 4.9 п.6 / раздел 4.14: «создаём сделку у нас — через 3
        # секунды она в Bitrix» — публикуется безусловно, не только когда
        # DSL перехода явно просит об этом (в отличие от `_run_actions`'
        # `integration_event`, который есть только на паре конкретных
        # переходов воронки). Доставка (не публикация) уже смотрит на
        # `bitrix_connector_enabled`/`integration_sources.is_active` —
        # ядро сделок само ничего не решает про Bitrix, только пишет факт.
        await get_outbox_service().publish(
            self._session,
            aggregate_type="deal",
            aggregate_id=deal.id,
            event_type="DEAL_CREATED",
            payload={"title": deal.title, "deal_type": deal.deal_type},
            target="bitrix24",
        )
        return deal

    # --- Обновление ------------------------------------------------------

    async def update(
        self,
        deal: Deal,
        payload: Any,
        *,
        expected_version: int,
        principal: Principal | None = None,
    ) -> Deal:
        conflicting = {"title": deal.title}
        if deal.version != expected_version:
            raise VersionConflictError(deal.version, conflicting)
        if deal.deleted_at is not None:
            raise AppError(ErrorCode.DEAL_NOT_ACTIVE, "Сделка удалена")

        data = payload.model_dump(exclude_unset=True)
        # `null` в PATCH обязательной колонки — не «не менять», а ошибка (раньше — 500 на NOT NULL).
        null_required = [key for key in _DEAL_NOT_NULL_FIELDS if key in data and data[key] is None]
        if null_required:
            raise ValidationError(
                "Обязательное поле нельзя очистить",
                [FieldError(field=key, reason="не может быть null") for key in null_required],
            )
        changes: dict[str, dict[str, Any]] = {}
        if principal is not None:
            await self._ensure_parties_in_scope(
                principal,
                organization_id=data.get("organization_id"),
                contact_id=data.get("contact_id"),
            )

        if "custom_fields" in data:
            new_custom = data.pop("custom_fields")
            if new_custom is not None:
                merged = {**deal.custom_fields, **new_custom}
                if merged != deal.custom_fields:
                    changes["custom_fields"] = {"old": deal.custom_fields, "new": merged}
                    deal.custom_fields = merged

        if "organization_id" in data and data["organization_id"] is not None:
            await self._ensure_ref_exists(Organization, data["organization_id"], "Организация")
        if "contact_id" in data and data["contact_id"] is not None:
            await self._ensure_ref_exists(Contact, data["contact_id"], "Контакт")

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

        # Версия занимается одним UPDATE ... WHERE version = :expected: два PATCH с одной версией
        # больше не проходят проверку оба, второй получает 409 CRM-1002.
        await claim_version(self._session, deal, expected_version, conflicting=conflicting)
        await self._session.flush()
        await self._audit.record(
            AuditAction.DEAL_UPDATED, entity_type="deal", entity_id=deal.id, changes=changes
        )
        return deal

    async def replace_products(
        self, deal: Deal, items: list[Any], *, expected_version: int
    ) -> list[DealProduct]:
        """Заменяет список продуктов сделки целиком. Версия сделки растёт: продукты входят
        в карточку, а её кэш привязан к версии."""
        conflicting = {"title": deal.title}
        if deal.version != expected_version:
            raise VersionConflictError(deal.version, conflicting)
        if deal.deleted_at is not None:
            raise AppError(ErrorCode.DEAL_NOT_ACTIVE, "Сделка удалена")
        for item in items:
            await self._ensure_ref_exists(Product, item.product_id, "Продукт")
        await claim_version(self._session, deal, expected_version, conflicting=conflicting)

        old = await self.load_products(deal.id)
        old_view = [_product_view(row) for row in old]
        for row in old:
            await self._session.delete(row)
        await self._session.flush()

        rows = [
            DealProduct(
                deal_id=deal.id,
                product_id=item.product_id,
                quantity=item.quantity,
                price=item.price,
                discount_pct=item.discount_pct,
                total=item.total,
                stream_number=item.stream_number,
            )
            for item in items
        ]
        self._session.add_all(rows)
        await self._session.flush()
        await self._audit.record(
            AuditAction.DEAL_UPDATED,
            entity_type="deal",
            entity_id=deal.id,
            changes={"products": {"old": old_view, "new": [_product_view(row) for row in rows]}},
        )
        return rows

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
                    conditions_tree=_condition_tree_view(t["conditions"], context),
                )
            )
        return results

    async def _apply_transition_field(self, deal: Deal, key: str, value: Any) -> None:
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
                await self._ensure_ref_exists(LossReason, value, "Причина отказа")
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
        conflicting = {"status_id": str(deal.status_id)}
        if deal.version != expected_version:
            raise VersionConflictError(deal.version, conflicting)
        workflow, graph = await self._load_graph(deal)
        statuses_by_id = {s["id"]: s for s in graph["statuses"]}
        from_status = statuses_by_id.get(str(deal.status_id))
        # Сделка в `parked` не закрыта: заморозка — пауза, из неё возвращаются обычным переходом.
        # Проверка `closed_at` пропускает её и для старых строк, где заморозка успела «закрыть»
        # сделку (миграция 0019 снимает у них `closed_at`, но переход не должен зависеть от неё).
        from_parked = from_status is not None and from_status["type"] == StatusType.PARKED.value
        if deal.deleted_at is not None or (deal.closed_at is not None and not from_parked):
            raise AppError(ErrorCode.DEAL_NOT_ACTIVE, "Сделка закрыта или удалена")
        # Версия занимается атомарно: параллельный переход с той же версией получает 409 CRM-1002
        # (Redis-лок снимается до коммита и одного этого не гарантирует).
        await claim_version(self._session, deal, expected_version, conflicting=conflicting)
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
            defer_denied_audit(
                self._session,
                entity_type="deal",
                entity_id=deal.id,
                reason="transition_role_forbidden",
            )
            raise AppError(
                ErrorCode.TRANSITION_FORBIDDEN,
                "Переход запрещён для этой роли",
                extra={"allowed_roles": allowed_roles},
            )

        if transition["requires_comment"] and not (comment and comment.strip()):
            raise AppError(
                ErrorCode.TRANSITION_COMMENT_REQUIRED, "Для этого перехода обязателен комментарий"
            )

        # Поля, заданные вместе с переходом (сумма, причина отказа, `custom_fields.*`), меняют
        # карточку так же, как PATCH, — значит, попадают в аудит с прежним значением; раньше
        # в журнале оставалась лишь смена статуса.
        field_changes: dict[str, dict[str, Any]] = {}
        for key, value in (fields or {}).items():
            old_value = _transition_field_value(deal, key)
            await self._apply_transition_field(deal, key, value)
            new_value = _transition_field_value(deal, key)
            if _json_safe(old_value) != _json_safe(new_value):
                field_changes[key] = {"old": _json_safe(old_value), "new": _json_safe(new_value)}

        context = await build_deal_context(self._session, deal)
        evaluation = dsl.evaluate(transition["conditions"], context)
        if not evaluation.ok:
            unmet_fields = {item.field for item in evaluation.unmet}
            if unmet_fields == {"signature_status"}:
                # Единственное, чего не хватает, — подписи: отдельный код, по которому клиент ведёт
                # на вкладку «Подписание». Смешанные отказы (подпись и что-то ещё) остаются
                # CRM-1201 со списком невыполненных условий.
                raise AppError(
                    ErrorCode.TRANSITION_SIGNATURE_REQUIRED,
                    "Для перехода нужен подписанный документ",
                    extra={"unmet": [item.as_dict() for item in evaluation.unmet]},
                )
            raise AppError(
                ErrorCode.TRANSITION_CONDITIONS,
                "Переход недоступен: условия не выполнены",
                extra={"unmet": [item.as_dict() for item in evaluation.unmet]},
            )

        to_status = statuses_by_id.get(str(to_status_id))
        if to_status is None:
            raise NotFoundError("Статус воронки", to_status_id)
        # Сделка живёт по снимку воронки, а строку статуса из черновика могли удалить: без проверки
        # запись истории упала бы на внешнем ключе уже после всех действий перехода.
        if not await self._session.scalar(
            select(WorkflowStatus.id).where(WorkflowStatus.id == uuid.UUID(to_status["id"]))
        ):
            raise AppError(
                ErrorCode.TRANSITION_CONDITIONS,
                "Целевой статус удалён из воронки: обратитесь к администратору",
                extra={"to_status_id": str(to_status_id)},
            )

        # `required_fields` целевого статуса — отдельно от `conditions` (админ может задать
        # только их). Условия проверены выше и остаются первыми: сид-воронки дублируют ими
        # поля закрытия, и фронтенд ждёт от них CRM-1201.
        missing = [f for f in to_status.get("required_fields") or [] if not _field_present(deal, f)]
        if missing:
            raise AppError(
                ErrorCode.TRANSITION_FIELDS_REQUIRED,
                f"Для перехода в статус «{to_status['name']}» заполните поля: {', '.join(missing)}",
                errors=[
                    FieldError(field=name, reason="обязательно для целевого статуса")
                    for name in missing
                ],
                extra={"missing_fields": missing},
            )

        now = dt.datetime.now(dt.UTC)
        duration_in_prev = now - deal.status_changed_at
        if from_parked:
            # new_spec §4.10: время в parked не считается против SLA, но
            # накапливается отдельно для отчётности.
            deal.sla_paused_total = deal.sla_paused_total + duration_in_prev

        previous_status_id = deal.status_id
        deal.status_id = uuid.UUID(to_status["id"])
        deal.status_changed_at = now
        await apply_sla(self._session, deal, graph, to_status, now)

        if to_status["type"] in _TERMINAL_TYPE_VALUES:
            deal.closed_at = now
        elif from_parked:
            deal.closed_at = None  # возобновление: сделка снова открыта

        # Подпись относится к этапу, на котором её запросили: подписанное КП («Согласование
        # КП») не должно засчитываться как подписанный договор на следующем этапе — из-за этого
        # сид-воронка пропускала «Подписание договора» целиком. Действие перехода
        # `request_signature` ниже заново выставит `pending` уже для нового документа.
        if deal.signature_status != DealSignatureStatus.NONE.value:
            deal.signature_status = DealSignatureStatus.NONE.value
            deal.active_signature_document_id = None

        # Сделка живёт по снимку, где переход ещё есть, а из черновика его могли убрать (сделок
        # по нему не было — удалить можно): тогда ссылаться истории не на что.
        transition_id: uuid.UUID | None = uuid.UUID(transition["id"])
        if not await self._session.scalar(
            select(WorkflowTransition.id).where(WorkflowTransition.id == transition_id)
        ):
            transition_id = None
        self._session.add(
            DealStatusHistory(
                deal_id=deal.id,
                from_status_id=previous_status_id,
                to_status_id=deal.status_id,
                changed_by=principal.user_id,
                transition_id=transition_id,
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
            changes={
                "status_id": {"old": str(previous_status_id), "new": str(deal.status_id)},
                **field_changes,
            },
        )
        if deal.closed_at is not None:
            await self._audit.record(
                AuditAction.DEAL_CLOSED,
                entity_type="deal",
                entity_id=deal.id,
                changes={"status_code": {"old": None, "new": to_status["code"]}},
            )

        # Раздел 4.9 п.6, дословно: «Транзакция: UPDATE deals ... + INSERT
        # outbox_events» — на КАЖДОМ переходе, не только там, где DSL
        # объявляет `integration_event` (см. докстринг в `create()` выше).
        await get_outbox_service().publish(
            self._session,
            aggregate_type="deal",
            aggregate_id=deal.id,
            event_type="DEAL_STATUS_CHANGED",
            payload={"status_code": to_status["code"]},
            target="bitrix24",
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
                # Оба посевных использования (`LEARNING_TRANSFER_REQUESTED`,
                # `LEARNING_ENROLLMENT_SENT`, `workflow.seed`) — про LMS;
                # раздел 4.14 не описывает DSL-действие для Bitrix — синхронизация
                # туда идёт из общего события ниже (см. `create`/`transition`),
                # не из этого действия конструктора.
                await get_outbox_service().publish(
                    self._session,
                    aggregate_type="deal",
                    aggregate_id=deal.id,
                    event_type=action.get("event_code", "UNKNOWN"),
                    payload=action.get("payload"),
                    target="lms",
                )
            elif kind == dsl.ActionType.REQUEST_SIGNATURE.value:
                await get_signing_service().request_signature_for_deal(
                    self._session, deal=deal, action=action, principal=principal, now=now
                )
            else:
                logger.warning("unknown_transition_action", action_type=kind, deal_id=str(deal.id))

    async def _run_create_task(
        self, deal: Deal, action: dict[str, Any], principal: Principal, now: dt.datetime
    ) -> None:
        assignee_id = await self._resolve_action_assignee(deal, action, principal)
        if assignee_id is None:
            # Задача не пропадает молча: исполнитель по роли не нашёлся — она ложится на
            # ответственного, а в ленте сделки остаётся системная заметка, кто должен был её взять.
            assignee_id = deal.owner_id
            wanted = action.get("assignee_role") or action.get("assignee")
            logger.warning(
                "create_task_action_assignee_fallback", deal_id=str(deal.id), wanted=str(wanted)
            )
            self._session.add(
                DealComment(
                    deal_id=deal.id,
                    author_id=None,
                    body=(
                        f"Задача «{action['title']}» назначена ответственному: исполнитель "
                        f"({wanted}) не определён"
                    ),
                    is_system=True,
                )
            )
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
        """Исполнитель задачи-действия перехода; `None` — определить не удалось.

        По роли (`assignee_role`) ищется по порядку: сам ответственный, если у него эта роль;
        руководитель его команды или прямой руководитель (для HEAD); активный участник сделки с
        такой ролью. Раньше работали только первые два для HEAD, а любая другая роль давала
        `None` и задача не создавалась."""
        assignee = action.get("assignee")
        if assignee == "owner":
            return deal.owner_id
        if assignee == "initiator":
            return principal.user_id
        if assignee == "manager":
            owner = await self._session.get(User, deal.owner_id)
            return owner.manager_id if owner else None

        role = action.get("assignee_role")
        if not role:
            return None
        owner = await self._session.get(User, deal.owner_id)
        if owner and owner.role == role:
            return owner.id
        if role == Role.HEAD.value and owner:
            if owner.team_id is not None:
                team = await self._session.get(Team, owner.team_id)
                if team and team.head_id:
                    return team.head_id
            if owner.manager_id:
                return owner.manager_id
        return await self._session.scalar(
            select(User.id)
            .join(DealParticipant, DealParticipant.user_id == User.id)
            .where(
                DealParticipant.deal_id == deal.id,
                User.role == role,
                User.status == UserStatus.ACTIVE.value,
                User.deleted_at.is_(None),
            )
            .order_by(DealParticipant.added_at)
            .limit(1)
        )

    async def _run_notify(self, deal: Deal, action: dict[str, Any], principal: Principal) -> None:
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
                    (
                        await self._session.execute(
                            select(DealParticipant.user_id).where(
                                DealParticipant.deal_id == deal.id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
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
        self,
        deal: Deal,
        principal: Principal,
        *,
        owner_id: uuid.UUID,
        reason: str,
        expected_version: int,
    ) -> Deal:
        conflicting = {"owner_id": str(deal.owner_id)}
        if deal.version != expected_version:
            raise VersionConflictError(deal.version, conflicting)
        if deal.owner_id == owner_id:
            raise ValidationError(
                "Новый ответственный совпадает с текущим",
                [FieldError(field="owner_id", reason="совпадает с текущим владельцем")],
            )
        new_owner = await self._session.get(User, owner_id)
        if new_owner is None:
            raise NotFoundError("Пользователь", owner_id)

        old_owner = deal.owner_id
        await claim_version(self._session, deal, expected_version, conflicting=conflicting)
        await _reassign_owner(
            self._session,
            deal,
            owner_id,
            reason=reason,
            actor_id=principal.user_id,
            bump_version=False,
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
        # Преемник — активный сотрудник, а HEAD передаёт дела только в свою команду: те же правила,
        # что при создании сделки на другого (`_resolve_owner`). Раньше проверялось лишь
        # существование пользователя — сделки уходили уволенному или чужой команде.
        successor_id = await self._resolve_owner(principal, successor_id)

        # Закрытые сделки не переназначаются: у них нет ответственного «в работе», а передача
        # меняла бы владельца в уже сданной отчётности.
        stmt = select(Deal).where(
            Deal.id.in_(deal_ids), Deal.deleted_at.is_(None), Deal.closed_at.is_(None)
        )
        clause = await deal_scope_clause(self._session, principal)
        if clause is not None:
            stmt = stmt.where(clause)
        deals = (await self._session.execute(stmt)).scalars().all()

        changed_ids: list[uuid.UUID] = []
        for deal in deals:
            if deal.owner_id == successor_id:
                continue
            await claim_version(self._session, deal)
            if await _reassign_owner(
                self._session,
                deal,
                successor_id,
                reason=reason,
                actor_id=principal.user_id,
                bump_version=False,
            ):
                changed_ids.append(deal.id)
                # Как при одиночной передаче: в ленте сделки остаётся след — кто и почему.
                self._session.add(
                    DealComment(
                        deal_id=deal.id,
                        author_id=principal.user_id,
                        body=f"Ответственный изменён (причина: {reason})",
                        is_system=True,
                    )
                )

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
        # Одно уведомление на всю пачку, а не по одному на сделку: при увольнении это сотни дел.
        await get_notification_service().notify_user(
            self._session,
            recipient_id=successor_id,
            template_code="DEAL_REASSIGNED",
            priority=NotificationPriority.NORMAL,
            payload={"count": len(changed_ids)},
        )
        return len(changed_ids)

    # --- История ---------------------------------------------------------

    async def history(
        self,
        deal_id: uuid.UUID,
        *,
        limit: int | None = None,
        statuses_cursor: Cursor | None = None,
        events_cursor: Cursor | None = None,
    ) -> HistoryPage:
        """История статусов и лента событий; без `limit` — целиком. Два списка листаются
        независимо, у каждого свой курсор."""
        statuses, next_statuses = await _ascending_page(
            self._session,
            select(DealStatusHistory).where(DealStatusHistory.deal_id == deal_id),
            DealStatusHistory.changed_at,
            DealStatusHistory.id,
            limit=limit,
            cursor=statuses_cursor,
        )
        events, next_events = await _ascending_page(
            self._session,
            select(DealEvent).where(DealEvent.deal_id == deal_id),
            DealEvent.created_at,
            DealEvent.id,
            limit=limit,
            cursor=events_cursor,
        )
        return HistoryPage(statuses, events, next_statuses, next_events)

    # --- Счётчики карточки -------------------------------------------------

    async def load_products(self, deal_id: uuid.UUID) -> list[DealProduct]:
        rows = (
            (await self._session.execute(select(DealProduct).where(DealProduct.deal_id == deal_id)))
            .scalars()
            .all()
        )
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
# Участники (раздел 5.5): даёт видимость сверх ownership — раздел 4, скоуп
# KAM = «owner_id = me OR участник». До этого сервиса таблица
# `deal_participants` не имела ни одной ручки записи, из-за чего ветка
# `exists participant` в `deal_scope_clause` была мертвым кодом: пригласить
# коллегу в сделку как participant было физически нечем.
# =============================================================================


class ParticipantService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    async def list(self, deal_id: uuid.UUID) -> list[DealParticipant]:
        rows = (
            (
                await self._session.execute(
                    select(DealParticipant)
                    .where(DealParticipant.deal_id == deal_id)
                    .order_by(DealParticipant.added_at)
                )
            )
            .scalars()
            .all()
        )
        return list(rows)

    def _ensure_can_manage(self, deal: Deal, principal: Principal) -> None:
        """Состав участников меняют ответственный, руководитель (в скоупе которого сделка)
        и администратор. Раньше хватало права на изменение сделки, а оно есть и у участника
        «наблюдатель»: тот мог сам сделать себя соисполнителем и позвать кого угодно."""
        if principal.is_admin or principal.role == Role.HEAD.value:
            return
        if deal.owner_id == principal.user_id:
            return
        defer_denied_audit(
            self._session,
            entity_type="deal",
            entity_id=deal.id,
            reason="participants_manage_forbidden",
        )
        raise ForbiddenError(
            "Менять участников сделки может ответственный, руководитель или администратор"
        )

    async def add(
        self, deal: Deal, principal: Principal, *, user_id: uuid.UUID, role_in_deal: str
    ) -> DealParticipant:
        self._ensure_can_manage(deal, principal)
        user = await self._session.get(User, user_id)
        if user is None:
            raise NotFoundError("Пользователь", user_id)
        if not user.is_active:
            raise ValidationError(
                "Участником можно сделать только активного сотрудника",
                [FieldError(field="user_id", reason="сотрудник неактивен")],
            )

        existing = await self._session.scalar(
            select(DealParticipant).where(
                DealParticipant.deal_id == deal.id,
                DealParticipant.user_id == user_id,
                DealParticipant.role_in_deal == role_in_deal,
            )
        )
        if existing is not None:
            return existing

        participant = DealParticipant(
            deal_id=deal.id, user_id=user_id, role_in_deal=role_in_deal, added_by=principal.user_id
        )
        self._session.add(participant)
        await self._session.flush()
        await self._audit.record(
            AuditAction.PARTICIPANT_ADDED,
            entity_type="deal",
            entity_id=deal.id,
            changes={
                "user_id": {"old": None, "new": str(user_id)},
                "role_in_deal": {"old": None, "new": role_in_deal},
            },
        )
        return participant

    async def get_or_404(self, participant_id: uuid.UUID) -> DealParticipant:
        participant = await self._session.get(DealParticipant, participant_id)
        if participant is None:
            raise NotFoundError("Участник сделки", participant_id)
        return participant

    async def remove(self, deal: Deal, principal: Principal, participant: DealParticipant) -> None:
        self._ensure_can_manage(deal, principal)
        await self._session.delete(participant)
        await self._session.flush()
        await self._audit.record(
            AuditAction.PARTICIPANT_REMOVED,
            entity_type="deal",
            entity_id=participant.deal_id,
            changes={
                "user_id": {"old": str(participant.user_id), "new": None},
                "role_in_deal": {"old": participant.role_in_deal, "new": None},
            },
        )


# =============================================================================
# Комментарии (раздел 6.6)
# =============================================================================


MAX_MENTIONS = 20


class CommentService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    async def list(
        self, deal_id: uuid.UUID, *, limit: int | None = None, cursor: Cursor | None = None
    ) -> tuple[list[DealComment], str | None]:
        """Комментарии в хронологическом порядке и курсор следующей страницы. Без `limit` —
        первые `MAX_LIMIT`: раньше отдавались все, и в сделке с тысячами комментариев один
        запрос грузил их целиком. Остальное — по `next_cursor`."""
        limit = min(limit, MAX_LIMIT) if limit is not None else MAX_LIMIT
        return await _ascending_page(
            self._session,
            select(DealComment).where(
                DealComment.deal_id == deal_id, DealComment.deleted_at.is_(None)
            ),
            DealComment.created_at,
            DealComment.id,
            limit=limit,
            cursor=cursor,
        )

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
            if parent is None or parent.deal_id != deal.id or parent.deleted_at is not None:
                raise NotFoundError("Комментарий", parent_id)

        mention_ids = await self._valid_mentions(mentions)
        comment = DealComment(
            deal_id=deal.id,
            author_id=principal.user_id,
            parent_id=parent_id,
            body=body,
            mentions=[str(m) for m in mention_ids],
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
        await self._notify_mentions(deal, comment, principal, mention_ids)
        await drop_deal_card_after_commit(self._session, deal.id)
        return comment

    async def _notify_mentions(
        self,
        deal: Deal,
        comment: DealComment,
        author: Principal,
        mention_ids: list[uuid.UUID],
    ) -> None:
        """Упомянутый узнаёт об этом: раньше `mentions` только сохранялись, и «@коллега»
        ничего не значило. Себя упоминать не о чем; в уведомление идут только идентификаторы —
        упомянутый мог не иметь доступа к сделке, и её название ему показывать нельзя."""
        for user_id in mention_ids:
            if user_id == author.user_id:
                continue
            await get_notification_service().notify_user(
                self._session,
                recipient_id=user_id,
                template_code="DEAL_MENTION",
                priority=NotificationPriority.NORMAL,
                entity_type="deal",
                entity_id=deal.id,
                payload={"comment_id": str(comment.id), "author_id": str(author.user_id)},
            )

    async def _valid_mentions(self, mentions: list[uuid.UUID]) -> list[uuid.UUID]:
        """Упоминания без повторов (порядок сохраняется) и только живых активных сотрудников:
        раньше в `mentions` принимался любой UUID — в том числе несуществующий или уволенного,
        а по списку из тысячи id можно было раздуть запись комментария."""
        unique = list(dict.fromkeys(mentions))
        if len(unique) > MAX_MENTIONS:
            raise ValidationError(
                f"В комментарии можно упомянуть не больше {MAX_MENTIONS} сотрудников",
                [FieldError(field="mentions", reason=f"не больше {MAX_MENTIONS}")],
            )
        if not unique:
            return []
        active = set(
            (
                await self._session.scalars(
                    select(User.id).where(
                        User.id.in_(unique),
                        User.status == UserStatus.ACTIVE.value,
                        User.deleted_at.is_(None),
                    )
                )
            ).all()
        )
        unknown = [str(m) for m in unique if m not in active]
        if unknown:
            raise ValidationError(
                "Упомянуть можно только активного сотрудника",
                [
                    FieldError(field="mentions", reason=f"нет активного сотрудника {u}")
                    for u in unknown
                ],
            )
        return unique

    async def update(self, comment: DealComment, principal: Principal, *, body: str) -> DealComment:
        if comment.is_system:
            raise AppError(ErrorCode.VALIDATION, "Системные комментарии нельзя редактировать")
        if comment.author_id != principal.user_id and not principal.is_admin:
            defer_denied_audit(
                self._session,
                entity_type="deal_comment",
                entity_id=comment.id,
                reason="edit_foreign_comment",
            )
            raise ForbiddenError("Редактировать можно только свой комментарий")

        self._session.add(
            DealCommentRevision(
                comment_id=comment.id, body=comment.body, edited_by=principal.user_id
            )
        )
        old_length = len(comment.body)
        comment.body = body
        comment.edited_at = dt.datetime.now(dt.UTC)
        await self._session.flush()
        # Текст в журнал не кладём: он бессрочный и неизменяемый, а комментарий можно
        # удалить (152-ФЗ, ошибочно вставленные данные) — прежние версии хранит
        # `deal_comment_revisions`, там их и смотрят.
        await self._audit.record(
            AuditAction.COMMENT_UPDATED,
            entity_type="deal_comment",
            entity_id=comment.id,
            changes={"body_length": {"old": old_length, "new": len(body)}},
        )
        return comment

    async def delete(
        self, comment: DealComment, principal: Principal, *, reason: str | None
    ) -> None:
        if comment.is_system:
            raise AppError(ErrorCode.VALIDATION, "Системные комментарии нельзя удалить")
        # Чужой комментарий удаляет только ADMIN: руководитель мог стереть слова подчинённого
        # (например, свою же просьбу «согласуй скидку»), и следов в сделке не оставалось.
        if comment.author_id != principal.user_id and not principal.is_admin:
            defer_denied_audit(
                self._session,
                entity_type="deal_comment",
                entity_id=comment.id,
                reason="delete_foreign_comment",
            )
            raise ForbiddenError("Удалить можно только свой комментарий")

        comment.deleted_at = dt.datetime.now(dt.UTC)
        await self._session.flush()
        await self._audit.record(
            AuditAction.COMMENT_DELETED,
            entity_type="deal_comment",
            entity_id=comment.id,
            changes={"reason": {"old": None, "new": reason}},
        )
        await drop_deal_card_after_commit(self._session, comment.deal_id)


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

    async def deal_titles(self, deal_ids: set[uuid.UUID]) -> dict[uuid.UUID, tuple[str, str]]:
        """Номер и название сделок задач — одним запросом на список."""
        if not deal_ids:
            return {}
        rows = await self._session.execute(
            select(Deal.id, Deal.number, Deal.title).where(Deal.id.in_(deal_ids))
        )
        return {row.id: (row.number, row.title) for row in rows}

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
        await self._ensure_assignee(assignee_id)
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
        await drop_deal_card_after_commit(self._session, deal_id)
        return task

    async def _ensure_assignee(self, user_id: uuid.UUID) -> None:
        """Исполнитель — существующий активный сотрудник; иначе FK давал 500, а задача
        «уволенному» тихо повисала."""
        user = await self._session.get(User, user_id)
        if user is None:
            raise NotFoundError("Пользователь", user_id)
        if not user.is_active:
            raise ValidationError(
                "Исполнителем можно назначить только активного сотрудника",
                [FieldError(field="assignee_id", reason="сотрудник неактивен")],
            )

    async def update(self, task: Task, principal: Principal, payload: Any) -> Task:
        data = payload.model_dump(exclude_unset=True)
        # `null` в PATCH обязательного поля — не «не менять», а ошибка (раньше — 500 на NOT NULL).
        null_required = [
            key
            for key in ("title", "assignee_id", "priority", "status")
            if key in data and data[key] is None
        ]
        if null_required:
            raise ValidationError(
                "Обязательное поле нельзя очистить",
                [FieldError(field=key, reason="не может быть null") for key in null_required],
            )
        if "assignee_id" in data and data["assignee_id"] != task.assignee_id:
            await self._ensure_assignee(data["assignee_id"])
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
                task.completed_by = principal.user_id
            elif key == "status" and value != TaskStatus.DONE.value:
                # Задачу вернули в работу — прежнее «завершена тем-то» больше не верно.
                task.completed_at = None
                task.completed_by = None

        if not changes:
            return task
        await self._session.flush()
        await self._audit.record(
            AuditAction.TASK_UPDATED, entity_type="task", entity_id=task.id, changes=changes
        )
        if "status" in changes:
            await drop_deal_card_after_commit(self._session, task.deal_id)
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
        await drop_deal_card_after_commit(self._session, task.deal_id)
        return task
