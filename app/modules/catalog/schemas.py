"""Схемы каталога: организации, контакты, продукты, справочники (раздел 6)."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.core.masking import mask_email, mask_phone

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

OrgTypeLiteral = Literal["university", "college", "company", "individual_entrepreneur"]
ProductFormatLiteral = Literal["online", "offline", "blended"]
LossReasonCategoryLiteral = Literal[
    "price", "timing", "competitor", "no_need", "no_budget", "no_contact", "other"
]
CustomFieldEntityLiteral = Literal["deal", "organization", "contact", "product"]
CustomFieldTypeLiteral = Literal[
    "string", "number", "date", "bool", "select", "multiselect", "file"
]
ContactChannelTypeLiteral = Literal["telegram", "whatsapp", "phone_extra", "email_extra"]


# =============================================================================
# Регионы
# =============================================================================


class RegionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    federal_district: str | None = None
    timezone: str | None = None
    code: str


class RegionListResponse(BaseModel):
    items: list[RegionOut]
    next_cursor: str | None = None


# =============================================================================
# Организации
# =============================================================================


class OrganizationCreateRequest(BaseModel):
    """Раздел 6: либо ИНН, либо название и базовые реквизиты."""

    name: NonEmptyStr | None = Field(default=None, max_length=512)
    short_name: str | None = Field(default=None, max_length=255)
    org_type: OrgTypeLiteral = "university"
    inn: str | None = Field(default=None, max_length=12)
    kpp: str | None = Field(default=None, max_length=9)
    ogrn: str | None = Field(default=None, max_length=15)
    legal_address: str | None = None
    actual_address: str | None = None
    region_id: uuid.UUID | None = None
    website: str | None = Field(default=None, max_length=255)
    main_phone: str | None = Field(default=None, max_length=32)
    main_email: str | None = Field(default=None, max_length=255)
    students_count: int | None = Field(default=None, ge=0)
    owner_id: uuid.UUID | None = None
    source: str | None = Field(default=None, max_length=32)
    external_ids: dict[str, Any] = Field(default_factory=dict)
    custom_fields: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _requires_identity(self) -> OrganizationCreateRequest:
        if not self.inn and not self.name:
            raise ValueError("Нужен либо ИНН, либо название организации")
        return self


class OrganizationUpdateRequest(BaseModel):
    name: NonEmptyStr | None = Field(default=None, max_length=512)
    short_name: str | None = Field(default=None, max_length=255)
    org_type: OrgTypeLiteral | None = None
    kpp: str | None = Field(default=None, max_length=9)
    ogrn: str | None = Field(default=None, max_length=15)
    legal_address: str | None = None
    actual_address: str | None = None
    region_id: uuid.UUID | None = None
    website: str | None = Field(default=None, max_length=255)
    main_phone: str | None = Field(default=None, max_length=32)
    main_email: str | None = Field(default=None, max_length=255)
    students_count: int | None = Field(default=None, ge=0)
    owner_id: uuid.UUID | None = None
    custom_fields: dict[str, Any] | None = None


class OrganizationOut(BaseModel):
    """Список/карточка по умолчанию: для `org_type='individual_entrepreneur'`

    телефон и email маскированы (dop.md §11.8 — данные ИП это ПДн физлица).
    Для остальных `org_type` сведения о юрлице не ПДн, маскировать нечего —
    `from_model` их не трогает. Полные значения ИП — только через
    `POST /api/organizations/{id}/reveal` (`OrganizationRevealOut`), с
    отдельной записью аудита `PII_REVEALED`, тем же приёмом что `ContactOut`.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    short_name: str | None = None
    org_type: str
    inn: str | None = None
    kpp: str | None = None
    ogrn: str | None = None
    legal_address: str | None = None
    actual_address: str | None = None
    region_id: uuid.UUID | None = None
    website: str | None = None
    main_phone: str | None = None
    main_email: str | None = None
    students_count: int | None = None
    external_ids: dict[str, Any]
    owner_id: uuid.UUID | None = None
    source: str | None = None
    custom_fields: dict[str, Any]
    verified_source: str | None = None
    verified_at: dt.datetime | None = None
    registry_status: str | None = None
    registry_checked_at: dt.datetime | None = None
    requisites_drift: dict[str, Any] | None = None
    manual_overrides: list[str]
    is_accredited: bool | None = None
    accreditation_until: dt.date | None = None
    version: int
    created_at: dt.datetime
    updated_at: dt.datetime

    @classmethod
    def from_model(cls, organization: Any) -> OrganizationOut:
        out = cls.model_validate(organization)
        if organization.org_type == "individual_entrepreneur":
            out.main_phone = mask_phone(organization.main_phone)
            out.main_email = mask_email(organization.main_email)
        return out


class OrganizationRevealOut(OrganizationOut):
    """Полные, немаскированные данные организации (`reveal`, dop.md §11.8)."""


class OrganizationListResponse(BaseModel):
    items: list[OrganizationOut]
    next_cursor: str | None = None


class DuplicateCandidateOut(BaseModel):
    """`name`/`inn` — `None`, если `accessible=false` (раздел 6: «ответ не
    раскрывает чувствительные поля, но возвращает признак существования»)."""

    id: uuid.UUID
    name: str | None
    inn: str | None = None
    match: Literal["inn", "similar_name"]
    accessible: bool


class DuplicateCheckResponse(BaseModel):
    found: bool
    candidates: list[DuplicateCandidateOut] = Field(default_factory=list)


class ApplyDriftRequest(BaseModel):
    fields: list[str] = Field(
        default_factory=list,
        description="Какие поля из requisites_drift принять; пусто — принять все",
    )


# =============================================================================
# Контакты
# =============================================================================


class ContactChannelIn(BaseModel):
    type: ContactChannelTypeLiteral
    value: NonEmptyStr = Field(max_length=255)
    is_primary: bool = False


class ContactChannelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: str
    value: str
    is_primary: bool
    is_verified: bool


class ContactCreateRequest(BaseModel):
    organization_id: uuid.UUID | None = None
    first_name: NonEmptyStr = Field(max_length=128)
    last_name: NonEmptyStr = Field(max_length=128)
    middle_name: str | None = Field(default=None, max_length=128)
    position: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    is_decision_maker: bool = False
    consent_id: uuid.UUID | None = None
    source: str | None = Field(default=None, max_length=32)
    external_ids: dict[str, Any] = Field(default_factory=dict)
    channels: list[ContactChannelIn] = Field(default_factory=list)


class ContactUpdateRequest(BaseModel):
    organization_id: uuid.UUID | None = None
    first_name: NonEmptyStr | None = Field(default=None, max_length=128)
    last_name: NonEmptyStr | None = Field(default=None, max_length=128)
    middle_name: str | None = Field(default=None, max_length=128)
    position: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=32)
    is_decision_maker: bool | None = None


class ContactOut(BaseModel):
    """Список/карточка по умолчанию: телефон и email маскированы (раздел 6.6).

    Полные значения — только через `POST /api/contacts/{id}/reveal`
    (`ContactRevealOut`), с отдельной записью аудита `PII_REVEALED`.
    """

    id: uuid.UUID
    organization_id: uuid.UUID | None = None
    first_name: str
    last_name: str
    middle_name: str | None = None
    position: str | None = None
    email: str | None = None
    phone: str | None = None
    is_decision_maker: bool
    is_anonymized: bool
    source: str | None = None
    external_ids: dict[str, Any]
    version: int
    created_at: dt.datetime
    updated_at: dt.datetime

    @classmethod
    def from_model(cls, contact: Any) -> ContactOut:
        return cls(
            id=contact.id,
            organization_id=contact.organization_id,
            first_name=contact.first_name,
            last_name=contact.last_name,
            middle_name=contact.middle_name,
            position=contact.position,
            email=mask_email(contact.email),
            phone=mask_phone(contact.phone),
            is_decision_maker=contact.is_decision_maker,
            is_anonymized=contact.is_anonymized,
            source=contact.source,
            external_ids=contact.external_ids,
            version=contact.version,
            created_at=contact.created_at,
            updated_at=contact.updated_at,
        )


class ContactRevealOut(BaseModel):
    """Полные, немаскированные контактные данные (раздел 6.6, `reveal`)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID | None = None
    first_name: str
    last_name: str
    middle_name: str | None = None
    position: str | None = None
    email: str | None = None
    phone: str | None = None
    is_decision_maker: bool
    channels: list[ContactChannelOut] = Field(default_factory=list)


class ContactListResponse(BaseModel):
    items: list[ContactOut]
    next_cursor: str | None = None


# =============================================================================
# Продукты и направления
# =============================================================================


class DirectionCreateRequest(BaseModel):
    code: NonEmptyStr = Field(max_length=64)
    name: NonEmptyStr = Field(max_length=255)
    parent_id: uuid.UUID | None = None


class DirectionUpdateRequest(BaseModel):
    name: NonEmptyStr | None = Field(default=None, max_length=255)
    parent_id: uuid.UUID | None = None


class DirectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    parent_id: uuid.UUID | None = None
    version: int
    created_at: dt.datetime
    updated_at: dt.datetime


class DirectionListResponse(BaseModel):
    items: list[DirectionOut]
    next_cursor: str | None = None


class ProductCreateRequest(BaseModel):
    code: NonEmptyStr = Field(max_length=64)
    name: NonEmptyStr = Field(max_length=255)
    description: str | None = None
    direction_id: uuid.UUID | None = None
    duration_hours: int | None = Field(default=None, ge=0)
    format: ProductFormatLiteral | None = None
    base_price: Decimal | None = None
    currency: str = Field(default="RUB", min_length=3, max_length=3)
    is_active: bool = True
    valid_from: dt.date | None = None
    valid_to: dt.date | None = None
    custom_fields: dict[str, Any] = Field(default_factory=dict)


class ProductUpdateRequest(BaseModel):
    name: NonEmptyStr | None = Field(default=None, max_length=255)
    description: str | None = None
    direction_id: uuid.UUID | None = None
    duration_hours: int | None = Field(default=None, ge=0)
    format: ProductFormatLiteral | None = None
    base_price: Decimal | None = None
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    is_active: bool | None = None
    valid_from: dt.date | None = None
    valid_to: dt.date | None = None
    custom_fields: dict[str, Any] | None = None


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    description: str | None = None
    direction_id: uuid.UUID | None = None
    duration_hours: int | None = None
    format: str | None = None
    base_price: Decimal | None = None
    currency: str
    is_active: bool
    valid_from: dt.date | None = None
    valid_to: dt.date | None = None
    custom_fields: dict[str, Any]
    version: int
    created_at: dt.datetime
    updated_at: dt.datetime


class ProductListResponse(BaseModel):
    items: list[ProductOut]
    next_cursor: str | None = None


# =============================================================================
# Причины отказа
# =============================================================================


class LossReasonCreateRequest(BaseModel):
    code: NonEmptyStr = Field(max_length=64)
    name: NonEmptyStr = Field(max_length=255)
    category: LossReasonCategoryLiteral
    is_active: bool = True
    sort_order: int = 0


class LossReasonUpdateRequest(BaseModel):
    name: NonEmptyStr | None = Field(default=None, max_length=255)
    category: LossReasonCategoryLiteral | None = None
    is_active: bool | None = None
    sort_order: int | None = None


class LossReasonOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    category: str
    is_active: bool
    sort_order: int
    version: int
    created_at: dt.datetime
    updated_at: dt.datetime


class LossReasonListResponse(BaseModel):
    items: list[LossReasonOut]
    next_cursor: str | None = None


# =============================================================================
# Производственный календарь
# =============================================================================


class HolidayCreateRequest(BaseModel):
    date: dt.date
    name: NonEmptyStr = Field(max_length=255)
    is_working_day: bool = False


class HolidayUpdateRequest(BaseModel):
    date: dt.date | None = None
    name: NonEmptyStr | None = Field(default=None, max_length=255)
    is_working_day: bool | None = None


class HolidayOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    date: dt.date
    name: str
    is_working_day: bool
    version: int
    created_at: dt.datetime
    updated_at: dt.datetime


class HolidayListResponse(BaseModel):
    items: list[HolidayOut]
    next_cursor: str | None = None


# =============================================================================
# Пользовательские поля
# =============================================================================


class CustomFieldDefCreateRequest(BaseModel):
    entity_type: CustomFieldEntityLiteral
    code: NonEmptyStr = Field(max_length=64)
    label: NonEmptyStr = Field(max_length=255)
    field_type: CustomFieldTypeLiteral
    options: dict[str, Any] | None = None
    is_required: bool = False
    validation: dict[str, Any] | None = None
    workflow_id: uuid.UUID | None = None
    sort_order: int = 0


class CustomFieldDefUpdateRequest(BaseModel):
    label: NonEmptyStr | None = Field(default=None, max_length=255)
    options: dict[str, Any] | None = None
    is_required: bool | None = None
    validation: dict[str, Any] | None = None
    sort_order: int | None = None
    is_active: bool | None = None


class CustomFieldDefOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    entity_type: str
    code: str
    label: str
    field_type: str
    options: dict[str, Any] | None = None
    is_required: bool
    validation: dict[str, Any] | None = None
    workflow_id: uuid.UUID | None = None
    sort_order: int
    is_active: bool
    version: int
    created_at: dt.datetime
    updated_at: dt.datetime


class CustomFieldDefListResponse(BaseModel):
    items: list[CustomFieldDefOut]
    next_cursor: str | None = None


# =============================================================================
# Лицензии/договоры вуз↔вендор↔ПО (П3, rtk_requiriments.md разд. 4, Треб.1)
# =============================================================================

TransferStatusLiteral = Literal["not_started", "in_progress", "transferred", "declined"]


class OrganizationLicenseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    vendor: str
    product_name: str
    contract_number: str
    license_signed_at: dt.date | None = None
    license_valid_year: int | None = None
    transfer_status: TransferStatusLiteral | None = None
    manager_full_name: str | None = None
    responsible_contacts: str | None = None
    comment: str | None = None
    version: int
    created_at: dt.datetime
    updated_at: dt.datetime


class OrganizationLicenseListResponse(BaseModel):
    items: list[OrganizationLicenseOut]
    next_cursor: str | None = None
