"""Рендеринг отчётов: xlsx / xls / pdf / png / json (new_spec §4.13, §2.3, §6; B1-4/B1-5/B1-6).

* **xlsx** — `openpyxl` в `write_only`-режиме (раздел 4.13: «стриминг,
  константная память») — тот же приём, что `imports.service` уже применяет к
  сгенерированным отчётам об ошибках импорта. Исключение по форме, не по режиму — файл для LMS
  (`lms_users_upload`): он повторяет шаблон заказчика, см. `render_lms_users_xlsx`. Ячейки
  дат/чисел получают `number_format` по `kind` каталога колонок (B1-2/B1-6), закреплённая
  шапка, автофильтр и альбомная печать на всю ширину (`render_xlsx`/`_xlsx_data_cell`).
* **xls** (B1-4) — `rendering_xls.render_xls`, `xlwt` (BIFF8/OLE2): единственная чистая-Python
  библиотека, которая пишет настоящий xls без системных пакетов. Лимит формата — 65 535 строк
  данных (`XLS_MAX_ROWS`), проверяется заранее в `reporting.service`.
* **pdf** — раздел 2.3 называет WeasyPrint, но `signing.rendering` (спринт 6)
  уже осознанно заменил его на `xhtml2pdf` (WeasyPrint тянет системные
  Pango/Cairo/GDK-Pixbuf, что противоречит принципу `Dockerfile`) и вложил
  шрифт DejaVu Sans для кириллицы. Здесь переиспользуется только разметка шрифта и рендер
  Jinja2 (`render_template_html`) — сборка PDF идёт через собственный `_html_to_pdf` (B1-8: свой
  `link_callback`, который помимо двух файлов шрифта разрешает ещё `data:` URI картинки графика;
  `signing.rendering.html_to_pdf` не трогаем — у него другой профиль риска, публичная страница
  подписания). Один Jinja2-шаблон таблицы на все виды отчёта: колонки/строки в отчёте
  динамические, а не поля с фиксированными именами, поэтому шаблон параметризован структурой
  `ReportDataset`, а не конкретным видом отчёта. Лист альбомный (B1-6, CSS `@page`) — вариант A
  сравнения задачи, без перехода на reportlab Platypus (повтора шапки таблицы на каждой
  странице и номеров страниц в этом черновике нет).
* **png** — раздел 2.3/4.13: «серверный matplotlib (не скриншот фронта — на
  сервере нет браузера)», backend `Agg` — без дисплея. matplotlib поставляет
  свой DejaVu Sans (та же гарнитура, что уже вложена для PDF) и уже
  поддерживает кириллицу без дополнительных шрифтов. Графики есть только для
  видов, где они осмысленны — `reporting.spec.ReportSpec.chart` (B1-2),
  остальные виды отчёта отдают только табличные форматы
  (`report_templates.output_formats` ограничивает выбор на уровне API).
  Построение самих фигур — `reporting.charts` (B1-8): один модуль на PNG,
  встроенную картинку в PDF (первая страница, под шапкой) и второй лист
  «График» в xlsx — то же изображение во всех трёх форматах.
* **json** (B1-5) — `render_json`, тот же `ReportDataset`, что рендерится в остальные форматы,
  версионированным конвертом (`schema`/`report`/`columns`/`rows`/`totals`) — тот же набор
  данных, что и `GET /reports/{id}/data`, но файлом с записью в аудит.
"""

from __future__ import annotations

import base64
import datetime as dt
import io
import json
import re
import uuid
from decimal import Decimal
from typing import Any

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.drawing.image import Image as XLImage
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from PIL import Image as PILImage
from xhtml2pdf import pisa

from app.modules.catalog import learner
from app.modules.imports.parsing import sanitize_formula
from app.modules.reporting import charts
from app.modules.reporting.builders import (
    DEFAULT_TIMEZONE_NAME,
    LMS_USERS_UPLOAD,
    ReportDataset,
)
from app.modules.reporting.models import ReportFormat
from app.modules.reporting.rendering_xls import (
    ReportTooLargeError,
    render_xls,
    resolve_tz,
    sheet_title,
)
from app.modules.reporting.result_schema import (
    REPORT_RESULT_SCHEMA_VERSION,
    ReportResultColumn,
    ReportResultReport,
    ReportResultTotals,
    ReportResultV1,
)
from app.modules.reporting.spec import derived_chart_kinds, get_spec
from app.modules.signing.rendering import (
    FONT_BOLD_PATH,
    FONT_REGULAR_PATH,
    RenderError,
    render_template_html,
)

__all__ = [
    "CHART_KINDS",
    "CONTENT_TYPES",
    "FILE_EXTENSIONS",
    "RenderNotSupportedError",
    "ReportTooLargeError",
    "render_json",
    "render_lms_users_xlsx",
    "render_pdf",
    "render_png",
    "render_report",
    "render_xls",
    "render_xlsx",
    "report_file_name",
]

CONTENT_TYPES: dict[str, str] = {
    ReportFormat.XLSX.value: ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    # B1-4: `application/vnd.ms-excel` — тип содержимого классического BIFF8/OLE2, а не xlsx.
    ReportFormat.XLS.value: "application/vnd.ms-excel",
    ReportFormat.PDF.value: "application/pdf",
    ReportFormat.PNG.value: "image/png",
    ReportFormat.JSON.value: "application/json",
}

#: Расширение файла по формату — B1-4/B1-5: раньше имя файла строилось как `f"{title}.{job.format}"`
#: (`reporting.service.generate`), что совпадало с форматом случайно (xlsx/pdf/png — расширение
#: и код формата совпадают); xls и json тоже совпадают буквально, но словарь делает это явным и
#: единообразным местом правки, если когда-нибудь появится формат, где это не так.
FILE_EXTENSIONS: dict[str, str] = {fmt.value: fmt.value for fmt in ReportFormat}

#: Запрещённые в имени файла Windows знаки (`\/:*?"<>`) и управляющие символы (B1-6) — то же
#: правило, что сайт заказчика предполагает для скачанного файла независимо от ОС пользователя.
_FORBIDDEN_FILENAME_CHARS = re.compile(r'[\\/:*?"<>\x00-\x1f]')
#: Предел длины имени файла без расширения (B1-6) — длинное название отчёта/параметров не должно
#: упираться в ограничения файловой системы (Windows — 260 знаков на весь путь, с запасом).
_MAX_FILENAME_LENGTH = 100


def report_file_name(title: str, generated_at: dt.datetime, extension: str) -> str:
    """Имя файла отчёта (B1-6): `{название}_{ГГГГ-ММ-ДД}.{расширение}` — раньше (`f"{dataset.
    title}.{job.format}"`, `reporting.service.generate`) повторная выгрузка одного и того же
    вида отчёта в разные дни давала одинаковое имя файла, браузер различал их только
    `(1)`/`(2)` в скачанных копиях. Дата — по `generated_at` (момент рендера, не момент запроса
    задания: тяжёлый отчёт может уйти в очередь и построиться позже)."""
    safe_title = _FORBIDDEN_FILENAME_CHARS.sub("_", title).strip() or "Отчёт"
    name = f"{safe_title}_{generated_at.strftime('%Y-%m-%d')}"[:_MAX_FILENAME_LENGTH]
    return f"{name}.{extension}"


#: Виды отчёта, для которых определён график. Ключ — `report_templates.code`. B1-2/B1-8:
#: производный от `ReportSpec.chart` (`reporting.spec`), а не самостоятельный литерал — иначе
#: новый график в `ReportSpec` и регистрация в `reporting.charts.CHART_BUILDERS` могли бы
#: расползтись.
CHART_KINDS = derived_chart_kinds()


class RenderNotSupportedError(Exception):
    """PNG запрошен для вида отчёта без определённого графика, либо запрошен неизвестный формат
    файла (`render_report`).

    Не пользовательская ошибка в обычном смысле: `reporting.service`
    проверяет `format in template.output_formats` до вызова рендеринга,
    поэтому попадание сюда означает рассинхрон между посевом шаблонов и
    `CHART_KINDS`, а не некорректный ввод пользователя.
    """


def _chart_kind_for(template_code: str | None) -> str | None:
    """`ReportSpec.chart` вида отчёта, либо `None` — вид без кода (xls/xlsx-рендер вне
    `POST /api/reports`, тесты) или без зарегистрированного графика (B1-8: встраивание в
    pdf/xlsx — лучшее усилие, а не обязательное условие рендера файла)."""
    if not template_code:
        return None
    spec = get_spec(template_code)
    return spec.chart if spec else None


#: B1-6, сокращённый вариант A (полный план задачи сравнивал его с переходом на reportlab
#: Platypus — вариант B, не сделан в этом черновике): CSS `@page` на существующем xhtml2pdf,
#: без замены движка. Альбомная ориентация проверена (`tests/test_reporting.py` —
#: `mediabox.width > mediabox.height`); `thead { display: table-header-group }` — попытка
#: повторить строку заголовка на каждой печатной странице, поддержка xhtml2pdf для этого на
#: длинных таблицах не проверена (в отличие от ориентации) и не покрыта тестом.
#: B1-8: картинка графика (та же, что в PNG/xlsx) под шапкой, перед таблицей — высота не больше
#: 110мм (раздел "Что сделать" п.6), ширина ограничена полем страницы. `chart_png_base64` — `None`
#: у видов без графика (`render_pdf` не задаёт его вовсе) — тег не рендерится совсем.
#:
#: Размер картинки — явные `width`/`height` в мм, посчитанные в Python (`_chart_image_size_mm`),
#: а не CSS `max-width`/`max-height`: проверено отдельно (не в автотесте, вручную через
#: содержимое PDF) — `xhtml2pdf` не применяет `max-height` к `<img>` вовсе (игнорирует молча,
#: картинка остаётся в исходном пикселе-в-пункт размере и может растянуться на несколько
#: страниц), а явные `width`/`height` в мм — применяет точно.
_TABLE_TEMPLATE = """
<style>
  @page { size: A4 landscape; margin: 10mm; }
  table { width:100%; border-collapse:collapse; font-size:9px; }
  thead { display: table-header-group; }
  th { border:1px solid #999; padding:4px 8px; background:#eef1fa; text-align:left; }
  td { border:1px solid #ccc; padding:4px 8px; }
</style>
<h2>{{ title }}</h2>
{% if note %}<p><em>{{ note }}</em></p>{% endif %}
{% if chart_png_base64 %}<img src="data:image/png;base64,{{ chart_png_base64 }}"
  style="width:{{ chart_width_mm }}mm; height:{{ chart_height_mm }}mm;
  display:block; margin:4px 0 8px 0;" />
{% endif %}
<table>
  <thead>
    <tr>
      {% for col in columns %}
      <th>{{ col }}</th>
      {% endfor %}
    </tr>
  </thead>
  <tbody>
    {% for row in rows %}
    <tr>
      {% for cell in row %}
      <td>{{ "" if cell is none else cell }}</td>
      {% endfor %}
    </tr>
    {% endfor %}
  </tbody>
</table>
<p style="color:#666; font-size:9px;">Сформировано: {{ generated_at }}</p>
"""


def _xlsx_cell(value: Any) -> Any:
    """Раздел 4.12 «запрет формул» / CSV-инъекция: значения отчёта в
    конечном счёте происходят из свободных текстовых полей (название
    организации, ФИО КАМа, название сделки), которые пользователь мог
    ввести с ведущим `=`/`+`/`-`/`@`. `imports.parsing.sanitize_formula` уже
    решает это для сгенерированных xlsx импорта — тот же риск, тот же приём.

    Не знает про `kind` каталога колонок (B1-2) и не трогает дату/время — этим занимается
    `_xlsx_data_cell` в `render_xlsx`, эта функция осталась как есть (в т.ч. для существующих
    тестов `TestXlsxRendering`/`TestCsvInjectionProtection`) и используется ей как последний шаг
    для всего, что не дата/`Decimal`."""
    if value is None:
        return ""
    if isinstance(value, str):
        return sanitize_formula(value)
    if isinstance(value, int | float | bool):
        return value
    return str(value)


#: Числовой формат ячейки xlsx по `kind` каталога колонок (B1-2/B1-6) — те же форматы, что уже
#: применяет xls (`rendering_xls._XlsStyles`), чтобы оба формата показывали одно и то же число
#: одинаково оформленным.
_XLSX_NUMBER_FORMATS: dict[str, str] = {
    "int": "0",
    "money": "#,##0.00",
    "pct": "0.0",
    "days": "0.0",
    "date": "DD.MM.YYYY",
    "datetime": "DD.MM.YYYY HH:MM",
}


def _xlsx_column_width(title: str) -> float:
    """Ширина колонки по заголовку (B1-6) — та же эвристика 8–60 знаков, что уже применяет xls
    (`rendering_xls._column_width`), в единицах xlsx (символы, без множителя 1/256 у BIFF8).
    Точный расчёт по 95-му процентилю длины значений первых строк — из полного плана B1-6, в эту
    сокращённую реализацию не вошёл: оценка по одному заголовку даёт разумный результат уже
    сейчас и не требует второго прохода по строкам перед записью (write-only не допускает
    произвольный доступ назад к уже записанным ячейкам)."""
    return float(max(8, min(60, len(title) + 2)))


def _xlsx_data_cell(sheet: Any, value: Any, kind: str | None, tz: dt.tzinfo) -> Any:
    """Ячейка таблицы отчёта с учётом `kind` каталога колонок (B1-2/B1-6). Раньше (черновик
    B1-4/`_xlsx_cell`) `datetime`/`date` превращались в `str(value)` — ячейка в файле получала
    текст вида `2026-01-05 00:00:00+00:00` вместо настоящей даты Excel с форматом ячейки; то же
    было с `Decimal` (денежные колонки до приведения конкретным builder'ом к `float`) — число
    отображалось строкой и не суммировалось формулами Excel. `kind=None` (вид отчёта без
    каталога колонок, B1-2) — дата получает формат по умолчанию, не наследует поведение
    `_xlsx_cell`."""
    if isinstance(value, dt.datetime):
        local = value.astimezone(tz) if value.tzinfo is not None else value
        cell = WriteOnlyCell(sheet, value=local.replace(tzinfo=None))
        cell.number_format = _XLSX_NUMBER_FORMATS.get(kind or "datetime", "DD.MM.YYYY HH:MM")
        return cell
    if isinstance(value, dt.date):
        cell = WriteOnlyCell(sheet, value=value)
        cell.number_format = _XLSX_NUMBER_FORMATS.get(kind or "date", "DD.MM.YYYY")
        return cell
    if isinstance(value, Decimal):
        value = float(value)
    if (
        kind in _XLSX_NUMBER_FORMATS
        and isinstance(value, int | float)
        and not isinstance(value, bool)
    ):
        cell = WriteOnlyCell(sheet, value=value)
        cell.number_format = _XLSX_NUMBER_FORMATS[kind]
        return cell
    return _xlsx_cell(value)


def render_xlsx(
    dataset: ReportDataset, *, tz_name: str | None = None, template_code: str | None = None
) -> bytes:
    workbook = Workbook(write_only=True)
    # B1-4: `sheet_title` — общая с xls функция (`rendering_xls`), чистит запрещённые Excel
    # знаки (`[]:*?/\`) из названия отчёта, не только обрезает длину до 31 знака, как было.
    sheet = workbook.create_sheet(title=sheet_title(dataset.title))
    # B1-6: закреплённая шапка (первая строка видна при скролле) — у write-only листа openpyxl
    # `freeze_panes` должен быть задан ДО первого `append()`: присвоение после молча не попадает
    # в сохранённый файл (проверено round-trip через `load_workbook`), это не очевидно из
    # документации openpyxl.
    sheet.freeze_panes = "A2"
    tz = resolve_tz(tz_name)
    kinds: list[str | None] = (
        list(dataset.kinds) if dataset.kinds is not None else [None] * len(dataset.columns)
    )

    header_font = Font(bold=True)
    header_fill = PatternFill("solid", fgColor="D9D9D9")
    header_align = Alignment(wrap_text=True, vertical="center")
    header_cells = []
    for title in dataset.columns:
        header_cell = WriteOnlyCell(sheet, value=title)
        header_cell.font = header_font
        header_cell.fill = header_fill
        header_cell.alignment = header_align
        header_cells.append(header_cell)
    sheet.append(header_cells)

    for row in dataset.rows:
        sheet.append(
            [
                _xlsx_data_cell(sheet, value, kinds[index] if index < len(kinds) else None, tz)
                for index, value in enumerate(row)
            ]
        )

    for index, title in enumerate(dataset.columns, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = _xlsx_column_width(title)

    if dataset.columns:
        last_column = get_column_letter(len(dataset.columns))
        sheet.auto_filter.ref = f"A1:{last_column}{len(dataset.rows) + 1}"
    # Печать: альбомная ориентация на одну страницу в ширину, строка заголовка повторяется на
    # каждой печатной странице — те же требования B1-6, что уже выполняет xls (`render_xls`).
    sheet.print_title_rows = "1:1"
    sheet.page_setup.orientation = "landscape"
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.sheet_properties.pageSetUpPr.fitToPage = True

    # B1-8, п.7: второй лист «График» с той же картинкой, что PNG/PDF — не родная диаграмма
    # Excel, чтобы все форматы показывали одно и то же. `write_only`-книга поддерживает
    # `add_image` на отдельном листе так же, как уже поддерживает `data_validations.append`
    # (`render_lms_users_xlsx` чуть ниже) — проверено (openpyxl 3.1.5 + Pillow): картинка
    # переживает `load_workbook` round-trip.
    chart_kind = _chart_kind_for(template_code)
    if chart_kind is not None:
        try:
            png_bytes = charts.render_png(dataset, chart_kind=chart_kind)
        except charts.RenderNotSupportedError:
            png_bytes = None
        if png_bytes is not None:
            chart_sheet = workbook.create_sheet(title="График")
            chart_sheet.add_image(XLImage(io.BytesIO(png_bytes)), "A1")

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# Ширина колонок листа «Лист1» шаблона LMS — как в оригинале заказчика («Загрузка пользователей»),
# по порядку колонок `learner.LMS_USER_COLUMNS`. «Пол» (L) в оригинале без своей ширины — остаётся
# стандартной.
_LMS_COLUMN_WIDTHS: tuple[float | None, ...] = (
    23.86,  # A  Фамилия
    24.86,  # B  Имя
    24.0,  # C  Отчество
    22.14,  # D  Номер телефона
    20.71,  # E  Email
    14.71,  # F  СНИЛС
    14.57,  # G  Серия паспорта
    15.86,  # H  Номер паспорта
    19.57,  # I  Кем выдан паспорт
    20.43,  # J  Дата выдачи паспорта
    18.43,  # K  Код подразделения
    None,  # L  Пол
    15.29,  # M  Дата рождения
    18.14,  # N  Регион регистрации
    29.14,  # O  Населенный пункт регистрации
    17.71,  # P  Улица регистрации
    16.29,  # Q  Дом регистрации
    20.29,  # R  Квартира регистрации
    19.0,  # S  Индекс регистрации
    23.0,  # T  Имя (дательный падеж)
    27.0,  # U  Фамилия (дательный падеж)
    26.14,  # V  Отчество (дательный падеж)
    37.14,  # W  Образование
    21.43,  # X  Профессия по диплому
    29.57,  # Y  Учебное заведение по диплому
    29.0,  # Z  Фамилия, указанная в дипломе
    15.29,  # AA Номер диплома
    14.86,  # AB Серия диплома
    31.0,  # AC Регистрационный номер диплома
    22.0,  # AD Дата выдачи диплома
)
_LMS_LOOKUP_WIDTH = 58.14
_LMS_DATE_FORMAT = "DD.MM.YYYY"
# В оригинале первые шесть заголовков выровнены по центру, остальные — по умолчанию.
_LMS_CENTERED_HEADERS = 6


def _lms_cell(sheet: Any, value: Any) -> Any:
    """Ячейка данных шаблона LMS. Число (телефон `79990234365`) — числом, дата — настоящей датой
    Excel в формате `ДД.ММ.ГГГГ`, всё остальное — текстом (СНИЛС, серия и номер паспорта, индекс:
    иначе Excel съел бы ведущие нули и дефисы). Пустое остаётся пустой ячейкой.

    Текст проходит через `sanitize_formula`: значения приходят от людей (адрес, ФИО, название
    учебного заведения), и ячейка, начинающаяся с `=`, стала бы формулой — файл заказчик открывает в
    Excel. Формул в шаблоне нет."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.date):
        cell = WriteOnlyCell(sheet, value=value)
        cell.number_format = _LMS_DATE_FORMAT
        return cell
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return value
    return sanitize_formula(str(value))


def render_lms_users_xlsx(dataset: ReportDataset) -> bytes:
    """Файл для загрузки учащихся в LMS: повторяет шаблон заказчика `Загрузка пользователей.xlsx`.

    * `Лист1` — 30 заголовков буквально как в оригинале (в том числе `Отчествопри наличии)`: LMS
      сопоставляет колонки по этому тексту) и строки учащихся; ширина колонок как в оригинале.
    * `Лист2` — справочники: `М`/`Ж` в A1:A2 и семь уровней образования в B1:B7.
    * На «Пол» (L) и «Образование» (W) повешены выпадающие списки на этот справочник, на 1000 строк.

    Книга пишется в `write_only`-режиме, как остальные отчёты: константная память, а выпадающие
    списки, ширина колонок и второй лист в нём поддерживаются (ширина задаётся до записи первой
    строки листа, списки — до сохранения книги).
    """
    workbook = Workbook(write_only=True)
    users = workbook.create_sheet(title=learner.LMS_USERS_SHEET)
    lookup = workbook.create_sheet(title=learner.LMS_LOOKUP_SHEET)

    for index, width in enumerate(_LMS_COLUMN_WIDTHS, start=1):
        if width is not None:
            users.column_dimensions[get_column_letter(index)].width = width
    lookup.column_dimensions["B"].width = _LMS_LOOKUP_WIDTH

    header_row = []
    for index, title in enumerate(dataset.columns):
        header = WriteOnlyCell(users, value=title)
        header.font = Font(name="Calibri", size=11, bold=True)
        if index < _LMS_CENTERED_HEADERS:
            header.alignment = Alignment(horizontal="center")
        header_row.append(header)
    users.append(header_row)
    for row in dataset.rows:
        users.append([_lms_cell(users, value) for value in row])

    sex_labels = list(learner.SEX_LABELS.values())
    education_labels = [label for _code, label in learner.EDUCATION_LEVELS]
    for position in range(max(len(sex_labels), len(education_labels))):
        lookup.append(
            [
                sex_labels[position] if position < len(sex_labels) else None,
                education_labels[position] if position < len(education_labels) else None,
            ]
        )

    targets = [target for target, _header in learner.LMS_USER_COLUMNS]
    for target, source in (
        ("sex", f"{learner.LMS_LOOKUP_SHEET}!$A$1:$A${len(sex_labels)}"),
        ("education", f"{learner.LMS_LOOKUP_SHEET}!$B$1:$B${len(education_labels)}"),
    ):
        validation = DataValidation(
            type="list", formula1=source, allow_blank=True, showErrorMessage=True
        )
        column = get_column_letter(targets.index(target) + 1)
        validation.add(f"{column}2:{column}{learner.LMS_VALIDATION_LAST_ROW}")
        # У write-only листа нет `add_data_validation`, но список проверок в нём есть и
        # сохраняется вместе с книгой.
        users.data_validations.append(validation)

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


#: Разрешённые для PDF-отчётов URI (B1-8) — те же два файла шрифта, что уже разрешает
#: `signing.rendering._deny_external_resources`, плюс сами `data:` URI (картинка графика,
#: см. докстринг шаблона выше). Отдельный `link_callback`, не правка файла `signing.rendering`
#: — у публичной страницы подписания свой, более строгий профиль риска (dop.md §10.11), сужать
#: его ради отчётов не нужно, как и расширять ради отчётов не следует.
_ALLOWED_PDF_FONT_URIS = frozenset({FONT_REGULAR_PATH.as_uri(), FONT_BOLD_PATH.as_uri()})


def _report_pdf_link_callback(uri: str, _rel: str) -> str:
    if uri.startswith("data:"):
        return uri
    if uri in _ALLOWED_PDF_FONT_URIS:
        return uri.removeprefix("file://")
    raise RenderError(f"Внешний ресурс запрещён в шаблоне отчёта: {uri!r}")


def _html_to_pdf(html: str) -> bytes:
    buffer = io.BytesIO()
    result = pisa.CreatePDF(html, dest=buffer, link_callback=_report_pdf_link_callback)
    if result.err:
        raise RenderError(f"Не удалось построить PDF из HTML (err={result.err})")
    return buffer.getvalue()


#: B1-8, п.6: высота картинки в PDF не больше 110мм; ширина — не больше поля печати (A4
#: альбомный минус поля по 10мм каждая сторона, `_TABLE_TEMPLATE`). dpi — тот же, что
#: `charts.figure_to_png` (130) — нужен, чтобы перевести пиксели PNG обратно в миллиметры.
_PDF_MAX_CHART_HEIGHT_MM = 110.0
_PDF_MAX_CHART_WIDTH_MM = 277.0
_CHART_PNG_DPI = 130


def _chart_image_size_mm(png_bytes: bytes) -> tuple[float, float]:
    """Явные `width`/`height` для вставки картинки в PDF, посчитанные в Python, а не в CSS:
    `xhtml2pdf` молча не применяет `max-width`/`max-height` к `<img>` (проверено отдельно руками,
    не автотестом — картинка оставалась в исходном пиксель-в-пункт размере и растягивала
    документ на лишние страницы), но точно применяет явные `width`/`height` в миллиметрах."""
    with PILImage.open(io.BytesIO(png_bytes)) as image:
        width_px, height_px = image.size
    mm_per_px = 25.4 / _CHART_PNG_DPI
    width_mm = width_px * mm_per_px
    height_mm = height_px * mm_per_px
    scale = min(_PDF_MAX_CHART_HEIGHT_MM / height_mm, _PDF_MAX_CHART_WIDTH_MM / width_mm, 1.0)
    return round(width_mm * scale, 1), round(height_mm * scale, 1)


def render_pdf(dataset: ReportDataset, *, template_code: str | None = None) -> bytes:
    """`template_code` — опционален (B1-8): без него (прямой вызов, как в большинстве тестов
    этого модуля) PDF строится без графика, как раньше. С видом отчёта, у которого
    `ReportSpec.chart` задан, картинка строится тем же вызовом, под тем же `_RENDER_LOCK`
    (`reporting.service._render_serialized`), что и остальной рендер — отдельного похода в
    воркер нет."""
    chart_width_mm: float | None = None
    chart_height_mm: float | None = None
    chart_png_base64: str | None = None
    chart_kind = _chart_kind_for(template_code)
    if chart_kind is not None:
        try:
            png_bytes: bytes | None = charts.render_png(dataset, chart_kind=chart_kind)
        except charts.RenderNotSupportedError:
            png_bytes = None
        if png_bytes is not None:
            chart_width_mm, chart_height_mm = _chart_image_size_mm(png_bytes)
            chart_png_base64 = base64.b64encode(png_bytes).decode("ascii")

    context = {
        "title": dataset.title,
        "note": dataset.note,
        "columns": dataset.columns,
        "rows": dataset.rows,
        "generated_at": dataset.generated_at.strftime("%d.%m.%Y %H:%M UTC"),
        "chart_png_base64": chart_png_base64,
        "chart_width_mm": chart_width_mm,
        "chart_height_mm": chart_height_mm,
    }
    return _html_to_pdf(render_template_html(_TABLE_TEMPLATE, context))


#: rtk_requiriments.md §6 п.4: «возможность формирования результирующего json-файла».
#: Версия конверта — `reporting.result_schema.REPORT_RESULT_SCHEMA_VERSION` (B9-9), не
#: самостоятельный литерал: пересматривается при несовместимых изменениях структуры файла
#: (правило версий — докстринг `result_schema.py`), меняется в одном месте.


def _json_cell(value: Any, tz: dt.tzinfo) -> Any:
    """Значение ячейки для json-файла (B1-5): без формул/усечения строк (`/data` и файл отдают
    данные «как есть», в отличие от xlsx/xls, где `sanitize_formula` защищает от CSV-инъекции —
    тот риск специфичен открытию в Excel, не парсингу JSON). `Decimal` — числом, aware-`datetime`
    переводится в пояс пользователя и остаётся со смещением (`2026-01-05T10:30:00+03:00`), дата
    без времени — `YYYY-MM-DD`."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dt.datetime):
        local = value.astimezone(tz) if value.tzinfo is not None else value
        return local.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, float):
        return value
    return str(value)


def render_json(
    dataset: ReportDataset,
    *,
    template_code: str,
    job_id: uuid.UUID | None,
    params: dict[str, Any] | None,
    tz_name: str | None,
) -> bytes:
    """Файл для `POST /api/reports` с `format=json` (B1-5). Тот же `ReportDataset`, что рендерится
    в xlsx/pdf/png, версионированным конвертом: `columns` всегда идут вместе со `rows` (B1-2:
    `dataset.keys`/`dataset.kinds`, пусто у видов без каталога колонок — тогда `key` у каждой
    колонки `None`), а не массивами по позиции без заголовков, — так перестановка колонок не
    ломает интегратора, который уже читает файл по ключам. Кодировка UTF-8 без BOM (`ensure_
    ascii=False`), `allow_nan=False` — `float('nan')`/`inf` в данных поднимет понятную `ValueError`
    при сериализации, а не отдаст невалидный JSON наружу."""
    # B1-6: `resolve_tz` — общая с xls/xlsx функция (`rendering_xls`), один и тот же откат на
    # `DEFAULT_TIMEZONE_NAME` для неизвестного/отсутствующего имени во всех трёх форматах.
    tz = resolve_tz(tz_name)

    keys = dataset.keys if dataset.keys is not None else [None] * len(dataset.columns)
    kinds = dataset.kinds if dataset.kinds is not None else ["text"] * len(dataset.columns)
    columns = [
        ReportResultColumn(key=key, title=title, kind=kind)
        for key, title, kind in zip(keys, dataset.columns, kinds, strict=True)
    ]
    # Служебные ключи (`_seed` и т.п., B1-2) не предназначены для интегратора.
    visible_params = {k: v for k, v in (params or {}).items() if not str(k).startswith("_")}

    # B9-9: конверт собирается через `ReportResultV1` (`reporting.result_schema`), не голым
    # словарём — форма файла проверяется тем же кодом, что проверяет и документирует её
    # (`tools/export_report_schema.py`/`docs/schemas/rtk-crm-report-1.schema.json`), а не только
    # текстом в этом докстринге.
    result = ReportResultV1(
        schema=REPORT_RESULT_SCHEMA_VERSION,
        report=ReportResultReport(
            code=template_code,
            title=dataset.title,
            job_id=str(job_id) if job_id is not None else None,
            generated_at=dataset.generated_at.astimezone(tz).isoformat(),
            timezone=tz_name or DEFAULT_TIMEZONE_NAME,
        ),
        params=visible_params,
        columns=columns,
        rows=[[_json_cell(value, tz) for value in row] for row in dataset.rows],
        totals=ReportResultTotals(
            row_count=len(dataset.rows),
            total_rows=(
                dataset.total_rows if dataset.total_rows is not None else len(dataset.rows)
            ),
            truncated=dataset.truncated,
        ),
        note=dataset.note,
    )
    # `model_dump()` без `mode="json"` — специально: тот режим молча превращает `float('nan')` в
    # `None` (проверено отдельно), а обычный режим сохраняет исходный `float('nan')`/`inf`, на
    # котором `json.dumps(..., allow_nan=False)` ниже всё ещё поднимает `ValueError`, как до
    # перевода конверта на Pydantic-модель.
    payload = result.model_dump(by_alias=True)
    return json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")


def render_png(dataset: ReportDataset, *, template_code: str) -> bytes:
    """Построение фигуры (`reporting.charts.CHART_BUILDERS`) и кодирование в PNG — вынесено в
    `reporting.charts` (B1-8), этот уровень только резолвит `template_code → ReportSpec.chart`
    и переводит рассинхрон с `CHART_KINDS` в `RenderNotSupportedError` именно этого модуля
    (историческое поведение/тип исключения для вызывающего кода не меняются)."""
    if template_code not in CHART_KINDS:
        raise RenderNotSupportedError(f"Для вида отчёта {template_code!r} график не определён")
    chart_kind = _chart_kind_for(template_code)
    assert chart_kind is not None  # noqa: S101 — инвариант: CHART_KINDS ⟺ spec.chart задан
    return charts.render_png(dataset, chart_kind=chart_kind)


def render_report(
    dataset: ReportDataset,
    *,
    format: str,
    template_code: str,
    job_id: uuid.UUID | None = None,
    params: dict[str, Any] | None = None,
    tz_name: str | None = None,
) -> bytes:
    """`job_id`/`params` — только для `format=json` (B1-5): конверт файла несёт, с какими
    параметрами и для какого задания он построен. `tz_name` (B1-6) нужен и xlsx/xls — перевод
    `datetime` в пояс пользователя при рендере ячейки, не только конверту json. Все аргументы
    приходят через один и тот же вызов из `reporting.service._render_serialized`, чтобы не
    заводить отдельную функцию рендера на каждый формат."""
    if format == ReportFormat.XLSX.value:
        if template_code == LMS_USERS_UPLOAD:
            return render_lms_users_xlsx(dataset)
        return render_xlsx(dataset, tz_name=tz_name, template_code=template_code)
    if format == ReportFormat.XLS.value:
        return render_xls(dataset, tz_name=tz_name)
    if format == ReportFormat.PDF.value:
        return render_pdf(dataset, template_code=template_code)
    if format == ReportFormat.PNG.value:
        return render_png(dataset, template_code=template_code)
    if format == ReportFormat.JSON.value:
        return render_json(
            dataset, template_code=template_code, job_id=job_id, params=params, tz_name=tz_name
        )
    raise RenderNotSupportedError(f"Неизвестный формат отчёта {format!r}")
