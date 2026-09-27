"""Проверка значений пользовательских полей по их определениям (`custom_field_defs`).

Определения (тип, варианты, границы, обязательность) раньше только отдавались интерфейсу:
сервер принимал в `custom_fields` что угодно, и мимо формы (скрипт, импорт, чужой клиент)
в карточку попадали числа-строки, даты «завтра» и значения вне списка выбора.

Правила выбраны так, чтобы ничего из уже работающего не ломать:

* ключи без определения (и с выключенным определением) сохраняются как есть — так живут
  служебные поля воронок (`park_reason`, `contract_number`, `auto_created`);
* `null` — «сбросить значение», допустим всегда, кроме обязательного поля;
* «обязательно» проверяется только при создании и только для активных определений этой
  сущности (для сделки — общих или привязанных к её воронке); при PATCH — лишь если ключ
  обязательного поля прислан пустым. Старые записи без такого значения редактировать
  остальными полями по-прежнему можно;
* `bool` обязательным не считается: `false` — осмысленное значение (как и в форме).

Код общий для сделок, организаций и продуктов; ошибки собираются разом в `errors[]`
с `field = custom_fields.<код>`."""

from __future__ import annotations

import datetime as dt
import math
import re
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import FieldError, ValidationError
from app.modules.catalog.models import CustomFieldDef

_LIST_TYPES = frozenset({"select", "multiselect"})


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or (isinstance(value, list) and not value)


def _choices(definition: CustomFieldDef) -> list[str] | None:
    """Допустимые варианты: `options.choices`; нет списка — любой вариант (не проверяем)."""
    options = definition.options
    raw = options.get("choices") if isinstance(options, dict) else None
    if not isinstance(raw, list) or not raw:
        return None
    return [str(item) for item in raw]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _check_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        dt.date.fromisoformat(value[:10]) if len(value) == 10 else dt.datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def _check_value(definition: CustomFieldDef, value: Any) -> str | None:
    """Причина отказа для одного значения или None. Пустое значение сюда не попадает."""
    rules = definition.validation if isinstance(definition.validation, dict) else {}
    kind = definition.field_type
    if kind == "string":
        if not isinstance(value, str):
            return "ожидается строка"
        max_length = rules.get("max_length")
        if (
            isinstance(max_length, int)
            and not isinstance(max_length, bool)
            and (len(value) > max_length)
        ):
            return f"не длиннее {max_length} символов"
        pattern = rules.get("pattern")
        if isinstance(pattern, str) and pattern:
            try:
                matches = re.search(pattern, value) is not None
            except re.error:
                matches = True  # некорректное выражение в определении не должно блокировать ввод
            if not matches:
                return "значение не подходит по формату"
    elif kind == "number":
        number = _number(value)
        if number is None:
            return "ожидается число"
        low, high = rules.get("min"), rules.get("max")
        if isinstance(low, int | float) and not isinstance(low, bool) and number < low:
            return f"не меньше {low}"
        if isinstance(high, int | float) and not isinstance(high, bool) and number > high:
            return f"не больше {high}"
    elif kind == "date":
        if not _check_date(value):
            return "ожидается дата в формате ГГГГ-ММ-ДД"
    elif kind == "bool":
        if not isinstance(value, bool):
            return "ожидается true или false"
    elif kind in _LIST_TYPES:
        allowed = _choices(definition)
        if kind == "select":
            if not isinstance(value, str):
                return "ожидается один из вариантов"
            items: list[Any] = [value]
        else:
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                return "ожидается список вариантов"
            items = value
        if allowed is not None:
            unknown = [item for item in items if item not in allowed]
            if unknown:
                return f"недопустимое значение: {', '.join(map(str, unknown))}"
    # `file` и любые будущие типы: ссылка на файл проверяется там, где файл привязывается.
    return None


async def load_definitions(
    session: AsyncSession, entity_type: str, *, workflow_id: uuid.UUID | None = None
) -> list[CustomFieldDef]:
    """Активные определения сущности. Для сделки — общие и привязанные к её воронке."""
    stmt = select(CustomFieldDef).where(
        CustomFieldDef.entity_type == entity_type, CustomFieldDef.is_active.is_(True)
    )
    if entity_type == "deal":
        stmt = stmt.where(
            or_(CustomFieldDef.workflow_id.is_(None), CustomFieldDef.workflow_id == workflow_id)
        )
    return list((await session.execute(stmt)).scalars().all())


def check_values(
    definitions: Iterable[CustomFieldDef],
    values: Mapping[str, Any],
    *,
    creating: bool,
    check_required: bool = True,
) -> list[FieldError]:
    """Ошибки значений по определениям. Не бросает — вызывающий решает, что делать.

    `creating` — отсутствие обязательного поля тоже ошибка; иначе обязательное поле
    проверяется, только если ключ прислан (и пуст). `check_required=False` — только тип/границы."""
    errors: list[FieldError] = []
    for definition in definitions:
        present = definition.code in values
        value = values.get(definition.code)
        empty = _is_empty(value)
        mandatory = check_required and definition.is_required and definition.field_type != "bool"
        if mandatory and empty and (present or creating):
            errors.append(
                FieldError(
                    field=f"custom_fields.{definition.code}",
                    reason=f"«{definition.label}»: обязательное поле",
                )
            )
        elif present and not empty:
            reason = _check_value(definition, value)
            if reason is not None:
                errors.append(
                    FieldError(
                        field=f"custom_fields.{definition.code}",
                        reason=f"«{definition.label}»: {reason}",
                    )
                )
    return errors


async def validate_custom_fields(
    session: AsyncSession,
    entity_type: str,
    values: Mapping[str, Any] | None,
    *,
    creating: bool,
    workflow_id: uuid.UUID | None = None,
    check_required: bool = True,
) -> None:
    """Бросает 422 (`ValidationError`), если значения нарушают определения.

    `creating` — проверить и обязательные поля, которых во входе нет.
    `check_required=False` — только тип/границы (переходы воронки: обязательность там задаёт
    сама воронка через `required_fields`, а не определения полей)."""
    supplied = values or {}
    if not supplied and not (creating and check_required):
        return  # нечего проверять — и запрос за определениями не нужен
    definitions = await load_definitions(session, entity_type, workflow_id=workflow_id)
    errors = check_values(definitions, supplied, creating=creating, check_required=check_required)
    if errors:
        raise ValidationError("Значения пользовательских полей не прошли проверку", errors)
