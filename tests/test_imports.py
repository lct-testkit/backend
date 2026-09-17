"""Тесты импорта каталогов (спринт 5, раздел 4.12).

Чистые функции без БД: разбор файлов (`imports.parsing`), автоподбор
маппинга (`imports.mapping`), валидация и нормализация полей
(`imports.fields`). Построчное применение/откат (`imports.service`)
требует сессии Postgres и здесь не покрыто — та же граница, что и у
остального DB-слоя репозитория.
"""

from __future__ import annotations

from decimal import Decimal

import openpyxl

from app.core.permissions import Permission, has_permission
from app.modules.imports.fields import (
    ORGANIZATION_FIELDS,
    PRODUCT_FIELDS,
    FieldSpec,
    normalize_phone_e164,
    validate_field,
)
from app.modules.imports.mapping import _levenshtein, suggest_mapping
from app.modules.imports.parsing import parse_csv, parse_xlsx, sanitize_formula


class TestPhoneNormalization:
    def test_local_format_with_leading_8(self) -> None:
        assert normalize_phone_e164("89991234567") == "+79991234567"

    def test_leading_plus7(self) -> None:
        assert normalize_phone_e164("+7 (999) 123-45-67") == "+79991234567"

    def test_ten_digits_no_country_code(self) -> None:
        assert normalize_phone_e164("9991234567") == "+79991234567"

    def test_invalid_length_rejected(self) -> None:
        assert normalize_phone_e164("12345") is None


class TestValidateField:
    def test_required_missing_fails(self) -> None:
        spec = FieldSpec("name", "Наименование", "text", required=True)
        value, error = validate_field(spec, "  ")
        assert value is None
        assert error is not None

    def test_optional_missing_is_none_without_error(self) -> None:
        spec = FieldSpec("short_name", "Краткое наименование", "text")
        value, error = validate_field(spec, "")
        assert value is None
        assert error is None

    def test_int_parses_from_float_like_string(self) -> None:
        spec = FieldSpec("students_count", "Студенты", "int")
        value, error = validate_field(spec, "1234")
        assert value == 1234
        assert error is None

    def test_int_rejects_garbage(self) -> None:
        spec = FieldSpec("students_count", "Студенты", "int")
        value, error = validate_field(spec, "много")
        assert value is None
        assert error is not None

    def test_decimal_accepts_comma_separator(self) -> None:
        spec = FieldSpec("base_price", "Цена", "decimal")
        value, error = validate_field(spec, "1500,50")
        assert value == Decimal("1500.50")
        assert error is None

    def test_email_valid(self) -> None:
        spec = FieldSpec("main_email", "Email", "email")
        value, error = validate_field(spec, "test@university.ru")
        assert value == "test@university.ru"
        assert error is None

    def test_email_invalid(self) -> None:
        spec = FieldSpec("main_email", "Email", "email")
        value, error = validate_field(spec, "not-an-email")
        assert value is None
        assert error is not None

    def test_phone_kind_normalizes(self) -> None:
        spec = FieldSpec("main_phone", "Телефон", "phone")
        value, error = validate_field(spec, "89991234567")
        assert value == "+79991234567"
        assert error is None

    def test_inn_kind_reuses_checksum_validator(self) -> None:
        spec = FieldSpec("inn", "ИНН", "inn")
        value, error = validate_field(spec, "7707049388")
        assert value == "7707049388"
        assert error is None

    def test_inn_kind_rejects_bad_checksum(self) -> None:
        spec = FieldSpec("inn", "ИНН", "inn")
        value, error = validate_field(spec, "7707049380")
        assert value is None
        assert error is not None

    def test_org_type_valid_value(self) -> None:
        spec = FieldSpec("org_type", "Тип", "org_type")
        value, error = validate_field(spec, "university")
        assert value == "university"
        assert error is None

    def test_org_type_invalid_value(self) -> None:
        spec = FieldSpec("org_type", "Тип", "org_type")
        value, error = validate_field(spec, "vendor")
        assert value is None
        assert error is not None

    def test_format_kind(self) -> None:
        spec = FieldSpec("format", "Формат", "format")
        assert validate_field(spec, "online") == ("online", None)
        assert validate_field(spec, "hybrid")[1] is not None


class TestSuggestMapping:
    def test_exact_synonym_match(self) -> None:
        mapping = suggest_mapping(["ИНН", "Наименование"], list(ORGANIZATION_FIELDS))
        assert mapping["ИНН"] == "inn"
        assert mapping["Наименование"] == "name"

    def test_fuzzy_match_with_typo(self) -> None:
        mapping = suggest_mapping(["Наимнование"], list(ORGANIZATION_FIELDS))
        assert mapping.get("Наимнование") == "name"

    def test_unmatched_header_absent_from_mapping(self) -> None:
        mapping = suggest_mapping(["Совершенно постороннее поле xyz"], list(ORGANIZATION_FIELDS))
        assert "Совершенно постороннее поле xyz" not in mapping

    def test_does_not_map_two_headers_to_same_target(self) -> None:
        mapping = suggest_mapping(["Наименование", "название"], list(ORGANIZATION_FIELDS))
        assert len(set(mapping.values())) == len(mapping.values())

    def test_product_fields_code_synonym(self) -> None:
        mapping = suggest_mapping(["Код", "Наименование"], list(PRODUCT_FIELDS))
        assert mapping["Код"] == "code"
        assert mapping["Наименование"] == "name"


class TestLevenshtein:
    def test_identical_strings(self) -> None:
        assert _levenshtein("test", "test") == 0

    def test_single_substitution(self) -> None:
        assert _levenshtein("test", "tent") == 1

    def test_empty_string(self) -> None:
        assert _levenshtein("", "abc") == 3


class TestSanitizeFormula:
    def test_neutralizes_leading_equals(self) -> None:
        assert sanitize_formula("=cmd|'/c calc'!A1").startswith("'=")

    def test_neutralizes_leading_at(self) -> None:
        assert sanitize_formula("@SUM(1,2)").startswith("'@")

    def test_leaves_normal_text_untouched(self) -> None:
        assert sanitize_formula("Обычный текст") == "Обычный текст"

    def test_leaves_negative_looking_number_prefixed(self) -> None:
        # Осознанный компромисс: "-79991234567" тоже попадёт под защиту,
        # это безопаснее, чем угадывать намерение по содержимому ячейки.
        assert sanitize_formula("-79991234567").startswith("'-")


class TestParseCsv:
    def test_semicolon_delimiter_detected(self) -> None:
        content = "Наименование;ИНН\nМГУ;7707049388\n".encode()
        table = parse_csv(content)
        assert table.headers == ["Наименование", "ИНН"]
        assert table.rows == [["МГУ", "7707049388"]]

    def test_cp1251_encoding_detected(self) -> None:
        content = "Наименование;ИНН\nМГУ;7707049388\n".encode("cp1251")
        table = parse_csv(content)
        assert table.headers == ["Наименование", "ИНН"]

    def test_utf8_bom_stripped(self) -> None:
        content = "﻿Наименование,ИНН\nМГУ,7707049388\n".encode()
        table = parse_csv(content)
        assert table.headers[0] == "Наименование"

    def test_blank_rows_skipped(self) -> None:
        content = "Наименование;ИНН\nМГУ;7707049388\n;\n".encode()
        table = parse_csv(content)
        assert len(table.rows) == 1


class TestParseXlsx:
    def test_reads_header_and_rows(self, tmp_path) -> None:
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["Наименование", "ИНН"])
        sheet.append(["МГУ", "7707049388"])
        path = tmp_path / "test.xlsx"
        workbook.save(path)

        table = parse_xlsx(path.read_bytes())
        assert table.headers == ["Наименование", "ИНН"]
        assert table.rows == [["МГУ", "7707049388"]]

    def test_integer_cell_rendered_without_decimal(self, tmp_path) -> None:
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["Студенты"])
        sheet.append([1200])
        path = tmp_path / "test.xlsx"
        workbook.save(path)

        table = parse_xlsx(path.read_bytes())
        assert table.rows == [["1200"]]


class TestImportPermissions:
    def test_only_head_and_admin_run_imports(self) -> None:
        assert not has_permission("KAM", Permission.IMPORT_RUN)
        assert has_permission("HEAD", Permission.IMPORT_RUN)
        assert has_permission("ADMIN", Permission.IMPORT_RUN)

    def test_only_head_and_admin_rollback(self) -> None:
        assert not has_permission("KAM", Permission.IMPORT_ROLLBACK)
        assert has_permission("HEAD", Permission.IMPORT_ROLLBACK)
        assert has_permission("ADMIN", Permission.IMPORT_ROLLBACK)
