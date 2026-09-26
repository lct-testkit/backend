"""Маскирование чувствительных данных.

Раздел 1 спецификации задаёт точные форматы:
телефон `+7 (9**) ***-**-12`, email `i***@domain.ru`,
ФИО при обезличивании — стабильный псевдоним `Пользователь #4821`.
Маскирование применяется в логах, аудите и ответах без прав.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re

_DIGITS = re.compile(r"\D+")

# Поля профиля учащегося (шаблон LMS «Загрузка пользователей»): СНИЛС, паспорт, дата рождения,
# адрес регистрации и реквизиты диплома. Это ПДн, которые нужны только для выгрузки в LMS и
# раскрываются только через `reveal` с отдельной записью аудита: ни в журнал аудита, ни в лог их
# значения не попадают, даже если код по ошибке положит их в `changes` или в поля события.
LEARNER_PII_KEYS: frozenset[str] = frozenset(
    {
        "snils",
        "passport_series",
        "passport_number",
        "passport_issued_by",
        "passport_dept_code",
        "birth_date",
        "reg_region",
        "reg_city",
        "reg_street",
        "reg_house",
        "reg_apartment",
        "reg_zip",
        "diploma_number",
        "diploma_series",
        "diploma_reg_number",
        "diploma_surname",
    }
)

# Ключи, значения которых нельзя писать в логи и аудит ни в каком виде. Профиль учащегося входит
# сюда же: `app.core.logging` вычищает из событий лога ровно этот набор.
SECRET_KEYS: frozenset[str] = (
    frozenset(
        {
            "password",
            "current_password",
            "new_password",
            "new_password_repeat",
            "token",
            "access_token",
            "refresh_token",
            "id_token",
            "logout_token",
            # Голый `code` здесь не секрет: это код продукта, статуса воронки, справочника и
            # `FieldError.code`. Раньше он вырезался, и события «создан продукт/справочник» в
            # аудите теряли код (`{"old": null, "new": "***"}`), а ответы об ошибках — свои
            # коды. Одноразовые коды называются явно.
            "authorization_code",
            "auth_code",
            "otp",
            "otp_code",
            "confirmation_code",
            "verification_code",
            "client_secret",
            "secret",
            "authorization",
            "cookie",
            "set-cookie",
            "signature_server_secret",
        }
    )
    | LEARNER_PII_KEYS
)

# Ключи, которые маскируются по своему формату, а не вырезаются целиком.
PII_PHONE_KEYS: frozenset[str] = frozenset({"phone", "main_phone", "mobile", "telephone"})
PII_EMAIL_KEYS: frozenset[str] = frozenset({"email", "main_email"})

REDACTED = "***"


def mask_phone(value: str | None) -> str | None:
    """`+79991234512` -> `+7 (9**) ***-**-12`."""
    if not value:
        return value
    digits = _DIGITS.sub("", value)
    if len(digits) < 4:
        return REDACTED
    country = digits[0] if len(digits) >= 11 else "7"
    first = digits[-10] if len(digits) >= 10 else "*"
    return f"+{country} ({first}**) ***-**-{digits[-2:]}"


def mask_email(value: str | None) -> str | None:
    """`ivanov@domain.ru` -> `i***@domain.ru`."""
    if not value:
        return value
    local, _, domain = value.partition("@")
    if not domain:
        return REDACTED
    head = local[0] if local else "*"
    return f"{head}***@{domain}"


def mask_inn(value: str | None) -> str | None:
    """ИНН в логах автоподстановки хранится маскированным (раздел 5.11)."""
    if not value:
        return value
    digits = _DIGITS.sub("", value)
    if len(digits) < 4:
        return REDACTED
    return f"{digits[:2]}{'*' * (len(digits) - 4)}{digits[-2:]}"


def mask_name(value: str | None) -> str | None:
    """ФИО в журналах: только инициалы, `Иванов Иван Иванович` -> `Иванов И. И.`."""
    if not value:
        return value
    parts = [p for p in value.split() if p]
    if not parts:
        return REDACTED
    initials = " ".join(f"{p[0]}." for p in parts[1:3])
    return f"{parts[0]} {initials}".strip()


_TEXT_EMAIL = re.compile(r"[^\s,;<>()]+@[^\s,;<>()]+")
_TEXT_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
# Подряд идущие слова с заглавной буквы (до трёх): фамилия, имя, отчество.
_TEXT_NAME = re.compile(r"[A-ZА-ЯЁ][a-zа-яё]+(?:\s+[A-ZА-ЯЁ][a-zа-яё]+){1,2}")


def mask_contacts_text(value: str | None) -> str | None:
    """Свободный текст со списком контактов («Иванов Иван, +7 999 123-45-12, i@vuz.ru»).

    Разбирать его на поля нечем, поэтому маскируется всё узнаваемое внутри: email и телефоны — по
    своим форматам, ФИО — до `Иванов И.` (`mask_name`). Остальной текст (должности, пояснения)
    остаётся как есть."""
    if not value:
        return value
    masked = _TEXT_EMAIL.sub(lambda match: mask_email(match.group()) or REDACTED, value)
    masked = _TEXT_PHONE.sub(lambda match: mask_phone(match.group()) or REDACTED, masked)
    return _TEXT_NAME.sub(lambda match: mask_name(match.group()) or REDACTED, masked)


def mask_tail(value: str | None, keep: int = 3) -> str | None:
    """СНИЛС, паспорт, адрес, диплом: виден только хвост, `11223344595` -> `***595`.

    Скрытых знаков должно быть не меньше, чем показанных. У короткого значения (серия паспорта из
    четырёх цифр, номер квартиры) хвост из трёх знаков раскрыл бы почти всё, поэтому оно
    закрывается целиком: `4512` -> `***`.
    """
    if not value:
        return value
    if len(value) < keep * 2:
        return REDACTED
    return f"{REDACTED}{value[-keep:]}"


def mask_year(value: dt.date | None) -> str | None:
    """Дата рождения и выдачи документов: остаётся только год, `1990-05-17` -> `1990`."""
    if value is None:
        return None
    return str(value.year)


def pseudonym(subject_id: object, salt: str = "") -> str:
    """Стабильный псевдоним для обезличивания: `Пользователь #4821`.

    Спецификация прямо запрещает безликое «Удалён»: псевдоним должен быть
    стабильным, чтобы история и аудит оставались связными.
    """
    raw = f"{salt}:{subject_id}".encode()
    number = int.from_bytes(hashlib.sha256(raw).digest()[:4], "big") % 10000
    return f"Пользователь #{number:04d}"


def mask_token(value: str | None, keep: int = 6) -> str | None:
    """Токены и ключи в логах: только хвост, чтобы можно было сопоставить запись."""
    if not value:
        return value
    if len(value) <= keep:
        return REDACTED
    return f"{REDACTED}{value[-keep:]}"


def mask_field(key: str, value: object, *, depth: int = 0) -> object:
    """Маскирует значение по имени поля.

    Аудит хранит изменения как `{"phone": {"old": ..., "new": ...}}`, поэтому
    для чувствительного поля нужно замаскировать не сам словарь, а значения
    внутри него — иначе ПДн утекут в журнал в открытом виде.
    """
    lowered = key.lower()

    if lowered in SECRET_KEYS:
        return REDACTED

    if isinstance(value, str):
        if lowered in PII_PHONE_KEYS:
            return mask_phone(value)
        if lowered in PII_EMAIL_KEYS:
            return mask_email(value)
        if lowered == "inn":
            return mask_inn(value)
        return value

    if isinstance(value, dict) and set(value) <= {"old", "new"}:
        # Обёртка old/new: применяем правило внешнего поля к обоим значениям.
        return {k: mask_field(key, v, depth=depth + 1) for k, v in value.items()}

    return mask_mapping(value, depth=depth + 1)


def mask_mapping(data: object, *, depth: int = 0) -> object:
    """Рекурсивно маскирует структуры перед записью в лог или аудит."""
    if depth > 8:
        return REDACTED
    if isinstance(data, dict):
        return {str(k): mask_field(str(k), v, depth=depth) for k, v in data.items()}
    if isinstance(data, list | tuple):
        return [mask_mapping(item, depth=depth + 1) for item in data]
    return data
