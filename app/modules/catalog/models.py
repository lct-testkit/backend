"""Модели каталога: организации, контакты, продукты, справочники (раздел 5.2/5.3).

Три вещи стоит понимать перед тем, как трогать этот файл:

* **ПДн контактов не шифруются на уровне колонки.** Раздел 5.2 говорит
  «чувствительные поля контактов шифруются», но в кодовой базе ещё нет ни
  ключа шифрования в `app/core/config.py`, ни истории его ротации, а
  `users.phone`/`users.email` (раздел 5.1, идентичная по чувствительности
  ПДн) уже хранятся открытым текстом и полагаются на маскирование на границе
  вывода (`app/core/masking.py`). Здесь тот же приём: колонки открытые,
  `ContactOut`/аудит/логи всегда маскируют, полные значения отдаёт только
  `POST /api/contacts/{id}/reveal` под своим правом и с отдельной записью
  аудита `PII_REVEALED`. Осознанное упрощение, не забытое требование.
* **`Organization.registry_version_id`, `import_job_id` — реальные FK со
  спринта 5.** До него `registry_versions`/`import_jobs` не существовали
  (тот же приём, что `Deal.organization_id` использовал в спринте 3 для ещё
  не существовавшего каталога), значения были «голым» UUID. Миграция 0006
  добавляет ограничения, ничего в данных менять не нужно. `ON DELETE
  SET NULL`, а не `RESTRICT`: у обеих таблиц нет ручки удаления, но если она
  появится, потеря версии реестра/задания импорта не должна тащить за собой
  запрет удалить организацию.
* **Дедупликация по ИНН — только частичный уникальный индекс.** Раздел 5.2:
  `UNIQUE(inn) WHERE inn IS NOT NULL AND deleted_at IS NULL`. Проверка
  контрольной суммы ИНН и поиск дублей до записи — в `catalog.service`, это
  лишь последний рубеж на уровне БД (раздел 3.2).

Полнотекстовый поиск по организациям (`search_vector`) заводится не через
`Computed(...)`: `to_tsvector('russian', text)` — не `IMMUTABLE`, Postgres
отказывается объявлять такую генерируемую колонку. Стандартный обход —
встроенный (без расширений) `tsvector_update_trigger`, он заводится в
`migrations/versions/0005_catalog_sprint.py`; колонка здесь — просто
`TSVECTOR`, которую наполняет триггер, а не приложение.
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
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Money, SoftDeleteMixin, TimestampMixin, UuidPkMixin, VersionMixin


class OrgType(StrEnum):
    UNIVERSITY = "university"
    COLLEGE = "college"
    COMPANY = "company"
    INDIVIDUAL_ENTREPRENEUR = "individual_entrepreneur"


class RegistryStatus(StrEnum):
    """Дублирует `egrul_entries.status` (раздел 5.11) для быстрого фильтра —
    ЕГРЮЛ-провайдер спринта 5 будет писать сюда, здесь только форма поля."""

    ACTIVE = "active"
    REORGANIZING = "reorganizing"
    LIQUIDATING = "liquidating"
    LIQUIDATED = "liquidated"
    INVALID = "invalid"


class ContactChannelType(StrEnum):
    TELEGRAM = "telegram"
    WHATSAPP = "whatsapp"
    PHONE_EXTRA = "phone_extra"
    EMAIL_EXTRA = "email_extra"


class ProductFormat(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    BLENDED = "blended"


class LossReasonCategory(StrEnum):
    PRICE = "price"
    TIMING = "timing"
    COMPETITOR = "competitor"
    NO_NEED = "no_need"
    NO_BUDGET = "no_budget"
    NO_CONTACT = "no_contact"
    OTHER = "other"


class CustomFieldEntityType(StrEnum):
    DEAL = "deal"
    ORGANIZATION = "organization"
    CONTACT = "contact"
    PRODUCT = "product"


class CustomFieldType(StrEnum):
    STRING = "string"
    NUMBER = "number"
    DATE = "date"
    BOOL = "bool"
    SELECT = "select"
    MULTISELECT = "multiselect"
    FILE = "file"


class Region(UuidPkMixin, TimestampMixin, Base):
    """Справочник субъектов РФ (раздел 5.2)."""

    __tablename__ = "regions"
    __table_args__ = (UniqueConstraint("code", name="uq_regions_code"),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    federal_district: Mapped[str | None] = mapped_column(String(64), nullable=True)
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    code: Mapped[str] = mapped_column(String(16), nullable=False)


class Organization(UuidPkMixin, TimestampMixin, VersionMixin, SoftDeleteMixin, Base):
    __tablename__ = "organizations"
    __table_args__ = (
        Index(
            "uq_organizations_inn_active",
            "inn",
            unique=True,
            postgresql_where=text("inn IS NOT NULL AND deleted_at IS NULL"),
        ),
        Index("ix_organizations_owner", "owner_id"),
        Index("ix_organizations_region", "region_id"),
        Index("ix_organizations_search_vector", "search_vector", postgresql_using="gin"),
        CheckConstraint(
            "org_type IN ('university','college','company','individual_entrepreneur')",
            name="organizations_org_type_valid",
        ),
        CheckConstraint(
            "registry_status IS NULL OR registry_status IN "
            "('active','reorganizing','liquidating','liquidated','invalid')",
            name="organizations_registry_status_valid",
        ),
    )

    name: Mapped[str] = mapped_column(String(512), nullable=False)
    short_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    org_type: Mapped[str] = mapped_column(String(32), nullable=False)
    inn: Mapped[str | None] = mapped_column(String(12), nullable=True)
    kpp: Mapped[str | None] = mapped_column(String(9), nullable=True)
    ogrn: Mapped[str | None] = mapped_column(String(15), nullable=True)
    legal_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    actual_address: Mapped[str | None] = mapped_column(Text, nullable=True)
    region_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("regions.id", ondelete="RESTRICT"), nullable=True
    )
    website: Mapped[str | None] = mapped_column(String(255), nullable=True)
    main_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    main_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    students_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    external_ids: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    import_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("import_jobs.id", ondelete="SET NULL"), nullable=True
    )
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    custom_fields: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    # --- Поля из dop.md §11.9: автоподстановка/сверка по ЕГРЮЛ ---
    verified_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    verified_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    registry_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("registry_versions.id", ondelete="SET NULL"), nullable=True
    )
    registry_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    registry_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    registry_checked_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    requisites_drift: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    manual_overrides: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    is_accredited: Mapped[bool | None] = mapped_column(nullable=True)
    accreditation_until: Mapped[dt.date | None] = mapped_column(Date, nullable=True)


class OrganizationBranch(UuidPkMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Филиалы и институты внутри вуза (раздел 5.2)."""

    __tablename__ = "organization_branches"
    __table_args__ = (Index("ix_organization_branches_org", "organization_id"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organization_branches.id", ondelete="RESTRICT"), nullable=True
    )


class Contact(UuidPkMixin, TimestampMixin, VersionMixin, SoftDeleteMixin, Base):
    """Физлицо: представитель вуза (organization_id задан) или B2C (пусто).

    Шифрование ПДн — осознанно не реализовано в этом спринте, см. docstring
    модуля. Маскирование — на границе вывода (`ContactOut`).
    """

    __tablename__ = "contacts"
    __table_args__ = (
        Index("ix_contacts_organization", "organization_id"),
        Index("ix_contacts_email", "email"),
        Index("ix_contacts_phone", "phone"),
        Index("ix_contacts_created_by", "created_by"),
    )

    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True
    )
    first_name: Mapped[str] = mapped_column(String(128), nullable=False)
    last_name: Mapped[str] = mapped_column(String(128), nullable=False)
    middle_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    position: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_decision_maker: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    is_anonymized: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    anonymized_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    # `consents` существует с спринта 1 (identity) — реальный FK, не заглушка.
    consent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("consents.id", ondelete="RESTRICT"), nullable=True
    )
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    external_ids: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # Кто завёл контакт. Без этого контакт без организации и сделки (обычный случай при ручном
    # вводе) исчезал у создателя сразу после `201`: скоуп контактов строится через организации и
    # сделки, а у только что созданного их ещё нет.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    # «Способ связи» из вендорского каталога: `email` | `phone` | `telegram` | `whatsapp`.
    contact_methods: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )


class ContactChannel(UuidPkMixin, Base):
    __tablename__ = "contact_channels"
    __table_args__ = (
        Index("ix_contact_channels_contact", "contact_id"),
        CheckConstraint(
            "type IN ('telegram','whatsapp','phone_extra','email_extra')",
            name="contact_channels_type_valid",
        ),
    )

    contact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    is_primary: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    is_verified: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    created_at: Mapped[dt.datetime] = mapped_column(server_default=text("now()"), nullable=False)


class Direction(UuidPkMixin, TimestampMixin, VersionMixin, SoftDeleteMixin, Base):
    """ИТ-направление (раздел 5.3). Иерархия через `parent_id`."""

    __tablename__ = "directions"
    __table_args__ = (UniqueConstraint("code", name="uq_directions_code"),)

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("directions.id", ondelete="RESTRICT"), nullable=True
    )


class Product(UuidPkMixin, TimestampMixin, VersionMixin, SoftDeleteMixin, Base):
    """Образовательный продукт: курс, программа, трек (раздел 5.3, 19)."""

    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("code", name="uq_products_code"),
        Index("ix_products_direction", "direction_id"),
        Index("ix_products_vendor", "vendor_id"),
        CheckConstraint(
            "format IS NULL OR format IN ('online','offline','blended')",
            name="products_format_valid",
        ),
    )

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    direction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("directions.id", ondelete="RESTRICT"), nullable=True
    )
    duration_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    format: Mapped[str | None] = mapped_column(String(16), nullable=True)
    base_price: Mapped[Money | None] = mapped_column(Numeric(14, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="RUB")
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    valid_from: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    import_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("import_jobs.id", ondelete="SET NULL"), nullable=True
    )
    # Вендор продукта — обычная организация (`org_type='company'`): каталог «Вендоры» приходит
    # файлом «Компания / Продукт / ФИО / Телефон / Почта / Способ связи», и без ссылки на
    # компанию из него терялась бы половина строки. Отдельной таблицы `vendors` нет: у вендора те
    # же реквизиты, что у любой организации, а ответственные люди — обычные контакты
    # (`ContactProduct`).
    vendor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="SET NULL"), nullable=True
    )
    # Раздел 19: «ПО», признак программы (`product_kind`) — сюда, без отдельной таблицы
    # `it_programs`.
    custom_fields: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class ContactProduct(Base):
    """Ответственный контакт продукта: у каждого продукта вендора есть человек, к которому идут по
    вопросам лицензий и поставки («Вендоры.xlsx»). Один человек может отвечать за несколько
    продуктов и наоборот."""

    __tablename__ = "contact_products"
    __table_args__ = (Index("ix_contact_products_product", "product_id"),)

    contact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"), primary_key=True
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(24), nullable=False, server_default="responsible")
    created_at: Mapped[dt.datetime] = mapped_column(server_default=text("now()"), nullable=False)


class ContactLearnerProfile(Base):
    """ПДн учащегося для шаблона LMS «Загрузка пользователей» (см. `catalog.learner`).

    Отдельной таблицей, а не колонками `contacts`: СНИЛС, паспорт и адрес регистрации нужны только
    для выгрузки в LMS и не должны попадать ни в `ContactOut`, ни в поиск, ни в аудит. Читается
    через `GET /contacts/{id}/learner-profile` (маскированно) и `.../reveal` (с записью аудита).
    Значения в БД уже нормализованы: СНИЛС — 11 цифр, серия — 4, номер — 6, код подразделения —
    `NNN-NNN`, пол — `M`/`F`, образование — код из `catalog.learner.EDUCATION_LEVELS`.
    """

    __tablename__ = "contact_learner_profiles"
    __table_args__ = (
        CheckConstraint(
            "sex IS NULL OR sex IN ('M','F')", name="contact_learner_profiles_sex_valid"
        ),
        CheckConstraint(
            "education IS NULL OR education IN ('none','basic_general','secondary_general',"
            "'secondary_vocational','higher_bachelor','higher_specialist_master',"
            "'higher_top_qualification')",
            name="contact_learner_profiles_education_valid",
        ),
    )

    contact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"), primary_key=True
    )
    snils: Mapped[str | None] = mapped_column(String(14), nullable=True)
    passport_series: Mapped[str | None] = mapped_column(String(8), nullable=True)
    passport_number: Mapped[str | None] = mapped_column(String(16), nullable=True)
    passport_issued_by: Mapped[str | None] = mapped_column(String(512), nullable=True)
    passport_issued_at: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    passport_dept_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    sex: Mapped[str | None] = mapped_column(String(1), nullable=True)
    birth_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    reg_region: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reg_city: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reg_street: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reg_house: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reg_apartment: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reg_zip: Mapped[str | None] = mapped_column(String(16), nullable=True)
    first_name_dative: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_name_dative: Mapped[str | None] = mapped_column(String(128), nullable=True)
    middle_name_dative: Mapped[str | None] = mapped_column(String(128), nullable=True)
    education: Mapped[str | None] = mapped_column(String(32), nullable=True)
    diploma_profession: Mapped[str | None] = mapped_column(String(255), nullable=True)
    diploma_institution: Mapped[str | None] = mapped_column(String(512), nullable=True)
    diploma_surname: Mapped[str | None] = mapped_column(String(128), nullable=True)
    diploma_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    diploma_series: Mapped[str | None] = mapped_column(String(64), nullable=True)
    diploma_reg_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    diploma_issued_at: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(server_default=text("now()"), nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        server_default=text("now()"), onupdate=func.now(), nullable=False
    )


class LossReason(UuidPkMixin, TimestampMixin, VersionMixin, Base):
    """Причина отказа (раздел 5.3). Деактивируется, не удаляется."""

    __tablename__ = "loss_reasons"
    __table_args__ = (
        UniqueConstraint("code", name="uq_loss_reasons_code"),
        CheckConstraint(
            "category IN ('price','timing','competitor','no_need','no_budget',"
            "'no_contact','other')",
            name="loss_reasons_category_valid",
        ),
    )

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(16), nullable=False)
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class Holiday(UuidPkMixin, TimestampMixin, VersionMixin, Base):
    """Производственный календарь — обязателен для SLA в рабочих днях
    (раздел 5.3; `crm.service.is_business_day` — единственное место, которое
    расширяет, когда справочник подключается к расчёту)."""

    __tablename__ = "holidays"
    __table_args__ = (UniqueConstraint("date", name="uq_holidays_date"),)

    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_working_day: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))


class CustomFieldDef(UuidPkMixin, TimestampMixin, VersionMixin, Base):
    """Определение пользовательского поля (раздел 5.3). Значения живут в
    `custom_fields jsonb` целевой сущности, не здесь."""

    __tablename__ = "custom_field_defs"
    __table_args__ = (
        UniqueConstraint("entity_type", "code", name="uq_custom_field_defs_entity_code"),
        CheckConstraint(
            "entity_type IN ('deal','organization','contact','product')",
            name="custom_field_defs_entity_type_valid",
        ),
        CheckConstraint(
            "field_type IN ('string','number','date','bool','select','multiselect','file')",
            name="custom_field_defs_field_type_valid",
        ),
    )

    entity_type: Mapped[str] = mapped_column(String(16), nullable=False)
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    field_type: Mapped[str] = mapped_column(String(16), nullable=False)
    options: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    is_required: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    validation: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workflows.id", ondelete="RESTRICT"), nullable=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))


class LicenseTransferStatus(StrEnum):
    """«Статус по передаче» (rtk_requiriments.md разд. 4, Треб.1). Кейс не
    перечисляет допустимые значения буквально — набор ниже описывает
    минимальный жизненный цикл передачи лицензии ИТ-продукта вузу:
    `not_started` (договор подписан, передача ещё не начиналась),
    `in_progress` (материалы/доступы передаются), `transferred` (вуз получил
    всё по договору), `declined` (передача не состоялась — расторжение и
    т.п.). Тот же приём, что `transfer_status` кодами, а не свободным
    текстом: значение из импорта проверяется на этапе `dry_run`
    (`imports.fields.validate_field`), как уже сделано для `org_type`/
    `format` — опечатка в файле становится видимой ошибкой строки, а не
    тихо сохраняется как есть."""

    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    TRANSFERRED = "transferred"
    DECLINED = "declined"


class OrganizationLicense(UuidPkMixin, TimestampMixin, VersionMixin, SoftDeleteMixin, Base):
    """Лицензии/договоры вуз↔вендор↔ПО (rtk_requiriments.md разд. 4, Треб.1 —
    каталог из 10 полей, загружаемый через импорт xls/xlsx: Название ВУЗа,
    Вендор, ПО, Номер договора, Подписание лицензии, Срок действия лицензии
    (год), Статус по передаче, ФИО Менеджера, Ответственные от ВУЗа,
    Комментарий).

    `product_name` — сознательно текстовое поле, не FK на `products.id`:
    кейс говорит про внешнее лицензируемое ПО вендора (конкретный продукт по
    договору), которое не обязано совпадать с каталогом образовательных
    программ Школы (`Product` — курс/трек обучения, другая сущность по
    смыслу, раздел 5.3). По той же причине `manager_full_name` и
    `responsible_contacts` — свободный текст из исходного xls, а не ссылка
    на `users`/`contacts`: «ФИО Менеджера» в кейсе не обязан быть нашим
    пользователем, «Ответственные от ВУЗа» — не обязаны быть контактами с
    согласием на обработку ПДн в этой системе (см. отчёт по П3 — решение
    сознательно упрощено, не изобретаем связи, которых кейс не просит).

    Естественный ключ импорта — `contract_number` (`imports.fields.
    natural_key_for`): общий импортёр (`imports.service`) структурно
    поддерживает один natural key на тип сущности — тот же приём, что
    `Organization.inn`/`Product.code` уже используют. Настоящий составной
    ключ (вуз, вендор, ПО), предложенный в задаче как альтернатива, здесь не
    применяется — сознательное упрощение (подробности — отчёт по П3), поэтому
    уникальность на уровне БД тоже не заведена: единственный путь записи —
    ревьюируемый импорт (dry-run → apply), отдельных create/update-ручек
    нет вообще (только чтение, см. `catalog.router.organization_licenses_
    router`)."""

    __tablename__ = "organization_licenses"
    __table_args__ = (
        Index("ix_organization_licenses_organization", "organization_id"),
        Index("ix_organization_licenses_contract_number", "contract_number"),
        CheckConstraint(
            "transfer_status IS NULL OR transfer_status IN "
            "('not_started','in_progress','transferred','declined')",
            name="organization_licenses_transfer_status_valid",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=False
    )
    vendor: Mapped[str] = mapped_column(String(255), nullable=False)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    contract_number: Mapped[str] = mapped_column(String(128), nullable=False)
    license_signed_at: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    license_valid_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transfer_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    manager_full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    responsible_contacts: Mapped[str | None] = mapped_column(Text, nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    import_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("import_jobs.id", ondelete="SET NULL"), nullable=True
    )
