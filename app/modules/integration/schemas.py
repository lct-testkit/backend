"""Pydantic-схемы модуля интеграций (раздел 7.8, раздел 8)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class IntegrationSourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    base_url: str | None
    auth_type: str | None
    # Имя переменной окружения — не секрет (см. `integration/security.py`),
    # полезно администратору видеть в `GET .../sources` при диагностике
    # «почему источник не работает».
    credentials_ref: str | None
    is_active: bool
    config: dict[str, Any]
    last_sync_at: dt.datetime | None
    last_error: str | None
    created_at: dt.datetime
    updated_at: dt.datetime


class IntegrationSourceUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    # Длины — как у колонок: длиннее значение давало ошибку БД (500), а не 422. Проверка самого
    # адреса (схема, учётные данные, внутренние сети) — в `IntegrationSourceService.update`.
    base_url: str | None = Field(default=None, max_length=512)
    auth_type: str | None = Field(default=None, max_length=16)
    # Имя переменной окружения, не значение и не произвольная строка.
    credentials_ref: str | None = Field(
        default=None, max_length=128, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"
    )
    is_active: bool | None = None
    config: dict[str, Any] | None = None


class ExternalRefOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    source_code: str
    external_id: str
    synced_version: int
    last_synced_at: dt.datetime | None
    sync_direction: str


class OutboxEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    aggregate_type: str
    aggregate_id: uuid.UUID
    event_type: str
    target: str | None
    status: str
    attempts: int
    next_retry_at: dt.datetime | None
    last_error: str | None
    created_at: dt.datetime
    sent_at: dt.datetime | None


class InboundMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_code: str
    external_id: str
    message_type: str | None
    status: str
    signature_valid: bool
    error: str | None
    resulting_entity_type: str | None
    resulting_entity_id: uuid.UUID | None
    processed_at: dt.datetime | None
    received_at: dt.datetime


class CmsLeadResponse(BaseModel):
    """Ответ `POST /api/v1/integrations/cms/leads`: чем закончилась доставка."""

    status: str = Field(
        description=(
            "`processed` — заявка принята и создала сделку; `duplicate` — такая заявка уже есть "
            "(открытая сделка того же человека и продукта либо заказ с тем же номером), новая "
            "сделка не заводилась; при повторе доставки с тем же `Idempotency-Key` возвращается "
            "статус первой обработки"
        )
    )
    inbound_message_id: uuid.UUID = Field(description="Запись в журнале входящих сообщений")
    deal_id: uuid.UUID | None = Field(default=None, description="Сделка по заявке")
    contact_id: uuid.UUID | None = Field(default=None, description="Контакт (клиент) сделки")


class LmsProgressPushRequest(BaseModel):
    """Тело `POST /api/v1/integrations/lms/progress` — раздел 8. Формат
    строк в `items` совпадает с тем, что `LmsClient.pull_progress()` читает
    из ответа `GET .../students/progress` (раздел 4.14) — один и тот же
    `lms.upsert_progress()` обрабатывает оба направления, см. докстринг
    `integration.lms`."""

    external_id: str = Field(description="Идентификатор доставки для дедупликации")
    items: list[dict[str, Any]] = Field(default_factory=list)


class BitrixWebhookRequest(BaseModel):
    external_id: str = Field(description="Идентификатор доставки для дедупликации")
    bitrix_id: str
    version: int = Field(ge=1)
    fields: dict[str, Any] = Field(default_factory=dict)
