"""Таблица аудита.

Раздел 5.10: `audit_log` партиционируется по месяцам, у роли приложения
только `INSERT` и `SELECT`. Каждая запись несёт `prev_hash` и `hash`, что
делает вырезание записи обнаружимым. `actor_id` намеренно без внешнего
ключа — запись должна переживать обезличивание пользователя.
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, Index, SmallInteger, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.ids import uuid7
from app.db.base import Base, IpAddressType


class AuditResult(StrEnum):
    SUCCESS = "success"
    DENIED = "denied"
    ERROR = "error"


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_actor_created", "actor_id", "created_at"),
        Index("ix_audit_log_entity", "entity_type", "entity_id", "created_at"),
        Index("ix_audit_log_action_created", "action", "created_at"),
        Index("ix_audit_log_request_id", "request_id"),
        # Отдельный индекс по времени: выборка журнала за период идёт без
        # других фильтров чаще всего.
        Index("ix_audit_log_created_at", "created_at"),
        # Партиционирование по месяцам объявляется в миграции.
        {"postgresql_partition_by": "RANGE (created_at)"},
    )

    # Ключ партиционирования обязан входить в первичный ключ.
    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid7)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), primary_key=True, server_default=func.now(), nullable=False
    )

    actor_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    actor_role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    impersonated_by: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)

    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    # Только изменённые поля в виде {"field": {"old": ..., "new": ...}}, уже маскированные.
    changes: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    result: Mapped[str] = mapped_column(String(16), nullable=False)

    ip: Mapped[str | None] = mapped_column(IpAddressType(), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # Версия состава хэшируемых полей (`audit.service.compute_hash`): 1 — исходный, 2 — ещё и
    # роль актора, подмена личности, IP и User-Agent, 3 — состав версии 2, но хэш — HMAC с
    # `AUDIT_HMAC_KEY`. Записи до расширения остаются версии 1 и проверяются по-старому:
    # пересчитывать их нельзя, таблица неизменяемая.
    hash_version: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("1")
    )


class AuditChainHead(Base):
    """Указатель на хэш последней записи цепочки — вынесен из `audit_log` отдельной

    синглтон-таблицей (перф-диагностика 27.09): `SELECT hash FROM audit_log ORDER BY
    created_at DESC, id DESC LIMIT 1` под advisory-локом цепочки планировался ~15-30мс —
    Postgres должен рассмотреть constraint exclusion по ВСЕМ месячным партициям на каждый
    вызов, а партиций со временем становится больше, а не меньше. Под нагрузкой (лок держит
    транзакцию целиком) это напрямую умножается на глубину очереди ожидающих. Эта таблица —
    не партиционирована и всегда одна строка: планирование и выполнение — доли миллисекунды
    независимо от размера `audit_log`.

    `id` — singleton-паттерн (`CHECK (id)`, миграция гарантирует ровно одну строку). Источник
    истины по-прежнему `audit_log.hash`: verify_chain() читает `audit_log` напрямую и никогда
    не смотрит на эту таблицу — она только ускоряет write-путь (`AuditService.record`), синхронно
    обновляется в той же транзакции, что и вставка, и откатывается вместе с ней."""

    __tablename__ = "audit_chain_head"

    id: Mapped[bool] = mapped_column(primary_key=True, default=True)
    hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
