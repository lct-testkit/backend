"""Разбор человеческих значений файла лицензий: статус по-русски, «да/нет»,
срок действия, даты (B3-3, спринт B3, BUG-10).

Реальный файл заказчика пишет «Передано», «В работе», «да», «нет», «2027» —
прежний импорт принимал только английские коды (`not_started`,
`in_progress`, …) и три формата даты, поэтому такой файл отклонялся почти
на каждой строке. Этот модуль учит три поля:

* «Статус по передаче» — `parse_transfer_status`: английские коды (как и
  раньше) плюс русские синонимы, с подсказкой по расстоянию Левенштейна на
  неизвестном значении.
* «Подписание лицензии» — `parse_signed`: «да»/«нет»/дата (включая
  серийный номер Excel и текстовый месяц) вместо только даты.
* «Срок действия лицензии (год)» — `parse_term`: в кейсе колонка названа
  «(год)», но в примерах файла встречаются и «3» (срок в годах), и «2027»
  (год окончания), и диапазон «2024-2027» — функция принимает оба
  толкования (решение зафиксировано в отчёте по эпику, вопрос владельцу
  остаётся открытым).

Используется и импортом (`imports.fields`, виды `license_signed`/
`license_term`/обновлённый `transfer_status`), и API лицензий (B3-6) — один
и тот же разбор для файла и для ручного ввода через форму.

`_levenshtein` здесь — отдельная копия (а не импорт `imports.mapping.
_levenshtein`): `catalog` не должен зависеть от `imports` (обратное —
`imports` уже зависит от `catalog`), иначе `.importlinter` поймал бы цикл.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass, field

_SPACES = re.compile(r"\s+")
_TRAILING_DOT = re.compile(r"\.$")

# Серийные даты Excel (базовая дата 1899-12-30, см. parsing.py): диапазон
# ограничен годами 2000..2100, чтобы случайное маленькое число («5» — срок в
# годах) не приняли за дату 1900 года.
_EXCEL_EPOCH = dt.date(1899, 12, 30)
_EXCEL_SERIAL_MIN = 36526  # 2000-01-01
_EXCEL_SERIAL_MAX = 73050  # 2100-01-01

_RU_MONTHS: dict[str, int] = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}
_RU_DATE_TEXT = re.compile(
    r"^(\d{1,2})\s+(" + "|".join(_RU_MONTHS) + r")\s+(\d{4})$",
)
_DATE_FORMATS = ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y")

_ENGLISH_STATUSES = frozenset({"not_started", "in_progress", "transferred", "declined"})
# Таблица «Контракт» задачи B3-3 — слово/фраза файла -> код. Вариант по роду
# («передано»/«передана», «не начата»/«не начато») — реальный файл пишет
# и то, и другое: лицензия или договор, разницы для системы нет.
_STATUS_SYNONYMS: dict[str, str] = {
    "передано": "transferred",
    "передана": "transferred",
    "передан": "transferred",
    "выдано": "transferred",
    "выдана": "transferred",
    "выдан": "transferred",
    "в работе": "in_progress",
    "в процессе": "in_progress",
    "на согласовании": "in_progress",
    "не начато": "not_started",
    "не начата": "not_started",
    "не начат": "not_started",
    "не передано": "not_started",
    "не передана": "not_started",
    "ожидает": "not_started",
    "отказ": "declined",
    "не состоялась": "declined",
    "не состоялся": "declined",
    "отклонено": "declined",
    "отклонена": "declined",
    "отказано": "declined",
}
# Представитель каждого кода для подсказки «допустимо: …» и для Левенштейна.
_STATUS_LABELS: dict[str, str] = {
    "transferred": "передана",
    "in_progress": "в процессе",
    "not_started": "не начата",
    "declined": "не состоялась",
}
#: Названия статусов передачи для отчёта, шаблона импорта и подсказок — те же, что в интерфейсе
#: (фильтр и карточка лицензии: «Передана / В процессе / Не начата / Не состоялась»; R-14, M-5).
#: Один набор на всё: раньше отчёт писал «Передано / В работе», импорт — «передано, в работе,
#: не начато, отказ». Остальные написания импорт по-прежнему принимает (`_STATUS_SYNONYMS`).
TRANSFER_STATUS_LABELS: dict[str, str] = {
    "not_started": "Не начата",
    "in_progress": "В процессе",
    "transferred": "Передана",
    "declined": "Не состоялась",
}
_STATUS_HINT = ", ".join(_STATUS_LABELS.values())

_SIGNED_TRUE = frozenset({"да", "подписан", "подписана", "подписано", "+", "1", "true", "yes"})
_SIGNED_FALSE = frozenset(
    {"нет", "не подписан", "не подписана", "не подписано", "-", "0", "false", "no"}
)

_PERPETUAL_WORDS = frozenset({"бессрочно", "без срока", "безсрочно"})
_TERM_YEARS_SUFFIX = re.compile(r"^(\d{1,2})\s*(?:год(?:а|ов)?|лет)$")
_TERM_YEAR_RANGE = re.compile(r"^(\d{4})\s*[-–—]\s*(\d{4})$")
_TERM_MIN_YEARS, _TERM_MAX_YEARS = 1, 50
_TERM_MIN_CALENDAR_YEAR, _TERM_MAX_CALENDAR_YEAR = 2000, 2100


def _normalize(raw: object) -> str:
    """Регистр, «ё», неразрывный пробел и повторные пробелы, точка на конце — не различают
    значение: «Передано.», «ПЕРЕДАНО», «передано » — один и тот же статус."""
    if raw is None:
        return ""
    text = unicodedata.normalize("NFKC", str(raw)).strip()
    text = text.casefold().replace("ё", "е")
    text = _SPACES.sub(" ", text).strip()
    return _TRAILING_DOT.sub("", text).strip()


def _levenshtein(a: str, b: str) -> int:
    """Расстояние редактирования — для подсказки «возможно, вы имели в виду …» на неизвестном
    статусе. Отдельная копия от `imports.mapping._levenshtein`, см. докстринг модуля."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            current[j] = min(
                previous[j] + 1,  # удаление
                current[j - 1] + 1,  # вставка
                previous[j - 1] + cost,  # замена
            )
        previous = current
    return previous[-1]


def parse_transfer_status(raw: object) -> tuple[str | None, str | None]:
    """`(код, ошибка)`. Английские коды (как раньше) и русские синонимы
    (таблица B3-3); «да»/«нет» намеренно не принимаются — двусмысленно,
    см. докстринг модуля."""
    stripped = str(raw).strip() if raw is not None else ""
    if stripped in _ENGLISH_STATUSES:
        return stripped, None

    normalized = _normalize(raw)
    if not normalized:
        return None, None
    code = _STATUS_SYNONYMS.get(normalized)
    if code is not None:
        return code, None
    if normalized in ("да", "нет"):
        return None, (
            f"«{raw}» неоднозначно для статуса по передаче — укажите один из: {_STATUS_HINT}"
        )

    best_label: str | None = None
    best_distance = max(3, len(normalized) // 4)
    for label in _STATUS_LABELS.values():
        distance = _levenshtein(normalized, label)
        if distance < best_distance:
            best_distance = distance
            best_label = label
    if best_label is not None:
        return None, (
            f"«{raw}»: неизвестный статус по передаче, возможно «{best_label}»? "
            f"Допустимо: {_STATUS_HINT}"
        )
    return None, f"«{raw}»: статус по передаче не распознан. Допустимо: {_STATUS_HINT}"


@dataclass(frozen=True, slots=True)
class SignedResult:
    fields: dict[str, object] = field(default_factory=dict)
    warning: str | None = None


def _parse_ru_text_date(normalized: str) -> dt.date | None:
    match = _RU_DATE_TEXT.match(normalized)
    if not match:
        return None
    day, month_name, year = match.groups()
    try:
        return dt.date(int(year), _RU_MONTHS[month_name], int(day))
    except ValueError:
        return None


def _parse_excel_serial(normalized: str) -> dt.date | None:
    if not normalized.isdigit():
        return None
    serial = int(normalized)
    if not (_EXCEL_SERIAL_MIN <= serial <= _EXCEL_SERIAL_MAX):
        return None
    return _EXCEL_EPOCH + dt.timedelta(days=serial)


def _parse_any_date(raw: str, normalized: str) -> dt.date | None:
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    text_date = _parse_ru_text_date(normalized)
    if text_date is not None:
        return text_date
    return _parse_excel_serial(normalized)


def parse_signed(
    raw: object, *, today: dt.date | None = None
) -> tuple[SignedResult | None, str | None]:
    """«Подписание лицензии»: да/нет/дата -> `is_signed` (+ `license_signed_at`, если указана
    дата). Пустое значение — `(None, None)`, вызывающий не трогает поля."""
    raw_str = str(raw) if raw is not None else ""
    normalized = _normalize(raw)
    if not normalized:
        return None, None
    if normalized in _SIGNED_TRUE:
        return SignedResult(fields={"is_signed": True}), None
    if normalized in _SIGNED_FALSE:
        return SignedResult(fields={"is_signed": False}), None

    parsed_date = _parse_any_date(raw_str, normalized)
    if parsed_date is None:
        return None, (f"«{raw}»: «Подписание лицензии» не распознано — ожидается да/нет или дата")
    warning = None
    if parsed_date > (today or dt.date.today()):
        warning = f"Дата подписания лицензии в будущем: {parsed_date.isoformat()}"
    fields: dict[str, object] = {"is_signed": True, "license_signed_at": parsed_date}
    return SignedResult(fields=fields, warning=warning), None


@dataclass(frozen=True, slots=True)
class TermResult:
    fields: dict[str, object] = field(default_factory=dict)
    warning: str | None = None


def parse_term(raw: object) -> tuple[TermResult | None, str | None]:
    """«Срок действия лицензии (год)»: «3»/«3 года» — срок в годах;
    «2027» или «2024-2027» — календарный год окончания (с предупреждением);
    «бессрочно» — без срока; дата — окончание. Пустое значение —
    `(None, None)`."""
    raw_str = str(raw) if raw is not None else ""
    normalized = _normalize(raw)
    if not normalized:
        return None, None

    if normalized in _PERPETUAL_WORDS:
        return TermResult(fields={"is_perpetual": True}), None

    if normalized.isdigit():
        number = int(normalized)
        if _TERM_MIN_YEARS <= number <= _TERM_MAX_YEARS:
            return (
                TermResult(fields={"license_term_years": number, "license_valid_year": number}),
                None,
            )
        if _TERM_MIN_CALENDAR_YEAR <= number <= _TERM_MAX_CALENDAR_YEAR:
            return (
                TermResult(
                    fields={
                        "license_valid_until": dt.date(number, 12, 31),
                        "license_valid_year": number,
                    },
                    warning=f"«{raw}» понято как год окончания лицензии",
                ),
                None,
            )
        return None, (
            f"«{raw}»: срок действия не распознан. Укажите срок в годах (1–50) "
            f"или год окончания (2000–2100)"
        )

    suffix_match = _TERM_YEARS_SUFFIX.match(normalized)
    if suffix_match:
        number = int(suffix_match.group(1))
        if _TERM_MIN_YEARS <= number <= _TERM_MAX_YEARS:
            return (
                TermResult(fields={"license_term_years": number, "license_valid_year": number}),
                None,
            )
        return None, (
            f"«{raw}»: срок действия не распознан. Укажите срок в годах (1–50) "
            f"или год окончания (2000–2100)"
        )

    range_match = _TERM_YEAR_RANGE.match(normalized)
    if range_match:
        end_year = int(range_match.group(2))
        if _TERM_MIN_CALENDAR_YEAR <= end_year <= _TERM_MAX_CALENDAR_YEAR:
            return (
                TermResult(
                    fields={
                        "license_valid_until": dt.date(end_year, 12, 31),
                        "license_valid_year": end_year,
                    },
                    warning=f"«{raw}»: диапазон понят как год окончания {end_year}",
                ),
                None,
            )

    parsed_date = _parse_any_date(raw_str, normalized)
    if parsed_date is not None:
        return TermResult(fields={"license_valid_until": parsed_date}), None

    return None, (
        f"«{raw}»: срок действия не распознан. Укажите срок в годах (1–50) "
        f"или год окончания (2000–2100)"
    )
