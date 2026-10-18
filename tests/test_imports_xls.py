"""B3-10: тесты на реальные байты .xls (не на книгу, собранную в памяти xlwt тут же, а на
файлы, закоммиченные в `tests/fixtures/imports/`, см. `tools/make_xls_fixtures.py`).

Без БД — чистый разбор (`parsing.py`). xlrd 2.0.1 читает только BIFF (.xls), не xlsx — приёмка
задачи: `.xls`, переименованный из `.xlsx`, тоже ожидаемо не открывается.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import AppError
from app.modules.imports.parsing import list_sheets, parse_table

FIXTURES = Path(__file__).parent / "fixtures" / "imports"

LICENSE_HEADERS = [
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


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class TestRealXlsBytes:
    def test_fixture_has_the_ole_signature(self) -> None:
        content = _fixture("licenses_10cols.xls")
        assert content[:8].hex() == "d0cf11e0a1b11ae1"

    def test_parse_xls_returns_ten_headers(self) -> None:
        table = parse_table(_fixture("licenses_10cols.xls"), source_format="xls")
        assert table.headers == LICENSE_HEADERS
        assert len(table.rows) == 1

    def test_date_cell_becomes_iso_text(self) -> None:
        table = parse_table(_fixture("licenses_10cols.xls"), source_format="xls")
        assert table.rows[0][4] == "2024-03-16"

    def test_number_cell_becomes_plain_integer_text(self) -> None:
        table = parse_table(_fixture("licenses_10cols.xls"), source_format="xls")
        assert table.rows[0][5] == "2027"  # не "2027.0"

    def test_contract_number_text_is_kept_as_is(self) -> None:
        table = parse_table(_fixture("licenses_10cols.xls"), source_format="xls")
        assert table.rows[0][3] == "Д-12/2024"

    def test_date_cell_in_a_1904_book_gives_the_same_calendar_day(self) -> None:
        table = parse_table(_fixture("licenses_date1904.xls"), source_format="xls")
        assert table.rows[0][4] == "2024-03-16"

    def test_empty_file_is_a_clear_error(self) -> None:
        with pytest.raises(AppError, match="пуст"):
            parse_table(_fixture("empty.xls"), source_format="xls")

    def test_truncated_file_is_a_clear_error_not_a_crash(self) -> None:
        truncated = _fixture("licenses_10cols.xls")[:100]
        with pytest.raises(AppError):
            parse_table(truncated, source_format="xls")

    def test_xlsx_renamed_to_xls_is_a_clear_error(self) -> None:
        # Сигнатура xlsx (ZIP, "PK\x03\x04") — не OLE, xlrd не откроет.
        fake = b"PK\x03\x04" + b"\x00" * 100
        with pytest.raises(AppError):
            parse_table(fake, source_format="xls")


class TestRealXlsMultisheet:
    def test_list_sheets_names_all_three_including_hidden(self) -> None:
        sheets = {
            s.name: s for s in list_sheets(_fixture("licenses_multisheet.xls"), source_format="xls")
        }
        assert set(sheets) == {"2024", "2025", "Черновик"}
        assert sheets["2024"].visible
        assert sheets["2025"].visible
        assert not sheets["Черновик"].visible

    def test_suggested_header_row_finds_row_three_on_first_sheet(self) -> None:
        sheets = {
            s.name: s
            for s in list_sheets(
                _fixture("licenses_multisheet.xls"),
                source_format="xls",
                expected_labels=LICENSE_HEADERS,
            )
        }
        assert sheets["2024"].suggested_header_row == 3

    def test_parsing_the_first_sheet_with_the_right_header_row_works(self) -> None:
        table = parse_table(
            _fixture("licenses_multisheet.xls"), source_format="xls", sheet="2024", header_row=3
        )
        assert table.headers == LICENSE_HEADERS
        # `xlrd` дополняет строку до ширины листа (10 колонок — по шапке), не обрезая её до числа
        # явно записанных xlwt-ячеек.
        assert table.rows == [
            [
                "МГТУ им. Н.Э. Баумана",
                "ООО «Базис»",
                "RT.DataLake",
                "Д-1-2024",
                "",
                "",
                "",
                "",
                "",
                "",
            ]
        ]
        assert table.source_rows == [4]

    def test_merging_visible_sheets_excludes_the_hidden_draft(self) -> None:
        # Листы «2024» (шапка в строке 3) и «2025» (шапка в строке 1) имеют шапку в разных
        # местах — без `expected_labels` для автопоиска по листу пришлось бы указывать общий
        # `header_row`, а его здесь нет; `sheets=["*"]` без `header_row` определяет его на
        # каждом листе отдельно (B3-2), после чего шапки совпадают и листы склеиваются.
        table = parse_table(
            _fixture("licenses_multisheet.xls"),
            source_format="xls",
            sheets=["*"],
            expected_labels=LICENSE_HEADERS,
        )
        assert table.headers == LICENSE_HEADERS
        assert set(table.sheet_names) == {"2024", "2025"}
        assert "Черновик" not in table.sheet_names


class TestSameContentAcrossFormats:
    """Один и тот же логический файл в трёх форматах даёт одну и ту же таблицу (кроме типов
    значений, уже приведённых к строке `_cell_to_text`/`csv`)."""

    @pytest.mark.parametrize("fmt", ["xlsx", "xls", "csv"])
    def test_headers_and_rows_match(self, fmt: str) -> None:
        if fmt == "xls":
            content = _fixture("licenses_10cols.xls")
        elif fmt == "xlsx":
            import io

            import openpyxl

            workbook = openpyxl.Workbook()
            sheet = workbook.active
            sheet.append(LICENSE_HEADERS)
            sheet.append(
                [
                    "МГТУ им. Н.Э. Баумана",
                    "ООО «Базис»",
                    "RT.DataLake",
                    "Д-12/2024",
                    "2024-03-16",
                    "2027",
                    "передано",
                    "Иванов Иван Иванович",
                    "",
                    "",
                ]
            )
            buffer = io.BytesIO()
            workbook.save(buffer)
            content = buffer.getvalue()
        else:
            rows = [
                LICENSE_HEADERS,
                [
                    "МГТУ им. Н.Э. Баумана",
                    "ООО «Базис»",
                    "RT.DataLake",
                    "Д-12/2024",
                    "2024-03-16",
                    "2027",
                    "передано",
                    "Иванов Иван Иванович",
                    "",
                    "",
                ],
            ]
            content = ("\n".join(";".join(row) for row in rows)).encode("utf-8-sig")

        table = parse_table(content, source_format=fmt)
        assert table.headers == LICENSE_HEADERS
        assert table.rows == [
            [
                "МГТУ им. Н.Э. Баумана",
                "ООО «Базис»",
                "RT.DataLake",
                "Д-12/2024",
                "2024-03-16",
                "2027",
                "передано",
                "Иванов Иван Иванович",
                "",
                "",
            ]
        ]
