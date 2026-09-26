"""Разбор тела вебхука сайта: JSON-объект → заявка (`LeadPayload`).

Одна и та же ручка принимает два «диалекта»: английские ключи CMS (`first_name`, `phone`,
`product_name`, `order_number`, …) и русские ключи выгрузки «Данные оплат» (`Номер заявки`,
`Курс`, `Фамилия`, `Имя`, `Отчество`, `Телефон`, `Email`, `Номер потока`, `Сумма`) — сайт шлёт
заказ тем же объектом, каким он лежит в выгрузке, и подгонять его под английские имена не нужно.
Ключи сравниваются без учёта регистра, пробелов, дефисов и подчёркиваний (`Номер заявки`,
`номер_заявки`, ` НОМЕР  ЗАЯВКИ ` — один ключ).

Модуль чистый (без БД): все проверки значений собираются в один `ValidationError` со списком
`errors[]` по полям, чтобы отправитель исправил всё за один заход, а не по ошибке за раз. Тело не
вставляется в JSONB как есть: `NaN`, `Infinity`, `\\u0000` и одиночные суррогаты Postgres не
принимает, и запрос падал бы 500 уже на сохранении «сырого» тела.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.errors import FieldError, ValidationError
from app.core.normalize import clean_text, normalize_email, normalize_phone, split_full_name
from app.modules.integration.json_safe import scrub_json, scrub_text

# Заглушки имени: у заявки «только телефон» имени нет, а контакту оно обязательно. Такой контакт
# помечается `external_ids.needs_normalization`, чтобы менеджер довёл карточку вручную.
NAMELESS_FIRST_NAME = "Без имени"
NAMELESS_LAST_NAME = "—"

_NAME_MAX = 128
_EMAIL_MAX = 254
_PHONE_MAX = 64
_COURSE_MAX = 255
_ORDER_NUMBER_MAX = 64
# Справочные текстовые поля (комментарий, страница, id заявки на сайте) обрезаются, а не отклоняют
# заявку: тело приходит с публичного сайта, и в карточку сделки не должна попасть простыня, но
# потерять лид из-за длинного комментария хуже, чем потерять его хвост (полное тело остаётся в
# журнале входящих).
_COMMENT_MAX = 4000
_SOURCE_URL_MAX = 2000
_EXTERNAL_ID_MAX = 255
_STREAM_MAX = 2_147_483_647  # int4: больше колонка не вместит
_AMOUNT_LIMIT = Decimal(10) ** 12  # numeric(14, 2)
_CENT = Decimal("0.01")

# Лид — плоский объект; глубокая вложенность — признак мусора и риск для рекурсивных обходов.
_MAX_DEPTH = 16

_SEPARATORS = re.compile(r"[\s\-_]+")
_ANY_SPACE = re.compile(r"\s+")

_GENERIC_DETAIL = "Некорректные данные заявки"
_CONTACT_REQUIRED = "Укажите email или телефон"
_COURSE_REQUIRED = "Для заказа укажите курс"
_REQUIRED = "обязательное поле"


def _canonical(key: object) -> str:
    """Ключ в сравнимом виде: `Номер  заявки`, `номер_заявки` и `НОМЕР-ЗАЯВКИ` совпадают."""
    text = unicodedata.normalize("NFKC", str(key)).strip().casefold().replace("ё", "е")
    return _SEPARATORS.sub("_", text)


# Целевое поле → допустимые ключи в порядке предпочтения (первый непустой побеждает).
_ALIASES: dict[str, tuple[str, ...]] = {
    "first_name": ("first_name", "имя"),
    "last_name": ("last_name", "фамилия"),
    "middle_name": ("middle_name", "отчество"),
    "name": ("name", "full_name", "фио"),
    "phone": ("phone", "телефон"),
    "email": ("email", "e-mail", "почта"),
    # Название раньше кода: человекочитаемое значение годится и для заголовка сделки.
    "course": ("курс", "course", "product_name", "program_code", "product_code"),
    "comment": ("comment", "комментарий"),
    "source_url": ("source_url",),
    "order_number": ("order_number", "order_id", "номер заявки"),
    "stream_number": ("stream_number", "номер потока"),
    "amount": ("amount", "сумма"),
    "external_id": ("external_id",),
    "created_at": ("created_at",),
}
_CANONICAL_ALIASES = {
    field: tuple(_canonical(alias) for alias in aliases) for field, aliases in _ALIASES.items()
}


@dataclass(frozen=True, slots=True)
class LeadPayload:
    """Заявка после разбора: значения нормализованы (email — нижний регистр, телефон — E.164)."""

    first_name: str | None = None
    last_name: str | None = None
    middle_name: str | None = None
    email: str | None = None
    phone: str | None = None
    #: Курс/продукт как его назвал отправитель; `course_candidates` — все названия и коды, что
    #: пришли в теле (по ним ищется продукт каталога).
    course: str | None = None
    course_candidates: tuple[str, ...] = ()
    comment: str | None = None
    source_url: str | None = None
    order_number: str | None = None
    stream_number: int | None = None
    amount: Decimal | None = None
    external_id: str | None = None
    created_at: dt.datetime | None = None

    @property
    def is_order(self) -> bool:
        return self.order_number is not None

    def lead_names(self) -> tuple[str, str, str | None, bool]:
        """Имя, фамилия и отчество для контакта-лида; недостающее заменяется заглушкой. Последний
        элемент — заглушка использована (контакт надо довести вручную)."""
        first = self.first_name or NAMELESS_FIRST_NAME
        last = self.last_name or NAMELESS_LAST_NAME
        placeholder = not (self.first_name and self.last_name)
        return first, last, self.middle_name, placeholder


# --- Тело запроса → JSON-объект ---------------------------------------------------------


def _reject_constant(name: str) -> Any:
    raise ValueError(f"недопустимое значение {name}")


def _parse_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):  # `1e999` разбирается в `inf`
        raise ValueError("число вне диапазона")
    return value


def _too_deep(value: Any, limit: int) -> bool:
    """Больше `limit` вложенных объектов/массивов? Обход без рекурсии: рекурсивные функции ниже
    (`scrub_json`, вставка в JSONB) должны получать только уже проверенную глубину."""
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, level = stack.pop()
        if level > limit:
            return True
        children = current.values() if isinstance(current, dict) else current
        stack.extend((child, level + 1) for child in children if isinstance(child, dict | list))
    return False


def parse_json_object(raw_body: bytes) -> dict[str, Any]:
    """Тело запроса → JSON-объект. Любое иное тело (пустое, `null`, массив, строка, число, битый
    JSON, не UTF-8) — 422, а не 500: подпись уже проверена, значит, ошибку допустил отправитель."""
    try:
        text = raw_body.decode("utf-8-sig")
        value = json.loads(text, parse_constant=_reject_constant, parse_float=_parse_float)
    except (ValueError, RecursionError) as exc:
        raise ValidationError("Тело запроса — невалидный JSON") from exc
    if not isinstance(value, dict):
        raise ValidationError("Тело запроса должно быть JSON-объектом")
    if _too_deep(value, _MAX_DEPTH):
        raise ValidationError("Слишком глубокая вложенность в теле запроса")
    cleaned: dict[str, Any] = scrub_json(value)
    return cleaned


def raw_evidence(raw_body: bytes, *, limit: int = 8192) -> dict[str, Any]:
    """«Сырое» тело для журнала входящих, когда разбирать его нельзя или не нужно (неверная
    подпись, битый JSON): начало текста, размер и признак обрезки. Ограничение по размеру нужно
    потому, что неверную подпись может прислать кто угодно — журнал не должен пухнуть от чужого
    мусора."""
    head = raw_body[:limit].decode("utf-8", errors="replace")
    return {
        "_raw": scrub_text(head),
        "_size_bytes": len(raw_body),
        "_truncated": len(raw_body) > limit,
    }


# --- JSON-объект → заявка ---------------------------------------------------------------


class _FieldProblem(Exception):
    """Значение поля непригодно; текст — причина для `errors[]`."""


def _is_present(value: Any) -> bool:
    return value is not None and not (isinstance(value, str) and not value.strip())


def _as_text(value: Any, *, allow_number: bool) -> str:
    if isinstance(value, str):
        return value
    if allow_number and isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if allow_number and isinstance(value, float) and value.is_integer():
        # Телефон и номер заказа числом: `79990234365` или `7.9990234365e10`, но не `2.5`.
        return str(int(value))
    raise _FieldProblem("ожидалась строка или число" if allow_number else "ожидалась строка")


def _parse_stream_number(value: Any) -> int:
    if isinstance(value, bool):
        raise _FieldProblem("номер потока — целое число")
    number: int
    if isinstance(value, int):
        number = value
    else:
        try:
            parsed = float(value.strip()) if isinstance(value, str) else float(value)
        except (TypeError, ValueError, OverflowError) as exc:
            raise _FieldProblem("номер потока — целое число") from exc
        if not parsed.is_integer():  # 2.5, inf, nan
            raise _FieldProblem("номер потока — целое число")
        number = int(parsed)
    if number < 1:
        raise _FieldProblem("номер потока — целое от 1")
    if number > _STREAM_MAX:
        raise _FieldProblem("слишком большой номер потока")
    return number


def _parse_amount(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise _FieldProblem("сумма — число")
    if isinstance(value, int | float):
        text = repr(value)  # `1500.5`, без двоичных «хвостов» `Decimal(float)`
    elif isinstance(value, str):
        text = _ANY_SPACE.sub("", value)  # «15 000,50»: пробелы (в т.ч. неразрывные) — тысячи
        if "," in text and "." in text:
            # Последний из двух разделителей десятичный, другой отделяет тысячи.
            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", "")
        else:
            text = text.replace(",", ".")
    else:
        raise _FieldProblem("сумма — число")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise _FieldProblem("сумма — число") from exc
    if not number.is_finite() or number <= 0:
        raise _FieldProblem("сумма должна быть положительным числом")
    if number >= _AMOUNT_LIMIT:
        raise _FieldProblem("слишком большая сумма")
    cents = number.quantize(_CENT)
    if cents <= 0:  # 0.001 округляется до нуля
        raise _FieldProblem("сумма должна быть положительным числом")
    return cents


def _parse_created_at(value: Any) -> dt.datetime | None:
    """Время заявки на сайте нужно лишь для справки в комментарии, поэтому непонятный формат не
    повод отклонять заявку — он просто игнорируется."""
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def parse_lead_payload(payload: Mapping[str, Any]) -> LeadPayload:
    """Значения тела → `LeadPayload` либо `ValidationError` со всеми найденными проблемами.

    Правила: нужен email или телефон (по ним ищется человек, без них заявка — «Без имени —», которую
    не с кем связать); формат email и телефона проверяется, а не молча отбрасывается; имя у лида
    необязательно (`LeadPayload.lead_names`), у заказа — обязательно вместе с курсом."""
    index: dict[str, list[Any]] = {}
    for key, value in payload.items():
        index.setdefault(_canonical(key), []).append(value)

    errors: list[FieldError] = []

    def fail(field: str, reason: str) -> None:
        errors.append(FieldError(field=field, reason=reason))

    def failed(field: str) -> bool:
        return any(error.field == field for error in errors)

    def present(field: str) -> list[Any]:
        return [
            value
            for alias in _CANONICAL_ALIASES[field]
            for value in index.get(alias, ())
            if _is_present(value)
        ]

    def pick(field: str) -> Any:
        found = present(field)
        return found[0] if found else None

    def pick_text(
        field: str, *, limit: int, allow_number: bool = False, informational: bool = False
    ) -> str | None:
        """`informational` — справочное поле: странный тип игнорируется, длинное обрезается."""
        raw = pick(field)
        if raw is None:
            return None
        try:
            value = clean_text(_as_text(raw, allow_number=allow_number or informational))
        except _FieldProblem as problem:
            if not informational:
                fail(field, str(problem))
            return None
        if value is not None and len(value) > limit:
            if not informational:
                fail(field, f"не длиннее {limit} символов")
                return None
            value = value[:limit]
        return value

    first_name = pick_text("first_name", limit=_NAME_MAX)
    last_name = pick_text("last_name", limit=_NAME_MAX)
    middle_name = pick_text("middle_name", limit=_NAME_MAX)
    full_name = pick_text("name", limit=3 * _NAME_MAX)
    if full_name and not (first_name and last_name and middle_name):
        # Одна строка `ФИО` дополняет только недостающие части: явные поля важнее.
        split_last, split_first, split_middle = split_full_name(full_name)
        last_name = last_name or split_last
        first_name = first_name or split_first
        middle_name = middle_name or split_middle
        for field, value in (
            ("last_name", last_name),
            ("first_name", first_name),
            ("middle_name", middle_name),
        ):
            if value is not None and len(value) > _NAME_MAX:
                fail(field, f"не длиннее {_NAME_MAX} символов")

    email: str | None = None
    raw_email = pick_text("email", limit=_EMAIL_MAX)
    if raw_email is not None:
        email = normalize_email(raw_email)
        if email is None:
            fail("email", "некорректный адрес электронной почты")

    phone: str | None = None
    raw_phone = pick_text("phone", limit=_PHONE_MAX, allow_number=True)
    if raw_phone is not None:
        phone = normalize_phone(raw_phone)
        if phone is None:
            fail("phone", "некорректный номер телефона")

    candidates: list[str] = []
    for raw in present("course"):
        try:
            candidate = clean_text(_as_text(raw, allow_number=False))
        except _FieldProblem as problem:
            fail("course", str(problem))
            continue
        if candidate is None or candidate in candidates:
            continue
        if len(candidate) > _COURSE_MAX:
            fail("course", f"не длиннее {_COURSE_MAX} символов")
            continue
        candidates.append(candidate)
    course = candidates[0] if candidates else None

    order_number = pick_text("order_number", limit=_ORDER_NUMBER_MAX, allow_number=True)
    external_id = pick_text("external_id", limit=_EXTERNAL_ID_MAX, informational=True)
    comment = pick_text("comment", limit=_COMMENT_MAX, informational=True)
    source_url = pick_text("source_url", limit=_SOURCE_URL_MAX, informational=True)

    stream_number: int | None = None
    raw_stream = pick("stream_number")
    if raw_stream is not None:
        try:
            stream_number = _parse_stream_number(raw_stream)
        except _FieldProblem as problem:
            fail("stream_number", str(problem))

    amount: Decimal | None = None
    raw_amount = pick("amount")
    if raw_amount is not None:
        try:
            amount = _parse_amount(raw_amount)
        except _FieldProblem as problem:
            fail("amount", str(problem))

    if not email and not phone and not failed("email") and not failed("phone"):
        fail("email", _CONTACT_REQUIRED)
        fail("phone", _CONTACT_REQUIRED)

    if order_number is not None:
        if course is None and not failed("course"):
            fail("course", _COURSE_REQUIRED)
        # Заказ — это оплаченный курс конкретного человека: без имени сделку не завести.
        if not last_name and not failed("last_name"):
            fail("last_name", _REQUIRED)
        if not first_name and not failed("first_name"):
            fail("first_name", _REQUIRED)

    if errors:
        # Причина одна («укажите курс», «укажите email или телефон») — она же и текст ошибки.
        reasons = {error.reason for error in errors}
        raise ValidationError(next(iter(reasons)) if len(reasons) == 1 else _GENERIC_DETAIL, errors)

    return LeadPayload(
        first_name=first_name,
        last_name=last_name,
        middle_name=middle_name,
        email=email,
        phone=phone,
        course=course,
        course_candidates=tuple(candidates),
        comment=comment,
        source_url=source_url,
        order_number=order_number,
        stream_number=stream_number,
        amount=amount,
        external_id=external_id,
        created_at=_parse_created_at(pick("created_at")),
    )
