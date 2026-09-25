"""Схемы модуля уведомлений (spec.txt §5.7/§6.11)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

ChannelLiteral = Literal["email", "telegram", "in_app"]
PriorityLiteral = Literal["normal", "high", "critical"]
DeliveryStatusLiteral = Literal["pending", "sent", "failed", "skipped"]


# =============================================================================
# Уведомления (GET /api/notifications, POST /api/notifications/read)
# =============================================================================


class NotificationDeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    channel: ChannelLiteral
    status: DeliveryStatusLiteral
    address_masked: str | None
    attempt: int
    error: str | None
    sent_at: dt.datetime | None


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    template_code: str
    entity_type: str | None
    entity_id: uuid.UUID | None
    payload: dict
    priority: PriorityLiteral
    is_read: bool
    read_at: dt.datetime | None
    created_at: dt.datetime
    # Отрендеренные по шаблону in_app subject/body (см. `service.render_for_display`).
    # `None`, если для кода события не заведён активный in_app-шаблон — тогда
    # фронт показывает `payload` как есть, а не пустую строку.
    subject: str | None = None
    body: str | None = None


class NotificationListResponse(BaseModel):
    items: list[NotificationOut]
    next_cursor: str | None = None


class NotificationReadRequest(BaseModel):
    """Массовая отметка прочитанности: по списку id, по фильтру, либо (пустое
    тело) — все непрочитанные получателя."""

    ids: list[uuid.UUID] | None = None
    priority: PriorityLiteral | None = None
    entity_type: str | None = None
    event_code: str | None = None


class NotificationReadResponse(BaseModel):
    updated: int


class UnreadCountResponse(BaseModel):
    """Число непрочитанных без потолка страницы — для значка в шапке."""

    count: int


# =============================================================================
# Настройки пользователя (/api/me/notification-prefs)
# =============================================================================


class NotificationPrefOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    event_code: str
    channels: list[ChannelLiteral]
    is_enabled: bool
    quiet_hours_start: dt.time | None
    quiet_hours_end: dt.time | None


class NotificationPrefListResponse(BaseModel):
    items: list[NotificationPrefOut]


class NotificationPrefUpsert(BaseModel):
    event_code: NonEmptyStr = Field(max_length=64)
    channels: list[ChannelLiteral] = Field(default_factory=list)
    is_enabled: bool = True
    quiet_hours_start: dt.time | None = None
    quiet_hours_end: dt.time | None = None


class NotificationPrefsUpdateRequest(BaseModel):
    prefs: list[NotificationPrefUpsert]


class EventCodeOut(BaseModel):
    """Код события, на который можно настроить `PUT /me/notification-prefs`, и каналы,
    по которым для него есть активные шаблоны."""

    code: str
    channels: list[ChannelLiteral]


class EventCodeListResponse(BaseModel):
    items: list[EventCodeOut]


# =============================================================================
# Администрирование шаблонов (/api/admin/notification-templates)
# =============================================================================


class NotificationTemplateCreateRequest(BaseModel):
    code: NonEmptyStr = Field(max_length=64)
    channel: ChannelLiteral
    subject_template: str | None = None
    body_template: NonEmptyStr
    locale: str = "ru"
    is_active: bool = True


class NotificationTemplateUpdateRequest(BaseModel):
    subject_template: str | None = None
    body_template: NonEmptyStr | None = None
    locale: str | None = None
    is_active: bool | None = None


class NotificationTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    channel: ChannelLiteral
    subject_template: str | None
    body_template: str
    locale: str
    is_active: bool
    version: int
    created_at: dt.datetime
    updated_at: dt.datetime


class NotificationTemplateListResponse(BaseModel):
    items: list[NotificationTemplateOut]
    next_cursor: str | None = None


class TemplatePreviewRequest(BaseModel):
    """Черновик шаблона и данные события, на которых его надо отрисовать."""

    subject_template: str | None = None
    body_template: NonEmptyStr
    payload: dict[str, Any] = Field(default_factory=dict)


class TemplatePreviewError(BaseModel):
    field: Literal["subject_template", "body_template"]
    message: str
    line: int | None = None


class TemplatePreviewResponse(BaseModel):
    """`ok=false` — это ответ, а не ошибка запроса: интерфейс показывает `error`
    рядом с полем, не дожидаясь сохранения."""

    ok: bool
    subject: str | None = None
    body: str | None = None
    # Переменные, которые шаблон берёт из данных события (заданные в самом
    # шаблоне — `{% set %}`, переменные циклов — не в счёт).
    variables: list[str] = Field(default_factory=list)
    error: TemplatePreviewError | None = None
