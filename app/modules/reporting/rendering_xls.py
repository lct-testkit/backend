"""Формат xls (B1-4): Функц.4/Функц.8 называют его первым из трёх форматов экспорта («xls, xlsx,
pdf»), рядом с «Пользователь п.3». `xlwt` — единственная чистая-Python библиотека, которая пишет
настоящий BIFF8/OLE2 без системных пакетов (политика `Dockerfile` — ни одного apt-пакета в
образе): колесо `xlwt-1.3.0-py2.py3-none-any.whl` импортируется и пишет файл на CPython 3.12,
результат начинается с сигнатуры `d0cf11e0a1b11ae1` и читается обратно `xlrd` (уже в lock,
используется импортом каталогов, `app/modules/imports/parsing.py`) без потери кириллицы/₽ и с
датами как датами, не строками — проверено локально при подготовке задачи.

Риски библиотеки (не развивается с 2017 года, лимиты формата) закрыты изоляцией в одной функции
`render_xls`: замена на другую реализацию (собственный writer, LibreOffice headless) не меняет
сигнатуру и не требует правок вызывающего кода (`reporting.rendering.render_report`).

Известные жёсткие ограничения формата xls (BIFF8), проверенные при подготовке задачи:

* не более 65 536 строк на лист (`XLS_MAX_ROWS` — с запасом под строку заголовка);
* не более 32 767 знаков в ячейке — длиннее обрезается, не роняет рендер;
* `datetime` с часовым поясом (`tzinfo` не `None`) заставляет `xlwt` упасть `TypeError` —
  переводится в пояс пользователя и становится наивным ДО записи в ячейку.
"""

from __future__ import annotations

import datetime as dt
import io
import re
import zoneinfo
from decimal import Decimal
from typing import Any

import xlwt

from app.modules.imports.parsing import sanitize_formula
from app.modules.reporting.builders import DEFAULT_TIMEZONE_NAME, ReportDataset

#: 65 536 строк — предел формата (16-битный индекс строки BIFF8). Одна строка уходит под шапку
#: таблицы (заголовки колонок) — реальный лимит данных на единицу меньше.
XLS_MAX_ROWS = 65_535
#: Предел длины значения ячейки в BIFF8 (глава 2.3 бинарного формата: 16-битная длина строки).
_XLS_MAX_CELL_LEN = 32_767
#: Запрещённые в имени листа Excel знаки и ограничение длины (31 символ, то же правило для xls
#: и xlsx — функция переиспользуется `reporting.rendering.render_xlsx`).
_FORBIDDEN_SHEET_CHARS = re.compile(r"[\[\]:\*\?/\\]")


class ReportTooLargeError(Exception):
    """Число строк отчёта превышает предел формата (`XLS_MAX_ROWS` у xls). `ReportJobService`
    превращает это в понятное сообщение заданию (`failed`), а не в 500."""


def sheet_title(title: str) -> str:
    """Имя листа Excel: не длиннее 31 знака, без `[]:*?/\\`. Общая для xls и xlsx (B1-4/B1-6) —
    раньше `render_xlsx` просто резал строку до 31 знака, не трогая запрещённые символы, что
    могло дать невалидное имя листа (например, название отчёта с `/`)."""
    cleaned = _FORBIDDEN_SHEET_CHARS.sub("_", title).strip()
    return (cleaned or "Отчёт")[:31]


def _xls_text(value: str) -> str:
    """Та же защита от CSV/формульной инъекции, что уже применяет `rendering._xlsx_cell`
    (`imports.parsing.sanitize_formula`), плюс обрезка до предела ячейки BIFF8 — длинное
    значение не должно ронять весь рендер."""
    text = sanitize_formula(value)
    if len(text) > _XLS_MAX_CELL_LEN:
        text = text[: _XLS_MAX_CELL_LEN - 1] + "…"
    return text


def resolve_tz(tz_name: str | None) -> dt.tzinfo:
    """Пояс по IANA-имени (B1-6) — неизвестное/отсутствующее имя откатывается на пояс по
    умолчанию (`DEFAULT_TIMEZONE_NAME`), а не роняет весь рендер."""
    try:
        return zoneinfo.ZoneInfo(tz_name) if tz_name else zoneinfo.ZoneInfo(DEFAULT_TIMEZONE_NAME)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return zoneinfo.ZoneInfo(DEFAULT_TIMEZONE_NAME)


def _to_naive(value: dt.datetime, tz: dt.tzinfo) -> dt.datetime:
    """`xlwt` не умеет aware-`datetime` (падает `TypeError`) — Excel вообще не хранит пояс в
    ячейке даты, поэтому значение переводится в пояс ПОЛЬЗОВАТЕЛЯ (B1-6, `tz`, не UTC — черновик
    B1-4 переводил в UTC как временную меру до появления контекста запроса в рендере) и
    становится наивным."""
    if value.tzinfo is not None:
        return value.astimezone(tz).replace(tzinfo=None)
    return value


class _XlsStyles:
    def __init__(self) -> None:
        self.header = xlwt.easyxf(
            "font: bold on; "
            "pattern: pattern solid, fore_colour gray25; "
            "borders: left thin, right thin, top thin, bottom thin; "
            "align: wrap on, vert centre;"
        )
        self.text = xlwt.easyxf("align: wrap on, vert top;")
        self.integer = xlwt.easyxf(num_format_str="0")
        self.money = xlwt.easyxf(num_format_str="#,##0.00")
        self.percent = xlwt.easyxf(num_format_str="0.0")
        self.days = xlwt.easyxf(num_format_str="0.0")
        self.date = xlwt.easyxf(num_format_str="DD.MM.YYYY")
        self.datetime = xlwt.easyxf(num_format_str="DD.MM.YYYY HH:MM")

    def for_kind(self, kind: str | None) -> Any:
        return {
            "int": self.integer,
            "money": self.money,
            "pct": self.percent,
            "days": self.days,
            "date": self.date,
            "datetime": self.datetime,
        }.get(kind or "text", self.text)


def _column_width(title: str) -> int:
    """Ширина колонки в единицах xlwt (1/256 символа) — эвристика по заголовку, без замера
    значений (точный расчёт по 95-му процентилю значений, как у B1-6, тогда и переедет сюда).
    Границы 8–60 символов — те же, что задаёт план B1-6 для xlsx, чтобы оба формата выглядели
    согласованно уже сейчас."""
    chars = max(8, min(60, len(title) + 2))
    return 256 * chars


def render_xls(dataset: ReportDataset, *, tz_name: str | None = None) -> bytes:
    """Рендер `ReportDataset` в xls (BIFF8). Лимит строк проверяется ДО записи — `ReportJobService`
    должен отловить `ReportTooLargeError` и завершить задание понятным сообщением, а не падать на
    середине записи файла. `tz_name` (B1-6) — пояс пользователя для перевода `datetime`;
    `None` (вызов без контекста, например прямые тесты) откатывается на `DEFAULT_TIMEZONE_NAME`."""
    if len(dataset.rows) > XLS_MAX_ROWS:
        raise ReportTooLargeError(
            f"xls вмещает не более {XLS_MAX_ROWS} строк: выберите xlsx или сузьте период "
            f"(в отчёте {len(dataset.rows)})"
        )

    book = xlwt.Workbook(encoding="utf-8")
    sheet = book.add_sheet(sheet_title(dataset.title))
    styles = _XlsStyles()
    tz = resolve_tz(tz_name)
    kinds: list[str | None] = (
        list(dataset.kinds) if dataset.kinds is not None else [None] * len(dataset.columns)
    )

    for col_index, title in enumerate(dataset.columns):
        sheet.write(0, col_index, title, styles.header)
        sheet.col(col_index).width = _column_width(title)

    for row_index, row in enumerate(dataset.rows, start=1):
        for col_index, value in enumerate(row):
            kind = kinds[col_index] if col_index < len(kinds) else None
            style = styles.for_kind(kind)
            if value is None:
                sheet.write(row_index, col_index, "", style)
            elif isinstance(value, dt.datetime):
                sheet.write(row_index, col_index, _to_naive(value, tz), style)
            elif isinstance(value, dt.date):
                sheet.write(row_index, col_index, value, style)
            elif isinstance(value, bool):
                sheet.write(row_index, col_index, str(value), styles.text)
            elif isinstance(value, Decimal):
                # B1-6: `Decimal` (значения денежных колонок до приведения builder'ом к `float`)
                # не совпадало ни с одной веткой выше и падало в текст ниже — число отображалось
                # строкой и не суммировалось в Excel.
                sheet.write(row_index, col_index, float(value), style)
            elif isinstance(value, int | float):
                sheet.write(row_index, col_index, value, style)
            else:
                sheet.write(row_index, col_index, _xls_text(str(value)), styles.text)

    # Шапка закреплена (видна при скролле), лист альбомный — те же требования, что B1-6 ставит
    # перед xlsx/pdf: отчёт в несколько колонок не должен превращаться в портретную простыню.
    sheet.set_panes_frozen(True)
    sheet.set_horz_split_pos(1)
    sheet.set_portrait(False)

    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()
