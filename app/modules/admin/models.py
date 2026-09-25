"""Системные таблицы: флаги, настройки, идемпотентные ключи.

Раздел 5.10 спецификации. Эти три таблицы нужны уже на каркасе, потому что
на них опираются идемпотентность запросов и переключение поведения без
переразвёртывания.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UuidPkMixin


class FeatureFlag(UuidPkMixin, TimestampMixin, Base):
    __tablename__ = "feature_flags"

    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    is_enabled: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Процент раскатки 0..100 для постепенного включения.
    rollout: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("100"))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)


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
    updated_by: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)


class AdminApproval(UuidPkMixin, TimestampMixin, Base):
    """Подтверждение операции вторым администратором (CRM-1902).

    Требуется для создания роли ADMIN и для обезличивания субъекта
    (new_spec §4.1 edge cases и §4.8.6). Ключ операции — хэш её параметров:
    подтверждается ровно то, что было запрошено, а не «что-нибудь похожее».
    """

    __tablename__ = "admin_approvals"
    __table_args__ = (
        Index("ix_admin_approvals_pending", "operation", "request_hash", "status"),
        CheckConstraint(
            "status IN ('pending','approved','rejected','expired','consumed')",
            name="approval_status_valid",
        ),
        CheckConstraint("requested_by <> approved_by", name="approval_four_eyes"),
    )

    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    requested_by: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"

    # Ключ хранится со скоупом актора (`{actor_id}:{key}`), поэтому длиннее
    # заголовка: заголовок ограничен 255 символами.
    key: Mapped[str] = mapped_column(String(320), primary_key=True)
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
