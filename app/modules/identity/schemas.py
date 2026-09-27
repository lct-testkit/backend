"""Схемы модуля identity.

`MeResponse` соответствует разделу 6.1: id, full_name, display_name, email,
role, team_id, manager_id, status, locale, timezone, consent_version,
consent_required, scopes, плюс признак требуемой смены пароля.

Схемы администрирования — раздел 6.2 и new_spec §4.1–4.8.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.core.patch import reject_null
from app.modules.identity.models import Role, UserStatus

RoleLiteral = Literal["KAM", "HEAD", "ADMIN", "AUDITOR", "INTEGRATION"]
StatusLiteral = Literal["invited", "active", "blocked", "terminated", "anonymized"]
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ReasonStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)]


def _check_phone(value: str) -> str:
    """На телефон уходит код подтверждения подписи: строка, которую нельзя
    набрать, оставила бы подписанта без кода."""
    value = value.strip()
    digits = re.sub(r"\D", "", value)
    if not re.fullmatch(r"\+?[\d\s()\-]+", value) or not 10 <= len(digits) <= 15:
        raise ValueError("Телефон: от 10 до 15 цифр, допустимы «+», пробелы, дефисы и скобки")
    return value


PhoneStr = Annotated[str, StringConstraints(max_length=32), AfterValidator(_check_phone)]


class MeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str
    display_name: str | None = None
    email: str | None = None
    phone: str | None = None
    role: str
    team_id: uuid.UUID | None = None
    manager_id: uuid.UUID | None = None
    status: str
    locale: str
    timezone: str
    consent_version: str | None = None
    consent_required: bool
    policy_version: str
    password_change_required: bool = False
    scopes: list[str]
    teams: list[uuid.UUID] = Field(default_factory=list)
    perm_epoch: int
    last_login_at: dt.datetime | None = None
    version: int = 1


class RecentItemOut(BaseModel):
    """Последний открытый объект: `type` — вид (`deal`), `id` — его идентификатор."""

    type: str
    id: uuid.UUID
    title: str
    opened_at: dt.datetime


class RecentListResponse(BaseModel):
    items: list[RecentItemOut]


class SessionInfo(BaseModel):
    sid: str
    device: str | None = None
    ip: str | None = None
    user_agent: str | None = None
    created_at: str
    last_seen_at: str
    is_current: bool = False


class SessionListResponse(BaseModel):
    items: list[SessionInfo]


class SessionsTerminatedResponse(BaseModel):
    """Итог «завершить остальные сессии»: сколько входов погашено (0 — уже нечего гасить)."""

    ok: bool = True
    terminated: int


class ConsentRequest(BaseModel):
    policy_version: str = Field(min_length=1, max_length=32)
    policy_text_hash: str = Field(min_length=64, max_length=64)


class ConsentResponse(BaseModel):
    accepted: bool
    policy_version: str
    accepted_at: dt.datetime


class PolicyResponse(BaseModel):
    """Действующая редакция политики обработки ПДн (для окна согласия)."""

    version: str
    text_hash: str | None = None


class PasswordChangeRequest(BaseModel):
    # Пароли не логируются и не попадают в аудит (раздел 6.1).
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)
    new_password_repeat: str = Field(min_length=12, max_length=256)

    @model_validator(mode="after")
    def _check_match(self) -> PasswordChangeRequest:
        if self.new_password != self.new_password_repeat:
            raise ValueError("Новый пароль и его повтор не совпадают")
        if self.new_password == self.current_password:
            raise ValueError("Новый пароль должен отличаться от текущего")
        return self


class PasswordChangeResponse(BaseModel):
    ok: bool = True
    sessions_terminated: int
    signature_requests_voided: int
    password_changed_at: dt.datetime


class AuthCallbackRequest(BaseModel):
    code: str
    state: str
    # Хранится на стороне BFF, если PKCE-верификатор не лежит в Redis.
    code_verifier: str | None = None
    redirect_uri: str | None = None


class AuthCallbackResponse(BaseModel):
    user: MeResponse
    consent_required: bool
    session_expires_in: int
    csrf_token: str


class LogoutRequest(BaseModel):
    # По умолчанию single logout: завершаются все сессии в realm.
    local_only: bool = False


class BackchannelLogoutRequest(BaseModel):
    logout_token: str


class OperationResult(BaseModel):
    ok: bool = True
    detail: str | None = None


class InviteCheckResponse(BaseModel):
    """Проверка одноразовой ссылки приглашения (new_spec §4.1 шаг 4)."""

    valid: bool
    email_masked: str | None = None
    full_name: str | None = None
    expires_at: dt.datetime
    login_url: str


# --- Администрирование пользователей (раздел 6.2) -------------------------


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str | None = None
    full_name: str
    display_name: str | None = None
    phone: str | None = None
    position: str | None = None
    role: str
    team_id: uuid.UUID | None = None
    manager_id: uuid.UUID | None = None
    status: str
    status_reason: str | None = None
    locale: str
    timezone: str
    perm_epoch: int
    consent_version: str | None = None
    must_change_password: bool = False
    last_login_at: dt.datetime | None = None
    invited_at: dt.datetime | None = None
    activated_at: dt.datetime | None = None
    blocked_at: dt.datetime | None = None
    auto_unblock_at: dt.datetime | None = None
    anonymized_at: dt.datetime | None = None
    created_at: dt.datetime
    updated_at: dt.datetime
    version: int


class UserListResponse(BaseModel):
    items: list[UserOut]
    next_cursor: str | None = None


class UserCreateRequest(BaseModel):
    full_name: NonEmptyStr = Field(max_length=255)
    email: EmailStr
    role: RoleLiteral
    team_id: uuid.UUID | None = None
    manager_id: uuid.UUID | None = None
    position: str | None = Field(default=None, max_length=255)
    require_totp: bool = False
    # Подтверждение второго администратора для создания роли ADMIN.
    approval_id: uuid.UUID | None = None


class UserCreateResponse(BaseModel):
    user: UserOut
    # Ссылка отдаётся один раз: в закрытом контуре SMTP может не быть.
    invite_url: str | None = None
    invite_expires_at: dt.datetime | None = None
    invite_email_sent: bool = False


class UserPatchRequest(BaseModel):
    role: RoleLiteral | None = None
    team_id: uuid.UUID | None = None
    manager_id: uuid.UUID | None = None
    status: Literal["invited", "active"] | None = None
    status_reason: str | None = Field(default=None, max_length=500)
    display_name: str | None = Field(default=None, max_length=255)
    phone: PhoneStr | None = None
    position: str | None = Field(default=None, max_length=255)
    locale: str | None = Field(default=None, max_length=8)
    timezone: str | None = Field(default=None, max_length=64)
    # Подтверждение второго администратора: обязательно для повышения до ADMIN.
    approval_id: uuid.UUID | None = None

    @field_validator("role", "status", "locale", "timezone", mode="before")
    @classmethod
    def _required_are_not_nulled(cls, value: Any) -> Any:
        # Эти колонки users — NOT NULL: `null` в PATCH раньше доходил до БД и давал 500.
        return reject_null(value)

    @model_validator(mode="after")
    def _not_empty(self) -> UserPatchRequest:
        if not self.model_fields_set:
            raise ValueError("Тело запроса не содержит изменяемых полей")
        return self


class MePatchRequest(BaseModel):
    """Свой профиль: имя для отображения, часовой пояс и телефон. Роль, email,
    статус и остальное меняет только администратор — лишние поля отклоняются,
    а не молча игнорируются."""

    model_config = ConfigDict(extra="forbid")

    display_name: (
        Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)] | None
    ) = None
    timezone: Annotated[str, StringConstraints(min_length=1, max_length=64)] | None = None
    phone: PhoneStr | None = None

    @field_validator("display_name")
    @classmethod
    def _blank_display_name_is_cleared(cls, value: str | None) -> str | None:
        return value or None

    @model_validator(mode="after")
    def _not_empty(self) -> MePatchRequest:
        if not self.model_fields_set:
            raise ValueError("Тело запроса не содержит изменяемых полей")
        if "timezone" in self.model_fields_set and self.timezone is None:
            raise ValueError("Часовой пояс нельзя очистить")
        return self


class UserBlockRequest(BaseModel):
    reason: ReasonStr
    auto_unblock_at: dt.datetime | None = None


class UserUnblockRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class PasswordResetRequest(BaseModel):
    reason: ReasonStr
    # Подозрение на компрометацию: подписи за последние 24 часа помечаются
    # как оспоренные (dop §10.7).
    suspect_compromise: bool = False


class OffboardPreviewItem(BaseModel):
    kind: str
    count: int
    supported: bool = True
    details: list[dict[str, Any]] = Field(default_factory=list)


class OffboardRequest(BaseModel):
    mode: Literal["preview", "confirm"] = "preview"
    successor_id: uuid.UUID | None = None
    reason: ReasonStr | None = None
    # «По-сделочно» (new_spec §4.7 шаг 2): `{id сделки: id преемника}`. Эти сделки уходят
    # названным преемникам, все остальные сделки, задачи и запросы подписи — `successor_id`.
    deal_successors: dict[uuid.UUID, uuid.UUID] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _confirm_needs_successor(self) -> OffboardRequest:
        if self.mode == "confirm" and self.successor_id is None:
            raise ValueError("Для подтверждения передачи дел нужен преемник")
        if self.mode == "confirm" and not self.reason:
            raise ValueError("Для подтверждения передачи дел нужна причина")
        return self


class OffboardResponse(BaseModel):
    mode: Literal["preview", "confirm"]
    user_id: uuid.UUID
    successor_id: uuid.UUID | None = None
    workload: list[OffboardPreviewItem]
    reassigned_deals: list[uuid.UUID] = Field(default_factory=list)
    signature_requests_reassigned: int = 0
    sessions_terminated: int = 0
    status: str | None = None
    warnings: list[str] = Field(default_factory=list)


class ErasureRequestBody(BaseModel):
    mode: Literal["anonymize", "hard_delete"] = "anonymize"
    reason: ReasonStr
    legal_basis: NonEmptyStr = Field(max_length=255)
    comment: str | None = Field(default=None, max_length=2000)
    approval_id: uuid.UUID | None = None


class ErasureBlocker(BaseModel):
    code: str
    detail: str
    count: int = 0
    legal_basis: str | None = None


class ErasureRequestOut(BaseModel):
    id: uuid.UUID
    subject_type: str
    subject_id: uuid.UUID
    mode: str
    status: str
    deadline_at: dt.datetime | None = None
    blockers: list[ErasureBlocker] = Field(default_factory=list)
    grace_until: dt.datetime | None = None


class ErasureRequestDetail(BaseModel):
    """Спринт 10: карточка запроса для `Администрирование → Удаляемые`

    (new_spec §4.8.4 шаг 4) — в отличие от `ErasureRequestOut` (форма ответа
    на создание), собирается из уже существующей записи, включая поля,
    появляющиеся только после исполнения (`rejection_reason`/`executed_at`/
    `act_file_id`). `blockers` в БД хранится как `{mode, comment, items}`
    (см. `DataErasureRequest.blockers`) — `from_model` разворачивает это в
    плоский список, а не полагается на `from_attributes` (форма JSONB не
    совпадает с формой ответа один в один).
    """

    id: uuid.UUID
    subject_type: str
    subject_id: uuid.UUID
    mode: str | None = None
    status: str
    reason: str | None = None
    legal_basis: str | None = None
    requested_by: uuid.UUID | None = None
    requested_at: dt.datetime
    deadline_at: dt.datetime | None = None
    grace_until: dt.datetime | None = None
    blockers: list[ErasureBlocker] = Field(default_factory=list)
    rejection_reason: str | None = None
    executed_at: dt.datetime | None = None
    act_file_id: uuid.UUID | None = None
    # Имя субъекта (сотрудник, контакт, ИП): у обезличенного — стабильный
    # псевдоним, у физически удалённого — `null`.
    subject_display: str | None = None

    @classmethod
    def from_model(cls, request: Any, subject_display: str | None = None) -> ErasureRequestDetail:
        stored = request.blockers or {}
        return cls(
            id=request.id,
            subject_type=request.subject_type,
            subject_id=request.subject_id,
            subject_display=subject_display,
            mode=stored.get("mode"),
            status=request.status,
            reason=request.reason,
            legal_basis=request.legal_basis,
            requested_by=request.requested_by,
            requested_at=request.requested_at,
            deadline_at=request.deadline_at,
            grace_until=request.grace_until,
            blockers=[ErasureBlocker(**item) for item in stored.get("items", [])],
            rejection_reason=request.rejection_reason,
            executed_at=request.executed_at,
            act_file_id=request.act_file_id,
        )


class ErasureRequestListResponse(BaseModel):
    items: list[ErasureRequestDetail]
    next_cursor: str | None = None


class ErasureRejectRequest(BaseModel):
    reason: ReasonStr


class TeamOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    parent_id: uuid.UUID | None = None
    head_id: uuid.UUID | None = None
    region_id: uuid.UUID | None = None
    created_at: dt.datetime
    updated_at: dt.datetime
    version: int


class TeamListResponse(BaseModel):
    items: list[TeamOut]
    next_cursor: str | None = None


class TeamCreateRequest(BaseModel):
    name: NonEmptyStr = Field(max_length=255)
    parent_id: uuid.UUID | None = None
    head_id: uuid.UUID | None = None
    region_id: uuid.UUID | None = None


class TeamPatchRequest(BaseModel):
    name: NonEmptyStr | None = Field(default=None, max_length=255)
    parent_id: uuid.UUID | None = None
    head_id: uuid.UUID | None = None
    region_id: uuid.UUID | None = None


class ApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    operation: str
    status: str
    entity_type: str | None = None
    entity_id: uuid.UUID | None = None
    payload: dict[str, Any]
    requested_by: uuid.UUID
    approved_by: uuid.UUID | None = None
    expires_at: dt.datetime
    created_at: dt.datetime


class ApprovalListResponse(BaseModel):
    items: list[ApprovalOut]
    next_cursor: str | None = None


class ApprovalDecision(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


ROLE_VALUES = tuple(role.value for role in Role)
STATUS_VALUES = tuple(status.value for status in UserStatus)
