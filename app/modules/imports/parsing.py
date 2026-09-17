"""Разбор файлов импорта: .xlsx/.xls/.csv (new_spec §4.12, раздел 4.12 dop.md).

Единый выход — построчные списки строк (`str`), заголовок отдельно: остальной
пайплайн (профилирование, маппинг, валидация) работает с текстом, не с типами
конкретного формата. Это стоит немного точности (ячейка-дата в xlsx превращается
в строку), но избавляет валидаторы полей от разбора трёх разных наборов типов
(`openpyxl`/`xlrd`/`csv` возвращают разные вещи для одного и того же понятия
«дата»), и предсказуемо: `str(cell)` для любого формата.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from collections.abc import Iterator
from dataclasses import dataclass

import openpyxl
import xlrd
from charset_normalizer import from_bytes

from app.core.errors import AppError, ErrorCode

# Раздел 4.12: лимит на файл — 50 МБ / 100 000 строк, здесь только защита от
# явно нечитаемых файлов; лимит по числу строк применяется выше, в сервисе
# (после того, как известен реальный `total_rows`), где он же попадает в отчёт.
_XLS_DATE_MODE_DEFAULT = 0


@dataclass(slots=True)
class ParsedTable:
    headers: list[str]
    rows: list[list[str]]


def _cell_to_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, dt.datetime):
        if value.time() == dt.time(0, 0):
            return value.date().isoformat()
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return repr(value)
    return str(value).strip()


def _detect_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=";,\t").delimiter
    except csv.Error:
        # dop.md §4.12: «;» — стандартный разделитель русского Excel.
        return ";"


def parse_csv(content: bytes) -> ParsedTable:
    detection = from_bytes(content).best()
    if detection is None:
        raise AppError(ErrorCode.IMPORT_BAD_FORMAT, "Не удалось определить кодировку файла")
    text = str(detection)
    # BOM уже снят `charset_normalizer` при декодировании UTF-8-BOM.
    sample = text[:4096]
    delimiter = _detect_delimiter(sample)
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = [[cell.strip() for cell in row] for row in reader if any(cell.strip() for cell in row)]
    if not rows:
        raise AppError(ErrorCode.IMPORT_BAD_FORMAT, "Файл пуст")
    return ParsedTable(headers=rows[0], rows=rows[1:])


def parse_xlsx(content: bytes) -> ParsedTable:
    try:
        workbook = openpyxl.load_workbook(
            io.BytesIO(content), read_only=True, data_only=True
        )
    except Exception as exc:  # noqa: BLE001 — любая ошибка формата -> понятный CRM-код
        raise AppError(
            ErrorCode.IMPORT_BAD_FORMAT, f"Файл повреждён или не является .xlsx: {exc}"
        ) from exc

    try:
        sheet = workbook.worksheets[0]
        rows_iter = sheet.iter_rows(values_only=True)
        try:
            header_row = next(rows_iter)
        except StopIteration as exc:
            raise AppError(ErrorCode.IMPORT_BAD_FORMAT, "Файл пуст") from exc

        headers = [_cell_to_text(c) for c in header_row]
        rows = [
            [_cell_to_text(c) for c in row]
            for row in rows_iter
            if any(c is not None and str(c).strip() for c in row)
        ]
    finally:
        workbook.close()

    return ParsedTable(headers=headers, rows=rows)


def parse_xls(content: bytes) -> ParsedTable:
    try:
        workbook = xlrd.open_workbook(file_contents=content)
    except Exception as exc:  # noqa: BLE001
        raise AppError(
            ErrorCode.IMPORT_BAD_FORMAT, f"Файл повреждён или не является .xls: {exc}"
        ) from exc

    sheet = workbook.sheet_by_index(0)
    if sheet.nrows == 0:
        raise AppError(ErrorCode.IMPORT_BAD_FORMAT, "Файл пуст")

    def _row(index: int) -> list[str]:
        out = []
        for cell in sheet.row(index):
            if cell.ctype == xlrd.XL_CELL_DATE:
                value = xlrd.xldate_as_datetime(cell.value, workbook.datemode)
                out.append(_cell_to_text(value))
            elif cell.ctype == xlrd.XL_CELL_NUMBER:
                out.append(_cell_to_text(cell.value))
            else:
                out.append(str(cell.value).strip())
        return out

    headers = _row(0)
    rows = [values for i in range(1, sheet.nrows) if any(values := _row(i))]
    return ParsedTable(headers=headers, rows=rows)


def parse_table(content: bytes, *, source_format: str) -> ParsedTable:
    fmt = source_format.lower().lstrip(".")
    if fmt == "xlsx":
        return parse_xlsx(content)
    if fmt == "xls":
        return parse_xls(content)
    if fmt == "csv":
        return parse_csv(content)
    raise AppError(ErrorCode.IMPORT_BAD_FORMAT, f"Формат {source_format!r} не поддерживается")


def sanitize_formula(value: str) -> str:
    """Нейтрализует потенциальную формулу перед записью в файл, который
    откроют в Excel (раздел 4.12: «запрет формул», CSV-инъекция). Экранирует
    так же, как это делает сам Excel — ведущим апострофом, а не отбрасыванием
    значения: данные не теряются, просто не исполняются как формула."""
    if value and value[0] in ("=", "+", "-", "@"):
        return f"'{value}"
    return value


def preview_rows(table: ParsedTable, *, limit: int = 100) -> Iterator[list[str]]:
    """Раздел 4.12, фаза 2: «читаем первые 100 строк для превью»."""
    return iter(table.rows[:limit])
