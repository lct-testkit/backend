"""Схемы модуля identity.

`MeResponse` соответствует разделу 6.1: id, full_name, display_name, email,
role, team_id, manager_id, status, locale, timezone, consent_version,
consent_required, scopes, плюс признак требуемой смены пароля.
"""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, Field


class MeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str
    display_name: str | None = None
    email: str | None = None
    role: str
    team_id: uuid.UUID | None = None
    manager_id: uuid.UUID | None = None
    status: str
    locale: str
    timezone: str
    consent_version: str | None = None
    consent_required: bool
    password_change_required: bool = False
    scopes: list[str]
    teams: list[uuid.UUID] = Field(default_factory=list)
    perm_epoch: int
    last_login_at: dt.datetime | None = None


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


class ConsentRequest(BaseModel):
    policy_version: str = Field(min_length=1, max_length=32)
    policy_text_hash: str = Field(min_length=64, max_length=64)


class ConsentResponse(BaseModel):
    accepted: bool
    policy_version: str
    accepted_at: dt.datetime


class PasswordChangeRequest(BaseModel):
    # Пароли не логируются и не попадают в аудит (раздел 6.1).
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=256)
    new_password_repeat: str = Field(min_length=12, max_length=256)


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


class LogoutRequest(BaseModel):
    # По умолчанию single logout: завершаются все сессии в realm.
    local_only: bool = False


class BackchannelLogoutRequest(BaseModel):
    logout_token: str


class OperationResult(BaseModel):
    ok: bool = True
    detail: str | None = None
