"""Базовый класс ORM и общие миксины.

Соглашения из раздела 2: первичные ключи — UUIDv7, все даты — `timestamptz`,
деньги — `numeric(14,2)`, soft delete через `deleted_at` с частичными
индексами, внешние ключи по умолчанию `ON DELETE RESTRICT`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated, Any

from sqlalchemy import BigInteger, DateTime, MetaData, Numeric, String, TypeDecorator, func, text
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.ids import uuid7


class IpAddressType(TypeDecorator):
    """`INET` на стороне Postgres, обычная строка на стороне Python.

    `asyncpg` (в отличие от `psycopg2`) декодирует `inet` в
    `ipaddress.IPv4Address`/`IPv6Address`, а не `str` — Pydantic-схемы вида
    `ip: str | None` (аудит, `security_events`, `consents`) падали с
    `Input should be a valid string` на первой же записи с непустым IP.
    Один тип-обёртка чинит это для всех колонок сразу, а не патчит каждую
    выходную схему по отдельности.
    """

    impl = INET
    cache_ok = True

    def process_bind_param(self, value: object, dialect: object) -> str | None:
        return str(value) if value is not None else None

    def process_result_value(self, value: object, dialect: object) -> str | None:
        return str(value) if value is not None else None


# Явные имена ограничений: иначе Alembic генерирует нестабильные автогенераты.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# --- Переиспользуемые типы -----------------------------------------------

UuidPk = Annotated[
    uuid.UUID,
    mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid7),
]
UuidFk = Annotated[uuid.UUID, mapped_column(PgUUID(as_uuid=True))]
Timestamp = Annotated[dt.datetime, mapped_column(DateTime(timezone=True))]
Money = Annotated[Decimal, mapped_column(Numeric(14, 2))]
Json = Annotated[dict[str, Any], mapped_column(JSONB)]
Str32 = Annotated[str, mapped_column(String(32))]
Str64 = Annotated[str, mapped_column(String(64))]
Str255 = Annotated[str, mapped_column(String(255))]
Str512 = Annotated[str, mapped_column(String(512))]


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    # Значения, вычисляемые на стороне БД (`server_default`, `onupdate`),
    # SQLAlchemy забирает через RETURNING сразу при flush. Без этого после
    # UPDATE атрибут помечается как expired, а ленивая подгрузка в асинхронном
    # сеансе падает с MissingGreenlet — ручка возвращала бы 500 при попытке
    # прочитать `updated_at` сразу после изменения.
    __mapper_args__ = {"eager_defaults": True}

    type_annotation_map = {
        uuid.UUID: PgUUID(as_uuid=True),
        dt.datetime: DateTime(timezone=True),
        Decimal: Numeric(14, 2),
        dict[str, Any]: JSONB,
        int: BigInteger,
    }

    def __repr__(self) -> str:
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk}>"


class UuidPkMixin:
    """UUIDv7 как первичный ключ. Автоинкременты наружу запрещены."""

    id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid7
    )


class TimestampMixin:
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class SoftDeleteMixin:
    """Мягкое удаление. Уникальность проверяется частичным индексом
    `WHERE deleted_at IS NULL`, иначе удалённая запись блокирует создание новой."""

    deleted_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None


class VersionMixin:
    """Оптимистичная блокировка через `If-Match` / `version` (раздел 2)."""

    version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("1")
    )
