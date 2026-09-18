"""Рендеринг отчётов: xlsx / pdf / png (new_spec §4.13, §2.3, §6).

* **xlsx** — `openpyxl` в `write_only`-режиме (раздел 4.13: «стриминг,
  константная память») — тот же приём, что `imports.service` уже применяет к
  сгенерированным отчётам об ошибках импорта.
* **pdf** — раздел 2.3 называет WeasyPrint, но `signing.rendering` (спринт 6)
  уже осознанно заменил его на `xhtml2pdf` (WeasyPrint тянет системные
  Pango/Cairo/GDK-Pixbuf, что противоречит принципу `Dockerfile`) и вложил
  шрифт DejaVu Sans для кириллицы. Здесь этот же пайплайн переиспользуется
  напрямую (`render_template_html`/`html_to_pdf`) — один Jinja2-шаблон
  таблицы на все виды отчёта: колонки/строки в отчёте динамические, а не
  поля с фиксированными именами, поэтому шаблон параметризован структурой
  `ReportDataset`, а не конкретным видом отчёта.
* **png** — раздел 2.3/4.13: «серверный matplotlib (не скриншот фронта — на
  сервере нет браузера)», backend `Agg` — без дисплея. matplotlib поставляет
  свой DejaVu Sans (та же гарнитура, что уже вложена для PDF) и уже
  поддерживает кириллицу без дополнительных шрифтов. Графики есть только для
  видов, где они осмысленны (воронка, динамика по месяцам) — остальные виды
  отчёта отдают только табличные форматы (`report_templates.output_formats`
  ограничивает выбор на уровне API, раздел 4.13 не требует графика для
  каждого отчёта).
"""

from __future__ import annotations

import io
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402 — backend должен быть выбран до импорта pyplot
from openpyxl import Workbook

from app.modules.imports.parsing import sanitize_formula
from app.modules.reporting.builders import ReportDataset
from app.modules.reporting.models import ReportFormat
from app.modules.signing.rendering import html_to_pdf, render_template_html

CONTENT_TYPES: dict[str, str] = {
    ReportFormat.XLSX.value: (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ),
    ReportFormat.PDF.value: "application/pdf",
    ReportFormat.PNG.value: "image/png",
}

#: Виды отчёта, для которых определён график. Ключ — `report_templates.code`.
CHART_KINDS = frozenset({"deal_funnel", "monthly_dynamics"})


class RenderNotSupportedError(Exception):
    """PNG запрошен для вида отчёта без определённого графика.

    Не пользовательская ошибка в обычном смысле: `reporting.service`
    проверяет `format in template.output_formats` до вызова рендеринга,
    поэтому попадание сюда означает рассинхрон между посевом шаблонов и
    `CHART_KINDS`, а не некорректный ввод пользователя.
    """


_TABLE_TEMPLATE = """
<h2>{{ title }}</h2>
{% if note %}<p><em>{{ note }}</em></p>{% endif %}
<table style="width:100%; border-collapse:collapse;">
  <thead>
    <tr>
      {% for col in columns %}
      <th style="border:1px solid #999; padding:4px 8px; background:#eef1fa; text-align:left;">
        {{ col }}
      </th>
      {% endfor %}
    </tr>
  </thead>
  <tbody>
    {% for row in rows %}
    <tr>
      {% for cell in row %}
      <td style="border:1px solid #ccc; padding:4px 8px;">{{ "" if cell is none else cell }}</td>
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
    решает это для сгенерированных xlsx импорта — тот же риск, тот же приём."""
    if value is None:
        return ""
    if isinstance(value, str):
        return sanitize_formula(value)
    if isinstance(value, int | float | bool):
        return value
    return str(value)


def render_xlsx(dataset: ReportDataset) -> bytes:
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet(title=(dataset.title[:31] or "Отчёт"))
    sheet.append(dataset.columns)
    for row in dataset.rows:
        sheet.append([_xlsx_cell(value) for value in row])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def render_pdf(dataset: ReportDataset) -> bytes:
    context = {
        "title": dataset.title,
        "note": dataset.note,
        "columns": dataset.columns,
        "rows": dataset.rows,
        "generated_at": dataset.generated_at.strftime("%d.%m.%Y %H:%M UTC"),
    }
    return html_to_pdf(render_template_html(_TABLE_TEMPLATE, context))


def _fig_to_png(fig: Any) -> bytes:
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", dpi=110, bbox_inches="tight")
    plt.close(fig)
    return buffer.getvalue()


def _render_funnel_chart(dataset: ReportDataset) -> bytes:
    labels = [str(row[0]) for row in dataset.rows]
    currently_in = [row[1] or 0 for row in dataset.rows]
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.bar(labels, currently_in, color="#4C6EF5")
    ax.set_title(dataset.title)
    ax.set_ylabel("Сделок сейчас в статусе")
    ax.tick_params(axis="x", rotation=25)
    fig.tight_layout()
    return _fig_to_png(fig)


def _render_monthly_chart(dataset: ReportDataset) -> bytes:
    months = [str(row[0]) for row in dataset.rows]
    created = [row[1] or 0 for row in dataset.rows]
    won = [row[2] or 0 for row in dataset.rows]
    lost = [row[3] or 0 for row in dataset.rows]
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.plot(months, created, marker="o", label="Создано")
    ax.plot(months, won, marker="o", label="Выиграно")
    ax.plot(months, lost, marker="o", label="Проиграно")
    ax.set_title(dataset.title)
    ax.legend()
    ax.tick_params(axis="x", rotation=40)
    fig.tight_layout()
    return _fig_to_png(fig)


def render_png(dataset: ReportDataset, *, template_code: str) -> bytes:
    if template_code == "deal_funnel":
        return _render_funnel_chart(dataset)
    if template_code == "monthly_dynamics":
        return _render_monthly_chart(dataset)
    raise RenderNotSupportedError(f"Для вида отчёта {template_code!r} график не определён")


def render_report(dataset: ReportDataset, *, format: str, template_code: str) -> bytes:
    if format == ReportFormat.XLSX.value:
        return render_xlsx(dataset)
    if format == ReportFormat.PDF.value:
        return render_pdf(dataset)
    if format == ReportFormat.PNG.value:
        return render_png(dataset, template_code=template_code)
    raise RenderNotSupportedError(f"Неизвестный формат отчёта {format!r}")
