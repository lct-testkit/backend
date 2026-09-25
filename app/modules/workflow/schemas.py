"""Схемы конструктора воронок (раздел 6.5).

`GraphIn`/`GraphOut` соответствуют телу `PUT /api/workflows/{id}/graph`: массив
статусов и массив переходов. SLA-правила в тело графа добавлены сознательно
(см. `app/modules/workflow/models.py`) — спецификация описывает их как часть
графа воронки, но не даёт отдельной ручки для записи, а хранить срок вне
графа означало бы, что «воронка без кода» на самом деле требует миграции для
SLA.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

DealTypeLiteral = Literal["b2b", "b2c"]
WorkflowStateLiteral = Literal["draft", "published", "archived"]
StatusTypeLiteral = Literal["initial", "intermediate", "won", "lost", "parked"]
SlaModeLiteral = Literal["keep", "recalculate", "reset"]
MappingJobStatusLiteral = Literal["pending", "running", "completed", "failed"]

CodeStr = Annotated[
    str, StringConstraints(strip_whitespace=True, pattern=r"^[a-z][a-z0-9_]{1,63}$")
]
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


# --- Воронка верхнего уровня ------------------------------------------------


class WorkflowCreateRequest(BaseModel):
    code: CodeStr
    name: NonEmptyStr = Field(max_length=255)
    deal_type: DealTypeLiteral
    is_default: bool = False


class WorkflowUpdateRequest(BaseModel):
    """`PATCH /workflows/{id}`: метаданные воронки. Граф правится через `PUT .../graph`."""

    name: NonEmptyStr | None = Field(default=None, max_length=255)
    is_default: bool | None = None


class WorkflowOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    deal_type: str
    state: str
    is_default: bool
    published_at: dt.datetime | None = None
    published_by: uuid.UUID | None = None
    graph_hash: str | None = None
    # Черновик графа отличается от опубликованного снимка (у воронки, которую ещё не
    # публиковали, — всегда `true`). Считается по содержимому, а не по `updated_at`.
    has_unpublished_changes: bool = False
    version: int
    created_at: dt.datetime
    updated_at: dt.datetime


class WorkflowListResponse(BaseModel):
    items: list[WorkflowOut]
    next_cursor: str | None = None


# --- Граф: статусы -----------------------------------------------------------


class StatusIn(BaseModel):
    """Статус в теле `PUT /graph`.

    `id` отсутствует у нового статуса и обязателен у существующего — так
    сервис отличает создание от правки без отдельного поля-флага.
    """

    id: uuid.UUID | None = None
    code: CodeStr
    name: NonEmptyStr = Field(max_length=255)
    type: StatusTypeLiteral = "intermediate"
    color: str | None = Field(default=None, max_length=16)
    sort_order: int = 0
    required_fields: list[str] = Field(default_factory=list)
    is_archived: bool = False


class StatusOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    type: str
    color: str | None = None
    sort_order: int
    required_fields: list[str]
    is_archived: bool
    archived_at: dt.datetime | None = None
    replaced_by_status_id: uuid.UUID | None = None


# --- Граф: переходы -----------------------------------------------------------


class TransitionIn(BaseModel):
    id: uuid.UUID | None = None
    # Ссылки на статусы внутри того же запроса допускают либо существующий
    # `id`, либо `code` нового статуса — тела приходят одним снимком, и
    # фронтенд не обязан сначала сохранять статусы, чтобы получить их id.
    from_status: str = Field(description="id существующего статуса или code нового")
    to_status: str = Field(description="id существующего статуса или code нового")
    name: NonEmptyStr = Field(max_length=255)
    allowed_roles: list[str] = Field(default_factory=list)
    conditions: dict[str, Any] = Field(default_factory=dict)
    actions: list[dict[str, Any]] = Field(default_factory=list)
    requires_comment: bool = False
    sort_order: int = 0


class TransitionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    from_status_id: uuid.UUID
    to_status_id: uuid.UUID
    name: str
    allowed_roles: list[str]
    conditions: dict[str, Any]
    actions: list[dict[str, Any]]
    requires_comment: bool
    sort_order: int


# --- Граф: SLA -----------------------------------------------------------------


class SlaRuleIn(BaseModel):
    status: str = Field(description="id существующего статуса или code нового")
    max_duration_hours: float = Field(gt=0, le=24 * 365)
    warn_threshold_pct: int = Field(default=80, ge=1, le=100)
    escalate_to_role: str | None = None
    escalate_to_user_id: uuid.UUID | None = None
    channels: list[str] = Field(default_factory=lambda: ["in_app"])
    count_business_days: bool = True
    is_active: bool = True


class SlaRuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status_id: uuid.UUID
    max_duration_hours: float
    warn_threshold_pct: int
    escalate_to_role: str | None = None
    escalate_to_user_id: uuid.UUID | None = None
    channels: list[str]
    count_business_days: bool
    is_active: bool

    @classmethod
    def from_model(cls, rule: Any) -> SlaRuleOut:
        return cls(
            id=rule.id,
            status_id=rule.status_id,
            max_duration_hours=rule.max_duration.total_seconds() / 3600,
            warn_threshold_pct=rule.warn_threshold_pct,
            escalate_to_role=rule.escalate_to_role,
            escalate_to_user_id=rule.escalate_to_user_id,
            channels=rule.channels,
            count_business_days=rule.count_business_days,
            is_active=rule.is_active,
        )


# --- Граф целиком --------------------------------------------------------------


class GraphIn(BaseModel):
    statuses: list[StatusIn]
    transitions: list[TransitionIn] = Field(default_factory=list)
    sla_rules: list[SlaRuleIn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_status_codes(self) -> GraphIn:
        codes = [s.code for s in self.statuses]
        if len(codes) != len(set(codes)):
            raise ValueError("Коды статусов должны быть уникальны в пределах воронки")
        return self


class GraphOut(BaseModel):
    workflow: WorkflowOut
    statuses: list[StatusOut]
    transitions: list[TransitionOut]
    sla_rules: list[SlaRuleOut]


# --- Валидация и публикация -----------------------------------------------


class ValidationIssue(BaseModel):
    code: str
    message: str
    path: str | None = None


class ValidateResponse(BaseModel):
    ok: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class PublishResponse(BaseModel):
    workflow: WorkflowOut
    graph_hash: str


# --- Архивирование статуса и мастер сопоставления --------------------------


class StatusImpactResponse(BaseModel):
    supported: bool
    active_count: int
    # Сделки без обязательных полей целевого статуса (`?target_status_id=`; иначе пусто): `id`,
    # `number`, `title`, `missing_fields`. Не больше 100, всего их — `problem_count`.
    problem_deals: list[dict[str, Any]] = Field(default_factory=list)
    problem_count: int = 0
    sla_affected: int = 0
    suggested_targets: list[StatusOut] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class StatusArchiveRequest(BaseModel):
    mapping_rules: dict[str, Any] = Field(default_factory=dict)
    target_status_id: uuid.UUID
    fallback_status_id: uuid.UUID | None = None
    sla_mode: SlaModeLiteral = "recalculate"


class StatusArchiveResponse(BaseModel):
    status: StatusOut
    job_id: uuid.UUID | None = None
    job_status: MappingJobStatusLiteral
    affected_count: int
    warnings: list[str] = Field(default_factory=list)


class StatusMappingJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    workflow_id: uuid.UUID
    from_status_id: uuid.UUID
    mapping_rules: dict[str, Any]
    affected_count: int
    processed_count: int
    failed_count: int
    status: str
    initiated_by: uuid.UUID | None = None
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    report: dict[str, Any] | None = None
    error: str | None = None
    created_at: dt.datetime
