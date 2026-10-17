"""Сиды B2B- и B2C-воронок (раздел 7 текущего спринта, new_spec §1).

Запускается один раз при подготовке демо-окружения, и повторно на каждом
`docker compose up`/хуке Helm перед `install`/`upgrade`:

    python -m app.modules.workflow.seed

Идемпотентно: если воронка с данным `code` уже существует, сид её не трогает — повторный
запуск на уже заполненной базе безопасен. Два исключения:

1. `backfill_resume_transitions`: воронки, развёрнутые до появления выхода из «Заморожена»,
   получают недостающие переходы «Возобновить» (см. его докстринг: только когда это безопасно).
2. `_demote_seed_default` (B2-2): при появлении новой default-воронки типа сделки (например
   `b2b_university_v2` на стенде, где default всё ещё прежняя сидовая `b2b_university_v1`) сид
   сам снимает `is_default` со своей прежней воронки — иначе уникальный индекс «одна default-
   воронка на тип сделки» упал бы, а вместе с ним и `helm upgrade`. Трогается только СИДОВАЯ
   воронка (`_SEED_OWNED_DEFAULTS`); решение администратора о default сид не отменяет.

Три воронки (с B2-1): `b2b_university_v2` (основная B2B, 14 шагов задания, default),
`b2b_university_v1` (дополнительная продажная с ПЭП-согласованием КП, не default) и
`b2c_individual_v1` (B2C). Все создаются сразу опубликованными. Это осознанное отличие от
обычного пути «черновик → правка → публикация»: цель сидов — дать рабочую воронку сразу после
`docker compose up`, а не черновик, который ещё нужно довести руками до публикуемого
состояния. Публикация идёт тем же валидатором и той же процедурой снимка, что и
`POST /workflows/{id}/publish` (`_validate_graph_data`, `_build_snapshot` переиспользуются
напрямую) — если сид пропустит вход в воронку или оставит статус-ловушку, скрипт откажется его
опубликовать, а не создаст молча нерабочий граф.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from dataclasses import dataclass, field
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import run_after_commit, session_scope
from app.core.logging import configure_logging
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.signing.models import SignatureTemplate
from app.modules.workflow.models import (
    SlaRule,
    StatusType,
    Workflow,
    WorkflowState,
    WorkflowStatus,
    WorkflowTransition,
)
from app.modules.workflow.service import (
    _build_snapshot,
    _validate_graph_data,
    invalidate_workflow_cache,
    snapshot_digest,
    snapshot_structure_warnings,
    transition_snapshot_entry,
)

logger = structlog.get_logger(__name__)

# SLA шагов воронки (new_spec §4.10): в начальном статусе — сутки, в промежуточных — три
# рабочих дня; предупреждение на 75% срока, эскалация — руководителю. Без правил у всех
# сделок `sla_due_at = null`, и фильтры с индикаторами SLA пусты.
_INITIAL_SLA_HOURS = 24
_STEP_SLA_HOURS = 72
_SLA_WARN_PCT = 75

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
    #: Срок пребывания в статусе в часах, выходные не считаются (`count_business_days`);
    #: `None` — без SLA (won, lost, parked).
    sla_hours: int | None = None


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
class StepDef:
    """Шаг линейного участка воронки (B2-1).

    `label` — короткий ярлык для переходов «назад» и «возобновить» («Вернуться
    на шаг 5», «Возобновить на шаге 5»): полные дословные названия задания
    доходят до 105 символов (шаг 10) и не годятся для подписи кнопки
    («Вернуться к статусу «Актуализация учебной программы …»»). `None` —
    старое поведение: полное имя статуса в этих переходах (см. `_legacy_steps`
    — адаптер для `_b2b_sales_spec`/`_b2c_spec`, их граф не меняется).
    `sla_hours=None` — старое поведение `_linear_funnel` (24ч начальный,
    72ч промежуточные); воронка v2 задаёт срок каждого шага явно по таблице
    задания.
    """

    code: str
    name: str
    sla_hours: int | None = None
    label: str | None = None


def _legacy_steps(steps: list[tuple[str, str]]) -> list[StepDef]:
    """Адаптер для воронок, граф которых не меняется (`_b2b_sales_spec`,
    `_b2c_spec`): те же код/имя, что раньше, SLA и имена переходов
    назад/возобновления — по умолчанию `_linear_funnel` (см. `StepDef`)."""
    return [StepDef(code=code, name=name) for code, name in steps]


@dataclass(slots=True)
class WorkflowSpec:
    code: str
    name: str
    deal_type: str
    statuses: list[StatusSpec]
    transitions: list[TransitionSpec]


def _linear_funnel(steps: list[StepDef]) -> tuple[list[StatusSpec], list[TransitionSpec]]:
    """Строит статусы и переходы обычного «шаг за шагом» участка воронки.

    Для каждого шага: переход вперёд, переход назад (с комментарием —
    раздел 4.9 требует объяснять откат), переход в `lost` и в `parked`.
    Специфика конкретных шагов (действия ПЭП, события интеграции, условия
    выхода) добавляется вызывающей стороной поверх (см. `_b2b_sales_spec`,
    `_b2b_spec`, `_b2c_spec`): переходы с той же парой статусов заменяют
    сгенерированные здесь заготовки.
    """
    statuses: list[StatusSpec] = [
        StatusSpec(
            code=step.code,
            name=step.name,
            type=StatusType.INITIAL.value if index == 0 else StatusType.INTERMEDIATE.value,
            sort_order=(index + 1) * 10,
            sla_hours=(
                step.sla_hours
                if step.sla_hours is not None
                else (_INITIAL_SLA_HOURS if index == 0 else _STEP_SLA_HOURS)
            ),
        )
        for index, step in enumerate(steps)
    ]

    statuses += [
        StatusSpec(
            code="won",
            name="Успешно закрыта",
            type=StatusType.WON.value,
            required_fields=["amount", "expected_close_date"],
            sort_order=1000,
        ),
        StatusSpec(
            code="lost",
            name="Отказ",
            type=StatusType.LOST.value,
            required_fields=["loss_reason_id"],
            sort_order=1010,
        ),
        StatusSpec(
            code="parked",
            name="Заморожена",
            type=StatusType.PARKED.value,
            sort_order=1020,
        ),
    ]

    transitions: list[TransitionSpec] = []
    for index, step in enumerate(steps):
        if index + 1 < len(steps):
            nxt = steps[index + 1]
            transitions.append(
                TransitionSpec(
                    from_code=step.code,
                    to_code=nxt.code,
                    name=f"Перейти к статусу «{nxt.name}»",
                    sort_order=10,
                )
            )
        if index > 0:
            prev = steps[index - 1]
            back_name = (
                f"Вернуться на шаг {prev.label}"
                if step.label is not None
                else f"Вернуться к статусу «{prev.name}»"
            )
            transitions.append(
                TransitionSpec(
                    from_code=step.code,
                    to_code=prev.code,
                    name=back_name,
                    requires_comment=True,
                    sort_order=20,
                )
            )
        transitions.append(
            TransitionSpec(
                from_code=step.code,
                to_code="lost",
                name="Отказ",
                requires_comment=True,
                conditions=_LOST_CONDITION,
                sort_order=30,
            )
        )
        transitions.append(
            TransitionSpec(
                from_code=step.code,
                to_code="parked",
                name="Заморозить",
                requires_comment=True,
                conditions=_PARK_CONDITION,
                sort_order=40,
            )
        )
        # Выход из заморозки: parked — пауза, а не закрытие (SLA стоит, время копится в
        # `sla_paused_total`). Без возврата на любой шаг сделка «зависла» бы навсегда.
        resume_name = (
            f"Возобновить на шаге {step.label}"
            if step.label is not None
            else f"Возобновить: вернуть на статус «{step.name}»"
        )
        transitions.append(
            TransitionSpec(
                from_code="parked",
                to_code=step.code,
                name=resume_name,
                requires_comment=True,
                sort_order=100 + index,
            )
        )

    return statuses, transitions


def _b2b_sales_spec() -> WorkflowSpec:
    """B2-1 (вариант B): старая продажная воронка B2B — граф НЕ меняется (живые сделки на
    стенде ссылаются на неё, мастер архивации требует 14 прогонов для перестройки графа на
    месте). Переименована только функция (была `_b2b_spec`) и имя воронки — чтобы не путать с
    новой `_b2b_spec` (v2, 14 шагов задания, см. ниже). Код `b2b_university_v1` и структура
    статусов/переходов остаются прежними: ПЭП-согласование КП (переход `kp_preparation →
    kp_approval`) живёт только здесь, в v2 подпись запрашивается вручную на шаге 6 (`signing`).
    Сидится с `is_default=False` (B2-2): на чистой БД по умолчанию идёт v2."""
    steps = _legacy_steps(
        [
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
    )

    extra = [
        # Раздел 7: вход в kp_approval запускает ПЭП-согласование КП
        # руководителем; отклонение возвращает сделку в kp_preparation.
        TransitionSpec(
            from_code="kp_preparation",
            to_code="kp_approval",
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
            from_code="kp_approval",
            to_code="legal_approval",
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
            from_code="contract_signing",
            to_code="lms_transfer",
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
            from_code="lms_transfer",
            to_code="training_launch",
            name="Запустить обучение",
            actions=[{"type": "integration_event", "event_code": "LEARNING_ENROLLMENT_SENT"}],
            sort_order=10,
        ),
        # monitoring — обычный шаг воронки: forward/backward-переходы для него
        # уже строит `_linear_funnel`, здесь только переход в терминальный won.
        TransitionSpec(
            from_code="closing_prolongation",
            to_code="won",
            name="Закрыть сделку успешно",
            conditions=_WON_CONDITION,
            sort_order=10,
        ),
    ]

    # Замена автогенерируемых прямых переходов из _linear_funnel там, где
    # выше уже описан осмысленный переход с действием/условием того же
    # направления — иначе получится дублирующий переход и конфликт по
    # уникальному индексу (workflow_id, from_status_id, to_status_id).
    overridden_pairs = {(t.from_code, t.to_code) for t in extra}

    statuses, base_transitions = _linear_funnel(steps)
    forward_and_backward = [
        t for t in base_transitions if (t.from_code, t.to_code) not in overridden_pairs
    ]
    transitions = forward_and_backward + extra
    return WorkflowSpec(
        code="b2b_university_v1",
        name="B2B: продажа КП с юридическим согласованием (дополнительная)",
        deal_type="b2b",
        statuses=statuses,
        transitions=transitions,
    )


#: 14 шагов задания (раздел 2, дословно из `rtk_requiriments.md:25-38`; шаг 5 —
#: с пометкой «(опционально)», как в таблице B2-1; шаг 2 сохраняет «с ним»).
#: SLA — предложение разработчика (вопрос владельцу, см. отчёт спринта B2),
#: в рабочих сутках (`sla_hours = 24 × дней`).
_B2B_STEPS: list[StepDef] = [
    StepDef("contact_search", "Поиск контактов ответственного в вузе", 24 * 3, "1"),
    StepDef(
        "communication",
        "Коммуникация с ним и уточнение актуальности программ по ИТ-направлениям",
        24 * 5,
        "2",
    ),
    StepDef("meeting", "Организация встречи с представителями вуза", 24 * 10, "3"),
    StepDef(
        "documents_exchange", "Обмен необходимым пакетом документов для подписания", 24 * 5, "4"
    ),
    StepDef(
        "documents_correction",
        "Корректировка документов перед подписанием (опционально)",
        24 * 3,
        "5",
    ),
    StepDef("signing", "Подписание документов", 24 * 10, "6"),
    StepDef(
        "materials_transfer",
        "Передача обучающих материалов по ИТ-направлению, лицензии ИТ-продукта "
        "и его документации в вуз",
        24 * 5,
        "7",
    ),
    StepDef("implementation_support", "Сопровождение внедрения ИТ-продуктов в вузе", 24 * 20, "8"),
    StepDef("teacher_training", "Обучение преподавателей", 24 * 20, "9"),
    StepDef(
        "curriculum_update",
        "Актуализация учебной программы по ИТ-направлению с учётом обучения "
        "преподавателя и добавления ИТ-продукта",
        24 * 20,
        "10",
    ),
    StepDef("classes", "Ведение занятий", 24 * 60, "11"),
    StepDef(
        "docs_update",
        "Актуализация документации по продукту и обучающим материалам",
        24 * 10,
        "12",
    ),
    StepDef("teacher_qualification", "Повышение квалификации преподавателей", 24 * 20, "13"),
    StepDef("execution_control", "Контроль за исполнением каждого этапа", 24 * 5, "14"),
]

#: Условие шагов 4/5/6: к шагу приложен файл-договор (черновик на 4/5, подписанный скан на 6).
_STEP_CONTRACT_ATTACHED = {"field": "step_attachments.contract", "op": "exists"}
_STEP_ACT_ATTACHED = {"field": "step_attachments.act", "op": "exists"}


def _b2b_spec() -> WorkflowSpec:
    """B2-1 (вариант B): новая основная воронка B2B — 14 шагов задания плюс won/lost/parked
    (17 статусов, 71 переход, 14 SLA-правил). Замена `_b2b_sales_spec` (v1) по умолчанию для
    типа сделки `b2b`: сид снимает default с v1 при первом запуске (B2-2).

    Шаг 5 опционален: 4→6 — главный путь вперёд (минуя корректировку), 4→5 — явный уход на
    правки. ПЭП запрашивается вручную на вкладке «Подписание» (не действием DSL, как в v1):
    переход 6→7 открывается либо по `signature_status=signed`, либо по приложенному на шаге
    подписанному сканy (`step_attachments.contract`). Шаг 14 («контроль за исполнением каждого
    этапа») закрывает сделку успешно только HEAD (`allowed_roles`, ADMIN проходит всегда) и
    несёт отдельный переход «новый цикл» на шаг 11 (пролонгация обучения на новый семестр).
    """
    extra = [
        TransitionSpec(
            from_code="contact_search",
            to_code="communication",
            name="Контакт найден: начать коммуникацию",
            conditions={"field": "contact_id", "op": "not_null"},
            sort_order=10,
        ),
        TransitionSpec(
            from_code="communication",
            to_code="meeting",
            name="Программы актуальны: организовать встречу",
            requires_comment=True,
            sort_order=10,
        ),
        TransitionSpec(
            from_code="meeting",
            to_code="documents_exchange",
            name="Встреча проведена: обменяться документами",
            sort_order=10,
        ),
        # Необязательный шаг 5: 4→6 — главная кнопка «вперёд» (минуя корректировку),
        # 4→5 — явный уход на правки. Фронтенд делит вперёд/назад по sort_order статусов, не
        # переходов, поэтому обе ветки из шага 4 остаются «вперёд» (new_spec: statusUtils.ts).
        TransitionSpec(
            from_code="documents_exchange",
            to_code="signing",
            name="Документы согласованы без правок: к подписанию",
            conditions=_STEP_CONTRACT_ATTACHED,
            sort_order=10,
        ),
        TransitionSpec(
            from_code="documents_exchange",
            to_code="documents_correction",
            name="Отправить документы на корректировку",
            requires_comment=True,
            conditions=_STEP_CONTRACT_ATTACHED,
            sort_order=20,
        ),
        TransitionSpec(
            from_code="documents_correction",
            to_code="signing",
            name="Документы скорректированы: к подписанию",
            requires_comment=True,
            conditions=_STEP_CONTRACT_ATTACHED,
            sort_order=10,
        ),
        # ПЭП запрашивается вручную на вкладке «Подписание» сделки (не действием DSL): условие
        # ниже пропускает и готовый `signature_status=signed`, и подписанный скан, приложенный
        # прямо на этом шаге — демо не должно зависеть от прохождения полного цикла ПЭП.
        TransitionSpec(
            from_code="signing",
            to_code="materials_transfer",
            name="Документы подписаны: передать материалы и лицензию",
            conditions={
                "any": [
                    {"field": "signature_status", "op": "eq", "value": "signed"},
                    _STEP_CONTRACT_ATTACHED,
                ]
            },
            actions=[{"type": "integration_event", "event_code": "LEARNING_TRANSFER_REQUESTED"}],
            sort_order=10,
        ),
        TransitionSpec(
            from_code="materials_transfer",
            to_code="implementation_support",
            name="Материалы переданы: сопровождать внедрение",
            conditions=_STEP_ACT_ATTACHED,
            actions=[
                {
                    "type": "create_task",
                    "title": "Провести установку и настройку ПО в вузе",
                    "assignee": "owner",
                    "due_days": 10,
                }
            ],
            sort_order=10,
        ),
        TransitionSpec(
            from_code="implementation_support",
            to_code="teacher_training",
            name="ПО внедрено: обучить преподавателей",
            actions=[
                {"type": "integration_event", "event_code": "LEARNING_ENROLLMENT_SENT"},
                {
                    "type": "create_task",
                    "title": "Согласовать график обучения",
                    "assignee": "owner",
                },
            ],
            sort_order=10,
        ),
        TransitionSpec(
            from_code="teacher_training",
            to_code="curriculum_update",
            name="Обучение преподавателей завершено: актуализировать программу",
            requires_comment=True,
            sort_order=10,
        ),
        TransitionSpec(
            from_code="curriculum_update",
            to_code="classes",
            name="Программа актуализирована: начать занятия",
            sort_order=10,
        ),
        TransitionSpec(
            from_code="classes",
            to_code="docs_update",
            name="Занятия ведутся: актуализировать документацию",
            sort_order=10,
        ),
        TransitionSpec(
            from_code="docs_update",
            to_code="teacher_qualification",
            name="Документация актуализирована: повысить квалификацию преподавателей",
            sort_order=10,
        ),
        TransitionSpec(
            from_code="teacher_qualification",
            to_code="execution_control",
            name="Квалификация преподавателей повышена: перейти к контролю исполнения",
            sort_order=10,
        ),
        # Итоговая приёмка: закрывает успешно только HEAD (критично для демо — KAM не должен
        # уметь закрыть взаимодействие сам). ADMIN проходит всегда (crm.service.transition).
        TransitionSpec(
            from_code="execution_control",
            to_code="won",
            name="Принять результаты и закрыть взаимодействие",
            requires_comment=True,
            allowed_roles=["HEAD"],
            conditions={
                "all": [
                    {"field": "amount", "op": "not_null"},
                    {"field": "expected_close_date", "op": "not_null"},
                    _STEP_ACT_ATTACHED,
                ]
            },
            sort_order=10,
        ),
        # Новый цикл: занятия, документация и повышение квалификации повторяются каждый
        # семестр — это не "возврат" (не требует lost/parked), отдельный переход на шаг 11.
        TransitionSpec(
            from_code="execution_control",
            to_code="classes",
            name="Начать новый цикл занятий (пролонгация)",
            requires_comment=True,
            sort_order=50,
        ),
    ]

    overridden_pairs = {(t.from_code, t.to_code) for t in extra}
    statuses, base_transitions = _linear_funnel(_B2B_STEPS)
    forward_and_backward = [
        t for t in base_transitions if (t.from_code, t.to_code) not in overridden_pairs
    ]
    transitions = forward_and_backward + extra
    return WorkflowSpec(
        code="b2b_university_v2",
        name="B2B: взаимодействие с вузом (14 шагов задания)",
        deal_type="b2b",
        statuses=statuses,
        transitions=transitions,
    )


def _b2c_spec() -> WorkflowSpec:
    steps = _legacy_steps(
        [
            ("site_application", "Заявка с сайта"),
            ("contact_verification", "Верификация контакта"),
            ("consultation", "Консультация"),
            ("payment_contract", "Оплата и договор оферты"),
            ("lms_enrollment", "Зачисление в LMS"),
            ("training_completed", "Завершение обучения"),
        ]
    )

    extra = [
        TransitionSpec(
            from_code="contact_verification",
            to_code="consultation",
            name="Подтвердить контакт",
            conditions={"field": "custom_fields.contact_verified", "op": "eq", "value": True},
            sort_order=10,
        ),
        TransitionSpec(
            from_code="payment_contract",
            to_code="lms_enrollment",
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
            from_code="training_completed",
            to_code="won",
            name="Закрыть сделку успешно",
            conditions=_WON_CONDITION,
            sort_order=10,
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


async def backfill_resume_transitions(
    session: AsyncSession, workflow: Workflow, spec: WorkflowSpec
) -> int:
    """Выход из «Заморожена» для воронки, развёрнутой сидом до его появления. Возвращает число
    добавленных переходов.

    Старые сиды заводили `parked` без единого исходящего перехода: сделка, замороженная на любом
    шаге, оставалась там навсегда. Новые сиды несут переходы «Возобновить» сразу, а на уже
    развёрнутую воронку они сами не попадают (сид существующую воронку не трогает).

    Безопасно для живых сделок, потому что они идут по снимку `published_graph`, а не по живым
    строкам, и правка только ДОБАВЛЯЕТ переходы: ни один существующий переход, статус и его
    идентификатор не меняются, а замороженные сделки получают выход. Поэтому переходы вносятся
    в оба места сразу — в живые строки (их видит конструктор и следующая публикация) и в снимок —
    а не пересборкой снимка из живых строк: та опубликовала бы всё, что администратор успел
    набросать в черновике (так уже было с архивацией статуса). Версия воронки растёт: открытый
    конструктор со старой версией не сможет сохранить граф без новых переходов и стереть их.

    Условия, при которых воронка НЕ трогается (это чужие решения, а не дефект сида):
    воронка не опубликована; у `parked` в снимке или в черновике уже есть хоть один исходящий
    переход (администратор настроил выход по-своему); статуса `parked` или шага нет в снимке
    (удалён, заархивирован). Тогда выход нужно добавить в конструкторе воронок вручную."""
    if workflow.state != WorkflowState.PUBLISHED.value or workflow.published_graph is None:
        return 0
    wanted = [t for t in spec.transitions if t.from_code == "parked"]
    if not wanted:
        return 0

    statuses = {
        row.code: row
        for row in (
            await session.execute(
                select(WorkflowStatus).where(WorkflowStatus.workflow_id == workflow.id)
            )
        ).scalars()
    }
    parked = statuses.get("parked")
    snapshot = workflow.published_graph
    live_ids = {item["id"] for item in snapshot.get("statuses", [])}
    if parked is None or parked.is_archived or str(parked.id) not in live_ids:
        return 0
    if any(item["from_status_id"] == str(parked.id) for item in snapshot.get("transitions", [])):
        return 0
    draft_exits = await session.scalar(
        select(func.count())
        .select_from(WorkflowTransition)
        .where(
            WorkflowTransition.workflow_id == workflow.id,
            WorkflowTransition.from_status_id == parked.id,
        )
    )
    if draft_exits:
        return 0

    added: list[WorkflowTransition] = []
    for item in wanted:
        target = statuses.get(item.to_code)
        if target is None or target.is_archived or str(target.id) not in live_ids:
            continue
        row = WorkflowTransition(
            workflow_id=workflow.id,
            from_status_id=parked.id,
            to_status_id=target.id,
            name=item.name,
            allowed_roles=item.allowed_roles,
            conditions=item.conditions,
            actions=item.actions,
            requires_comment=item.requires_comment,
            sort_order=item.sort_order,
        )
        session.add(row)
        added.append(row)
    if not added:
        return 0
    await session.flush()  # идентификаторы новых переходов нужны снимку

    updated = {
        **snapshot,
        "transitions": [
            *snapshot.get("transitions", []),
            *(transition_snapshot_entry(row) for row in added),
        ],
    }
    old_hash = workflow.graph_hash
    workflow.published_graph = updated
    workflow.graph_hash = snapshot_digest(updated)
    workflow.version += 1
    await session.flush()
    workflow_id = workflow.id
    run_after_commit(session, lambda: invalidate_workflow_cache(workflow_id))

    for warning in snapshot_structure_warnings(updated):
        logger.warning("workflow_backfill_warning", code=spec.code, warning=warning)
    await AuditService(session).record(
        AuditAction.WORKFLOW_UPDATED,
        entity_type="workflow",
        entity_id=workflow.id,
        changes={
            "resume_transitions_added": {"old": 0, "new": len(added)},
            "graph_hash": {"old": old_hash, "new": workflow.graph_hash},
        },
    )
    logger.info("workflow_resume_transitions_backfilled", code=spec.code, added=len(added))
    return len(added)


#: B2-2: на каждом `docker compose up`/хуке Helm сид запускается заново. Если бы он слепо
#: создавал новую default-воронку, уникальный индекс `uq_workflows_default_per_deal_type`
#: (одна опубликованная default-воронка на тип сделки) упал бы, а вместе с ним и
#: `helm upgrade` — сид обязан снять default со СВОЕЙ прежней воронки сам, тем же приёмом,
#: что `WorkflowService._demote_other_defaults` в API (см. `_demote_seed_default`).
_SEED_OWNED_DEFAULTS: dict[str, str] = {"b2b": "b2b_university_v1"}

#: Переключатель смены default сидом. `true` (демо/dev) — сид сам снимает default со своей
#: прежней воронки (`_SEED_OWNED_DEFAULTS`) при появлении новой; `false` (рекомендация для
#: prod, вопрос владельцу №7 отчёта спринта B2) — сид только добавляет воронку, не трогая
#: текущий default, администратор переключает вручную (`PATCH /api/workflows/{id}`).
#: Переменная окружения, а не поле `Settings`: сид — CLI-скрипт, не часть процесса api/worker.
_SEED_SWITCH_DEFAULT_WORKFLOW_ENV = "SEED_SWITCH_DEFAULT_WORKFLOW"


def _seed_switch_default_workflow_enabled() -> bool:
    raw = os.environ.get(_SEED_SWITCH_DEFAULT_WORKFLOW_ENV, "true").strip().lower()
    return raw not in {"false", "0", "no", ""}


async def _demote_seed_default(session: AsyncSession, deal_type: str, new_code: str) -> bool:
    """Снимает `is_default` с опубликованной default-воронки `deal_type`, если ею остаётся
    прежняя СИДОВАЯ воронка (`_SEED_OWNED_DEFAULTS`) — то есть решение администратора не
    трогается: воронка, назначенная default вручную через API, сидом не демотируется."""
    current = (
        await session.execute(
            select(Workflow).where(
                Workflow.deal_type == deal_type,
                Workflow.is_default.is_(True),
                Workflow.state == WorkflowState.PUBLISHED.value,
            )
        )
    ).scalar_one_or_none()
    if current is None or current.code == new_code:
        return False
    owned_code = _SEED_OWNED_DEFAULTS.get(deal_type)
    if owned_code is None or current.code != owned_code:
        logger.info("workflow_seed_default_kept", deal_type=deal_type, current_default=current.code)
        return False
    current.is_default = False
    current.version += 1
    await session.flush()
    workflow_id = current.id
    run_after_commit(session, lambda: invalidate_workflow_cache(workflow_id))
    await AuditService(session).record(
        AuditAction.WORKFLOW_UPDATED,
        entity_type="workflow",
        entity_id=current.id,
        changes={"is_default": {"old": True, "new": False}},
    )
    logger.info("workflow_seed_default_demoted", code=current.code, new_default=new_code)
    return True


async def seed_workflow(
    session: AsyncSession, spec: WorkflowSpec, *, is_default: bool = True
) -> Workflow | None:
    existing = (
        await session.execute(select(Workflow).where(Workflow.code == spec.code))
    ).scalar_one_or_none()
    if existing is not None:
        await backfill_resume_transitions(session, existing, spec)
        logger.info("workflow_seed_skip_existing", code=spec.code)
        return None

    if is_default and _seed_switch_default_workflow_enabled():
        await _demote_seed_default(session, spec.deal_type, spec.code)
    # Если default на этот тип сделки уже есть (чужой или переключатель выключен), новая
    # воронка создаётся НЕ default — иначе уникальный индекс упадёт, а сид обязан не падать
    # на предсказуемой коллизии (риск спринта: сид — хук Helm с backoffLimit).
    existing_default = await session.scalar(
        select(Workflow.id).where(
            Workflow.deal_type == spec.deal_type,
            Workflow.is_default.is_(True),
            Workflow.state == WorkflowState.PUBLISHED.value,
        )
    )
    effective_default = is_default and existing_default is None

    workflow = Workflow(
        code=spec.code,
        name=spec.name,
        deal_type=spec.deal_type,
        is_default=effective_default,
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
    sla_rules = [
        SlaRule(
            workflow_id=workflow.id,
            status_id=by_code[status_spec.code].id,
            max_duration=dt.timedelta(hours=status_spec.sla_hours),
            warn_threshold_pct=_SLA_WARN_PCT,
            escalate_to_role="HEAD",
            channels=["in_app"],
            count_business_days=True,
        )
        for status_spec in spec.statuses
        if status_spec.sla_hours is not None
    ]
    session.add_all(sla_rules)
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
        raise RuntimeError(f"Сид {spec.code!r} не проходит валидацию графа: {'; '.join(errors)}")
    for warning in warnings:
        logger.warning("workflow_seed_warning", code=spec.code, warning=warning)

    snapshot = _build_snapshot(workflow, statuses, transitions, sla_rules)
    digest = snapshot_digest(snapshot)

    workflow.published_graph = snapshot
    workflow.graph_hash = digest
    workflow.published_at = dt.datetime.now(dt.UTC)
    workflow.published_by = None
    workflow.state = WorkflowState.PUBLISHED.value
    await session.flush()

    audit = AuditService(session)
    await audit.record(
        AuditAction.WORKFLOW_CREATED,
        entity_type="workflow",
        entity_id=workflow.id,
        changes={"code": {"old": None, "new": spec.code}},
    )
    await audit.record(
        AuditAction.WORKFLOW_PUBLISHED,
        entity_type="workflow",
        entity_id=workflow.id,
        changes={"graph_hash": {"old": None, "new": digest}},
    )

    logger.info(
        "workflow_seeded",
        code=spec.code,
        statuses=len(spec.statuses),
        transitions=len(spec.transitions),
    )
    return workflow


#: Тело шаблона согласования КП (dop.md §10.4 фаза 1 п.1) — минимальный, но
#: настоящий Jinja2→HTML документ: рендерится, хэшируется и подписывается по
#: тому же конвейеру, что и любой другой документ ПЭП.
_KP_APPROVAL_TEMPLATE = """
<h1>Коммерческое предложение {{ deal_number }}</h1>
<p><strong>Организация:</strong> {{ organization_name }}</p>
<p><strong>Сумма:</strong> {{ amount }} {{ currency }}</p>
<p><strong>Количество обучающихся:</strong> {{ students_planned }}</p>
<p><strong>Ответственный со стороны ИТ Школы:</strong> {{ owner_name }}</p>
<p>Настоящим документом руководитель согласовывает коммерческое предложение
по сделке {{ deal_number }} для направления на подписание контрагенту.</p>
<p>Дата формирования: {{ today }}</p>
"""


async def seed_signature_templates(session: AsyncSession) -> None:
    """Шаблон `kp_approval`, на который ссылается переход `kp_preparation →
    kp_approval` ТОЛЬКО дополнительной воронки `_b2b_sales_spec` (v1, ПЭП-
    согласование КП руководителем). Основная воронка `_b2b_spec` (v2, 14
    шагов задания) этот шаблон не использует: подпись там запрашивается
    вручную на вкладке «Подписание» сделки (шаг 6, `signing`), а не
    действием DSL `request_signature`. Идемпотентно — тем же приёмом, что
    `seed_workflow`: существующий `code` сид не трогает."""
    existing = await session.scalar(
        select(SignatureTemplate.id).where(SignatureTemplate.code == "kp_approval")
    )
    if existing is not None:
        return
    session.add(
        SignatureTemplate(
            code="kp_approval",
            name="Согласование коммерческого предложения",
            doc_type="kp",
            body_template=_KP_APPROVAL_TEMPLATE,
            required_signer_roles=[{"role": "HEAD"}],
            default_deadline_days=7,
        )
    )
    await session.flush()
    logger.info("signature_template_seeded", code="kp_approval")


async def seed_default_workflows() -> None:
    """Порядок важен (B2-2): v2 сидится первой и на чистой БД занимает default для `b2b`;
    дополнительная v1 — следом, явно `is_default=False`, независимо от переключателя (она
    никогда не default); на стенде со старым сидом `_demote_seed_default` внутри `seed_workflow`
    снимет default с уже существующей v1, прежде чем v1 дойдёт до ветки «уже есть»."""
    async with session_scope() as session:
        await seed_signature_templates(session)
        await seed_workflow(session, _b2b_spec())
        await seed_workflow(session, _b2b_sales_spec(), is_default=False)
        await seed_workflow(session, _b2c_spec())


def main() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    asyncio.run(seed_default_workflows())


if __name__ == "__main__":
    main()
