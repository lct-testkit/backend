"""Схемы каталога: организации, контакты, продукты, справочники (раздел 6)."""

from __future__ import annotations

import datetime as dt
import re
import uuid
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from app.core.masking import (
    mask_contacts_text,
    mask_email,
    mask_name,
    mask_phone,
    mask_tail,
    mask_year,
)
from app.core.normalize import clean_text, normalize_email, normalize_phone
from app.modules.catalog import learner

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

OrgTypeLiteral = Literal["university", "college", "company", "individual_entrepreneur"]
ProductFormatLiteral = Literal["online", "offline", "blended"]
# B3-7: "course" — ИТ-программа/курс Школы, "software" — ИТ-продукт (ПО)
# вендора, лицензируемый вузу (см. OrganizationLicense, ProductKind).
ProductKindLiteral = Literal["course", "software"]
LossReasonCategoryLiteral = Literal[
    "price", "timing", "competitor", "no_need", "no_budget", "no_contact", "other"
]
CustomFieldEntityLiteral = Literal["deal", "organization", "contact", "product"]
CustomFieldTypeLiteral = Literal[
    "string", "number", "date", "bool", "select", "multiselect", "file"
]
ContactChannelTypeLiteral = Literal["telegram", "whatsapp", "phone_extra", "email_extra"]
# «Способ связи» человека: какими каналами с ним связываются (`contacts.contact_methods`).
ContactMethodLiteral = Literal["email", "phone", "telegram", "whatsapp"]
# Роль контакта при продукте: сейчас только ответственный (каталог «Вендоры»).
ProductContactRoleLiteral = Literal["responsible"]


def _valid_email(value: str | None) -> str | None:
    """Канонический адрес (нижний регистр). Пустая строка -> `None`: так поле очищают в PATCH.
    Непустое значение, которое не разобралось, — 422 с именем поля, а не молчаливая потеря
    ключа дедупликации."""
    if value is None or clean_text(value) is None:
        return None
    normalized = normalize_email(value)
    if normalized is None:
        raise PydanticCustomError("invalid_email", "некорректный адрес электронной почты")
    return normalized


def _valid_phone(value: str | None) -> str | None:
    """Телефон в E.164 (`+79990234365`); пустая строка -> `None`, нераспознанное — 422."""
    if value is None or clean_text(value) is None:
        return None
    normalized = normalize_phone(value)
    if normalized is None:
        raise PydanticCustomError("invalid_phone", "некорректный номер телефона")
    return normalized


def _unique_methods(value: list[ContactMethodLiteral]) -> list[ContactMethodLiteral]:
    return list(dict.fromkeys(value))


ContactEmail = Annotated[str | None, Field(max_length=255), AfterValidator(_valid_email)]
ContactPhone = Annotated[str | None, Field(max_length=32), AfterValidator(_valid_phone)]
ContactMethods = Annotated[list[ContactMethodLiteral], AfterValidator(_unique_methods)]


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
    # B4-11: поля, отклонённые через `dismiss-drift` («Оставить как есть») — UI показывает
    # «отклонено N полей».
    drift_dismissed: dict[str, Any] = Field(default_factory=dict)
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


# --- B4-1/B4-2: ответственный за вуз ----------------------------------------


class OrganizationOwnerSetRequest(BaseModel):
    """`PUT /organizations/{id}/owner`. `owner_id=None` — снять ответственного."""

    owner_id: uuid.UUID | None = None
    reason: NonEmptyStr = Field(max_length=500)


class OrganizationOwnerRemoveRequest(BaseModel):
    """`DELETE /organizations/{id}/owner` — вуз становится «ничьим» (B4-2)."""

    reason: NonEmptyStr = Field(max_length=500)


class BulkOrganizationOwnerRequest(BaseModel):
    """Ровно один из `from_owner_id`/`organization_ids`; `owner_id=None` снимает
    ответственного у всех выбранных вузов."""

    from_owner_id: uuid.UUID | None = None
    organization_ids: list[uuid.UUID] | None = Field(default=None, min_length=1, max_length=100)
    owner_id: uuid.UUID | None = None
    reason: NonEmptyStr = Field(max_length=500)
    carry_open_deals: bool = False

    @model_validator(mode="after")
    def _exactly_one_selector(self) -> BulkOrganizationOwnerRequest:
        if (self.from_owner_id is None) == (self.organization_ids is None):
            raise ValueError("Нужен ровно один из from_owner_id или organization_ids")
        return self


class BulkOwnerSkipOut(BaseModel):
    organization_id: uuid.UUID
    reason: str


class BulkOrganizationOwnerResponse(BaseModel):
    batch_id: uuid.UUID
    changed: int
    skipped: list[BulkOwnerSkipOut] = Field(default_factory=list)
    deals_reassigned: int = 0
    remaining: int = 0


# --- B4-3: мягкое удаление ---------------------------------------------------


class OrganizationDeleteRequest(BaseModel):
    reason: NonEmptyStr = Field(max_length=500)


# =============================================================================
# Контакты
# =============================================================================


#: B4-9: `@handle` Telegram — 5-32 латиница/цифры/`_` (та же форма, что Keycloak
#: `preferred_username`, но отдельная константа: контакт не субъект Keycloak).
_TELEGRAM_HANDLE = re.compile(r"^[A-Za-z0-9_]{5,32}$")
#: `https://t.me/name` или `t.me/name` — сводится к `@name`.
_TELEGRAM_URL = re.compile(r"^(?:https?://)?t\.me/([A-Za-z0-9_]{5,32})/?$", re.IGNORECASE)


class ContactChannelIn(BaseModel):
    """B4-9: значение нормализуется по типу — `phone_extra`/`whatsapp` в E.164,
    `email_extra` в нижний регистр, `telegram` в `@handle` (ссылка `https://t.me/name`
    сводится к нему же). Не разобралось — 422 с именем поля `value`."""

    type: ContactChannelTypeLiteral
    value: NonEmptyStr = Field(max_length=255)
    is_primary: bool = False

    @field_validator("value", mode="after")
    @classmethod
    def _normalize_value(cls, value: str, info: ValidationInfo) -> str:
        channel_type = info.data.get("type")
        if channel_type in ("phone_extra", "whatsapp"):
            normalized = normalize_phone(value)
            if normalized is None:
                raise PydanticCustomError("invalid_phone", "некорректный номер телефона")
            return normalized
        if channel_type == "email_extra":
            normalized = normalize_email(value)
            if normalized is None:
                raise PydanticCustomError("invalid_email", "некорректный адрес электронной почты")
            return normalized
        if channel_type == "telegram":
            text = value.strip()
            match = _TELEGRAM_URL.match(text)
            handle = match.group(1) if match else text.removeprefix("@")
            if not _TELEGRAM_HANDLE.fullmatch(handle):
                raise PydanticCustomError(
                    "invalid_telegram",
                    "некорректный Telegram: нужен @handle из 5-32 латинских букв/цифр/_",
                )
            return f"@{handle}"
        return value


def _at_most_one_primary_per_type(channels: list[ContactChannelIn]) -> list[ContactChannelIn]:
    seen_primary: set[str] = set()
    for channel in channels:
        if channel.is_primary:
            if channel.type in seen_primary:
                raise PydanticCustomError(
                    "duplicate_primary_channel",
                    f"два первичных канала типа {channel.type!r}",
                )
            seen_primary.add(channel.type)
    return channels


#: B4-9: не больше стольких каналов связи на контакт за один запрос.
ContactChannelsList = Annotated[
    list[ContactChannelIn], Field(max_length=10), AfterValidator(_at_most_one_primary_per_type)
]


class ContactChannelOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    type: str
    value: str
    is_primary: bool
    is_verified: bool


class ContactChannelListResponse(BaseModel):
    """B4-9: `GET /contacts/{id}/channels` — маскированные значения (`mask_channel_
    value`); полные — только через `POST /contacts/{id}/reveal`."""

    items: list[ContactChannelOut] = Field(default_factory=list)


class ContactCreateRequest(BaseModel):
    """Email приводится к нижнему регистру, телефон — к E.164; и тот и другой при дубле
    (`ContactService.create`) дают 409 CRM-1301. Пустая строка — «нет значения»."""

    organization_id: uuid.UUID | None = None
    first_name: NonEmptyStr = Field(max_length=128)
    last_name: NonEmptyStr = Field(max_length=128)
    middle_name: str | None = Field(default=None, max_length=128)
    position: str | None = Field(default=None, max_length=255)
    email: ContactEmail = None
    phone: ContactPhone = None
    contact_methods: ContactMethods = Field(default_factory=list)
    is_decision_maker: bool = False
    # B4-9: при создании контакта согласия на него ещё нет (оно ссылается на
    # `subject_id` контакта) — только `null`, привязка вторым шагом через PATCH.
    consent_id: uuid.UUID | None = None
    source: str | None = Field(default=None, max_length=32)
    external_ids: dict[str, Any] = Field(default_factory=dict)
    channels: ContactChannelsList = Field(default_factory=list)


class ContactUpdateRequest(BaseModel):
    """Частичное обновление: передаются только меняемые поля. `email`/`phone` пустой строкой
    очищаются; `contact_methods` заменяется списком целиком (`null` не допускается).

    B4-9: `channels` — `null` (поле не пришло или пришло `null`) не меняет каналы, `[]`
    удаляет все, непустой список заменяет целиком (сохраняя `id`/`is_verified`
    совпавших по паре тип+значение). `source` заполненный меняет только ADMIN.
    `consent_id` должен указывать на принятое неотозванное согласие этого контакта."""

    organization_id: uuid.UUID | None = None
    first_name: NonEmptyStr | None = Field(default=None, max_length=128)
    last_name: NonEmptyStr | None = Field(default=None, max_length=128)
    middle_name: str | None = Field(default=None, max_length=128)
    position: str | None = Field(default=None, max_length=255)
    email: ContactEmail = None
    phone: ContactPhone = None
    # Не `None` по умолчанию: колонка NOT NULL, и явный `null` в PATCH должен быть 422, а не 500.
    contact_methods: ContactMethods = Field(default_factory=list)
    is_decision_maker: bool | None = None
    channels: ContactChannelsList | None = None
    source: str | None = Field(default=None, max_length=32)
    consent_id: uuid.UUID | None = None


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
    contact_methods: list[str] = Field(default_factory=list)
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
            contact_methods=list(contact.contact_methods or []),
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
    contact_methods: list[str] = Field(default_factory=list)
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
    # B3-7: ИТ-программа (курс) или ИТ-продукт (ПО) вендора; по умолчанию —
    # курс, как и раньше у всех продуктов до этой задачи.
    kind: ProductKindLiteral = "course"
    # Вендор продукта — организация каталога; несуществующая или удалённая — 404.
    vendor_id: uuid.UUID | None = None
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
    # B3-7: смена типа у продукта, уже использованного в сделке или
    # лицензии, — 409 ENTITY_IN_USE (см. ProductService.update).
    kind: ProductKindLiteral | None = None
    # `null` снимает вендора с продукта.
    vendor_id: uuid.UUID | None = None
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
    kind: ProductKindLiteral = "course"
    vendor_id: uuid.UUID | None = None
    # Только чтение: название вендора берётся из организации `vendor_id` при выдаче.
    vendor_name: str | None = None
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

    @classmethod
    def from_model(cls, product: Any, vendor_name: str | None = None) -> ProductOut:
        out = cls.model_validate(product)
        out.vendor_name = vendor_name
        return out


class ProductListResponse(BaseModel):
    items: list[ProductOut]
    next_cursor: str | None = None


# =============================================================================
# Ответственные за продукты: связь контакт — продукт (каталог «Вендоры»)
# =============================================================================


class ProductContactLinkRequest(BaseModel):
    role: ProductContactRoleLiteral = "responsible"


class ProductContactOut(BaseModel):
    """Ответственный контакт продукта. Телефон и email маскированы, как в списке контактов:
    полные значения — только через `POST /api/contacts/{id}/reveal`."""

    contact: ContactOut
    role: str


class ProductContactListResponse(BaseModel):
    items: list[ProductContactOut]
    next_cursor: str | None = None


class ContactProductOut(BaseModel):
    """Продукт, за который отвечает контакт."""

    product: ProductOut
    role: str


class ContactProductListResponse(BaseModel):
    items: list[ContactProductOut]
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
    # B3-6 (BUG-12): название вуза — раньше выдача лицензии требовала
    # отдельного запроса на организацию, чтобы узнать, чей это договор.
    organization_name: str | None = None
    organization_short_name: str | None = None
    vendor: str
    vendor_id: uuid.UUID | None = None
    product_name: str
    product_id: uuid.UUID | None = None
    contract_number: str | None = None
    license_signed_at: dt.date | None = None
    license_valid_year: int | None = None
    transfer_status: TransferStatusLiteral | None = None
    manager_full_name: str | None = None
    responsible_contacts: str | None = None
    comment: str | None = None
    # B3-3: разобранные значения «Подписание лицензии» / «Срок действия».
    is_signed: bool | None = None
    license_term_years: int | None = None
    license_valid_until: dt.date | None = None
    is_perpetual: bool = False
    version: int
    created_at: dt.datetime
    updated_at: dt.datetime

    @classmethod
    def from_model(
        cls,
        license_: Any,
        *,
        reveal: bool,
        organization_name: str | None = None,
        organization_short_name: str | None = None,
    ) -> OrganizationLicenseOut:
        """ФИО менеджера и «ответственные от вуза» — персональные данные из исходного xls. Без
        права `contact:reveal` они отдаются маскированными: инициалами вместо ФИО, email и
        телефоны по своим форматам (`mask_contacts_text`). Раскрытие остальным — через обычный
        `reveal` контактов, где оно попадает в аудит."""
        out = cls.model_validate(license_)
        out.organization_name = organization_name
        out.organization_short_name = organization_short_name
        if not reveal:
            out.manager_full_name = mask_name(license_.manager_full_name)
            out.responsible_contacts = mask_contacts_text(license_.responsible_contacts)
        return out


class OrganizationLicenseListResponse(BaseModel):
    items: list[OrganizationLicenseOut]
    next_cursor: str | None = None
    # B3-6: только когда запрошен `with_total=true` (доп. COUNT-запрос).
    total: int | None = None


class LicenseCreateRequest(BaseModel):
    """B3-6: вендор и ПО принимаются либо ссылкой (`vendor_id`/`product_id`
    — существующая запись каталога), либо текстом (`vendor`/`product_name`
    — как раньше у импорта; сервис пытается найти единственное совпадение
    в каталоге тем же `catalog.license_matching.LicenseMatcher`, что и
    импорт, но не создаёт недостающее автоматически, см. отчёт по эпику).
    Нужно указать хотя бы одно из каждой пары — проверяется в сервисе
    (`ValidationError`, а не здесь: единый формат ошибки Problem Details).

    `transfer_status` — обычная строка, не `Literal`: сервис пропускает её
    через `catalog.license_values.parse_transfer_status`, который принимает
    и английские коды, и русские синонимы (B3-3) — тем же путём, что и
    импорт, а не отдельной, более строгой проверкой на API."""

    organization_id: uuid.UUID
    vendor_id: uuid.UUID | None = None
    vendor: NonEmptyStr | None = Field(default=None, max_length=255)
    product_id: uuid.UUID | None = None
    product_name: NonEmptyStr | None = Field(default=None, max_length=255)
    contract_number: str | None = Field(default=None, max_length=128)
    is_signed: bool | None = None
    license_signed_at: dt.date | None = None
    license_term_years: int | None = Field(default=None, ge=1, le=50)
    license_valid_until: dt.date | None = None
    is_perpetual: bool = False
    transfer_status: str | None = None
    manager_full_name: str | None = Field(default=None, max_length=255)
    responsible_contacts: str | None = None
    comment: str | None = None


class LicenseUpdateRequest(BaseModel):
    """Все поля необязательны (PATCH); `organization_id` не редактируется —
    удалить и создать заново (см. задачу B3-6)."""

    vendor_id: uuid.UUID | None = None
    vendor: NonEmptyStr | None = Field(default=None, max_length=255)
    product_id: uuid.UUID | None = None
    product_name: NonEmptyStr | None = Field(default=None, max_length=255)
    contract_number: str | None = Field(default=None, max_length=128)
    is_signed: bool | None = None
    license_signed_at: dt.date | None = None
    license_term_years: int | None = Field(default=None, ge=1, le=50)
    license_valid_until: dt.date | None = None
    is_perpetual: bool | None = None
    transfer_status: str | None = None
    manager_full_name: str | None = Field(default=None, max_length=255)
    responsible_contacts: str | None = None
    comment: str | None = None


# =============================================================================
# Профиль учащегося: ПДн для шаблона LMS «Загрузка пользователей»
# =============================================================================

# Маскированный вид: даты — только год; пол, образование и падежные формы ФИО (само ФИО и так есть
# в карточке контакта) — как есть; остальное (СНИЛС, паспорт, адрес регистрации, диплом) — хвост.
_PROFILE_YEAR_ONLY: frozenset[str] = frozenset(
    {"passport_issued_at", "birth_date", "diploma_issued_at"}
)
_PROFILE_CLEAR: frozenset[str] = frozenset(
    {"sex", "education", "first_name_dative", "last_name_dative", "middle_name_dative"}
)

# Значение поля профиля во входящем запросе: строка как в шаблоне LMS (`13.03.2020`, `М`, метка из
# списка «Образование»). Длина ограничена заранее: разбор ниже не должен работать над мегабайтами.
ProfileInput = Annotated[str | None, Field(max_length=1024)]


class LearnerProfileOut(BaseModel):
    """Профиль учащегося в маскированном виде (`GET /contacts/{id}/learner-profile`).

    СНИЛС, паспорт, адрес регистрации и реквизиты диплома — `***` и три последних знака (короткие
    значения закрыты целиком); даты рождения и выдачи документов — только год (`"1990"`). Полные
    значения отдаёт `POST .../learner-profile/reveal` с записью аудита. Если профиля нет, все
    поля `null`.
    """

    snils: str | None = None
    passport_series: str | None = None
    passport_number: str | None = None
    passport_issued_by: str | None = None
    passport_issued_at: str | None = None
    passport_dept_code: str | None = None
    sex: str | None = None
    birth_date: str | None = None
    reg_region: str | None = None
    reg_city: str | None = None
    reg_street: str | None = None
    reg_house: str | None = None
    reg_apartment: str | None = None
    reg_zip: str | None = None
    first_name_dative: str | None = None
    last_name_dative: str | None = None
    middle_name_dative: str | None = None
    education: str | None = None
    education_label: str | None = None
    diploma_profession: str | None = None
    diploma_institution: str | None = None
    diploma_surname: str | None = None
    diploma_number: str | None = None
    diploma_series: str | None = None
    diploma_reg_number: str | None = None
    diploma_issued_at: str | None = None

    @classmethod
    def from_model(cls, profile: Any) -> LearnerProfileOut:
        if profile is None:
            return cls()
        values: dict[str, Any] = {}
        for name in learner.PROFILE_TARGETS:
            raw = getattr(profile, name)
            if name in _PROFILE_YEAR_ONLY:
                values[name] = mask_year(raw)
            elif name in _PROFILE_CLEAR:
                values[name] = raw
            else:
                values[name] = mask_tail(raw)
        values["education_label"] = learner.EDUCATION_LABELS.get(profile.education or "")
        return cls(**values)


class LearnerProfileRevealOut(BaseModel):
    """Полный профиль учащегося (`POST .../learner-profile/reveal`). Каждый вызов пишет в аудит
    `PII_REVEALED` (категория, без значений)."""

    model_config = ConfigDict(from_attributes=True)

    snils: str | None = None
    passport_series: str | None = None
    passport_number: str | None = None
    passport_issued_by: str | None = None
    passport_issued_at: dt.date | None = None
    passport_dept_code: str | None = None
    sex: str | None = None
    birth_date: dt.date | None = None
    reg_region: str | None = None
    reg_city: str | None = None
    reg_street: str | None = None
    reg_house: str | None = None
    reg_apartment: str | None = None
    reg_zip: str | None = None
    first_name_dative: str | None = None
    last_name_dative: str | None = None
    middle_name_dative: str | None = None
    education: str | None = None
    education_label: str | None = None
    diploma_profession: str | None = None
    diploma_institution: str | None = None
    diploma_surname: str | None = None
    diploma_number: str | None = None
    diploma_series: str | None = None
    diploma_reg_number: str | None = None
    diploma_issued_at: dt.date | None = None

    @classmethod
    def from_model(cls, profile: Any) -> LearnerProfileRevealOut:
        if profile is None:
            return cls()
        out = cls.model_validate(profile)
        out.education_label = learner.EDUCATION_LABELS.get(profile.education or "")
        return out


class LearnerProfileUpdateRequest(BaseModel):
    """Частичное обновление профиля: обновляются только переданные поля, пустая строка (или
    `null`) очищает поле. Значения разбираются так же, как при импорте шаблона LMS
    (`catalog.learner.parse_profile_value`): СНИЛС с контрольной суммой, серия паспорта из 4 цифр,
    даты `ДД.ММ.ГГГГ` или `ГГГГ-ММ-ДД`, пол `М`/`Ж`, образование из списка листа «Лист2»."""

    model_config = ConfigDict(extra="forbid")

    snils: ProfileInput = None
    passport_series: ProfileInput = None
    passport_number: ProfileInput = None
    passport_issued_by: ProfileInput = None
    passport_issued_at: ProfileInput = None
    passport_dept_code: ProfileInput = None
    sex: ProfileInput = None
    birth_date: ProfileInput = None
    reg_region: ProfileInput = None
    reg_city: ProfileInput = None
    reg_street: ProfileInput = None
    reg_house: ProfileInput = None
    reg_apartment: ProfileInput = None
    reg_zip: ProfileInput = None
    first_name_dative: ProfileInput = None
    last_name_dative: ProfileInput = None
    middle_name_dative: ProfileInput = None
    education: ProfileInput = None
    diploma_profession: ProfileInput = None
    diploma_institution: ProfileInput = None
    diploma_surname: ProfileInput = None
    diploma_number: ProfileInput = None
    diploma_series: ProfileInput = None
    diploma_reg_number: ProfileInput = None
    diploma_issued_at: ProfileInput = None


# =============================================================================
# B4-12 (A-18): запрос доступа к чужой записи
# =============================================================================


class AccessRequestCreateRequest(BaseModel):
    entity_type: Literal["organization"] = "organization"
    entity_id: uuid.UUID
    reason: NonEmptyStr = Field(max_length=1000)


class AccessRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    requester_id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    reason: str
    status: str
    decided_by: uuid.UUID | None
    decided_at: dt.datetime | None
    decision_comment: str | None
    expires_at: dt.datetime | None
    created_at: dt.datetime


class AccessRequestListResponse(BaseModel):
    items: list[AccessRequestOut]
    next_cursor: str | None = None


class AccessRequestDecisionRequest(BaseModel):
    decision: Literal["approve", "reject"]
    days: int | None = Field(default=None, ge=1, le=365)
    comment: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _reject_requires_comment(self) -> AccessRequestDecisionRequest:
        if self.decision == "reject" and not (self.comment and self.comment.strip()):
            raise ValueError("отказ требует комментарий")
        return self
