"""Разбор значений файла лицензий (B3-3): статусы по-русски, «да/нет», срок,
даты. Без БД — чистые функции. См. `app.modules.catalog.license_values` и
BUG-10 (статус по-русски и «да/нет» давали ошибку строки).
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.modules.catalog.license_values import (
    parse_signed,
    parse_term,
    parse_transfer_status,
)


class TestParseTransferStatus:
    @pytest.mark.parametrize("code", ["not_started", "in_progress", "transferred", "declined"])
    def test_english_codes_pass_through(self, code: str) -> None:
        value, error = parse_transfer_status(code)
        assert value == code
        assert error is None

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Передано", "transferred"),
            ("передана", "transferred"),
            ("Выдано", "transferred"),
            ("В работе", "in_progress"),
            ("в процессе", "in_progress"),
            ("На согласовании", "in_progress"),
            ("Не начато", "not_started"),
            ("не начата", "not_started"),
            ("Ожидает", "not_started"),
            ("Отказ", "declined"),
            ("не состоялась", "declined"),
            ("Отклонено", "declined"),
            ("ПЕРЕДАНО.", "transferred"),
            ("  в работе  ", "in_progress"),
            ("пЕредАно", "transferred"),
        ],
    )
    def test_russian_synonyms(self, raw: str, expected: str) -> None:
        value, error = parse_transfer_status(raw)
        assert error is None
        assert value == expected

    @pytest.mark.parametrize("raw", ["да", "нет", "Да", "НЕТ"])
    def test_yes_no_rejected_as_ambiguous(self, raw: str) -> None:
        value, error = parse_transfer_status(raw)
        assert value is None
        assert error is not None
        assert "неоднозначно" in error

    def test_unknown_value_gives_hint_with_allowed_list(self) -> None:
        value, error = parse_transfer_status("как-то так")
        assert value is None
        assert error is not None
        assert "передано" in error

    def test_typo_suggests_closest_match(self) -> None:
        value, error = parse_transfer_status("передоно")
        assert value is None
        assert error is not None
        assert "передано" in error

    def test_empty_gives_no_value_and_no_error(self) -> None:
        assert parse_transfer_status("") == (None, None)
        assert parse_transfer_status(None) == (None, None)


class TestParseSigned:
    @pytest.mark.parametrize("raw", ["да", "Подписан", "подписана", "подписано", "+", "1", "true"])
    def test_true_variants(self, raw: str) -> None:
        result, error = parse_signed(raw)
        assert error is None
        assert result is not None
        assert result.fields == {"is_signed": True}

    @pytest.mark.parametrize("raw", ["нет", "Не подписан", "-", "0", "false"])
    def test_false_variants(self, raw: str) -> None:
        result, error = parse_signed(raw)
        assert error is None
        assert result is not None
        assert result.fields == {"is_signed": False}

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("15.03.2024", dt.date(2024, 3, 15)),
            ("2024-03-15", dt.date(2024, 3, 15)),
            ("15/03/2024", dt.date(2024, 3, 15)),
            ("15.03.24", dt.date(2024, 3, 15)),
            ("15 марта 2024", dt.date(2024, 3, 15)),
        ],
    )
    def test_date_variants_set_is_signed_true(self, raw: str, expected: dt.date) -> None:
        result, error = parse_signed(raw, today=dt.date(2025, 1, 1))
        assert error is None
        assert result is not None
        assert result.fields == {"is_signed": True, "license_signed_at": expected}
        assert result.warning is None

    def test_excel_serial_number_is_a_date(self) -> None:
        # 45366 -> 2024-03-15 (база 1899-12-30).
        result, error = parse_signed("45366", today=dt.date(2025, 1, 1))
        assert error is None
        assert result is not None
        assert result.fields["license_signed_at"] == dt.date(2024, 3, 15)

    def test_future_date_gives_warning_not_error(self) -> None:
        result, error = parse_signed("15.03.2030", today=dt.date(2025, 1, 1))
        assert error is None
        assert result is not None
        assert result.warning is not None
        assert "будущем" in result.warning

    def test_unrecognized_value_is_error(self) -> None:
        result, error = parse_signed("какая-то строка без смысла")
        assert result is None
        assert error is not None

    def test_empty_gives_no_value_and_no_error(self) -> None:
        assert parse_signed("") == (None, None)
        assert parse_signed(None) == (None, None)


class TestParseTerm:
    @pytest.mark.parametrize("raw", ["бессрочно", "без срока", "Бессрочно"])
    def test_perpetual(self, raw: str) -> None:
        result, error = parse_term(raw)
        assert error is None
        assert result is not None
        assert result.fields == {"is_perpetual": True}

    @pytest.mark.parametrize("raw", ["3", "1", "50"])
    def test_plain_small_integer_is_years(self, raw: str) -> None:
        result, error = parse_term(raw)
        assert error is None
        assert result is not None
        assert result.fields["license_term_years"] == int(raw)
        assert result.fields["license_valid_year"] == int(raw)
        assert result.warning is None

    @pytest.mark.parametrize(("raw", "years"), [("3 года", 3), ("5 лет", 5), ("1 год", 1)])
    def test_years_with_suffix_word(self, raw: str, years: int) -> None:
        result, error = parse_term(raw)
        assert error is None
        assert result is not None
        assert result.fields["license_term_years"] == years

    def test_calendar_year_is_end_of_year_with_warning(self) -> None:
        result, error = parse_term("2027")
        assert error is None
        assert result is not None
        assert result.fields["license_valid_until"] == dt.date(2027, 12, 31)
        assert result.fields["license_valid_year"] == 2027
        assert result.warning is not None
        assert "год окончания" in result.warning

    def test_year_range_is_end_year_with_warning(self) -> None:
        result, error = parse_term("2024-2027")
        assert error is None
        assert result is not None
        assert result.fields["license_valid_until"] == dt.date(2027, 12, 31)
        assert result.warning is not None

    def test_date_is_end_of_license(self) -> None:
        result, error = parse_term("31.12.2027")
        assert error is None
        assert result is not None
        assert result.fields == {"license_valid_until": dt.date(2027, 12, 31)}

    @pytest.mark.parametrize("raw", ["1999", "-5", "99999999", "0", "51"])
    def test_out_of_range_values_are_errors(self, raw: str) -> None:
        result, error = parse_term(raw)
        assert result is None
        assert error is not None
        assert "срок в годах" in error

    def test_empty_gives_no_value_and_no_error(self) -> None:
        assert parse_term("") == (None, None)
        assert parse_term(None) == (None, None)
