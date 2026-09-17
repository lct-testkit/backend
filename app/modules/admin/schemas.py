"""Схемы административных системных ручек (раздел 6.12)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FeatureFlagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    is_enabled: bool
    description: str | None = None
    rollout: int
    updated_by: uuid.UUID | None = None
    updated_at: dt.datetime


class FeatureFlagListResponse(BaseModel):
    items: list[FeatureFlagOut]
    next_cursor: str | None = None


class FeatureFlagPatch(BaseModel):
    is_enabled: bool | None = None
    description: str | None = None
    rollout: int | None = Field(default=None, ge=0, le=100)


class SystemSettingOut(BaseModel):
    key: str
    # У секретных настроек значение заменяется маркером, а не отдаётся наружу.
    value: Any
    description: str | None = None
    is_secret: bool
    updated_by: uuid.UUID | None = None
    updated_at: dt.datetime


class SystemSettingListResponse(BaseModel):
    items: list[SystemSettingOut]


class SystemSettingPut(BaseModel):
    value: Any
    description: str | None = None
    is_secret: bool | None = None


class AuditChainReport(BaseModel):
    checked: int
    ok: bool
    problems: list[str]


class AuditEntryOut(BaseModel):
    """Запись журнала. `changes` уже маскированы на записи (раздел 1)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: dt.datetime
    actor_id: uuid.UUID | None = None
    actor_role: str | None = None
    impersonated_by: uuid.UUID | None = None
    action: str
    entity_type: str | None = None
    entity_id: uuid.UUID | None = None
    changes: dict[str, Any] | None = None
    result: str
    ip: str | None = None
    user_agent: str | None = None
    request_id: str | None = None
    prev_hash: str | None = None
    hash: str


class AuditListResponse(BaseModel):
    items: list[AuditEntryOut]
    next_cursor: str | None = None
