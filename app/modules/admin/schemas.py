"""Схемы административных системных ручек (раздел 6.12)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Маркер, которым списки и ответы заменяют значение секретной настройки.
SECRET_PLACEHOLDER = "********"


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


class FeatureFlagCreate(BaseModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{1,62}$", description="Код флага, snake_case")
    is_enabled: bool = False
    description: str | None = Field(default=None, max_length=1000)
    rollout: int = Field(default=100, ge=0, le=100)


class FeatureFlagPatch(BaseModel):
    is_enabled: bool | None = None
    description: str | None = None
    rollout: int | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def _required_fields_are_not_nulled(self) -> FeatureFlagPatch:
        # `null` в PATCH обязательного поля — не «оставить как есть»: в колонку NOT NULL он
        # доходил до БД и давал 500. Чтобы не менять поле, его просто не передают.
        for name in ("is_enabled", "rollout"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} не может быть null")
        return self


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

    @field_validator("value")
    @classmethod
    def _not_the_secret_placeholder(cls, value: Any) -> Any:
        # Форма, отправившая обратно то, что показал `GET`, затёрла бы
        # настоящий секрет самим маркером.
        if value == SECRET_PLACEHOLDER:
            raise ValueError(
                "Маркер «********» не является значением: передайте новое значение секрета"
            )
        return value


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
    #: Версия состава хэшируемых полей записи (1 — до расширения, 2 — с ролью, IP и UA).
    hash_version: int = 1


class AuditListResponse(BaseModel):
    items: list[AuditEntryOut]
    next_cursor: str | None = None
