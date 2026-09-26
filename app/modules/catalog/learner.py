"""Данные учащегося и шаблон LMS «Загрузка пользователей»: колонки, справочники, проверки.

Шаблон — файл ЛМС ИТ Школы (`Загрузка пользователей.xlsx`): лист «Лист1» с 30 колонками и лист
«Лист2» со справочниками для выпадающих списков «Пол» (L) и «Образование» (W). Здесь единственное
описание этого шаблона: им пользуются импорт (`imports.fields`), профиль учащегося
(`catalog.service`) и экспорт отчёта `lms_users_upload` (`reporting.builders`), поэтому заголовки,
порядок и проверки не могут разойтись между чтением и формированием.

Заголовки скопированы из оригинала БУКВАЛЬНО, в том числе без открывающих скобок в «Отчествопри
наличии)» и «Имядательный падеж)»: ЛМС сопоставляет колонки по этому тексту. При чтении, наоборот,
скобки и пробелы не различаются (`header_key`): файл, пересохранённый вручную, тоже разбирается.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable

LMS_USERS_SHEET = "Лист1"
LMS_LOOKUP_SHEET = "Лист2"
# Как в оригинале: выпадающие списки повешены на 1000 строк.
LMS_VALIDATION_LAST_ROW = 1001

# (поле профиля/контакта, заголовок колонки) в порядке колонок шаблона.
LMS_USER_COLUMNS: tuple[tuple[str, str], ...] = (
    ("last_name", "Фамилия"),
    ("first_name", "Имя"),
    ("middle_name", "Отчествопри наличии)"),
    ("phone", "Номер телефона"),
    ("email", "Email"),
    ("snils", "СНИЛС"),
    ("passport_series", "Серия паспорта"),
    ("passport_number", "Номер паспорта"),
    ("passport_issued_by", "Кем выдан паспорт"),
    ("passport_issued_at", "Дата выдачи паспорта"),
    ("passport_dept_code", "Код подразделения"),
    ("sex", "Пол"),
    ("birth_date", "Дата рождения"),
    ("reg_region", "Регион регистрации"),
    ("reg_city", "Населенный пункт регистрации"),
    ("reg_street", "Улица регистрации"),
    ("reg_house", "Дом регистрации"),
    ("reg_apartment", "Квартира регистрации"),
    ("reg_zip", "Индекс регистрации"),
    ("first_name_dative", "Имядательный падеж)"),
    ("last_name_dative", "Фамилиядательный падеж)"),
    ("middle_name_dative", "Отчестводательный падеж)"),
    ("education", "Образование"),
    ("diploma_profession", "Профессия по диплому"),
    ("diploma_institution", "Учебное заведение по диплому"),
    ("diploma_surname", "Фамилия, указанная в дипломе"),
    ("diploma_number", "Номер диплома"),
    ("diploma_series", "Серия диплома"),
    ("diploma_reg_number", "Регистрационный номер диплома"),
    ("diploma_issued_at", "Дата выдачи диплома"),
)
LMS_HEADER_BY_TARGET: dict[str, str] = dict(LMS_USER_COLUMNS)

# Поля, которые лежат в `contact_learner_profiles`, а не в самом контакте.
CONTACT_TARGETS: frozenset[str] = frozenset(
    {"last_name", "first_name", "middle_name", "phone", "email"}
)
PROFILE_TARGETS: tuple[str, ...] = tuple(
    target for target, _ in LMS_USER_COLUMNS if target not in CONTACT_TARGETS
)

SEX_LABELS: dict[str, str] = {"M": "М", "F": "Ж"}

# (код в БД, текст в выпадающем списке «Лист2»). Тире — как в оригинале: в «9 классов» и
# «11 классов» дефис, в «Высшее образование – …» длинное.
EDUCATION_LEVELS: tuple[tuple[str, str], ...] = (
    ("none", "Без образования"),
    ("basic_general", "Основное общее образование - 9 классов"),
    ("secondary_general", "Среднее общее образование - 11 классов"),
    ("secondary_vocational", "Среднее профессиональное образование"),
    ("higher_bachelor", "Высшее образование – бакалавриат"),
    ("higher_specialist_master", "Высшее образование – специалитет, магистратура"),
    ("higher_top_qualification", "Высшее образование – подготовка кадров высшей квалификации"),
)
EDUCATION_LABELS: dict[str, str] = dict(EDUCATION_LEVELS)

# Максимальные длины текстовых колонок `contact_learner_profiles`.
PROFILE_MAX_LENGTH: dict[str, int] = {
    "passport_issued_by": 512,
    "reg_region": 255,
    "reg_city": 255,
    "reg_street": 255,
    "reg_house": 64,
    "reg_apartment": 64,
    "first_name_dative": 128,
    "last_name_dative": 128,
    "middle_name_dative": 128,
    "diploma_profession": 255,
    "diploma_institution": 512,
    "diploma_surname": 128,
    "diploma_number": 64,
    "diploma_series": 64,
    "diploma_reg_number": 64,
}
# Дата в файле бывает и датой Excel (`2026-03-13`), и текстом (`13.03.2026`).
_DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y")
_NON_DIGITS = re.compile(r"\D+")
_HEADER_NOISE = re.compile(r"[\W_]+", re.UNICODE)
_DASHES = str.maketrans({"–": "-", "—": "-", "‑": "-", "−": "-"})


def header_key(header: object) -> str:
    """Ключ сравнения заголовка колонки: регистр, скобки, пробелы, знаки не важны.

    `Отчество(при наличии)`, `Отчество (при наличии)` и `Отчествопри наличии)` — одна колонка.
    """
    return _HEADER_NOISE.sub("", str(header or "").casefold().replace("ё", "е"))


# Ключ заголовка -> поле. Основа — заголовки шаблона, плюс безобидные синонимы.
_HEADER_ALIASES: dict[str, str] = {
    header_key(header): target for target, header in LMS_USER_COLUMNS
}
_HEADER_ALIASES.update(
    {
        header_key("Телефон"): "phone",
        header_key("Почта"): "email",
        header_key("E-mail"): "email",
        header_key("Отчество"): "middle_name",
        header_key("Отчество (при наличии)"): "middle_name",
        header_key("Имя (дательный падеж)"): "first_name_dative",
        header_key("Фамилия (дательный падеж)"): "last_name_dative",
        header_key("Отчество (дательный падеж)"): "middle_name_dative",
    }
)


def target_for_header(header: object) -> str | None:
    return _HEADER_ALIASES.get(header_key(header))


def parse_date(raw: str) -> dt.date:
    text = raw.strip()
    # Дата из Excel приходит ISO-строкой, иногда со временем.
    if "T" in text or (" " in text and ":" in text):
        try:
            return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except ValueError:
            pass
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError("нераспознанный формат даты (ожидается ДД.ММ.ГГГГ или ГГГГ-ММ-ДД)")


def parse_birth_date(raw: str) -> dt.date:
    value = parse_date(raw)
    today = dt.date.today()
    if value > today:
        raise ValueError("дата рождения в будущем")
    if value.year < 1900:
        raise ValueError("дата рождения раньше 1900 года")
    return value


def parse_past_date(raw: str) -> dt.date:
    value = parse_date(raw)
    if value > dt.date.today():
        raise ValueError("дата в будущем")
    if value.year < 1900:
        raise ValueError("дата раньше 1900 года")
    return value


def parse_sex(raw: str) -> str:
    key = raw.strip().casefold().rstrip(".")
    if key in {"м", "муж", "мужской", "мужчина", "m", "male"}:
        return "M"
    if key in {"ж", "жен", "женский", "женщина", "f", "female"}:
        return "F"
    raise ValueError("допустимые значения — М или Ж")


def parse_education(raw: str) -> str:
    def _key(text: str) -> str:
        return " ".join(text.translate(_DASHES).casefold().replace("ё", "е").split())

    wanted = _key(raw)
    for code, label in EDUCATION_LEVELS:
        if wanted in (_key(label), code):
            return code
    raise ValueError("значение не из справочника «Образование» (лист «Лист2» шаблона)")


def normalize_snils(raw: str) -> str:
    """11 цифр без разделителей; контрольное число проверяется для номеров выше 001-001-998."""
    digits = _NON_DIGITS.sub("", raw)
    if len(digits) != 11:
        raise ValueError("СНИЛС должен содержать 11 цифр")
    if int(digits[:9]) > 1_001_998:
        total = sum(int(d) * w for d, w in zip(digits[:9], range(9, 0, -1), strict=True))
        expected = total if total < 100 else (0 if total in (100, 101) else total % 101)
        if expected == 100:
            expected = 0
        if expected != int(digits[9:]):
            raise ValueError("неверное контрольное число СНИЛС")
    return digits


def format_snils(digits: str) -> str:
    """`11223344595` -> `112-233-445 95`."""
    if len(digits) != 11:
        return digits
    return f"{digits[0:3]}-{digits[3:6]}-{digits[6:9]} {digits[9:]}"


def _fixed_digits(raw: str, length: int, what: str) -> str:
    digits = _NON_DIGITS.sub("", raw)
    if len(digits) != length:
        raise ValueError(f"{what} должен содержать {length} цифр")
    return digits


def normalize_passport_series(raw: str) -> str:
    return _fixed_digits(raw, 4, "серия паспорта")


def normalize_passport_number(raw: str) -> str:
    return _fixed_digits(raw, 6, "номер паспорта")


def normalize_dept_code(raw: str) -> str:
    digits = _fixed_digits(raw, 6, "код подразделения")
    return f"{digits[:3]}-{digits[3:]}"


def normalize_zip(raw: str) -> str:
    return _fixed_digits(raw, 6, "индекс")


def normalize_text(field: str, raw: str) -> str:
    text = " ".join(raw.split())
    limit = PROFILE_MAX_LENGTH.get(field)
    if limit is not None and len(text) > limit:
        raise ValueError(f"слишком длинное значение (не больше {limit} символов)")
    return text


# Проверка/нормализация значения поля профиля из строки. Возвращает значение для БД
# (дата — `date`, остальное — `str`) или бросает `ValueError` с причиной по-русски.
PROFILE_PARSERS: dict[str, Callable[[str], object]] = {
    "snils": normalize_snils,
    "passport_series": normalize_passport_series,
    "passport_number": normalize_passport_number,
    "passport_dept_code": normalize_dept_code,
    "passport_issued_at": parse_past_date,
    "birth_date": parse_birth_date,
    "sex": parse_sex,
    "education": parse_education,
    "reg_zip": normalize_zip,
    "diploma_issued_at": parse_past_date,
}


def parse_profile_value(field: str, raw: str) -> object:
    """Единая точка разбора поля профиля: импорт файла и `PUT /learner-profile` дают одинаковый
    результат и одинаковые сообщения."""
    parser = PROFILE_PARSERS.get(field)
    if parser is not None:
        return parser(raw)
    return normalize_text(field, raw)
