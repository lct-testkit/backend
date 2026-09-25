"""Описание целевых полей импорта по типу сущности (new_spec §4.12).

Спринт 5 поддерживает `organization` (основной сценарий кейса — «реестр из
500 вузов») и `product` (тот же генерический механизм на втором типе
каталога, чтобы `import_jobs.entity_type` было проверено не только на одном
случае). `contact` сознательно не включён: контакты — ПДн (раздел 5.2),
массовый импорт потребовал бы решения про согласия на обработку прямо в
момент bulk-вставки, которое эта спецификация нигде не описывает — заводить
его явочным порядком в рамках generic-импортёра рискованнее, чем отложить.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import EmailStr, TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from app.modules.catalog.validators import validate_requisite

FieldKind = Literal[
    "text",
    "int",
    "decimal",
    "bool",
    "date",
    "email",
    "phone",
    "inn",
    "kpp",
    "ogrn",
    "region_code",
    "direction_code",
    "org_type",
    "format",
    # П3 (rtk_requiriments.md разд. 4, Треб.1): `organization_name` — тот же
    # приём FK-резолва, что `region_code`/`direction_code` (перехватывается
    # в `imports.service._extract_row` до `validate_field`, см. `_FK_TARGETS`
    # там же), только колонка поиска — `Organization.name`, а не `.code`.
    # `transfer_status` — обычное поле с фиксированным набором значений
    # (валидируется ниже, тем же приёмом, что `org_type`/`format`).
    "organization_name",
    "transfer_status",
]

_EMAIL_ADAPTER: TypeAdapter[str] = TypeAdapter(EmailStr)
_PHONE_DIGITS = re.compile(r"\D+")
_DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y")


@dataclass(slots=True, frozen=True)
class FieldSpec:
    target: str
    label: str
    kind: FieldKind
    required: bool = False


ORGANIZATION_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("name", "Наименование", "text", required=True),
    FieldSpec("short_name", "Краткое наименование", "text"),
    FieldSpec("org_type", "Тип организации", "org_type"),
    FieldSpec("inn", "ИНН", "inn"),
    FieldSpec("kpp", "КПП", "kpp"),
    FieldSpec("ogrn", "ОГРН", "ogrn"),
    FieldSpec("legal_address", "Юридический адрес", "text"),
    FieldSpec("actual_address", "Фактический адрес", "text"),
    FieldSpec("region_code", "Код региона", "region_code"),
    FieldSpec("website", "Сайт", "text"),
    FieldSpec("main_phone", "Телефон", "phone"),
    FieldSpec("main_email", "Email", "email"),
    FieldSpec("students_count", "Количество студентов", "int"),
)

PRODUCT_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("code", "Код", "text", required=True),
    FieldSpec("name", "Наименование", "text", required=True),
    FieldSpec("description", "Описание", "text"),
    FieldSpec("direction_code", "Код направления", "direction_code"),
    FieldSpec("duration_hours", "Длительность, часы", "int"),
    FieldSpec("format", "Формат", "format"),
    FieldSpec("base_price", "Цена", "decimal"),
    FieldSpec("currency", "Валюта", "text"),
)

# П3 (rtk_requiriments.md разд. 4, Треб.1): лицензии/договоры вуз↔вендор↔ПО.
# Порядок и названия полей — буквально из списка кейса. `organization_name`
# резолвится в `organization_id` (см. докстринг kind'а выше);
# `contract_number` — natural key импорта (см. `catalog.models.
# OrganizationLicense`, докстринг — почему не составной ключ).
LICENSE_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("organization_name", "Название ВУЗа", "organization_name", required=True),
    FieldSpec("vendor", "Вендор", "text", required=True),
    FieldSpec("product_name", "ПО", "text", required=True),
    FieldSpec("contract_number", "Номер договора", "text", required=True),
    FieldSpec("license_signed_at", "Подписание лицензии", "date"),
    FieldSpec("license_valid_year", "Срок действия лицензии (год)", "int"),
    FieldSpec("transfer_status", "Статус по передаче", "transfer_status"),
    FieldSpec("manager_full_name", "ФИО Менеджера", "text"),
    FieldSpec("responsible_contacts", "Ответственные от ВУЗа", "text"),
    FieldSpec("comment", "Комментарий", "text"),
)

_NATURAL_KEYS: dict[str, str] = {
    "organization": "inn",
    "product": "code",
    "license": "contract_number",
}
_ENTITY_FIELDS: dict[str, tuple[FieldSpec, ...]] = {
    "organization": ORGANIZATION_FIELDS,
    "product": PRODUCT_FIELDS,
    "license": LICENSE_FIELDS,
}


def fields_for(entity_type: str) -> tuple[FieldSpec, ...]:
    fields = _ENTITY_FIELDS.get(entity_type)
    if fields is None:
        raise ValueError(f"Неизвестный тип сущности импорта: {entity_type!r}")
    return fields


def natural_key_for(entity_type: str) -> str:
    return _NATURAL_KEYS[entity_type]


def normalize_phone_e164(value: str) -> str | None:
    """Раздел 4.12: «телефон — нормализация к E.164»."""
    digits = _PHONE_DIGITS.sub("", value)
    if len(digits) == 11 and digits[0] in ("7", "8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    if len(digits) != 11 or digits[0] != "7":
        return None
    return f"+{digits}"


def validate_field(spec: FieldSpec, raw: str) -> tuple[object | None, str | None]:
    """Возвращает `(значение, ошибка)` — ровно одно из двух непусто (кроме
    случая, когда значение пустое и поле не обязательно — тогда оба `None`)."""
    value = raw.strip()
    if not value:
        if spec.required:
            return None, f"«{spec.label}» — обязательное поле"
        return None, None

    if spec.kind == "text":
        return value, None
    if spec.kind == "int":
        try:
            return int(float(value.replace(",", "."))), None
        except ValueError:
            return None, f"«{spec.label}» должно быть целым числом"
    if spec.kind == "decimal":
        try:
            return Decimal(value.replace(",", ".")), None
        except InvalidOperation:
            return None, f"«{spec.label}» должно быть числом"
    if spec.kind == "bool":
        lowered = value.lower()
        if lowered in ("1", "true", "да", "yes"):
            return True, None
        if lowered in ("0", "false", "нет", "no"):
            return False, None
        return None, f"«{spec.label}» должно быть да/нет"
    if spec.kind == "date":
        for fmt in _DATE_FORMATS:
            try:
                return dt.datetime.strptime(value, fmt).date(), None
            except ValueError:
                continue
        return None, f"«{spec.label}» — нераспознанный формат даты"
    if spec.kind == "email":
        try:
            return _EMAIL_ADAPTER.validate_python(value), None
        except PydanticValidationError:
            return None, f"«{spec.label}» — некорректный email"
    if spec.kind == "phone":
        normalized = normalize_phone_e164(value)
        if normalized is None:
            return None, f"«{spec.label}» — некорректный номер телефона"
        return normalized, None
    if spec.kind in ("inn", "kpp", "ogrn"):
        result = validate_requisite(spec.kind, value)
        if not result.ok:
            return None, result.reason
        return value, None
    if spec.kind == "org_type":
        allowed = {"university", "college", "company", "individual_entrepreneur"}
        if value not in allowed:
            return None, f"«{spec.label}»: допустимые значения — {', '.join(sorted(allowed))}"
        return value, None
    if spec.kind == "format":
        allowed = {"online", "offline", "blended"}
        if value not in allowed:
            return None, f"«{spec.label}»: допустимые значения — {', '.join(sorted(allowed))}"
        return value, None
    if spec.kind == "transfer_status":
        allowed = {"not_started", "in_progress", "transferred", "declined"}
        if value not in allowed:
            return None, f"«{spec.label}»: допустимые значения — {', '.join(sorted(allowed))}"
        return value, None
    if spec.kind in ("region_code", "direction_code", "organization_name"):
        # Существование проверяется в сервисе (нужен доступ к БД) — здесь
        # только формат непустой строки. `imports.service._extract_row`
        # перехватывает эти виды раньше `validate_field` (см. `_FK_TARGETS`),
        # так что эта ветка — тот же смысловой запасной путь, что уже был
        # у region_code/direction_code, а не отдельная логика.
        return value, None

    return None, f"Неизвестный тип поля: {spec.kind}"
