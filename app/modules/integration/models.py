"""Модели интеграций (new_spec §4.14, §7.8).

До этого спринта в модуле жил только протокол `OutboxService` и его
логирующая заглушка (см. докстринг `integration.service`) — сама таблица
`outbox_events` не существовала, хотя `crm.service._run_actions` уже вызывал
`get_outbox_service().publish(...)` для действия DSL `integration_event`
(раздел 8, шаги `lms_transfer`/`training_launch` посевных воронок) и
`signing.service` — при подписании документа. Этот спринт заводит все шесть
таблиц раздела 7.8 и подключает к ним реализацию.

Шесть таблиц:

* `integration_sources` — реестр подключённых внешних систем (`cms`, `lms`,
  `bitrix24`). `credentials_ref` — это **имя переменной окружения**, а не сам
  секрет (раздел 7.8: «ссылка на секрет, НЕ сам секрет») — тот же принцип,
  что `signature_server_secret` уже применяет к `SecretStr` в `config.py`,
  только на уровне БД вместо кода.
* `inbound_messages` — сырое тело любого входящего вызова (вебхук CMS,
  вебхук LMS/push-вариант, вебхук Bitrix) до разбора. `UNIQUE(source_code,
  external_id)` — дедупликация повторной доставки на уровне БД, тот же приём,
  что `signature_otp_codes`/`import_row_results` уже используют для похожих
  «не обработать дважды» случаев. Два поля добавлены сверх терпкого перечня
  раздела 7.8 (по аналогии с `Signature.key_version` в спринте 6 и
  `ImportRowResult.row_data` в спринте 5 — оба документированы в тех
  докстрингах тем же приёмом): `resulting_entity_type`/`resulting_entity_id` —
  без них статус `duplicate` (раздел 7.8 перечисляет его в CHECK, но не
  объясняет, чем он отличается от отброшенной по `UNIQUE` повторной
  доставки) не на что было бы сослаться — сюда попадает сделка/контакт,
  распознанные как уже существующие по бизнес-ключу (телефон/ИНН), а не
  просто второй `external_id`.
* `outbox_events` — исходящие события, раздел 3.6: «в одной транзакции с
  бизнес-изменением пишем строку в outbox_events». Индекс `(status,
  next_retry_at) WHERE status IN ('pending','failed')` — буквально из
  раздела 7.8, на нём построен `integration.tasks.sweep_outbox_events`.
* `external_refs` — двусторонняя таблица соответствия для Bitrix
  (`our_id ↔ bitrix_id`), раздел 4.14. Оба уникальных индекса из раздела 7.8
  сохранены дословно: `(source_code, entity_type, external_id)` защищает от
  двух наших сущностей, ссылающихся на одну и ту же внешнюю запись;
  `(source_code, entity_type, entity_id)` — от одной нашей сущности с двумя
  внешними id одновременно.
* `sync_cursors` — курсор пагинации для pull-интеграций (LMS). `UNIQUE
  (source_code, resource)` не назван в разделе 7.8 явно, но без него «upsert
  курсора» не определён — без уникального индекса конкурентный тик двух
  воркеров породил бы два курсора для одного и того же ресурса.
* `learning_progress` — данные из LMS. `UNIQUE(deal_id, external_course_id)`
  как цель `ON CONFLICT`: зачисление всегда порождается конкретной сделкой
  (`LEARNING_ENROLLMENT_SENT` шлётся из перехода по конкретной сделке), не
  голым контактом — `contact_id` денормализован от сделки для быстрых
  фильтров отчётности, а не независимый ключ сопоставления.
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UuidPkMixin


class IntegrationSourceCode(StrEnum):
    CMS = "cms"
    LMS = "lms"
    BITRIX24 = "bitrix24"


class InboundStatus(StrEnum):
    RECEIVED = "received"
    PROCESSED = "processed"
    FAILED = "failed"
    DUPLICATE = "duplicate"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    DEAD = "dead"


class SyncDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class IntegrationSource(UuidPkMixin, TimestampMixin, Base):
    __tablename__ = "integration_sources"
    __table_args__ = (
        UniqueConstraint("code", name="uq_integration_sources_code"),
        CheckConstraint("code IN ('cms','lms','bitrix24')", name="integration_sources_code_valid"),
    )

    code: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    auth_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # Имя переменной окружения, не значение — см. докстринг модуля.
    credentials_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    last_sync_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class InboundMessage(UuidPkMixin, Base):
    __tablename__ = "inbound_messages"
    __table_args__ = (
        UniqueConstraint("source_code", "external_id", name="uq_inbound_messages_source_external"),
        Index("ix_inbound_messages_received_at", "received_at"),
        CheckConstraint(
            "status IN ('received','processed','failed','duplicate')",
            name="inbound_messages_status_valid",
        ),
    )

    source_code: Mapped[str] = mapped_column(String(16), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    message_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    signature_valid: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'received'")
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # См. докстринг модуля: не из раздела 7.8, нужно для статуса `duplicate`
    # и для трассировки «какая сделка/контакт родились из этого сообщения».
    resulting_entity_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resulting_entity_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    processed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class OutboxEvent(UuidPkMixin, Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        Index(
            "ix_outbox_events_pending_retry",
            "status",
            "next_retry_at",
            postgresql_where=text("status IN ('pending','failed')"),
        ),
        Index("ix_outbox_events_aggregate", "aggregate_type", "aggregate_id"),
        CheckConstraint(
            "status IN ('pending','sent','failed','dead')", name="outbox_events_status_valid"
        ),
    )

    aggregate_type: Mapped[str] = mapped_column(String(32), nullable=False)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # Свободный текст, не CHECK: раздел 7.8 задаёт колонку как `text` без
    # перечисления значений. Диспетчер `integration.tasks` понимает 'bitrix'
    # и 'lms' — остальное честно стареет до `dead` с `last_error=
    # 'unknown_target'`, тем же приёмом, что `notification` уже применяет к
    # неизвестным каналам.
    target: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    next_retry_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False, index=True
    )
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExternalRef(UuidPkMixin, Base):
    __tablename__ = "external_refs"
    __table_args__ = (
        UniqueConstraint(
            "source_code",
            "entity_type",
            "external_id",
            name="uq_external_refs_source_entity_external",
        ),
        UniqueConstraint(
            "source_code",
            "entity_type",
            "entity_id",
            name="uq_external_refs_source_entity_our",
        ),
        CheckConstraint(
            "sync_direction IN ('inbound','outbound')", name="external_refs_sync_direction_valid"
        ),
    )

    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    source_code: Mapped[str] = mapped_column(String(16), nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    synced_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    last_synced_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sync_direction: Mapped[str] = mapped_column(String(8), nullable=False)


class SyncCursor(UuidPkMixin, Base):
    __tablename__ = "sync_cursors"
    __table_args__ = (
        UniqueConstraint("source_code", "resource", name="uq_sync_cursors_source_resource"),
    )

    source_code: Mapped[str] = mapped_column(String(16), nullable=False)
    resource: Mapped[str] = mapped_column(String(64), nullable=False)
    cursor_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        onupdate=text("now()"),
        nullable=False,
    )


class LearningProgress(UuidPkMixin, Base):
    __tablename__ = "learning_progress"
    __table_args__ = (
        UniqueConstraint("deal_id", "external_course_id", name="uq_learning_progress_deal_course"),
        Index("ix_learning_progress_contact", "contact_id"),
        CheckConstraint(
            "progress_pct IS NULL OR (progress_pct >= 0 AND progress_pct <= 100)",
            name="learning_progress_pct_range",
        ),
    )

    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="RESTRICT"), nullable=True
    )
    deal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("deals.id", ondelete="RESTRICT"), nullable=True
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=True
    )
    external_course_id: Mapped[str] = mapped_column(String(255), nullable=False)
    enrolled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    progress_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score: Mapped[Any | None] = mapped_column(Numeric(5, 2), nullable=True)
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_activity_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    raw: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    synced_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
