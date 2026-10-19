"""Pydantic-схемы модуля интеграций (раздел 7.8, раздел 8)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.integration.field_mapping import FieldMapping
from app.modules.integration.routing import Routing


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
    # B5-2/B5-5: забор по API.
    pull_enabled: bool
    pull_interval_seconds: int
    pull_request: dict[str, Any] | None
    last_run: dict[str, Any] | None
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
    # B5-2: включатель и период забора по API (60..86400 с — как в CHECK колонки).
    pull_enabled: bool | None = None
    pull_interval_seconds: int | None = Field(default=None, ge=60, le=86400)


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
    # B5-2: вебхук (как раньше), забор по расписанию или кнопка «Забрать сейчас» (B5-5).
    channel: str
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

    # B9-2: пример строки — `tests/test_lms_progress.py::TestIncompleteRowsDoNotEraseData`
    # (`_FULL`/`_row`): полный набор полей одной строки прогресса.
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "external_id": "lms-push-2026-03-15-001",
                "items": [
                    {
                        "deal_id": "018f0a3e-7c3e-7c3e-8c3e-3e7c3e7c3e7c",
                        "course_external_id": "c-1",
                        "enrolled_at": "2026-09-01T10:00:00Z",
                        "progress_pct": 60,
                        "score": 4.5,
                        "completed_at": "2026-09-20T12:00:00Z",
                        "last_activity_at": "2026-09-21T09:00:00Z",
                    }
                ],
            }
        }
    )

    external_id: str = Field(description="Идентификатор доставки для дедупликации")
    items: list[dict[str, Any]] = Field(default_factory=list)


class BitrixWebhookRequest(BaseModel):
    # B9-2: пример — реальные значения из `tests/test_integration.py`
    # (`fields={"title": "x"}` на реальном сценарии переименования).
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "external_id": "bitrix-evt-2026-03-15-001",
                "bitrix_id": "42",
                "version": 2,
                "fields": {"title": "РТ Университет — Python: базовый курс"},
            }
        }
    )

    external_id: str = Field(description="Идентификатор доставки для дедупликации")
    bitrix_id: str
    version: int = Field(ge=1)
    fields: dict[str, Any] = Field(default_factory=dict)


# =============================================================================
# B5-3: настраиваемый маппинг «утверждённых полей»
# =============================================================================


class TargetOut(BaseModel):
    code: str
    label: str


class MappingGetResponse(BaseModel):
    """Текущий маппинг (`null` — администратор ничего не настраивал) и каталог целей для
    экрана настройки, чтобы форма не хардкодила список."""

    mapping: FieldMapping | None
    targets: list[TargetOut]


class RoutingGetResponse(BaseModel):
    """B5-4: текущая маршрутизация (`null` — не настроена, заявки идут в B2C-воронку по
    умолчанию, как и раньше)."""

    routing: Routing | None


class MappingValidationErrorOut(BaseModel):
    field: str
    reason: str


class MappingPreviewRequest(BaseModel):
    """Либо `record` (образец тела), либо `from_remote: true` (первая запись с источника —
    требует настроенный `CMS_BASE_URL`). `mapping` — черновик для предпросмотра ДО сохранения;
    без него используется уже сохранённый маппинг источника."""

    record: dict[str, Any] | None = None
    from_remote: bool = False
    mapping: FieldMapping | None = None

    @model_validator(mode="after")
    def _exactly_one_sample_source(self) -> MappingPreviewRequest:
        if (self.record is None) == (not self.from_remote):
            raise ValueError("нужен либо record, либо from_remote: true")
        return self


class MappingPreviewResponse(BaseModel):
    record: dict[str, Any]
    payload: dict[str, Any]
    ignored_keys: list[str] = Field(default_factory=list)
    product: dict[str, Any] | None = None
    errors: list[MappingValidationErrorOut] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# --- B5-5: ручной запуск «Забрать сейчас» и статус синхронизации ---------------------------


class PullRequestBody(BaseModel):
    """`updated_since=null` — читать с сохранённого курсора (обычный ручной запуск); дата в
    прошлом — подобрать заявки за период заново (например, упавшие), не откатывая курсор назад
    (`CmsPullService.run`/`sweep_lms_progress_pull`: итоговый курсор — максимум старого и
    вычисленного на этом прогоне)."""

    updated_since: str | None = None


class PullRequestResponse(BaseModel):
    status: str = "queued"
    requested_at: dt.datetime


class SourceCheckOut(BaseModel):
    """Итог проверки соединения с источником («Проверить сейчас»)."""

    code: str = Field(description="Код источника (`cms`, `lms`, `bitrix24`).")
    ok: bool = Field(description="Система ответила и приняла запрос.")
    message: str = Field(description="Что произошло, по-русски: «Отвечает» или причина отказа.")
    duration_ms: int = Field(description="Сколько заняла проверка, мс.")
    http_status: int | None = Field(
        default=None, description="HTTP-статус ответа системы; пусто, если ответа не было."
    )
    checked_at: dt.datetime = Field(description="Когда проверено (UTC).")


# =============================================================================
# B5-9: webhook-подписки
# =============================================================================


class WebhookSubscriptionCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    url: str = Field(min_length=1, max_length=1024)
    event_types: list[str] = Field(min_length=1, max_length=16)
    filters: dict[str, Any] = Field(default_factory=dict)


class WebhookSubscriptionUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    url: str | None = Field(default=None, min_length=1, max_length=1024)
    event_types: list[str] | None = Field(default=None, min_length=1, max_length=16)
    filters: dict[str, Any] | None = None
    is_active: bool | None = None


class WebhookSubscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    url: str
    event_types: list[str]
    filters: dict[str, Any]
    is_active: bool
    consecutive_failures: int
    disabled_reason: str | None
    created_at: dt.datetime
    updated_at: dt.datetime


class WebhookSubscriptionCreatedOut(WebhookSubscriptionOut):
    """Ответ на создание/ротацию секрета: `secret` показывается РОВНО ОДИН РАЗ."""

    secret: str


class WebhookDeliveryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    subscription_id: uuid.UUID
    event_type: str
    aggregate_type: str
    aggregate_id: uuid.UUID
    status: str
    attempts: int
    next_retry_at: dt.datetime | None
    last_error: str | None
    last_status_code: int | None
    created_at: dt.datetime
    sent_at: dt.datetime | None


class WebhookTestResultOut(BaseModel):
    status_code: int | None
    elapsed_ms: float
    error: str | None = None


# =============================================================================
# B5-10: API-ключи
# =============================================================================


class ApiKeyCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    user_id: uuid.UUID
    #: Пусто — `settings.api_key_default_ttl_days` (90 по умолчанию).
    expires_in_days: int | None = Field(default=None, ge=1, le=365)


class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    key_prefix: str
    user_id: uuid.UUID
    expires_at: dt.datetime
    last_used_at: dt.datetime | None
    revoked_at: dt.datetime | None
    created_at: dt.datetime


class ApiKeyCreatedOut(ApiKeyOut):
    """Ответ на создание: `key` показывается РОВНО ОДИН РАЗ."""

    key: str


class IntegrationStatusOut(BaseModel):
    """Сводка для экрана источника: `last_run` — снимок момента завершения последнего прогона
    (cron или ручного), `inbound_last_24h`/`outbox_last_24h` — живые счётчики по статусам за
    последние сутки (не из `last_run`)."""

    code: str
    is_active: bool
    pull_enabled: bool
    pull_interval_seconds: int
    cursors: dict[str, str | None]
    last_sync_at: dt.datetime | None
    last_error: str | None
    last_run: dict[str, Any] | None
    pull_request: dict[str, Any] | None
    inbound_last_24h: dict[str, int]
    outbox_last_24h: dict[str, int]
