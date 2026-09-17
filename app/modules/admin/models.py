"""Системные таблицы: флаги, настройки, идемпотентные ключи.

Раздел 5.10 спецификации. Эти три таблицы нужны уже на каркасе, потому что
на них опираются идемпотентность запросов и переключение поведения без
переразвёртывания.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import DateTime, Integer, SmallInteger, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UuidPkMixin


class FeatureFlag(UuidPkMixin, TimestampMixin, Base):
    __tablename__ = "feature_flags"

    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    is_enabled: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("false")
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Процент раскатки 0..100 для постепенного включения.
    rollout: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("100")
    )
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )


class SystemSetting(TimestampMixin, Base):
    __tablename__ = "system_settings"

    # Ключ и есть первичный ключ: настройка адресуется по имени.
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Секретные значения не отдаются наружу в открытом виде (раздел 6.12).
    is_secret: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    # Хэш тела: тот же ключ с другим телом — это CRM-1003, а не повтор.
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request_method: Mapped[str] = mapped_column(String(10), nullable=False)
    request_path: Mapped[str] = mapped_column(String(512), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
