"""B5-4: целевая воронка и тип сделки на источник; маршрут на B2B (BUG-15).

Функц.5 ТЗ требует добавлять полученное «в существующий или новый workflow». До этой задачи
`CmsLeadService._process_lead` жёстко заводила `deal_type="b2c"` без выбора воронки — заявка вуза
попасть в B2B-воронку не могла. Модуль чистый (без БД), как `field_mapping.py`: `resolve_route`
смотрит на уже промапленный словарь полей (после `apply_mapping`, раньше `parse_lead_payload`) и
выбирает маршрут; проверка при сохранении (`validate_routing`) — единственное, что трогает сессию
(существование и состояние воронки/статуса).

Коды статусов и воронок НИГДЕ не зашиты литералом: маршрут хранит `workflow_code`/
`initial_status_code`, пустое значение — воронка/статус по умолчанию для `deal_type`. Это
осознанно: спринт B2 (эпик 1, недоступен из этого эпика) переименует статусы B2B-воронки — маршрут
с исчезнувшим `initial_status_code` откатывается на начальный статус воронки с предупреждением в
логе (`DealService.create` и так подставляет начальный статус для неизвестного кода, см. её
докстринг — предупреждение здесь просто делает это явным для исполняемого маршрута, а не для
прямого вызова API)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.integration.lead_payload import _canonical
from app.modules.workflow.models import StatusType, Workflow, WorkflowState
from app.modules.workflow.service import get_cached_published_graph

logger = structlog.get_logger(__name__)

_MAX_RULES = 10


class Route(BaseModel):
    deal_type: Literal["b2c", "b2b"] = "b2c"
    #: Пусто — опубликованная по умолчанию воронка этого `deal_type` (как и раньше).
    workflow_code: str | None = None
    #: Пусто — начальный статус воронки.
    initial_status_code: str | None = None
    owner_policy: Literal["least_loaded", "organization_owner", "fixed"] = "least_loaded"
    fixed_owner_id: str | None = None


class RouteCondition(BaseModel):
    target: str
    equals: str | None = None
    in_: list[str] | None = Field(default=None, alias="in")

    model_config = {"populate_by_name": True}


class RoutingRule(BaseModel):
    when: RouteCondition
    route: Route


class ProgressWhen(BaseModel):
    """B5-8: условие срабатывания правила движения сделки по прогрессу LMS. Хотя бы одно из
    полей обязательно — иначе правило никогда не сработает."""

    completed: bool | None = None
    progress_pct_gte: int | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def _at_least_one_condition(self) -> ProgressWhen:
        if self.completed is None and self.progress_pct_gte is None:
            raise ValueError("нужно completed или progress_pct_gte")
        return self


class ProgressRule(BaseModel):
    """B5-8: `{when, workflow_code, from_status, to_status, comment}` — переводит сделку в
    `to_status`, когда прогресс учащегося в `workflow_code` (текущий статус сделки — `from_status`)
    удовлетворяет `when`. `comment` — шаблон с подстановкой `{progress_pct}`."""

    when: ProgressWhen
    workflow_code: str = Field(min_length=1, max_length=64)
    from_status: str = Field(min_length=1, max_length=64)
    to_status: str = Field(min_length=1, max_length=64)
    comment: str = Field(default="", max_length=500)


class Routing(BaseModel):
    default: Route = Field(default_factory=Route)
    rules: list[RoutingRule] = Field(default_factory=list, max_length=_MAX_RULES)
    #: B5-8: правила движения сделки по прогрессу LMS (источник `lms`, см. `integration.lms.
    #: move_deal_by_progress`). У источника `cms` этот список просто не используется.
    progress_rules: list[ProgressRule] = Field(default_factory=list, max_length=_MAX_RULES)


@dataclass(slots=True)
class RoutingValidationError:
    field: str
    reason: str


def _condition_matches(condition: RouteCondition, mapped_payload: dict[str, Any]) -> bool:
    value = mapped_payload.get(condition.target)
    if value is None:
        return False
    text = str(value)
    if condition.equals is not None:
        return _canonical(text) == _canonical(condition.equals)
    if condition.in_ is not None:
        return any(_canonical(text) == _canonical(candidate) for candidate in condition.in_)
    return False


def resolve_route(routing: Routing | None, mapped_payload: dict[str, Any]) -> Route:
    """`routing=None` — прежнее поведение без изменений (деfолт `Route()`: B2C, воронка и
    начальный статус по умолчанию). Первое совпавшее правило побеждает, иначе — `default`."""
    if routing is None:
        return Route()
    for rule in routing.rules:
        if _condition_matches(rule.when, mapped_payload):
            return rule.route
    return routing.default


@dataclass(slots=True)
class ResolvedWorkflow:
    workflow: Workflow
    initial_status_code: str
    fallback_warning: bool = False


async def resolve_workflow_for_route(
    session: AsyncSession, route: Route
) -> ResolvedWorkflow | None:
    """Воронка и реальный начальный статус для исполнения маршрута. `None` — нет
    опубликованной воронки этого `deal_type` вовсе (ни по коду, ни по умолчанию) — вызывающий
    решает, заводить ли заявку без воронки или отказывать.

    `fallback_warning=True` — заданный `initial_status_code` не нашёлся в текущем графе
    (статус переименован/архивирован после сохранения маршрута, см. докстринг модуля):
    `DealService.create` сама подставит начальный статус, здесь только явный лог."""
    workflow: Workflow | None = None
    if route.workflow_code:
        workflow = await session.scalar(
            select(Workflow).where(
                Workflow.code == route.workflow_code,
                Workflow.state == WorkflowState.PUBLISHED.value,
            )
        )
        if workflow is not None and workflow.deal_type != route.deal_type:
            workflow = None
    if workflow is None:
        workflow = await session.scalar(
            select(Workflow).where(
                Workflow.deal_type == route.deal_type,
                Workflow.is_default.is_(True),
                Workflow.state == WorkflowState.PUBLISHED.value,
            )
        )
    if workflow is None:
        return None

    graph = await get_cached_published_graph(workflow)
    statuses = graph["statuses"]
    initial = next((s for s in statuses if s["type"] == StatusType.INITIAL.value), None)
    if initial is None:
        return None

    fallback = False
    if route.initial_status_code:
        chosen = next(
            (
                s
                for s in statuses
                if s["code"] == route.initial_status_code
                and s["type"] in (StatusType.INITIAL.value, StatusType.INTERMEDIATE.value)
            ),
            None,
        )
        if chosen is not None:
            initial = chosen
        else:
            fallback = True
            logger.warning(
                "integration_route_status_missing",
                workflow_code=workflow.code,
                requested_status=route.initial_status_code,
                fallback_status=initial["code"],
            )
    return ResolvedWorkflow(
        workflow=workflow, initial_status_code=initial["code"], fallback_warning=fallback
    )


async def validate_routing(session: AsyncSession, routing: Routing) -> list[RoutingValidationError]:
    errors: list[RoutingValidationError] = []

    async def _check_route(route: Route, prefix: str) -> None:
        if route.workflow_code:
            workflow = await session.scalar(
                select(Workflow).where(Workflow.code == route.workflow_code)
            )
            if workflow is None:
                errors.append(
                    RoutingValidationError(f"{prefix}.workflow_code", "воронка не найдена")
                )
                return
            if workflow.state != WorkflowState.PUBLISHED.value:
                errors.append(
                    RoutingValidationError(f"{prefix}.workflow_code", "воронка не опубликована")
                )
                return
            if workflow.deal_type != route.deal_type:
                errors.append(
                    RoutingValidationError(
                        f"{prefix}.workflow_code", "deal_type воронки не совпадает с маршрутом"
                    )
                )
                return
            if route.initial_status_code:
                graph = await get_cached_published_graph(workflow)
                found = next(
                    (
                        s
                        for s in graph["statuses"]
                        if s["code"] == route.initial_status_code
                        and s["type"] in (StatusType.INITIAL.value, StatusType.INTERMEDIATE.value)
                    ),
                    None,
                )
                if found is None:
                    errors.append(
                        RoutingValidationError(
                            f"{prefix}.initial_status_code",
                            "статус не найден в опубликованном графе воронки",
                        )
                    )
        if route.owner_policy == "fixed":
            if not route.fixed_owner_id:
                errors.append(
                    RoutingValidationError(f"{prefix}.fixed_owner_id", "нужен fixed_owner_id")
                )
            else:
                import uuid

                from app.modules.identity.models import User

                owner = None
                try:
                    owner = await session.get(User, uuid.UUID(route.fixed_owner_id))
                except ValueError:
                    pass
                if owner is None or not owner.is_active:
                    errors.append(
                        RoutingValidationError(
                            f"{prefix}.fixed_owner_id", "пользователь не найден или неактивен"
                        )
                    )

    async def _check_progress_rule(rule: ProgressRule, prefix: str) -> None:
        workflow = await session.scalar(select(Workflow).where(Workflow.code == rule.workflow_code))
        if workflow is None:
            errors.append(RoutingValidationError(f"{prefix}.workflow_code", "воронка не найдена"))
            return
        if workflow.state != WorkflowState.PUBLISHED.value:
            errors.append(
                RoutingValidationError(f"{prefix}.workflow_code", "воронка не опубликована")
            )
            return
        graph = await get_cached_published_graph(workflow)
        codes = {s["code"]: s for s in graph["statuses"]}
        from_ok = rule.from_status in codes
        to_ok = rule.to_status in codes
        if not from_ok:
            errors.append(
                RoutingValidationError(
                    f"{prefix}.from_status", "статус не найден в опубликованном графе воронки"
                )
            )
        if not to_ok:
            errors.append(
                RoutingValidationError(
                    f"{prefix}.to_status", "статус не найден в опубликованном графе воронки"
                )
            )
        if from_ok and to_ok:
            from_id = codes[rule.from_status]["id"]
            to_id = codes[rule.to_status]["id"]
            has_transition = any(
                t["from_status_id"] == from_id and t["to_status_id"] == to_id
                for t in graph["transitions"]
            )
            if not has_transition:
                errors.append(
                    RoutingValidationError(
                        f"{prefix}.to_status",
                        "переход из from_status в to_status не найден в графе воронки",
                    )
                )

    await _check_route(routing.default, "default")
    for index, rule in enumerate(routing.rules):
        await _check_route(rule.route, f"rules[{index}].route")
    if len(routing.rules) > _MAX_RULES:
        errors.append(RoutingValidationError("rules", f"не более {_MAX_RULES} правил"))
    for index, progress_rule in enumerate(routing.progress_rules):
        await _check_progress_rule(progress_rule, f"progress_rules[{index}]")
    if len(routing.progress_rules) > _MAX_RULES:
        errors.append(RoutingValidationError("progress_rules", f"не более {_MAX_RULES} правил"))
    return errors
