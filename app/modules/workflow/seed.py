"""Сиды B2B- и B2C-воронок (раздел 7 текущего спринта, new_spec §1).

Запускается один раз при подготовке демо-окружения:

    python -m app.modules.workflow.seed

Идемпотентно: если воронка с данным `code` уже существует, сид её не
трогает — повторный запуск на уже заполненной базе безопасен.

Обе воронки создаются сразу опубликованными. Это осознанное отличие от
обычного пути «черновик → правка → публикация»: цель сидов — дать рабочую
воронку сразу после `docker compose up`, а не черновик, который ещё нужно
довести руками до публикуемого состояния. Публикация идёт тем же
валидатором и той же процедурой снимка, что и `POST /workflows/{id}/publish`
(`_validate_graph_data`, `_build_snapshot` переиспользуются напрямую) —
если сид пропустит вход в воронку или оставит статус-ловушку, скрипт
откажется его опубликовать, а не создаст молча нерабочий граф.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.logging import configure_logging
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.workflow.models import (
    StatusType,
    Workflow,
    WorkflowState,
    WorkflowStatus,
    WorkflowTransition,
)
from app.modules.workflow.service import _build_snapshot, _validate_graph_data

logger = structlog.get_logger(__name__)

# Условие «Заморозить»: дата возобновления или причина обязательны, иначе
# сделка «зависает» в parked без понятного плана дальнейших действий.
_PARK_CONDITION = {
    "any": [
        {"field": "custom_fields.resume_at", "op": "not_null"},
        {"field": "custom_fields.park_reason", "op": "not_null"},
    ]
}
_LOST_CONDITION = {"field": "loss_reason_id", "op": "not_null"}
_WON_CONDITION = {
    "all": [
        {"field": "amount", "op": "not_null"},
        {"field": "expected_close_date", "op": "not_null"},
    ]
}


@dataclass(slots=True)
class StatusSpec:
    code: str
    name: str
    type: str = StatusType.INTERMEDIATE.value
    required_fields: list[str] = field(default_factory=list)
    sort_order: int = 0


@dataclass(slots=True)
class TransitionSpec:
    from_code: str
    to_code: str
    name: str
    requires_comment: bool = False
    conditions: dict[str, Any] = field(default_factory=dict)
    actions: list[dict[str, Any]] = field(default_factory=list)
    allowed_roles: list[str] = field(default_factory=list)
    sort_order: int = 0


@dataclass(slots=True)
class WorkflowSpec:
    code: str
    name: str
    deal_type: str
    statuses: list[StatusSpec]
    transitions: list[TransitionSpec]


def _linear_funnel(
    steps: list[tuple[str, str]],
) -> tuple[list[StatusSpec], list[TransitionSpec]]:
    """Строит статусы и переходы обычного «шаг за шагом» участка воронки.

    Для каждого шага: переход вперёд, переход назад (с комментарием —
    раздел 4.9 требует объяснять откат), переход в `lost` и в `parked`.
    Специфика конкретных шагов (действия ПЭП, события интеграции, условия
    выхода) добавляется вызывающей стороной поверх (см. `_b2b_spec`,
    `_b2c_spec`): переходы с той же парой статусов заменяют сгенерированные
    здесь заготовки.
    """
    statuses: list[StatusSpec] = [
        StatusSpec(
            code=code,
            name=name,
            type=StatusType.INITIAL.value if index == 0 else StatusType.INTERMEDIATE.value,
            sort_order=(index + 1) * 10,
        )
        for index, (code, name) in enumerate(steps)
    ]

    statuses += [
        StatusSpec(
            code="won", name="Успешно закрыта", type=StatusType.WON.value,
            required_fields=["amount", "expected_close_date"], sort_order=1000,
        ),
        StatusSpec(
            code="lost", name="Отказ", type=StatusType.LOST.value,
            required_fields=["loss_reason_id"], sort_order=1010,
        ),
        StatusSpec(
            code="parked", name="Заморожена", type=StatusType.PARKED.value, sort_order=1020,
        ),
    ]

    transitions: list[TransitionSpec] = []
    for index, (code, name) in enumerate(steps):
        if index + 1 < len(steps):
            next_code, next_name = steps[index + 1]
            transitions.append(
                TransitionSpec(
                    from_code=code, to_code=next_code,
                    name=f"Перейти к статусу «{next_name}»", sort_order=10,
                )
            )
        if index > 0:
            prev_code, prev_name = steps[index - 1]
            transitions.append(
                TransitionSpec(
                    from_code=code, to_code=prev_code,
                    name=f"Вернуться к статусу «{prev_name}»",
                    requires_comment=True, sort_order=20,
                )
            )
        transitions.append(
            TransitionSpec(
                from_code=code, to_code="lost", name="Отказ",
                requires_comment=True, conditions=_LOST_CONDITION, sort_order=30,
            )
        )
        transitions.append(
            TransitionSpec(
                from_code=code, to_code="parked", name="Заморозить",
                requires_comment=True, conditions=_PARK_CONDITION, sort_order=40,
            )
        )

    return statuses, transitions


def _b2b_spec() -> WorkflowSpec:
    steps = [
        ("identification", "Идентификация вуза"),
        ("first_contact", "Первичный контакт"),
        ("qualification", "Квалификация"),
        ("meeting_scheduled", "Назначение встречи"),
        ("meeting_held", "Проведение встречи"),
        ("requirements", "Сбор требований"),
        ("kp_preparation", "Формирование КП"),
        ("kp_approval", "Согласование КП"),
        ("legal_approval", "Юридическое согласование"),
        ("contract_signing", "Подписание договора"),
        ("lms_transfer", "Передача материалов в LMS"),
        ("training_launch", "Запуск обучения"),
        ("monitoring", "Мониторинг успеваемости"),
        ("closing_prolongation", "Закрытие периода и пролонгация"),
    ]

    extra = [
        # Раздел 7: вход в kp_approval запускает ПЭП-согласование КП
        # руководителем; отклонение возвращает сделку в kp_preparation.
        TransitionSpec(
            from_code="kp_preparation", to_code="kp_approval",
            name="Отправить КП на согласование",
            actions=[
                {
                    "type": "request_signature",
                    "template": "kp_approval",
                    "signers": [{"role": "HEAD"}],
                    "order": "sequential",
                    "deadline_days": 7,
                    "on_rejected": "previous_status",
                    "on_expired": "notify_initiator",
                }
            ],
            sort_order=10,
        ),
        # Юридическое согласование — задача юристу (в системе нет отдельной
        # роли LAWYER, поэтому задача ставится HEAD, который её делегирует).
        TransitionSpec(
            from_code="kp_approval", to_code="legal_approval",
            name="Передать на юридическое согласование",
            actions=[
                {
                    "type": "create_task",
                    "title": "Юридическое согласование договора",
                    "assignee_role": "HEAD",
                    "due_days": 3,
                    "priority": "high",
                }
            ],
            sort_order=10,
        ),
        # Выйти из подписания можно только когда договор подписан ПЭП или
        # приложен подписанный скан с административным подтверждением.
        TransitionSpec(
            from_code="contract_signing", to_code="lms_transfer",
            name="Передать материалы в LMS",
            conditions={
                "any": [
                    {"field": "signature_status", "op": "eq", "value": "signed"},
                    {"field": "attachments.contract", "op": "exists"},
                ]
            },
            actions=[
                {
                    "type": "integration_event",
                    "event_code": "LEARNING_TRANSFER_REQUESTED",
                }
            ],
            sort_order=10,
        ),
        TransitionSpec(
            from_code="lms_transfer", to_code="training_launch",
            name="Запустить обучение",
            actions=[
                {"type": "integration_event", "event_code": "LEARNING_ENROLLMENT_SENT"}
            ],
            sort_order=10,
        ),
        # monitoring — обычный шаг воронки: forward/backward-переходы для него
        # уже строит `_linear_funnel`, здесь только переход в терминальный won.
        TransitionSpec(
            from_code="closing_prolongation", to_code="won",
            name="Закрыть сделку успешно", conditions=_WON_CONDITION, sort_order=10,
        ),
    ]

    # Замена автогенерируемых прямых переходов из _linear_funnel там, где
    # выше уже описан осмысленный переход с действием/условием того же
    # направления — иначе получится дублирующий переход и конфликт по
    # уникальному индексу (workflow_id, from_status_id, to_status_id).
    overridden_pairs = {(t.from_code, t.to_code) for t in extra}

    statuses, base_transitions = _linear_funnel(steps)
    forward_and_backward = [
        t
        for t in base_transitions
        if (t.from_code, t.to_code) not in overridden_pairs
    ]
    transitions = forward_and_backward + extra
    return WorkflowSpec(
        code="b2b_university_v1",
        name="B2B: продвижение продукта вузу",
        deal_type="b2b",
        statuses=statuses,
        transitions=transitions,
    )


def _b2c_spec() -> WorkflowSpec:
    steps = [
        ("site_application", "Заявка с сайта"),
        ("contact_verification", "Верификация контакта"),
        ("consultation", "Консультация"),
        ("payment_contract", "Оплата и договор оферты"),
        ("lms_enrollment", "Зачисление в LMS"),
        ("training_completed", "Завершение обучения"),
    ]

    extra = [
        TransitionSpec(
            from_code="contact_verification", to_code="consultation",
            name="Подтвердить контакт",
            conditions={"field": "custom_fields.contact_verified", "op": "eq", "value": True},
            sort_order=10,
        ),
        TransitionSpec(
            from_code="payment_contract", to_code="lms_enrollment",
            name="Зачислить в LMS",
            conditions={
                "any": [
                    {"field": "attachments.contract", "op": "exists"},
                    {"field": "custom_fields.payment_confirmed", "op": "eq", "value": True},
                ]
            },
            actions=[{"type": "integration_event", "event_code": "LEARNING_ENROLLMENT_SENT"}],
            sort_order=10,
        ),
        TransitionSpec(
            from_code="training_completed", to_code="won",
            name="Закрыть сделку успешно", conditions=_WON_CONDITION, sort_order=10,
        ),
    ]
    overridden_pairs = {(t.from_code, t.to_code) for t in extra}

    statuses, base_transitions = _linear_funnel(steps)
    forward_and_backward = [
        t for t in base_transitions if (t.from_code, t.to_code) not in overridden_pairs
    ]
    transitions = forward_and_backward + extra
    return WorkflowSpec(
        code="b2c_individual_v1",
        name="B2C: обучение физлица",
        deal_type="b2c",
        statuses=statuses,
        transitions=transitions,
    )


async def seed_workflow(session: AsyncSession, spec: WorkflowSpec) -> Workflow | None:
    existing = (
        await session.execute(select(Workflow).where(Workflow.code == spec.code))
    ).scalar_one_or_none()
    if existing is not None:
        logger.info("workflow_seed_skip_existing", code=spec.code)
        return None

    workflow = Workflow(
        code=spec.code,
        name=spec.name,
        deal_type=spec.deal_type,
        is_default=True,
        state=WorkflowState.DRAFT.value,
    )
    session.add(workflow)
    await session.flush()

    by_code: dict[str, WorkflowStatus] = {}
    for status_spec in spec.statuses:
        row = WorkflowStatus(
            workflow_id=workflow.id,
            code=status_spec.code,
            name=status_spec.name,
            type=status_spec.type,
            sort_order=status_spec.sort_order,
            required_fields=status_spec.required_fields,
        )
        session.add(row)
        by_code[status_spec.code] = row
    await session.flush()

    for transition_spec in spec.transitions:
        session.add(
            WorkflowTransition(
                workflow_id=workflow.id,
                from_status_id=by_code[transition_spec.from_code].id,
                to_status_id=by_code[transition_spec.to_code].id,
                name=transition_spec.name,
                allowed_roles=transition_spec.allowed_roles,
                conditions=transition_spec.conditions,
                actions=transition_spec.actions,
                requires_comment=transition_spec.requires_comment,
                sort_order=transition_spec.sort_order,
            )
        )
    await session.flush()

    statuses = list(by_code.values())
    transitions = list(
        (
            await session.execute(
                select(WorkflowTransition).where(WorkflowTransition.workflow_id == workflow.id)
            )
        )
        .scalars()
        .all()
    )

    errors, warnings = _validate_graph_data(statuses, transitions)
    if errors:
        raise RuntimeError(
            f"Сид {spec.code!r} не проходит валидацию графа: {'; '.join(errors)}"
        )
    for warning in warnings:
        logger.warning("workflow_seed_warning", code=spec.code, warning=warning)

    snapshot = _build_snapshot(workflow, statuses, transitions, [])
    digest = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()

    workflow.published_graph = snapshot
    workflow.graph_hash = digest
    workflow.published_at = dt.datetime.now(dt.UTC)
    workflow.published_by = None
    workflow.state = WorkflowState.PUBLISHED.value
    await session.flush()

    audit = AuditService(session)
    await audit.record(
        AuditAction.WORKFLOW_CREATED, entity_type="workflow", entity_id=workflow.id,
        changes={"code": {"old": None, "new": spec.code}},
    )
    await audit.record(
        AuditAction.WORKFLOW_PUBLISHED, entity_type="workflow", entity_id=workflow.id,
        changes={"graph_hash": {"old": None, "new": digest}},
    )

    logger.info(
        "workflow_seeded",
        code=spec.code,
        statuses=len(spec.statuses),
        transitions=len(spec.transitions),
    )
    return workflow


async def seed_default_workflows() -> None:
    async with session_scope() as session:
        await seed_workflow(session, _b2b_spec())
        await seed_workflow(session, _b2c_spec())


def main() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    asyncio.run(seed_default_workflows())


if __name__ == "__main__":
    main()
