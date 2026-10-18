"""B3-2: листы книги и строка заголовков (без БД — книги собирает openpyxl в памяти).

Реальный файл заказчика — книга из нескольких листов с титульной строкой
перед шапкой, иногда со скрытым листом (черновик/архив). До этой задачи
читался только первый лист и первая строка.

Полный контракт задачи (режим `sheets: ["*"]` для склейки всех листов,
`fill_down` для объединённых ячеек, `source_row`/`sheet` на строку
результата) не реализован — см. отчёт по эпику. Эти тесты покрывают то,
что реализовано: выбор листа, автопоиск строки заголовка, разбор с явно
указанной строкой заголовка.
"""

from __future__ import annotations

import io

import openpyxl
import pytest

from app.core.errors import AppError
from app.modules.imports.parsing import detect_header_row, list_sheets, parse_table

_LICENSE_LABELS = [
    "Название ВУЗа",
    "Вендор",
    "ПО",
    "Номер договора",
    "Подписание лицензии",
    "Срок действия лицензии (год)",
    "Статус по передаче",
    "ФИО Менеджера",
    "Ответственные от ВУЗа",
    "Комментарий",
]


def _xlsx_bytes(sheets: dict[str, list[list[object]]], *, hidden: set[str] = frozenset()) -> bytes:
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets.items():
        sheet = workbook.create_sheet(name)
        for row in rows:
            sheet.append(row)
        if name in hidden:
            sheet.sheet_state = "hidden"
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


class TestDetectHeaderRow:
    def test_header_on_first_row_when_it_matches(self) -> None:
        rows = [["Название ВУЗа", "Вендор", "ПО"], ["МГТУ", "Яндекс", "DataLake"]]
        assert detect_header_row(rows, _LICENSE_LABELS) == 1

    def test_header_after_title_and_blank_rows(self) -> None:
        rows = [
            ["Каталог лицензий 2026"],
            [],
            ["Название ВУЗа", "Вендор", "ПО", "Номер договора"],
            ["МГТУ", "Яндекс", "DataLake", "Д-1"],
        ]
        assert detect_header_row(rows, _LICENSE_LABELS) == 3

    def test_too_few_matches_falls_back_to_first_row(self) -> None:
        rows = [["совсем", "другие", "колонки"], ["x", "y", "z"]]
        assert detect_header_row(rows, _LICENSE_LABELS) == 1

    def test_tolerates_a_typo_in_one_header(self) -> None:
        rows = [["Названи ВУЗа", "Вендор", "ПО", "Номер договора"], ["МГТУ", "Яндекс", "DL", "Д-1"]]
        assert detect_header_row(rows, _LICENSE_LABELS) == 1

    def test_no_expected_labels_defaults_to_first_row(self) -> None:
        assert detect_header_row([["a"], ["b"]], []) == 1


class TestListSheetsXlsx:
    def test_names_and_row_counts(self) -> None:
        content = _xlsx_bytes(
            {
                "Лист1": [["A", "B"], ["1", "2"]],
                "Лист2": [["C"], ["3"], ["4"], ["5"]],
            }
        )
        sheets = list_sheets(content, source_format="xlsx")
        by_name = {s.name: s for s in sheets}
        assert by_name["Лист1"].rows == 2
        assert by_name["Лист2"].rows == 4
        assert by_name["Лист1"].visible
        assert by_name["Лист2"].visible

    def test_hidden_sheet_is_marked_not_visible(self) -> None:
        content = _xlsx_bytes(
            {"Видимый": [["A"], ["1"]], "Архив": [["A"], ["2"]]}, hidden={"Архив"}
        )
        sheets = {s.name: s for s in list_sheets(content, source_format="xlsx")}
        assert sheets["Видимый"].visible
        assert not sheets["Архив"].visible

    def test_suggested_header_row_uses_expected_labels(self) -> None:
        content = _xlsx_bytes(
            {
                "Лицензии": [
                    ["Каталог на 2026 год"],
                    ["Название ВУЗа", "Вендор", "ПО", "Номер договора"],
                    ["МГТУ", "Яндекс", "DataLake", "Д-1"],
                ]
            }
        )
        sheets = list_sheets(content, source_format="xlsx", expected_labels=_LICENSE_LABELS)
        assert sheets[0].suggested_header_row == 2


class TestParseTableWithSheetAndHeaderRow:
    def test_default_behaviour_is_unchanged(self) -> None:
        content = _xlsx_bytes({"Лист1": [["Название ВУЗа", "Вендор"], ["МГТУ", "Яндекс"]]})
        table = parse_table(content, source_format="xlsx")
        assert table.headers == ["Название ВУЗа", "Вендор"]
        assert table.rows == [["МГТУ", "Яндекс"]]

    def test_reads_a_named_sheet(self) -> None:
        content = _xlsx_bytes(
            {
                "Лист1": [["x"], ["1"]],
                "Лист2": [["Название ВУЗа", "Вендор"], ["СПбГУ", "Ростелеком"]],
            }
        )
        table = parse_table(content, source_format="xlsx", sheet="Лист2")
        assert table.headers == ["Название ВУЗа", "Вендор"]
        assert table.rows == [["СПбГУ", "Ростелеком"]]
        assert table.sheet_name == "Лист2"

    def test_reads_header_from_an_explicit_row(self) -> None:
        content = _xlsx_bytes(
            {
                "Лицензии": [
                    ["Каталог на 2026 год"],
                    ["Название ВУЗа", "Вендор"],
                    ["МГТУ", "Яндекс"],
                    ["СПбГУ", "Ростелеком"],
                ]
            }
        )
        table = parse_table(content, source_format="xlsx", header_row=2)
        assert table.headers == ["Название ВУЗа", "Вендор"]
        assert table.rows == [["МГТУ", "Яндекс"], ["СПбГУ", "Ростелеком"]]
        assert table.header_row == 2

    def test_unknown_sheet_name_is_a_clear_error(self) -> None:
        content = _xlsx_bytes({"Лист1": [["a"], ["1"]]})
        with pytest.raises(AppError):
            parse_table(content, source_format="xlsx", sheet="Нет такого")

    def test_header_row_out_of_range_is_a_clear_error(self) -> None:
        content = _xlsx_bytes({"Лист1": [["a"], ["1"]]})
        with pytest.raises(AppError):
            parse_table(content, source_format="xlsx", header_row=50)
