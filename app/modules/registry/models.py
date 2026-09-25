"""Локальный реестр ЕГРЮЛ и журнал автоподстановки (dop.md §11.9, раздел 5.11).

Три вещи стоит понимать перед тем, как трогать этот файл:

* **`egrul_entries.inn` — первичный ключ, не суррогатный UUID.** ИНН уже
  уникален по построению (dop.md §11.6: «ИНН — естественный ключ») и это
  единственный способ, которым `upsert` при повторном импорте выгрузки
  тривиален: `ON CONFLICT (inn) DO UPDATE`, без промежуточного поиска.
* **`is_educational` — обычная колонка, не `GENERATED ALWAYS AS`,** хотя
  здесь (в отличие от `organizations.search_vector`, которому Postgres
  запрещает `GENERATED` из-за неimmutable `to_tsvector`) это было бы
  технически возможно. Выбор сознательный, не вынужденный: признак нужен
  уже во время самого разбора выгрузки (`registry.egrul_xml` фильтрует
  необразовательные записи *до* вставки, раздел 11.3 п.4 — «реестр
  сжимается с миллионов до тысяч записей»), поэтому вычислять его второй
  раз выражением в БД после вставки было бы лишней сущностью.
* **`registry_versions.checksum` — sha256 самого XML-файла**, не контрольная
  сумма данных внутри: dop.md §11.3 «Версионирование» нужно для ответа на
  вопрос «на какую дату мы знали эти реквизиты», а не для проверки
  целостности парсинга (это делает подсчёт `entries_count`).

Реальная схема выгрузки ФНС (открытые данные ЕГРЮЛ) не была доступна на
момент разработки — `registry.egrul_xml` разбирает распространённую
публичную схему (`СвЮЛ`/`СвНаимЮЛ`/`СвОКВЭД`/...) практически с fallback
по альтернативным именам атрибутов; при получении реального дампа ФНС
имена тегов стоит сверить и поправить в одном месте (`egrul_xml.py`), сама
модель данных и стриминговый пайплайн (батч 5000, версия реестра,
проекция полей) от конкретных имён тегов не зависят.
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UuidPkMixin


class RegistrySource(StrEnum):
    FNS_EGRUL = "fns_egrul"
    ROSOBRNADZOR = "rosobrnadzor"
    MANUAL = "manual"


class RegistryImportStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class EgrulStatus(StrEnum):
    ACTIVE = "active"
    REORGANIZING = "reorganizing"
    LIQUIDATING = "liquidating"
    LIQUIDATED = "liquidated"
    INVALID = "invalid"


# ОКВЭД образовательных организаций (dop.md §11.3, п.4): 85.21/85.22/85.42 и
# смежные — «высшее образование» и соседние коды того же класса 85.
_EDUCATIONAL_OKVED_PREFIXES: tuple[str, ...] = ("85",)


def is_educational_okved(main: str | None, extra: list[str] | None) -> bool:
    codes = [main, *(extra or [])]
    return any(
        code and code.replace(".", "").startswith(_EDUCATIONAL_OKVED_PREFIXES) for code in codes
    )


class RegistryVersion(UuidPkMixin, TimestampMixin, Base):
    """Версия загруженной выгрузки (dop.md §11.9). Ссылка на `files.id` —
    логическая (`files` не импортирует `registry`, а тут — файл выгрузки),
    без FK по тому же приёму, что `Organization.import_job_id` до этого
    спринта: `files` не должен знать о существовании реестра."""

    __tablename__ = "registry_versions"
    __table_args__ = (
        CheckConstraint(
            "source IN ('fns_egrul','rosobrnadzor','manual')",
            name="registry_versions_source_valid",
        ),
        CheckConstraint(
            "status IN ('pending','running','completed','failed')",
            name="registry_versions_status_valid",
        ),
    )

    source: Mapped[str] = mapped_column(String(32), nullable=False)
    file_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'pending'")
    )
    published_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    imported_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    entries_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    imported_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class EgrulEntry(TimestampMixin, Base):
    """Локальный реестр (dop.md §11.9). `director_name` — ПДн руководителя
    (§11.8, ст.8 152-ФЗ «общедоступные источники»): маскируется в логах
    (`registry.service._mask_query` для `org_lookup_log`) и в ответе
    `GET /api/org-lookup/inn/{inn}` (`registry.schemas.OrgDetailsOut.
    from_details` зовёт `mask_name`), участвует в перечне ПДн раздела 4.8.
    Колонка при этом хранится открытым текстом, не шифруется на уровне
    БД — то же осознанное упрощение, что и у контактов (см. docstring
    `catalog.models`: ключа шифрования и его ротации в проекте ещё нет)."""

    __tablename__ = "egrul_entries"
    __table_args__ = (
        Index("ix_egrul_entries_search_vector", "search_vector", postgresql_using="gin"),
        Index(
            "ix_egrul_entries_short_name_trgm",
            "short_name",
            postgresql_using="gin",
            postgresql_ops={"short_name": "gin_trgm_ops"},
        ),
        Index("ix_egrul_entries_status", "status"),
        Index("ix_egrul_entries_region_code", "region_code"),
        Index(
            "ix_egrul_entries_educational",
            "is_educational",
            postgresql_where=text("is_educational"),
        ),
        CheckConstraint(
            "status IN ('active','reorganizing','liquidating','liquidated','invalid')",
            name="egrul_entries_status_valid",
        ),
    )

    inn: Mapped[str] = mapped_column(String(12), primary_key=True)
    ogrn: Mapped[str | None] = mapped_column(String(15), nullable=True)
    kpp: Mapped[str | None] = mapped_column(String(9), nullable=True)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    short_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    opf_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    opf_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    registration_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    termination_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    region_code: Mapped[str | None] = mapped_column(String(4), nullable=True)
    legal_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    address_parts: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    okved_main: Mapped[str | None] = mapped_column(String(16), nullable=True)
    okved_extra: Mapped[list[str] | None] = mapped_column(ARRAY(String(16)), nullable=True)
    director_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    director_position: Mapped[str | None] = mapped_column(String(255), nullable=True)
    capital: Mapped[Any | None] = mapped_column(Numeric(18, 2), nullable=True)
    is_educational: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    registry_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("registry_versions.id", ondelete="SET NULL"), nullable=True
    )
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)


class UniversityRegistry(Base):
    """Реестр вузов Рособрнадзора (лицензии/аккредитация, dop.md §11.3/§11.9).
    Связывается с `egrul_entries` по ИНН, отдельной таблицы-справочника
    Рособрнадзора нет — источник тот же провайдерный слой (`RegistrySource`),
    просто другой `source`.

    dop.md прямо помечает этот реестр как «дополнительно», без описанного
    формата выгрузки — в отличие от ЕГРЮЛ, для которого §11.3 расписывает
    XML-схему и пайплайн. Таблица и связь по ИНН существуют (провайдеры уже
    читают её в `get_by_inn` ради `is_accredited`/`accreditation_until`), но
    ни один импортёр её не наполняет: `registry.tasks._process_version`
    обрабатывает только `source='fns_egrul'` и явно отклоняет остальные (см.
    его docstring). Пока это не сделано, аккредитация в ответах org-lookup
    всегда `None` — честная незаполненность, а не скрытый баг."""

    __tablename__ = "university_registry"

    inn: Mapped[str] = mapped_column(String(12), primary_key=True)
    license_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    license_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    accreditation_until: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    founder_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    forms: Mapped[list[str] | None] = mapped_column(ARRAY(String(32)), nullable=True)
    directions_codes: Mapped[list[str] | None] = mapped_column(ARRAY(String(32)), nullable=True)
    students_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    campus_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    registry_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("registry_versions.id", ondelete="SET NULL"), nullable=True
    )
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        server_default=text("now()"), onupdate=text("now()"), nullable=False
    )


class OrgLookupLog(UuidPkMixin, Base):
    """Журнал автоподстановки (dop.md §11.9): rate-limit по IP/пользователю
    и метрика доли организаций, заведённых через автоподстановку.
    `query_masked` — уже замаскированная строка на момент записи
    (`app.core.masking.mask_inn`/частичная маска текста), не сырой ввод."""

    __tablename__ = "org_lookup_log"
    __table_args__ = (Index("ix_org_lookup_log_user_created", "user_id", "created_at"),)

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    query_masked: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    matched_inn: Mapped[str | None] = mapped_column(String(12), nullable=True)
    response_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    created_at: Mapped[dt.datetime] = mapped_column(server_default=text("now()"), nullable=False)
