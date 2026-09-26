"""Реквизиты и версия объекта принимают только цифры 0–9.

`str.isdigit()` истинно и для «²», «٣», «①» — `int()` на них падает, и ИНН вида `770708389²`
давал 500 при создании организации, в импорте и в автоподстановке; заголовок `If-Match: ²` — тоже.
"""

from __future__ import annotations

import pytest

from app.core.normalize import is_ascii_digits
from app.modules.catalog.validators import (
    validate_inn,
    validate_kpp,
    validate_ogrn,
    validate_ogrnip,
)

_SUPERSCRIPT = "²"
_ARABIC_INDIC = "٣"


class TestAsciiDigits:
    def test_ordinary_digits(self) -> None:
        assert is_ascii_digits("0123456789")

    @pytest.mark.parametrize("value", ["²", "٣", "①", "12²", "", " 1", "1 ", "1.5", "-1"])
    def test_everything_else(self, value: str) -> None:
        assert not is_ascii_digits(value)


class TestRequisitesDoNotCrashOnUnicodeDigits:
    @pytest.mark.parametrize(
        "validator",
        [validate_inn, validate_kpp, validate_ogrn, validate_ogrnip],
    )
    @pytest.mark.parametrize(
        "value",
        [
            "770708389" + _SUPERSCRIPT,
            "77070100" + _SUPERSCRIPT,
            "1027700132195" + _ARABIC_INDIC,
            _SUPERSCRIPT * 10,
            _ARABIC_INDIC * 9,
        ],
    )
    def test_rejected_without_exception(self, validator, value: str) -> None:
        result = validator(value)
        assert result.ok is False
        assert result.reason

    def test_valid_inn_still_passes(self) -> None:
        assert validate_inn("7707049388").ok
