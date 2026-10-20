"""B5-3: настраиваемый маппинг «утверждённых полей» (Функц.5 ТЗ).

До этой задачи «утверждённость» зашита в код: словарь алиасов `lead_payload._ALIASES`. Этот
модуль добавляет второй, administratable слой НАД ним: администратор задаёт правила
«откуда переименовать и как преобразовать» в терминах источника (`lead_id`, `product_interest`,
...), а результат — словарь с уже КАНОНИЧЕСКИМИ именами целей (`first_name`, `phone`, `course`,
`custom_fields.<код>`, ...), который проходит обычный `parse_lead_payload` без изменений: правила
значений (нормализация email/телефона, лимиты длины, обязательность контакта) не дублируются
здесь — маппинг только переименовывает и элементарно преобразует.

Модуль чистый (без БД), как `lead_payload.py`: `validate_mapping` — единственная async-функция,
и только она обращается к сессии (проверка `custom_fields.<код>` против активных определений).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.integration.lead_payload import _canonical

# =============================================================================
# Реестр целей (раздел «утверждённые поля»)
# =============================================================================


@dataclass(frozen=True, slots=True)
class TargetSpec:
    code: str
    label: str


def _lead_targets() -> dict[str, TargetSpec]:
    """14 целей `lead_payload._ALIASES` — B5-4 добавляет туда же `organization_name`,
    `organization_inn`, `students_planned`, `lead_kind` (маршрутизация на B2B)."""
    labels: dict[str, str] = {
        "first_name": "Имя",
        "last_name": "Фамилия",
        "middle_name": "Отчество",
        "name": "ФИО целиком",
        "phone": "Телефон",
        "email": "Email",
        "course": "Курс/продукт",
        "comment": "Комментарий",
        "source_url": "Страница заявки",
        "order_number": "Номер заказа",
        "stream_number": "Номер потока",
        "amount": "Сумма",
        "external_id": "Внешний id заявки",
        "created_at": "Дата заявки на сайте",
        # B5-4: маршрутизация на B2B и на источник.
        "organization_name": "Название вуза",
        "organization_inn": "ИНН вуза",
        "students_planned": "Число учащихся",
        "lead_kind": "Тип заявки (для правил маршрутизации)",
    }
    return {code: TargetSpec(code=code, label=label) for code, label in labels.items()}


#: Цели по коду источника. Пока только `cms`: у LMS (B5-6) свой набор, заводится там же.
TARGETS: dict[str, dict[str, TargetSpec]] = {"cms": _lead_targets()}

#: Цели, хотя бы одна из которых обязана быть в правилах — иначе заявка без контакта будет
#: отклонена `parse_lead_payload` на каждой доставке (раздел «риски» B5-3).
_CONTACT_TARGETS = ("email", "phone")

_TRANSFORMS = ("none", "trim", "lower", "upper", "value_map", "template", "join", "constant")
_MAX_RULES = 50
_MAX_VALUE_MAP_PAIRS = 200
_MAX_VALUE_MAP_STR = 255
_MAX_TEMPLATE_LEN = 500


# =============================================================================
# Правило и маппинг
# =============================================================================


class MappingRule(BaseModel):
    target: str = Field(min_length=1, max_length=128)
    #: Путь через точку (вложенность на один уровень, например `contact.phone`); для `join` —
    #: `sources` вместо одного `source`.
    source: str | None = Field(default=None, max_length=255)
    sources: list[str] | None = Field(default=None, max_length=10)
    transform: Literal[
        "none", "trim", "lower", "upper", "value_map", "template", "join", "constant"
    ] = "none"
    required: bool = False
    default: Any = None
    value_map: dict[str, str] | None = None
    #: Единственная подстановка `{value}`, без вычислений — исполнение кода запрещено.
    template: str | None = Field(default=None, max_length=_MAX_TEMPLATE_LEN)
    constant: Any = None

    @model_validator(mode="after")
    def _shape_matches_transform(self) -> MappingRule:
        if self.transform == "join":
            if not self.sources or len(self.sources) < 2:
                raise ValueError("Для преобразования «склейка» нужно не меньше двух источников")
        elif self.transform == "constant":
            pass
        elif not self.source:
            raise ValueError("Нужен источник значения (кроме склейки и константы)")
        if self.transform == "value_map" and not self.value_map:
            raise ValueError("Для преобразования «таблица значений» нужен словарь соответствий")
        if self.transform == "template" and not self.template:
            raise ValueError("Для преобразования «шаблон» нужна строка шаблона")
        return self


class FieldMapping(BaseModel):
    version: int = 1
    mode: Literal["strict", "passthrough"] = "strict"
    rules: list[MappingRule] = Field(default_factory=list, max_length=_MAX_RULES)
    approved_by: uuid.UUID | None = None
    approved_at: dt.datetime | None = None


@dataclass(slots=True)
class MappingValidationError:
    field: str
    reason: str


@dataclass(slots=True)
class MappedRecord:
    payload: dict[str, Any]
    ignored_keys: list[str] = dataclass_field(default_factory=list)
    warnings: list[str] = dataclass_field(default_factory=list)


# =============================================================================
# Применение маппинга (без БД)
# =============================================================================


def _get_target(payload: dict[str, Any], target: str) -> Any:
    if "." in target:
        prefix, key = target.split(".", 1)
        return payload.get(prefix, {}).get(key)
    return payload.get(target)


def _set_target(payload: dict[str, Any], target: str, value: Any) -> None:
    if "." in target:
        prefix, key = target.split(".", 1)
        payload.setdefault(prefix, {})[key] = value
    else:
        payload[target] = value


def _eval_rule(
    rule: MappingRule, index: dict[str, Any], warnings: list[str]
) -> tuple[Any, set[str]]:
    """Значение правила и множество канонических ключей источника, которые оно «потребило»
    (режим `strict` отбрасывает всё, что ни одно правило не потребило)."""
    if rule.transform == "constant":
        return rule.constant, set()

    if rule.transform == "join":
        consumed: set[str] = set()
        parts: list[str] = []
        for src in rule.sources or ():
            canon = _canonical(src)
            consumed.add(canon)
            raw = index.get(canon)
            if raw not in (None, ""):
                parts.append(str(raw))
        return (" ".join(parts) if parts else rule.default), consumed

    canon = _canonical(rule.source or "")
    consumed = {canon}
    raw = index.get(canon)
    if raw in (None, ""):
        return rule.default, consumed

    value: Any = raw
    if rule.transform == "trim" and isinstance(value, str):
        value = value.strip()
    elif rule.transform == "lower" and isinstance(value, str):
        value = value.strip().lower()
    elif rule.transform == "upper" and isinstance(value, str):
        value = value.strip().upper()
    elif rule.transform == "value_map":
        key = str(value)
        mapped = (rule.value_map or {}).get(key)
        if mapped is None:
            warnings.append(f"{rule.target}: значение {key!r} не найдено в value_map")
            return rule.default, consumed
        value = mapped
    elif rule.transform == "template":
        value = (rule.template or "{value}").replace("{value}", str(value))
    return value, consumed


def apply_mapping(
    raw: dict[str, Any], mapping: FieldMapping | None, source_code: str
) -> MappedRecord:
    """`mapping=None` — прежнее поведение без изменений (администратор ничего не настраивал)."""
    del source_code  # зарезервировано: реестр целей сверяется в validate_mapping, не здесь
    if mapping is None:
        return MappedRecord(payload=dict(raw))

    index: dict[str, Any] = {}
    original_key: dict[str, str] = {}
    for key, value in raw.items():
        canon = _canonical(key)
        index[canon] = value
        original_key.setdefault(canon, str(key))

    used: set[str] = set()
    payload: dict[str, Any] = {}
    warnings: list[str] = []

    for rule in mapping.rules:
        # Цепочка запасных правил с одной целью: первое непустое значение побеждает, более
        # поздние правила для уже заполненной цели не трогают её.
        if _get_target(payload, rule.target) not in (None, ""):
            continue
        value, consumed = _eval_rule(rule, index, warnings)
        used |= consumed
        if value in (None, ""):
            if rule.required:
                warnings.append(f"Обязательное поле {rule.target} не заполнено")
            continue
        _set_target(payload, rule.target, value)

    if mapping.mode == "passthrough":
        for canon, value in index.items():
            if canon in used:
                continue
            payload.setdefault(original_key[canon], value)
        ignored: list[str] = []
    else:
        ignored = [original_key[canon] for canon in index if canon not in used]

    return MappedRecord(payload=payload, ignored_keys=ignored, warnings=warnings)


# =============================================================================
# Проверка при сохранении (нужна БД — только для custom_fields.<код>)
# =============================================================================


async def validate_mapping(
    session: AsyncSession, mapping: FieldMapping, source_code: str
) -> list[MappingValidationError]:
    errors: list[MappingValidationError] = []
    targets = TARGETS.get(source_code, {})

    if len(mapping.rules) > _MAX_RULES:
        errors.append(MappingValidationError("rules", f"не более {_MAX_RULES} правил"))

    custom_codes: set[str] = set()
    for index, rule in enumerate(mapping.rules):
        prefix = f"rules[{index}]"
        if rule.target.startswith("custom_fields."):
            code = rule.target.split(".", 1)[1]
            if not code:
                errors.append(
                    MappingValidationError(f"{prefix}.target", "пустой код custom_fields")
                )
            else:
                custom_codes.add(code)
        elif rule.target not in targets:
            errors.append(
                MappingValidationError(f"{prefix}.target", f"неизвестная цель: {rule.target}")
            )
        if rule.transform == "value_map" and rule.value_map:
            if len(rule.value_map) > _MAX_VALUE_MAP_PAIRS:
                errors.append(
                    MappingValidationError(
                        f"{prefix}.value_map", f"не более {_MAX_VALUE_MAP_PAIRS} пар"
                    )
                )
            for key, mapped in rule.value_map.items():
                if len(key) > _MAX_VALUE_MAP_STR or len(mapped) > _MAX_VALUE_MAP_STR:
                    errors.append(
                        MappingValidationError(
                            f"{prefix}.value_map", f"строки не длиннее {_MAX_VALUE_MAP_STR}"
                        )
                    )
                    break
        if rule.transform == "template" and rule.template:
            residual = rule.template.replace("{value}", "")
            if "{" in residual or "}" in residual:
                errors.append(
                    MappingValidationError(
                        f"{prefix}.template", "допустима только подстановка {value}"
                    )
                )

    target_codes = {rule.target for rule in mapping.rules}
    if not any(code in target_codes for code in _CONTACT_TARGETS):
        errors.append(
            MappingValidationError(
                "rules", "нужна цель email или phone — иначе любая заявка будет отклонена"
            )
        )

    if custom_codes:
        from app.modules.catalog.custom_fields import load_definitions

        definitions = await load_definitions(session, "deal")
        known = {definition.code for definition in definitions}
        for code in sorted(custom_codes - known):
            errors.append(
                MappingValidationError(
                    f"custom_fields.{code}",
                    "нет активного определения пользовательского поля для сделки",
                )
            )

    return errors
