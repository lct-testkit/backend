"""Схемы модуля сделок (раздел 6.6).

`fields` в `TransitionRequest` намеренно свободный `dict[str, Any]`, а не
типизированная модель: раздел 6.6 описывает его как произвольный набор
значений, которыми переход закрывает `required_fields` целевого статуса —
набор этих полей задаётся администратором в конструкторе воронок (раздел
6.5), а не фиксируется в бэкенде статически. Whitelist того, что реально
можно записать через `fields`, живёт в `DealService._apply_transition_field`
(`app/modules/crm/service.py`) — это граница безопасности, а не эта схема.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

DealTypeLiteral = Literal["b2b", "b2c"]
PriorityLiteral = Literal["low", "normal", "high", "critical"]
SlaStateLiteral = Literal["ok", "warning", "breached", "paused"]
TaskStatusLiteral = Literal["open", "in_progress", "done", "cancelled"]
CommentFormatLiteral = Literal["plain", "markdown"]

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

# --- Продукты сделки ---------------------------------------------------------


class DealProductIn(BaseModel):
    product_id: uuid.UUID
    quantity: int = Field(default=1, gt=0)
    price: Decimal | None = None
    discount_pct: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    total: Decimal | None = None


class DealProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    product_id: uuid.UUID
    quantity: int
    price: Decimal | None = None
    discount_pct: Decimal
    total: Decimal | None = None


# --- Сделка ------------------------------------------------------------------


class DealCreateRequest(BaseModel):
    title: NonEmptyStr = Field(max_length=255)
    deal_type: DealTypeLiteral
    workflow_id: uuid.UUID | None = None
    organization_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    amount: Decimal | None = None
    currency: str = Field(default="RUB", min_length=3, max_length=3)
    students_planned: int | None = Field(default=None, ge=0)
    expected_close_date: dt.date | None = None
    priority: PriorityLiteral = "normal"
    products: list[DealProductIn] = Field(default_factory=list)
    custom_fields: dict[str, Any] = Field(default_factory=dict)
    source: str | None = Field(default=None, max_length=32)
    external_ids: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _requires_party(self) -> DealCreateRequest:
        # new_spec §4.9: для B2B организация обязательна, для B2C — контакт.
        if self.deal_type == "b2b" and self.organization_id is None:
            raise ValueError("Для сделки B2B обязателен organization_id")
        if self.deal_type == "b2c" and self.contact_id is None:
            raise ValueError("Для сделки B2C обязателен contact_id")
        return self


class DealUpdateRequest(BaseModel):
    """Частичное обновление. `owner_id`, `status_id`, `workflow_id`,
    `deal_type` через эту ручку не меняются — для этого есть `/reassign` и
    `/transition`, у которых собственные права и побочные эффекты."""

    title: NonEmptyStr | None = Field(default=None, max_length=255)
    amount: Decimal | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    students_planned: int | None = Field(default=None, ge=0)
    expected_close_date: dt.date | None = None
    priority: PriorityLiteral | None = None
    organization_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    source: str | None = Field(default=None, max_length=32)
    custom_fields: dict[str, Any] | None = None


class DealOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    number: str
    title: str
    deal_type: str
    workflow_id: uuid.UUID
    status_id: uuid.UUID
    organization_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    owner_id: uuid.UUID
    created_by: uuid.UUID | None = None
    amount: Decimal | None = None
    currency: str
    students_planned: int | None = None
    expected_close_date: dt.date | None = None
    status_changed_at: dt.datetime
    sla_due_at: dt.datetime | None = None
    sla_state: str
    priority: str
    loss_reason_id: uuid.UUID | None = None
    closed_at: dt.datetime | None = None
    custom_fields: dict[str, Any]
    source: str | None = None
    external_ids: dict[str, Any]
    owner_unavailable: bool
    signature_status: str
    version: int
    created_at: dt.datetime
    updated_at: dt.datetime


class DealCardOut(BaseModel):
    """Карточка сделки (раздел 6.6): сделка + продукты + счётчики."""

    deal: DealOut
    products: list[DealProductOut] = Field(default_factory=list)
    open_tasks_count: int = 0
    comments_count: int = 0


class DealListResponse(BaseModel):
    items: list[DealOut]
    next_cursor: str | None = None


# --- Переходы ------------------------------------------------------------


class TransitionConditionOut(BaseModel):
    field: str
    op: str
    expected: Any = None
    actual: Any = None
    satisfied: bool


class AvailableTransitionOut(BaseModel):
    id: uuid.UUID
    name: str
    to_status_id: uuid.UUID
    requires_comment: bool
    role_allowed: bool
    satisfied: bool
    conditions: list[TransitionConditionOut] = Field(default_factory=list)
    actions: list[dict[str, Any]] = Field(default_factory=list)


class AvailableTransitionsResponse(BaseModel):
    items: list[AvailableTransitionOut]


class TransitionRequest(BaseModel):
    to_status_id: uuid.UUID
    comment: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)
    # Вложения: модуль files — спринт 4. Поле принимается ради стабильности
    # контракта с фронтендом, но пока не обрабатывается (см. docstring модуля).
    attachments: list[uuid.UUID] = Field(default_factory=list)


class TransitionResponse(BaseModel):
    deal: DealOut


# --- История --------------------------------------------------------------


class DealStatusHistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    from_status_id: uuid.UUID | None = None
    to_status_id: uuid.UUID
    changed_by: uuid.UUID | None = None
    transition_id: uuid.UUID | None = None
    reason: str
    comment: str | None = None
    duration_in_prev: dt.timedelta | None = None
    sla_state_at_change: str | None = None
    changed_at: dt.datetime


class DealEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_type: str
    actor_id: uuid.UUID | None = None
    payload: dict[str, Any] | None = None
    created_at: dt.datetime


class DealHistoryResponse(BaseModel):
    statuses: list[DealStatusHistoryOut]
    events: list[DealEventOut]


# --- Назначение ответственного ---------------------------------------------


class ReassignRequest(BaseModel):
    owner_id: uuid.UUID
    reason: NonEmptyStr = Field(max_length=500)


class BulkReassignRequest(BaseModel):
    deal_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    successor_id: uuid.UUID
    reason: NonEmptyStr = Field(max_length=500)


class BulkReassignResponse(BaseModel):
    reassigned_count: int


# --- Участники -----------------------------------------------------------


class ParticipantAddRequest(BaseModel):
    user_id: uuid.UUID
    role_in_deal: Literal["watcher", "co_owner", "lawyer", "methodist"]


class ParticipantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    deal_id: uuid.UUID
    user_id: uuid.UUID
    role_in_deal: str
    added_by: uuid.UUID | None = None
    added_at: dt.datetime


class ParticipantListResponse(BaseModel):
    items: list[ParticipantOut]


# --- Комментарии -------------------------------------------------------------


class CommentCreateRequest(BaseModel):
    body: NonEmptyStr
    parent_id: uuid.UUID | None = None
    mentions: list[uuid.UUID] = Field(default_factory=list)
    is_internal: bool = False


class CommentUpdateRequest(BaseModel):
    body: NonEmptyStr


class CommentDeleteRequest(BaseModel):
    reason: str | None = None


class CommentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    deal_id: uuid.UUID
    author_id: uuid.UUID | None = None
    parent_id: uuid.UUID | None = None
    body: str
    body_format: str
    mentions: list[str]
    is_system: bool
    is_internal: bool
    edited_at: dt.datetime | None = None
    created_at: dt.datetime
    updated_at: dt.datetime


class CommentListResponse(BaseModel):
    items: list[CommentOut]


# --- Задачи --------------------------------------------------------------


class TaskCreateRequest(BaseModel):
    deal_id: uuid.UUID
    title: NonEmptyStr = Field(max_length=255)
    description: str | None = None
    assignee_id: uuid.UUID
    due_at: dt.datetime | None = None
    priority: PriorityLiteral = "normal"


class TaskUpdateRequest(BaseModel):
    title: NonEmptyStr | None = Field(default=None, max_length=255)
    description: str | None = None
    assignee_id: uuid.UUID | None = None
    due_at: dt.datetime | None = None
    priority: PriorityLiteral | None = None
    status: TaskStatusLiteral | None = None


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    deal_id: uuid.UUID
    title: str
    description: str | None = None
    assignee_id: uuid.UUID
    created_by: uuid.UUID | None = None
    due_at: dt.datetime | None = None
    completed_at: dt.datetime | None = None
    completed_by: uuid.UUID | None = None
    status: str
    priority: str
    auto_created_by_transition_id: uuid.UUID | None = None
    created_at: dt.datetime
    updated_at: dt.datetime


class TaskListResponse(BaseModel):
    items: list[TaskOut]
    next_cursor: str | None = None
