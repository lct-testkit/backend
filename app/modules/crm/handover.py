"""Мастер передачи дел (B4-4, new_spec §4.7): распределение портфеля одного
пользователя по сделкам и вузам между несколькими преемниками.

Используется двумя путями: `AdminUserService.offboard_confirm` (сотрудник
увольняется, учётка переходит в `terminated`) и `POST /api/users/{id}/handover`
(портфель передаётся без увольнения — отпуск, смена региона). Оба вызывают
`HandoverService.resolve` → `HandoverService.execute` с одним и тем же планом;
разница — только в том, что происходит с самой учёткой уходящего, это решает
вызывающий код, не этот модуль.

Порядок разрешения преемника для сделки: явный `deal_successors` → первое
подходящее правило из `rules` → наименее загруженный из `pool_ids` → общий
`default_successor_id`. Для вуза: `organization_successors` → правило с
`organization_id` → правило с `direction_id`, если большинство открытых сделок
вуза у уходящего этого направления → `default_successor_id`. Задача уходит
тому же преемнику, что и её сделка, иначе — `default_successor_id`.
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode, FieldError, ValidationError
from app.core.optimistic import claim_version
from app.core.security import Principal
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.catalog.models import Organization, Product
from app.modules.crm.models import (
    OPEN_TASK_STATUSES,
    Deal,
    DealComment,
    DealProduct,
    Task,
)
from app.modules.crm.service import _reassign_owner, ensure_assignable_user
from app.modules.identity.models import User
from app.modules.notification.service import NotificationPriority, get_notification_service

#: Раздел «Риски» B4-4: один вызов не трогает больше этого числа сделок/вузов — при
#: превышении 422 с подсказкой разбить передачу на несколько вызовов.
MAX_DEALS_PER_CALL = 2000
MAX_ORGANIZATIONS_PER_CALL = 300

RuleFieldName = Literal["direction_id", "product_id", "organization_id", "deal_type"]


class HandoverRuleMatch(BaseModel):
    """Ровно одно поле заполнено — правило понимает один признак за раз (422 иначе)."""

    direction_id: uuid.UUID | None = None
    product_id: uuid.UUID | None = None
    organization_id: uuid.UUID | None = None
    deal_type: Literal["b2b", "b2c"] | None = None

    @model_validator(mode="after")
    def _exactly_one_field(self) -> HandoverRuleMatch:
        present = [
            name
            for name in ("direction_id", "product_id", "organization_id", "deal_type")
            if getattr(self, name) is not None
        ]
        if len(present) != 1:
            raise ValueError(
                "Условие должно содержать ровно одно поле: направление, продукт, вуз "
                "или тип сделки"
            )
        return self


class HandoverRule(BaseModel):
    match: HandoverRuleMatch
    successor_id: uuid.UUID


class HandoverPlan(BaseModel):
    """Тело запроса мастера передачи дел (и `OffboardRequest`, который строит этот план
    из своих полей — см. `AdminUserService.offboard_confirm`)."""

    default_successor_id: uuid.UUID
    deal_successors: dict[uuid.UUID, uuid.UUID] = Field(default_factory=dict)
    organization_successors: dict[uuid.UUID, uuid.UUID] = Field(default_factory=dict)
    rules: list[HandoverRule] = Field(default_factory=list, max_length=200)
    #: Равномерное распределение остатка сделок без явного правила.
    pool_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    include_closed_deals: bool = True
    include_organizations: bool = True


@dataclass(slots=True)
class DistributionCounts:
    deals: int = 0
    organizations: int = 0
    tasks: int = 0


@dataclass(slots=True)
class ResolvedHandover:
    """Итог `HandoverService.resolve`: кому что уходит, до записи в БД."""

    user_id: uuid.UUID
    deal_successors: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    organization_successors: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)
    task_successors: dict[uuid.UUID, uuid.UUID] = field(default_factory=dict)

    @property
    def distribution(self) -> list[tuple[uuid.UUID, DistributionCounts]]:
        counts: dict[uuid.UUID, DistributionCounts] = defaultdict(DistributionCounts)
        for successor_id in self.deal_successors.values():
            counts[successor_id].deals += 1
        for successor_id in self.organization_successors.values():
            counts[successor_id].organizations += 1
        for successor_id in self.task_successors.values():
            counts[successor_id].tasks += 1
        return sorted(counts.items(), key=lambda item: str(item[0]))


@dataclass(slots=True)
class ExecutionResult:
    reassigned_deals: list[uuid.UUID]
    reassigned_organizations: list[uuid.UUID]
    reassigned_tasks: int
    distribution: list[tuple[uuid.UUID, DistributionCounts]]


class HandoverService:
    """Движок распределения портфеля. Без своей сессии в конструкторе — обе операции
    (`resolve`, `execute`) принимают `session` явно: `resolve` не пишет в БД (кроме
    блокировки строк на чтение не требуется), `execute` пишет внутри транзакции
    вызывающего (тот же приём, что `offboard_confirm` — коммит делает FastAPI после
    обработчика)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve(
        self, user_id: uuid.UUID, plan: HandoverPlan, principal: Principal
    ) -> ResolvedHandover:
        await self._validate_successors(user_id, plan, principal)

        all_deal_rows = (
            await self._session.execute(
                select(Deal.id, Deal.organization_id, Deal.deal_type, Deal.closed_at).where(
                    Deal.owner_id == user_id, Deal.deleted_at.is_(None)
                )
            )
        ).all()
        owned_deal_ids = {row.id for row in all_deal_rows}
        invalid_explicit = set(plan.deal_successors) - owned_deal_ids
        if invalid_explicit:
            raise ValidationError(
                "Сделка не принадлежит уходящему сотруднику",
                [
                    FieldError(field="deal_successors", reason=str(deal_id))
                    for deal_id in invalid_explicit
                ],
            )
        # Закрытые сделки без явного правила остаются за `include_closed_deals`; названная
        # явно в `deal_successors` — переходит всегда, это намеренное указание вызывающего.
        deal_rows = [
            row
            for row in all_deal_rows
            if plan.include_closed_deals or row.closed_at is None or row.id in plan.deal_successors
        ]
        if len(deal_rows) > MAX_DEALS_PER_CALL:
            raise AppError(
                ErrorCode.VALIDATION,
                "Слишком много сделок для одной передачи — разбейте на несколько вызовов",
                extra={"deals": len(deal_rows), "limit": MAX_DEALS_PER_CALL, "remaining": 0},
            )
        deal_ids = [row.id for row in deal_rows]

        # Направления продуктов каждой сделки — один запрос на всю пачку, не на сделку.
        product_rows: Sequence[Any] = []
        if deal_ids:
            product_rows = (
                await self._session.execute(
                    select(DealProduct.deal_id, Product.id, Product.direction_id)
                    .join(Product, Product.id == DealProduct.product_id)
                    .where(DealProduct.deal_id.in_(deal_ids))
                )
            ).all()
        directions_by_deal: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
        products_by_deal: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
        for row in product_rows:
            products_by_deal[row.deal_id].append(row[1])
            if row.direction_id is not None:
                directions_by_deal[row.deal_id].append(row.direction_id)

        # Текущая загрузка кандидатов пула — сколько открытых сделок уже назначено в БД;
        # дальше растёт в памяти по мере раздачи остатка этой передачи.
        pool_load: dict[uuid.UUID, int] = dict.fromkeys(plan.pool_ids, 0)
        if plan.pool_ids:
            loaded = (
                await self._session.execute(
                    select(Deal.owner_id, func.count())
                    .where(
                        Deal.owner_id.in_(plan.pool_ids),
                        Deal.deleted_at.is_(None),
                        Deal.closed_at.is_(None),
                    )
                    .group_by(Deal.owner_id)
                )
            ).all()
            for owner_id, count in loaded:
                pool_load[owner_id] = int(count)

        deal_successors: dict[uuid.UUID, uuid.UUID] = {}
        for row in deal_rows:
            explicit = plan.deal_successors.get(row.id)
            if explicit is not None:
                deal_successors[row.id] = explicit
                continue
            rule_successor = self._match_deal_rule(
                plan.rules,
                organization_id=row.organization_id,
                deal_type=row.deal_type,
                product_ids=products_by_deal.get(row.id, []),
                direction_ids=directions_by_deal.get(row.id, []),
            )
            if rule_successor is not None:
                deal_successors[row.id] = rule_successor
                continue
            if plan.pool_ids:
                chosen = min(plan.pool_ids, key=lambda pid: pool_load[pid])
                pool_load[chosen] += 1
                deal_successors[row.id] = chosen
                continue
            deal_successors[row.id] = plan.default_successor_id

        organization_successors: dict[uuid.UUID, uuid.UUID] = {}
        if plan.include_organizations:
            org_rows = (
                await self._session.execute(
                    select(Organization.id).where(
                        Organization.owner_id == user_id, Organization.deleted_at.is_(None)
                    )
                )
            ).all()
            if len(org_rows) > MAX_ORGANIZATIONS_PER_CALL:
                raise AppError(
                    ErrorCode.VALIDATION,
                    "Слишком много вузов для одной передачи — разбейте на несколько вызовов",
                    extra={
                        "organizations": len(org_rows),
                        "limit": MAX_ORGANIZATIONS_PER_CALL,
                        "remaining": 0,
                    },
                )
            # Направление открытых сделок уходящего по каждому вузу — для правила
            # «большинство сделок вуза этого направления».
            org_directions: dict[uuid.UUID, Counter[uuid.UUID]] = defaultdict(Counter)
            for row in deal_rows:
                if row.organization_id is None or row.closed_at is not None:
                    continue
                for direction_id in directions_by_deal.get(row.id, []):
                    org_directions[row.organization_id][direction_id] += 1

            for (org_id,) in org_rows:
                explicit = plan.organization_successors.get(org_id)
                if explicit is not None:
                    organization_successors[org_id] = explicit
                    continue
                by_org_rule = self._match_rule_field(plan.rules, "organization_id", org_id)
                if by_org_rule is not None:
                    organization_successors[org_id] = by_org_rule
                    continue
                majority = org_directions.get(org_id)
                if majority:
                    top_direction, _ = majority.most_common(1)[0]
                    by_direction_rule = self._match_rule_field(
                        plan.rules, "direction_id", top_direction
                    )
                    if by_direction_rule is not None:
                        organization_successors[org_id] = by_direction_rule
                        continue
                organization_successors[org_id] = plan.default_successor_id

        task_rows = (
            await self._session.execute(
                select(Task.id, Task.deal_id).where(
                    Task.assignee_id == user_id,
                    Task.status.in_(OPEN_TASK_STATUSES),
                    Task.deleted_at.is_(None),
                )
            )
        ).all()
        task_successors = {
            row.id: deal_successors.get(row.deal_id, plan.default_successor_id) for row in task_rows
        }

        return ResolvedHandover(
            user_id=user_id,
            deal_successors=deal_successors,
            organization_successors=organization_successors,
            task_successors=task_successors,
        )

    @staticmethod
    def _match_rule_field(
        rules: list[HandoverRule], field_name: RuleFieldName, value: uuid.UUID
    ) -> uuid.UUID | None:
        for rule in rules:
            if getattr(rule.match, field_name) == value:
                return rule.successor_id
        return None

    @classmethod
    def _match_deal_rule(
        cls,
        rules: list[HandoverRule],
        *,
        organization_id: uuid.UUID | None,
        deal_type: str,
        product_ids: list[uuid.UUID],
        direction_ids: list[uuid.UUID],
    ) -> uuid.UUID | None:
        for rule in rules:
            match = rule.match
            if match.organization_id is not None and match.organization_id == organization_id:
                return rule.successor_id
            if match.deal_type is not None and match.deal_type == deal_type:
                return rule.successor_id
            if match.product_id is not None and match.product_id in product_ids:
                return rule.successor_id
            if match.direction_id is not None and match.direction_id in direction_ids:
                return rule.successor_id
        return None

    async def _validate_successors(
        self, user_id: uuid.UUID, plan: HandoverPlan, principal: Principal
    ) -> None:
        """Все преемники, упомянутые где-либо в плане, проходят `ensure_assignable_user`:
        активны, не сам уходящий, и (для HEAD) в его команде. Ошибка любого — отказ до
        начала записи, чтобы `execute` не делал частичную работу."""
        successor_ids: set[uuid.UUID] = {plan.default_successor_id}
        successor_ids.update(plan.deal_successors.values())
        successor_ids.update(plan.organization_successors.values())
        successor_ids.update(plan.pool_ids)
        successor_ids.update(rule.successor_id for rule in plan.rules)
        for successor_id in successor_ids:
            if successor_id == user_id:
                raise ValidationError(
                    "Преемник не может совпадать с уходящим сотрудником",
                    [FieldError(field="successor_id", reason=str(successor_id))],
                )
            await ensure_assignable_user(self._session, principal, successor_id, roles=None)

    async def execute(
        self,
        resolved: ResolvedHandover,
        principal: Principal,
        *,
        reason: str,
        deal_notification_template: str = "DEAL_REASSIGNED",
    ) -> ExecutionResult:
        departing = await self._session.get(User, resolved.user_id)
        departing_name = departing.effective_name if departing else str(resolved.user_id)

        reassigned_deals: list[uuid.UUID] = []
        if resolved.deal_successors:
            deals = (
                (
                    await self._session.execute(
                        select(Deal).where(Deal.id.in_(resolved.deal_successors.keys()))
                    )
                )
                .scalars()
                .all()
            )
            for deal in deals:
                successor_id = resolved.deal_successors[deal.id]
                await claim_version(self._session, deal)
                if not await _reassign_owner(
                    self._session,
                    deal,
                    successor_id,
                    reason=reason,
                    actor_id=principal.user_id,
                    bump_version=False,
                ):
                    continue
                reassigned_deals.append(deal.id)
                successor = await self._session.get(User, successor_id)
                successor_name = successor.effective_name if successor else str(successor_id)
                self._session.add(
                    DealComment(
                        deal_id=deal.id,
                        author_id=principal.user_id,
                        body=(
                            f"Ответственный изменён: {departing_name} → "
                            f"{successor_name} (причина: {reason})"
                        ),
                        is_system=True,
                    )
                )

        reassigned_organizations: list[uuid.UUID] = []
        if resolved.organization_successors:
            organizations = (
                (
                    await self._session.execute(
                        select(Organization).where(
                            Organization.id.in_(resolved.organization_successors.keys())
                        )
                    )
                )
                .scalars()
                .all()
            )
            for organization in organizations:
                successor_id = resolved.organization_successors[organization.id]
                if organization.owner_id == successor_id:
                    continue
                old_owner_id = organization.owner_id
                await claim_version(self._session, organization)
                organization.owner_id = successor_id
                reassigned_organizations.append(organization.id)
                await AuditService(self._session).record(
                    AuditAction.ORGANIZATION_OWNER_CHANGED,
                    entity_type="organization",
                    entity_id=organization.id,
                    changes={
                        "owner_id": {
                            "old": str(old_owner_id) if old_owner_id else None,
                            "new": str(successor_id),
                        },
                        "reason": {"old": None, "new": reason},
                    },
                )

        reassigned_tasks = 0
        if resolved.task_successors:
            tasks = (
                (
                    await self._session.execute(
                        select(Task).where(Task.id.in_(resolved.task_successors.keys()))
                    )
                )
                .scalars()
                .all()
            )
            for task in tasks:
                task.assignee_id = resolved.task_successors[task.id]
                reassigned_tasks += 1

        if reassigned_deals or reassigned_organizations:
            await self._session.flush()
            await AuditService(self._session).record(
                AuditAction.DEAL_REASSIGNED_BULK,
                entity_type="user",
                entity_id=resolved.user_id,
                changes={
                    "deal_ids": {"old": None, "new": [str(i) for i in reassigned_deals]},
                    "organization_ids": {
                        "old": None,
                        "new": [str(i) for i in reassigned_organizations],
                    },
                    "reason": {"old": None, "new": reason},
                },
            )

        distribution = resolved.distribution
        notification_service = get_notification_service()
        for successor_id, counts in distribution:
            if counts.deals:
                await notification_service.notify_user(
                    self._session,
                    recipient_id=successor_id,
                    template_code=deal_notification_template,
                    priority=NotificationPriority.NORMAL,
                    payload={"count": counts.deals, "from_user": str(resolved.user_id)},
                )
            if counts.organizations:
                await notification_service.notify_user(
                    self._session,
                    recipient_id=successor_id,
                    template_code="ORG_OWNER_ASSIGNED",
                    priority=NotificationPriority.NORMAL,
                    payload={"count": counts.organizations},
                )

        return ExecutionResult(
            reassigned_deals=reassigned_deals,
            reassigned_organizations=reassigned_organizations,
            reassigned_tasks=reassigned_tasks,
            distribution=distribution,
        )
