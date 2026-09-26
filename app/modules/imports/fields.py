"""Описание целевых полей импорта по типу сущности (new_spec §4.12).

Каталожные типы — `organization`, `product`, `license` (реестр вузов, курсы, лицензии/договоры).
Три типа о людях приходят файлами заказчика и порождают по несколько записей на строку:

* `vendor_contact` — «Вендоры»: компания-вендор, её продукты, ответственный человек;
* `payment` — «Данные оплат»: оплаченный заказ физлица на курс (JSON с сайта);
* `learner` — «Загрузка пользователей»: шаблон учащихся LMS (30 колонок).

Контакты — ПДн (раздел 5.2), и раньше их массовый импорт сознательно не заводили: он требовал
решения про согласия прямо в момент bulk-вставки. Здесь основание другое и явное: вендорский
каталог — рабочие контакты по договору, оплаты — исполнение договора оферты (152-ФЗ, ст. 6 ч. 1
п. 5), шаблон LMS — данные самого обучающегося; все три файла загружает руководитель или
администратор (`import:run`), а значения ПДн не попадают ни в аудит, ни в логи (`core.masking`).
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import EmailStr, TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from app.core.normalize import normalize_phone, parse_contact_methods, split_list
from app.modules.catalog import learner
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
    # Файлы о людях.
    "person_name",  # «Иванов Иван Иванович» одной ячейкой — три поля в `_extract_row`
    "product_list",  # несколько значений в одной ячейке: «RT.DataLake», «RT.Warehouse»
    "contact_methods",  # «Почта, Чат в ТГ» — о нераспознанном предупреждает `_extract_row`
    "stream",  # номер потока курса: целое от 1
    "money",  # сумма: положительное конечное число
    "currency",  # трёхбуквенный код валюты
    "learner",  # поле шаблона LMS, разбор — `catalog.learner.parse_profile_value`
]

_EMAIL_ADAPTER: TypeAdapter[str] = TypeAdapter(EmailStr)
_DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y")
_INT_LIMIT = 2_147_483_647  # INTEGER в Postgres
_MONEY_LIMIT = Decimal("1e12")  # NUMERIC(14, 2)
_THOUSANDS = re.compile(r"[\s ]")


@dataclass(slots=True, frozen=True)
class FieldSpec:
    target: str
    label: str
    kind: FieldKind
    required: bool = False
    # Длина колонки в БД: слишком длинное значение — ошибка строки при проверке, а не падение
    # применения на «отравленной» строке.
    max_length: int | None = None


ORGANIZATION_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("name", "Наименование", "text", required=True, max_length=512),
    FieldSpec("short_name", "Краткое наименование", "text", max_length=255),
    FieldSpec("org_type", "Тип организации", "org_type"),
    FieldSpec("inn", "ИНН", "inn"),
    FieldSpec("kpp", "КПП", "kpp"),
    FieldSpec("ogrn", "ОГРН", "ogrn"),
    FieldSpec("legal_address", "Юридический адрес", "text"),
    FieldSpec("actual_address", "Фактический адрес", "text"),
    FieldSpec("region_code", "Код региона", "region_code"),
    FieldSpec("website", "Сайт", "text", max_length=255),
    FieldSpec("main_phone", "Телефон", "phone"),
    FieldSpec("main_email", "Email", "email"),
    FieldSpec("students_count", "Количество студентов", "int"),
)

PRODUCT_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("code", "Код", "text", required=True, max_length=64),
    FieldSpec("name", "Наименование", "text", required=True, max_length=255),
    FieldSpec("description", "Описание", "text"),
    FieldSpec("direction_code", "Код направления", "direction_code"),
    FieldSpec("duration_hours", "Длительность, часы", "int"),
    FieldSpec("format", "Формат", "format"),
    FieldSpec("base_price", "Цена", "decimal"),
    FieldSpec("currency", "Валюта", "currency"),
)

# П3 (rtk_requiriments.md разд. 4, Треб.1): лицензии/договоры вуз↔вендор↔ПО.
# Порядок и названия полей — буквально из списка кейса. `organization_name`
# резолвится в `organization_id` (см. докстринг kind'а выше);
# `contract_number` — natural key импорта (см. `catalog.models.
# OrganizationLicense`, докстринг — почему не составной ключ).
LICENSE_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("organization_name", "Название ВУЗа", "organization_name", required=True),
    FieldSpec("vendor", "Вендор", "text", required=True, max_length=255),
    FieldSpec("product_name", "ПО", "text", required=True, max_length=255),
    FieldSpec("contract_number", "Номер договора", "text", required=True, max_length=128),
    FieldSpec("license_signed_at", "Подписание лицензии", "date"),
    FieldSpec("license_valid_year", "Срок действия лицензии (год)", "int"),
    FieldSpec("transfer_status", "Статус по передаче", "transfer_status"),
    FieldSpec("manager_full_name", "ФИО Менеджера", "text", max_length=255),
    FieldSpec("responsible_contacts", "Ответственные от ВУЗа", "text"),
    FieldSpec("comment", "Комментарий", "text"),
)

# «Вендоры.xlsx»: Компания | Продукт | ФИО | Телефон | Почта | Способ связи.
VENDOR_CONTACT_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("vendor_name", "Компания", "text", required=True, max_length=512),
    FieldSpec("product_names", "Продукт", "product_list"),
    FieldSpec("full_name", "ФИО", "person_name"),
    FieldSpec("last_name", "Фамилия", "text", max_length=128),
    FieldSpec("first_name", "Имя", "text", max_length=128),
    FieldSpec("middle_name", "Отчество", "text", max_length=128),
    FieldSpec("phone", "Телефон", "phone"),
    FieldSpec("email", "Почта", "email"),
    FieldSpec("contact_methods", "Способ связи", "contact_methods"),
)

# «Данные оплат.json»: Номер заявки, Курс, Фамилия, Имя, Отчество, Телефон, Email, Номер потока.
PAYMENT_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("order_number", "Номер заявки", "text", required=True, max_length=64),
    FieldSpec("product_name", "Курс", "text", required=True, max_length=255),
    FieldSpec("full_name", "ФИО", "person_name"),
    FieldSpec("last_name", "Фамилия", "text", max_length=128),
    FieldSpec("first_name", "Имя", "text", max_length=128),
    FieldSpec("middle_name", "Отчество", "text", max_length=128),
    FieldSpec("phone", "Телефон", "phone"),
    FieldSpec("email", "Email", "email"),
    FieldSpec("stream_number", "Номер потока", "stream"),
    FieldSpec("amount", "Сумма", "money"),
    FieldSpec("currency", "Валюта", "currency"),
)

# «Загрузка пользователей.xlsx»: 30 колонок шаблона LMS (заголовки и порядок — `catalog.learner`).
_LEARNER_KINDS: dict[str, FieldKind] = {"phone": "phone", "email": "email"}
LEARNER_FIELDS: tuple[FieldSpec, ...] = tuple(
    FieldSpec(
        target,
        header,
        _LEARNER_KINDS.get(target, "text" if target in learner.CONTACT_TARGETS else "learner"),
        max_length=128 if target in learner.CONTACT_TARGETS else None,
    )
    for target, header in learner.LMS_USER_COLUMNS
)

_ENTITY_FIELDS: dict[str, tuple[FieldSpec, ...]] = {
    "organization": ORGANIZATION_FIELDS,
    "product": PRODUCT_FIELDS,
    "license": LICENSE_FIELDS,
    "vendor_contact": VENDOR_CONTACT_FIELDS,
    "payment": PAYMENT_FIELDS,
    "learner": LEARNER_FIELDS,
}
_NATURAL_KEYS: dict[str, str] = {
    "organization": "inn",
    "product": "code",
    "license": "contract_number",
}

# Типы, строка которых порождает несколько записей и применяется обработчиком
# (`imports.handlers`), а не `_apply_*_row` общего сервиса.
HANDLED_ENTITY_TYPES: frozenset[str] = frozenset({"vendor_contact", "payment", "learner"})

# Что обязательно присутствовать в маппинге, чтобы строка вообще могла быть применена. Кроме
# `required=True` полей: «ФИО или фамилия+имя», «email или телефон» — это условия «или», их одним
# флагом поля не выразить.
_NAME_GROUP = ("full_name", ("last_name", "first_name"))
_CONTACT_GROUP = ("email", "phone")


def fields_for(entity_type: str) -> tuple[FieldSpec, ...]:
    fields = _ENTITY_FIELDS.get(entity_type)
    if fields is None:
        raise ValueError(f"Неизвестный тип сущности импорта: {entity_type!r}")
    return fields


def natural_key_for(entity_type: str) -> str:
    return _NATURAL_KEYS[entity_type]


_NAME_REQUIREMENT = "ФИО (или Фамилия и Имя)"
_CONTACT_REQUIREMENT = "Email или Телефон"


def requirements_for(entity_type: str) -> list[str]:
    """Что обязательно должно быть в маппинге, человекочитаемо (для окна сопоставления колонок)."""
    by_target = {f.target: f for f in fields_for(entity_type)}
    required = [f.label for f in by_target.values() if f.required]
    key = _NATURAL_KEYS.get(entity_type)
    if key is not None and by_target[key].label not in required:
        required.append(by_target[key].label)
    if entity_type in HANDLED_ENTITY_TYPES:
        required += [_NAME_REQUIREMENT, _CONTACT_REQUIREMENT]
    return required


def missing_mapping_labels(entity_type: str, mapped_targets: set[str]) -> list[str]:
    """Названия того, чего не хватает в маппинге; пусто — маппинг достаточен для применения.

    Раньше проверялось только ключевое поле: несмапленное обязательное поле давало строки без
    `organization_id`, и применение падало на первой же — задание зависало навсегда.
    """
    by_target = {f.target: f for f in fields_for(entity_type)}
    missing = [f.label for f in by_target.values() if f.required and f.target not in mapped_targets]
    key = _NATURAL_KEYS.get(entity_type)
    if key is not None and key not in mapped_targets and by_target[key].label not in missing:
        missing.append(by_target[key].label)
    if entity_type in HANDLED_ENTITY_TYPES:
        if not (
            _NAME_GROUP[0] in mapped_targets or all(t in mapped_targets for t in _NAME_GROUP[1])
        ):
            missing.append(_NAME_REQUIREMENT)
        if not any(t in mapped_targets for t in _CONTACT_GROUP):
            missing.append(_CONTACT_REQUIREMENT)
    return missing


ENTITY_TYPE_LABELS: dict[str, str] = {
    "organization": "Организации (реестр вузов)",
    "product": "Продукты и курсы",
    "license": "Лицензии и договоры вуз — вендор — ПО",
    "vendor_contact": "Вендоры и ответственные за продукты",
    "payment": "Оплаты (заказы физлиц на курсы)",
    "learner": "Учащиеся LMS (шаблон «Загрузка пользователей»)",
}


def normalize_phone_e164(value: str) -> str | None:
    """Раздел 4.12: «телефон — нормализация к E.164». Вся логика — в `core.normalize`, чтобы импорт,
    вебхук и ручной ввод давали один и тот же ключ."""
    return normalize_phone(value)


def _parse_number(value: str, label: str, what: str) -> tuple[Decimal | None, str | None]:
    try:
        number = Decimal(_THOUSANDS.sub("", value).replace(",", "."))
    except InvalidOperation:
        return None, f"«{label}» должно быть {what}"
    if not number.is_finite():
        return None, f"«{label}» должно быть {what}"
    return number, None


def validate_field(spec: FieldSpec, raw: str) -> tuple[object | None, str | None]:
    """Возвращает `(значение, ошибка)` — ровно одно из двух непусто (кроме
    случая, когда значение пустое и поле не обязательно — тогда оба `None`)."""
    value = raw.strip()
    if not value:
        if spec.required:
            return None, f"«{spec.label}» — обязательное поле"
        return None, None
    if spec.max_length is not None and len(value) > spec.max_length:
        return None, f"«{spec.label}»: не больше {spec.max_length} символов"

    if spec.kind == "text":
        return value, None
    if spec.kind == "person_name":
        return " ".join(value.split()), None
    if spec.kind == "int":
        number, error = _parse_number(value, spec.label, "целым числом")
        if error or number is None:
            return None, error
        if number != number.to_integral_value() or abs(number) > _INT_LIMIT:
            return None, f"«{spec.label}» должно быть целым числом"
        return int(number), None
    if spec.kind == "stream":
        number, error = _parse_number(value, spec.label, "целым числом")
        if error or number is None:
            return None, error
        if number != number.to_integral_value() or not 1 <= number <= 10_000:
            return None, f"«{spec.label}» — целое число от 1"
        return int(number), None
    if spec.kind == "decimal":
        number, error = _parse_number(value, spec.label, "числом")
        if error or number is None:
            return None, error
        if abs(number) >= _MONEY_LIMIT:
            return None, f"«{spec.label}»: слишком большое значение"
        return number, None
    if spec.kind == "money":
        number, error = _parse_number(value, spec.label, "числом")
        if error or number is None:
            return None, error
        if number <= 0 or number >= _MONEY_LIMIT:
            return None, f"«{spec.label}» должна быть больше нуля"
        return number, None
    if spec.kind == "currency":
        code = value.upper()
        if not re.fullmatch(r"[A-Z]{3}", code):
            return None, f"«{spec.label}» — трёхбуквенный код валюты (RUB, USD)"
        return code, None
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
            return _EMAIL_ADAPTER.validate_python(value).lower(), None
        except PydanticValidationError:
            return None, f"«{spec.label}» — некорректный email"
    if spec.kind == "phone":
        normalized = normalize_phone(value)
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
    if spec.kind == "product_list":
        return split_list(value), None
    if spec.kind == "contact_methods":
        methods, _unknown = parse_contact_methods(value)
        return methods, None
    if spec.kind == "learner":
        try:
            return learner.parse_profile_value(spec.target, value), None
        except ValueError as exc:
            return None, f"«{spec.label}»: {exc}"
    if spec.kind in ("region_code", "direction_code", "organization_name"):
        # Существование проверяется в сервисе (нужен доступ к БД) — здесь
        # только формат непустой строки. `imports.service._extract_row`
        # перехватывает эти виды раньше `validate_field` (см. `_FK_TARGETS`),
        # так что эта ветка — тот же смысловой запасной путь, что уже был
        # у region_code/direction_code, а не отдельная логика.
        return value, None

    return None, f"Неизвестный тип поля: {spec.kind}"
