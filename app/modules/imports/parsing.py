"""Разбор файлов импорта: .xlsx/.xls/.csv (new_spec §4.12, раздел 4.12 dop.md).

Единый выход — построчные списки строк (`str`), заголовок отдельно: остальной
пайплайн (профилирование, маппинг, валидация) работает с текстом, не с типами
конкретного формата. Это стоит немного точности (ячейка-дата в xlsx превращается
в строку), но избавляет валидаторы полей от разбора трёх разных наборов типов
(`openpyxl`/`xlrd`/`csv` возвращают разные вещи для одного и того же понятия
«дата»), и предсказуемо: `str(cell)` для любого формата.

B3-2 (книга из нескольких листов): строки нумеруются АБСОЛЮТНО — как в Excel,
включая пустые строки, которые не входят в `rows`, но учитываются при счёте.
Поэтому `source_rows[i]` — это номер строки `rows[i]` в файле, а не порядковый
номер среди непустых строк: шапка в строке 4 после титула и двух пустых строк
даёт `source_rows[0] == 5`, даже если между шапкой и первой строкой данных
пустых строк не было.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

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
    # B3-2: имя листа, из которого взят разбор, и номер строки заголовка (с 1) — для `ImportOptions`
    # и отчёта об ошибках (строка Excel, не порядковый номер среди непустых). Пустая строка по
    # умолчанию для старых вызовов (CSV/JSON листов не знают).
    sheet_name: str = ""
    header_row: int = 1
    #: Номер строки Excel (с 1, считая пустые строки) для каждой строки `rows` — для отчёта об
    #: ошибках (B3-5) и `ImportRowResult.source_row`. Для CSV/JSON — порядковый номер данных (с 1,
    #: без шапки), т.к. у этих форматов нет понятия «пустая строка книги».
    source_rows: list[int] = field(default_factory=list)
    #: Имя листа для каждой строки `rows` — при `sheets=["*"]` разные строки приходят с разных
    #: листов; для одиночного листа/CSV/JSON — один и тот же `sheet_name` (может быть "") повторно.
    sheet_names: list[str] = field(default_factory=list)
    #: B3-2 (`fill_down`): какие ячейки получили значение из строки выше — `(номер_строки_Excel,
    #: подпись_колонки)`. Источник предупреждений dry-run «значение взято из строки N».
    filled_cells: list[tuple[int, str]] = field(default_factory=list)


@dataclass(slots=True)
class SheetInfo:
    """Один лист книги для экрана выбора при импорте (B3-2): сколько в нём строк, виден ли он
    (скрытые листы не предлагаются по умолчанию) и где эвристика нашла заголовок."""

    name: str
    rows: int
    visible: bool
    suggested_header_row: int


# Сколько первых строк листа смотрит автопоиск шапки — дальше заголовка в реальных файлах не
# бывает, а полный проход по книге на поиск шапки был бы дорог для больших листов.
_HEADER_SEARCH_LIMIT = 30
# Меньше трёх совпадений с ожидаемыми заголовками — эвристика не уверена, безопаснее начать с
# первой строки и дать пользователю поправить вручную, чем угадать не туда.
_HEADER_MATCH_MIN = 3


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
        if not math.isfinite(value):
            # `inf`/`nan` из ячейки Excel или `1e999` из JSON: строка «inf» дальше прошла бы в
            # `Decimal`/`int` и уронила применение. Пустое значение даёт понятную ошибку поля.
            return ""
        if value.is_integer():
            return str(int(value))
        return repr(value)
    return str(value).strip()


def _is_blank_row(row: list[str]) -> bool:
    return not any(cell.strip() for cell in row)


def _normalize_label(value: str) -> str:
    return " ".join(value.strip().casefold().split())


def _levenshtein(a: str, b: str) -> int:
    """Копия `imports.mapping._levenshtein` — для `detect_header_row`, которая должна остаться
    чистой функцией парсинга и не зависеть от `mapping.py` (словарь синонимов туда ещё не
    добрался бы на этапе, когда лист/шапка только выбираются)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
        previous = current
    return previous[-1]


def detect_header_row(rows: list[list[str]], expected_labels: list[str]) -> int:
    """B3-2: номер строки заголовка (с 1) среди первых `_HEADER_SEARCH_LIMIT` строк листа —
    эвристика для реальных файлов с титульной строкой и пустыми строками перед шапкой.

    `rows` — строки листа В ПОРЯДКЕ КНИГИ, включая пустые (позиция в списке = номер строки Excel
    минус 1): иначе результат не совпал бы с тем, что увидит разбор при том же `header_row`.

    Каждая ячейка строки сравнивается с каждым ожидаемым заголовком (`fields_for(entity_type)`,
    передаются вызывающим): совпадение — нормализованное равенство или расстояние Левенштейна не
    больше четверти длины. Строка с наибольшим числом совпадений побеждает; меньше
    `_HEADER_MATCH_MIN` совпадений — результат `1` (первая строка, доверять эвристике рискованно).
    """
    normalized_labels = [_normalize_label(label) for label in expected_labels if label.strip()]
    if not normalized_labels:
        return 1

    best_row, best_score = 1, -1
    for offset, row in enumerate(rows[:_HEADER_SEARCH_LIMIT], start=1):
        normalized_cells = [_normalize_label(cell) for cell in row if cell.strip()]
        if not normalized_cells:
            continue
        score = 0
        for label in normalized_labels:
            threshold = max(1, len(label) // 4)
            if any(
                cell == label or _levenshtein(cell, label) <= threshold for cell in normalized_cells
            ):
                score += 1
        if score > best_score:
            best_row, best_score = offset, score

    return best_row if best_score >= _HEADER_MATCH_MIN else 1


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
    return ParsedTable(
        headers=rows[0],
        rows=rows[1:],
        source_rows=list(range(1, len(rows))),
        sheet_names=[""] * (len(rows) - 1),
    )


def _finalize_sheet(
    all_rows: list[list[str]],
    *,
    header_row: int,
    fill_down: Sequence[str] = (),
) -> tuple[list[str], list[list[str]], list[int], list[tuple[int, str]]]:
    """Общая часть `parse_xlsx`/`parse_xls`: разделить лист (все строки, включая пустые, в порядке
    книги) на шапку и данные, протянуть `fill_down` по объединённым ячейкам и отбросить пустые
    строки данных, не теряя при этом абсолютную нумерацию строк Excel."""
    if not all_rows:
        raise AppError(ErrorCode.IMPORT_BAD_FORMAT, "Файл пуст")
    row_index = header_row - 1
    if not 0 <= row_index < len(all_rows):
        raise AppError(
            ErrorCode.IMPORT_BAD_FORMAT,
            f"Строка заголовка {header_row} вне диапазона листа (строк: {len(all_rows)})",
        )
    headers = all_rows[row_index]
    # Копия: `fill_down` ниже мутирует ячейки на месте, исходные строки книги (например, при
    # склейке нескольких листов) не должны меняться для соседних вызовов.
    data = [list(row) for row in all_rows[row_index + 1 :]]

    target_idx = [i for i, h in enumerate(headers) if h in fill_down] if fill_down else []
    filled: list[tuple[int, str]] = []
    if target_idx:
        last_value: dict[int, str] = {}
        for offset, row in enumerate(data):
            abs_row = row_index + 2 + offset
            if _is_blank_row(row):
                # Полностью пустая строка разделяет блоки объединённых ячеек — не переносить
                # значение через неё.
                last_value.clear()
                continue
            for idx in target_idx:
                if idx >= len(row):
                    continue
                if row[idx].strip():
                    last_value[idx] = row[idx]
                elif idx in last_value:
                    row[idx] = last_value[idx]
                    filled.append((abs_row, headers[idx]))

    kept_rows: list[list[str]] = []
    source_rows: list[int] = []
    for offset, row in enumerate(data):
        if _is_blank_row(row):
            continue
        kept_rows.append(row)
        source_rows.append(row_index + 2 + offset)

    return headers, kept_rows, source_rows, filled


def list_sheets_xlsx(
    content: bytes, *, expected_labels: list[str] | None = None
) -> list[SheetInfo]:
    """B3-2: листы книги для экрана выбора при импорте — имя, число строк данных, видимость,
    предполагаемая строка заголовка (если передан список ожидаемых колонок типа сущности)."""
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise AppError(
            ErrorCode.IMPORT_BAD_FORMAT, f"Файл повреждён или не является .xlsx: {exc}"
        ) from exc
    try:
        result: list[SheetInfo] = []
        for sheet in workbook.worksheets:
            all_rows = [
                [_cell_to_text(c) for c in row] for row in sheet.iter_rows(values_only=True)
            ]
            non_blank = sum(1 for row in all_rows if not _is_blank_row(row))
            suggested = detect_header_row(all_rows, expected_labels) if expected_labels else 1
            result.append(
                SheetInfo(
                    name=sheet.title,
                    rows=non_blank,
                    visible=sheet.sheet_state == "visible",
                    suggested_header_row=suggested,
                )
            )
        return result
    finally:
        workbook.close()


def _xlsx_sheet_rows(workbook: Any, sheet: str | None) -> tuple[str, list[list[str]]]:
    if sheet is not None:
        if sheet not in workbook.sheetnames:
            raise AppError(ErrorCode.IMPORT_BAD_FORMAT, f"Лист {sheet!r} не найден в файле")
        worksheet = workbook[sheet]
    else:
        worksheet = workbook.worksheets[0]
    all_rows = [[_cell_to_text(c) for c in row] for row in worksheet.iter_rows(values_only=True)]
    return worksheet.title, all_rows


def parse_xlsx(
    content: bytes,
    *,
    sheet: str | None = None,
    header_row: int | None = None,
    fill_down: Sequence[str] = (),
) -> ParsedTable:
    """`sheet`/`header_row` — B3-2 (книга из нескольких листов, шапка не в первой строке). Без
    них поведение буквально то же, что раньше (первый лист, первая строка — буквально, без
    пропуска пустых строк до неё): существующие вызовы ни одного из восьми типов сущности не
    меняют поведение."""
    try:
        workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 — любая ошибка формата -> понятный CRM-код
        raise AppError(
            ErrorCode.IMPORT_BAD_FORMAT, f"Файл повреждён или не является .xlsx: {exc}"
        ) from exc

    try:
        sheet_title, all_rows = _xlsx_sheet_rows(workbook, sheet)
        resolved_header_row = header_row or 1
        headers, rows, source_rows, filled = _finalize_sheet(
            all_rows, header_row=resolved_header_row, fill_down=fill_down
        )
    finally:
        workbook.close()

    return ParsedTable(
        headers=headers,
        rows=rows,
        sheet_name=sheet_title,
        header_row=resolved_header_row,
        source_rows=source_rows,
        sheet_names=[sheet_title] * len(rows),
        filled_cells=filled,
    )


def _xls_row(sheet: Any, workbook: Any, index: int) -> list[str]:
    out = []
    for cell in sheet.row(index):
        if cell.ctype == xlrd.XL_CELL_DATE:
            value = xlrd.xldate_as_datetime(cell.value, workbook.datemode)
            out.append(_cell_to_text(value))
        elif cell.ctype == xlrd.XL_CELL_NUMBER:
            out.append(_cell_to_text(cell.value))
        elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
            out.append(_cell_to_text(bool(cell.value)))
        else:
            out.append(str(cell.value).strip())
    return out


def list_sheets_xls(content: bytes, *, expected_labels: list[str] | None = None) -> list[SheetInfo]:
    """B3-2: аналог `list_sheets_xlsx` для .xls."""
    try:
        workbook = xlrd.open_workbook(file_contents=content, formatting_info=False)
    except Exception as exc:  # noqa: BLE001
        raise AppError(
            ErrorCode.IMPORT_BAD_FORMAT, f"Файл повреждён или не является .xls: {exc}"
        ) from exc

    result: list[SheetInfo] = []
    for sheet in workbook.sheets():
        all_rows = [_xls_row(sheet, workbook, i) for i in range(sheet.nrows)]
        non_blank = sum(1 for row in all_rows if not _is_blank_row(row))
        suggested = detect_header_row(all_rows, expected_labels) if expected_labels else 1
        result.append(
            SheetInfo(
                name=sheet.name,
                rows=non_blank,
                # 0 = видимый, 1 = скрытый, 2 = очень скрытый (запись BOUNDSHEET).
                visible=sheet.visibility == 0,
                suggested_header_row=suggested,
            )
        )
    return result


def _xls_sheet_rows(workbook: Any, sheet: str | None) -> tuple[Any, str, list[list[str]]]:
    if sheet is not None:
        try:
            worksheet = workbook.sheet_by_name(sheet)
        except xlrd.XLRDError as exc:
            raise AppError(
                ErrorCode.IMPORT_BAD_FORMAT, f"Лист {sheet!r} не найден в файле"
            ) from exc
    else:
        worksheet = workbook.sheet_by_index(0)
    all_rows = [_xls_row(worksheet, workbook, i) for i in range(worksheet.nrows)]
    return worksheet, worksheet.name, all_rows


def parse_xls(
    content: bytes,
    *,
    sheet: str | None = None,
    header_row: int | None = None,
    fill_down: Sequence[str] = (),
) -> ParsedTable:
    """См. докстринг `parse_xlsx` — те же гарантии обратной совместимости по умолчанию."""
    try:
        workbook = xlrd.open_workbook(file_contents=content)
    except Exception as exc:  # noqa: BLE001
        raise AppError(
            ErrorCode.IMPORT_BAD_FORMAT, f"Файл повреждён или не является .xls: {exc}"
        ) from exc

    _worksheet, sheet_name, all_rows = _xls_sheet_rows(workbook, sheet)
    resolved_header_row = header_row or 1
    headers, rows, source_rows, filled = _finalize_sheet(
        all_rows, header_row=resolved_header_row, fill_down=fill_down
    )

    return ParsedTable(
        headers=headers,
        rows=rows,
        sheet_name=sheet_name,
        header_row=resolved_header_row,
        source_rows=source_rows,
        sheet_names=[sheet_name] * len(rows),
        filled_cells=filled,
    )


def _reject_json_constant(token: str) -> Any:
    raise ValueError(f"недопустимая константа {token}")


def _json_cell(value: Any) -> str:
    if isinstance(value, dict | list):
        # Вложенное значение не разворачиваем в колонки: как строка оно хотя бы видно в отчёте
        # об ошибках, а не пропадает молча.
        return json.dumps(value, ensure_ascii=False)
    return _cell_to_text(value)


def parse_json(content: bytes) -> ParsedTable:
    """JSON-выгрузка сайта («Данные оплат.json»): массив объектов, заголовки — ключи.

    Элементы массива, не являющиеся объектами, пропускаются без ошибки: выгрузка сайта начинается с
    `null`, и это не повод отклонять весь файл. Корень-объект допустим, если в нём ровно один
    массив (`{"orders": [...]}`). `NaN`/`Infinity` в JSON недопустимы: иначе они превращались бы
    в числа, на которых падает применение.
    """
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        detection = from_bytes(content).best()
        if detection is None:
            raise AppError(
                ErrorCode.IMPORT_BAD_FORMAT, "Не удалось определить кодировку файла"
            ) from None
        text = str(detection)
    try:
        data = json.loads(text, parse_constant=_reject_json_constant)
    except ValueError as exc:
        raise AppError(
            ErrorCode.IMPORT_BAD_FORMAT, f"Файл не является корректным JSON: {exc}"
        ) from exc

    if isinstance(data, dict):
        arrays = [value for value in data.values() if isinstance(value, list)]
        if len(arrays) != 1:
            raise AppError(
                ErrorCode.IMPORT_BAD_FORMAT,
                "Ожидается массив объектов (или объект с одним массивом верхнего уровня)",
            )
        data = arrays[0]
    if not isinstance(data, list):
        raise AppError(ErrorCode.IMPORT_BAD_FORMAT, "Ожидается массив объектов")

    records = [item for item in data if isinstance(item, dict)]
    if not records:
        raise AppError(ErrorCode.IMPORT_BAD_FORMAT, "В файле нет ни одной записи-объекта")

    headers: dict[str, None] = {}
    for record in records:
        for key in record:
            headers.setdefault(str(key), None)
    names = list(headers)
    rows = [[_json_cell(record.get(name)) for name in names] for record in records]
    return ParsedTable(
        headers=names,
        rows=rows,
        source_rows=list(range(1, len(rows) + 1)),
        sheet_names=[""] * len(rows),
    )


def list_sheets(
    content: bytes, *, source_format: str, expected_labels: list[str] | None = None
) -> list[SheetInfo]:
    """B3-2: листы книги для экрана выбора при импорте; CSV/JSON — один «лист» без имени."""
    fmt = source_format.lower().lstrip(".")
    if fmt == "xlsx":
        return list_sheets_xlsx(content, expected_labels=expected_labels)
    if fmt == "xls":
        return list_sheets_xls(content, expected_labels=expected_labels)
    table = parse_table(content, source_format=source_format)
    header_row = (
        detect_header_row([table.headers, *table.rows], expected_labels) if expected_labels else 1
    )
    return [
        SheetInfo(name="", rows=len(table.rows) + 1, visible=True, suggested_header_row=header_row)
    ]


def _visible_sheet_names(content: bytes, *, fmt: str) -> list[str]:
    return [s.name for s in list_sheets(content, source_format=fmt) if s.visible]


def _open_book_rows(content: bytes, *, fmt: str, name: str) -> list[list[str]]:
    """Все строки одного именованного листа (включая пустые, в порядке книги) — общая часть
    `_parse_merged_sheets` для xlsx и xls, без промежуточного `ParsedTable`."""
    if fmt == "xlsx":
        try:
            workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        except Exception as exc:  # noqa: BLE001
            raise AppError(
                ErrorCode.IMPORT_BAD_FORMAT, f"Файл повреждён или не является .xlsx: {exc}"
            ) from exc
        try:
            _title, rows = _xlsx_sheet_rows(workbook, name)
        finally:
            workbook.close()
        return rows
    try:
        workbook = xlrd.open_workbook(file_contents=content)
    except Exception as exc:  # noqa: BLE001
        raise AppError(
            ErrorCode.IMPORT_BAD_FORMAT, f"Файл повреждён или не является .xls: {exc}"
        ) from exc
    _sheet, _name, rows = _xls_sheet_rows(workbook, name)
    return rows


def _parse_merged_sheets(
    content: bytes,
    *,
    fmt: str,
    sheets: list[str],
    header_row: int | None,
    fill_down: Sequence[str],
    expected_labels: list[str] | None,
) -> ParsedTable:
    """B3-2: `sheets=["*"]` (или явный список имён) — склейка нескольких листов за один импорт.
    Листы должны иметь одинаковую шапку (после разрешения `header_row`/автопоиска); иначе — понятная
    ошибка с именем первого несовпавшего листа, а не молчаливая склейка разнородных таблиц."""
    names = _visible_sheet_names(content, fmt=fmt) if sheets == ["*"] else list(sheets)
    if not names:
        raise AppError(ErrorCode.IMPORT_BAD_FORMAT, "В книге нет видимых листов для склейки")

    merged_headers: list[str] | None = None
    merged_rows: list[list[str]] = []
    merged_source_rows: list[int] = []
    merged_sheet_names: list[str] = []
    merged_filled: list[tuple[int, str]] = []
    resolved_header_row = 1

    for name in names:
        all_rows = _open_book_rows(content, fmt=fmt, name=name)
        if header_row is not None:
            sheet_header_row = header_row
        elif expected_labels:
            sheet_header_row = detect_header_row(all_rows, expected_labels)
        else:
            sheet_header_row = 1

        headers, rows, source_rows, filled = _finalize_sheet(
            all_rows, header_row=sheet_header_row, fill_down=fill_down
        )

        if merged_headers is None:
            merged_headers = headers
            resolved_header_row = sheet_header_row
        elif headers != merged_headers:
            raise AppError(
                ErrorCode.IMPORT_BAD_FORMAT,
                f"Лист {name!r}: другие заголовки ({', '.join(headers)}) — "
                "выберите один лист для импорта",
            )

        merged_rows.extend(rows)
        merged_source_rows.extend(source_rows)
        merged_sheet_names.extend([name] * len(rows))
        merged_filled.extend(filled)

    return ParsedTable(
        headers=merged_headers or [],
        rows=merged_rows,
        sheet_name="*",
        header_row=resolved_header_row,
        source_rows=merged_source_rows,
        sheet_names=merged_sheet_names,
        filled_cells=merged_filled,
    )


def parse_table(
    content: bytes,
    *,
    source_format: str,
    sheet: str | None = None,
    sheets: list[str] | None = None,
    header_row: int | None = None,
    fill_down: Sequence[str] = (),
    expected_labels: list[str] | None = None,
) -> ParsedTable:
    fmt = source_format.lower().lstrip(".")
    if sheets:
        if fmt not in ("xlsx", "xls"):
            raise AppError(
                ErrorCode.IMPORT_BAD_FORMAT,
                "Склейка нескольких листов поддерживается только для xlsx и xls",
            )
        return _parse_merged_sheets(
            content,
            fmt=fmt,
            sheets=sheets,
            header_row=header_row,
            fill_down=fill_down,
            expected_labels=expected_labels,
        )
    if fmt == "xlsx":
        return parse_xlsx(content, sheet=sheet, header_row=header_row, fill_down=fill_down)
    if fmt == "xls":
        return parse_xls(content, sheet=sheet, header_row=header_row, fill_down=fill_down)
    if fmt == "csv":
        return parse_csv(content)
    if fmt == "json":
        return parse_json(content)
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
