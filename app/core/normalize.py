"""Нормализация идентификаторов и названий для дедупликации.

Единственное место, где телефон, email, ФИО и названия организаций/продуктов приводятся к
сравнимому виду. Ручной ввод контакта, вебхук сайта, импорт файлов и шаблон LMS обязаны давать
один и тот же ключ для одного и того же человека: иначе «+7 (999) 023-43-65» и «79990234365» —
два разных контакта, а «Ivanov@Mail.ru» и «ivanov@mail.ru» — тоже.

Модуль чистый (без БД и настроек): им пользуются `catalog`, `imports` и `integration`.
"""

from __future__ import annotations

import re
import unicodedata

from email_validator import EmailNotValidError, validate_email

_NON_DIGITS = re.compile(r"\D+")
_SPACES = re.compile(r"\s+")
# «доб. 123», «ext 12», «#45» на конце номера — добавочный, в E.164 его нет.
_PHONE_EXTENSION = re.compile(r"(?i)\s*(?:доб(?:авочный)?\.?|ext\.?|#)\s*\d+\s*$")
_QUOTE_CHARS = "«»\"“”„'`"
_OPEN_TO_CLOSE = {"«": "»", "“": "”", "„": "“", '"': '"'}
_LIST_SEPARATORS = ",;\n|"
_PUNCTUATION = re.compile(r"[^\w\s\-.]", re.UNICODE)

_TRANSLIT = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}


def is_ascii_digits(value: str) -> bool:
    """Только цифры 0–9. `str.isdigit()` истинно и для «²», «٣» и прочих Unicode-цифр, на которых
    `int()` потом падает: ИНН вида `770708389²` давал 500 при создании организации, в импорте и в
    автоподстановке."""
    return value.isascii() and value.isdigit()


def clean_text(value: object) -> str | None:
    """Обрезка и схлопывание пробелов; пустая строка -> `None`."""
    if value is None:
        return None
    text = _SPACES.sub(" ", unicodedata.normalize("NFKC", str(value))).strip()
    return text or None


def normalize_email(value: object) -> str | None:
    """Канонический адрес (нижний регистр) или `None`, если значение пустое или не email.

    Различить «пусто» и «не email» вызывающему помогает сам исходный текст: непустой вход при
    `None` на выходе — некорректный адрес.
    """
    text = clean_text(value)
    if text is None:
        return None
    try:
        result = validate_email(text, check_deliverability=False)
    except EmailNotValidError:
        return None
    return result.normalized.lower()


def normalize_phone(value: object) -> str | None:
    """Телефон в E.164 (`+79990234365`) или `None`, если разобрать нельзя.

    Российские форматы: `8 999 …`, `7 (999) …`, `+7-999-…`, 10 цифр без кода страны. Номер с
    явным `+` и другой страной (8–15 цифр) сохраняется как есть — международные контакты у
    вузов и вендоров бывают.
    """
    text = clean_text(value)
    if text is None:
        return None
    text = _PHONE_EXTENSION.sub("", text)
    digits = _NON_DIGITS.sub("", text)
    if not digits:
        return None
    if text.startswith("+"):
        if digits.startswith("7") and len(digits) == 11:
            return f"+{digits}"
        if not digits.startswith("7") and 8 <= len(digits) <= 15:
            return f"+{digits}"
        return None
    if len(digits) == 11 and digits[0] in "78":
        return f"+7{digits[1:]}"
    if len(digits) == 10:
        return f"+7{digits}"
    return None


def name_key(value: object) -> str:
    """Ключ сравнения фамилии/имени: регистр, «ё» и пунктуация не важны."""
    text = clean_text(value) or ""
    text = text.casefold().replace("ё", "е")
    return _SPACES.sub(" ", _PUNCTUATION.sub("", text)).strip()


def company_key(value: object) -> str:
    """Ключ сравнения названия организации/продукта.

    Кавычки, регистр, «ё», тире и лишние пробелы не различают названия: `ООО «Базис»`,
    `ооо "Базис"` и `ООО  Базис` — одна компания. Организационно-правовая форма остаётся в
    ключе: `ПАО «Ростелеком»` и `ООО «Ростелеком»` — разные юрлица.
    """
    text = clean_text(value) or ""
    text = text.casefold().replace("ё", "е")
    for quote in _QUOTE_CHARS:
        text = text.replace(quote, " ")
    text = text.replace("—", "-").replace("–", "-")
    return _SPACES.sub(" ", text).strip(" .,;:-")


def split_full_name(value: object) -> tuple[str | None, str | None, str | None]:
    """`Иванов Иван Иванович` -> (фамилия, имя, отчество). Порядок как в русском ФИО.

    Одно слово — только фамилия; отчество из нескольких слов (`Али оглы`) склеивается.
    """
    text = clean_text(value)
    if text is None:
        return None, None, None
    parts = text.split(" ")
    last = parts[0]
    first = parts[1] if len(parts) > 1 else None
    middle = " ".join(parts[2:]) if len(parts) > 2 else None
    return last, first, middle


def strip_quotes(value: str) -> str:
    return value.strip().strip(_QUOTE_CHARS).strip()


def split_list(value: object) -> list[str]:
    """Значения из одной ячейки: `«RT.DataLake», «RT.Warehouse»` -> два элемента.

    Разделители (`,` `;` перенос строки `|`) внутри кавычек не режут: название `«Аврора, SDK»`
    остаётся одним. Кавычки по краям каждого элемента снимаются, дубликаты убираются с
    сохранением порядка.
    """
    text = clean_text(value)
    if text is None:
        return []
    items: list[str] = []
    buffer: list[str] = []
    closer: str | None = None
    for char in str(value):
        if closer is not None:
            buffer.append(char)
            if char == closer:
                closer = None
            continue
        if char in _OPEN_TO_CLOSE:
            closer = _OPEN_TO_CLOSE[char]
            buffer.append(char)
            continue
        if char in _LIST_SEPARATORS:
            items.append("".join(buffer))
            buffer = []
            continue
        buffer.append(char)
    items.append("".join(buffer))

    result: list[str] = []
    seen: set[str] = set()
    for raw in items:
        item = clean_text(strip_quotes(raw))
        if item is None:
            continue
        key = company_key(item)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def slugify_code(value: str, *, max_length: int = 48) -> str:
    """Латинский слаг из названия: `«Базис Dynamix»` -> `bazis-dynamix`, `RT.DataLake` ->
    `rt-datalake`. Для кода автоматически заводимого продукта (код обязателен и уникален)."""
    lowered = (clean_text(value) or "").casefold()
    chars: list[str] = []
    for char in lowered:
        if char in _TRANSLIT:
            chars.append(_TRANSLIT[char])
        elif char.isascii() and char.isalnum():
            chars.append(char)
        else:
            chars.append("-")
    slug = re.sub(r"-{2,}", "-", "".join(chars)).strip("-")
    return (slug[:max_length].strip("-")) or "item"


# Способ связи из вендорского каталога: «Почта, Чат в ТГ».
_CONTACT_METHOD_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("email", ("почт", "e-mail", "email", "mail", "имейл", "емейл")),
    ("telegram", ("тг", "tg", "telegram", "телеграм", "телега")),
    ("whatsapp", ("whatsapp", "whats app", "ватсап", "вотсап", "вацап", "wa")),
    ("phone", ("телефон", "звонок", "звонит", "phone", "смс", "sms", "мобильн")),
)
CONTACT_METHOD_CODES = ("email", "phone", "telegram", "whatsapp")


def parse_contact_methods(value: object) -> tuple[list[str], list[str]]:
    """`Почта, Чат в ТГ` -> (`["email", "telegram"]`, `[]`). Второй элемент — нераспознанные
    фрагменты: импорт показывает их предупреждением, а не молча теряет."""
    known: list[str] = []
    unknown: list[str] = []
    for token in split_list(value):
        lowered = token.casefold()
        words = set(re.findall(r"[\w-]+", lowered))
        matched: str | None = None
        for code, hints in _CONTACT_METHOD_HINTS:
            for hint in hints:
                # Короткие подсказки («тг», «wa», «tg») — только целым словом, длинные —
                # по вхождению («почтой», «телеграмм»).
                if (len(hint) <= 3 and hint in words) or (len(hint) > 3 and hint in lowered):
                    matched = code
                    break
            if matched:
                break
        if matched is None:
            unknown.append(token)
        elif matched not in known:
            known.append(matched)
    return known, unknown
