"""LP-2 (QA 20.10): тексты ошибок разбора профиля учащегося — с заглавной буквы, согласованные
по роду и числу («Серия паспорта — 4 цифры», «СНИЛС — 11 цифр»). Чистые функции, без БД."""

from __future__ import annotations

import pytest

from app.modules.catalog import learner

CASES = [
    ("snils", "123", "СНИЛС — 11 цифр"),
    ("snils", "112-233-445 96", "СНИЛС — неверное контрольное число"),
    ("passport_series", "12", "Серия паспорта — 4 цифры"),
    ("passport_number", "12345", "Номер паспорта — 6 цифр"),
    ("passport_dept_code", "12", "Код подразделения — 6 цифр"),
    ("reg_zip", "1234", "Индекс — 6 цифр"),
    ("birth_date", "завтра", "Неверная дата — укажите в формате ДД.ММ.ГГГГ"),
    ("passport_issued_at", "31.31.2020", "Неверная дата — укажите в формате ДД.ММ.ГГГГ"),
    ("birth_date", "01.01.2999", "Дата рождения не может быть в будущем"),
    ("birth_date", "01.01.1850", "Дата рождения не может быть раньше 1900 года"),
    ("diploma_issued_at", "01.01.2999", "Дата не может быть в будущем"),
    ("passport_issued_at", "01.01.1850", "Дата не может быть раньше 1900 года"),
    ("sex", "?", "Пол — «М» или «Ж»"),
    (
        "education",
        "школа",
        "Выберите значение из справочника «Образование» (лист «Лист2» шаблона)",
    ),
]


@pytest.mark.parametrize(("field", "raw", "message"), CASES)
def test_message_text(field: str, raw: str, message: str) -> None:
    with pytest.raises(ValueError) as caught:
        learner.parse_profile_value(field, raw)
    assert str(caught.value) == message


def test_every_message_starts_with_a_capital_letter() -> None:
    for field, raw, _ in CASES:
        with pytest.raises(ValueError) as caught:
            learner.parse_profile_value(field, raw)
        text = str(caught.value)
        assert text[0].isupper(), text


def test_too_long_text() -> None:
    field, limit = next(iter(learner.PROFILE_MAX_LENGTH.items()))
    with pytest.raises(ValueError) as caught:
        learner.parse_profile_value(field, "я" * (limit + 1))
    assert str(caught.value) == f"Слишком длинное значение — не больше {limit} символов"


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (1, "1 цифра"),
        (4, "4 цифры"),
        (6, "6 цифр"),
        (11, "11 цифр"),
        (12, "12 цифр"),
        (21, "21 цифра"),
    ],
)
def test_digit_word_agreement(count: int, expected: str) -> None:
    assert learner._digits_word(count) == expected
