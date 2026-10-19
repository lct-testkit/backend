"""Диаграммы отчётов (B1-8): PNG как отдельный формат вывода и то же изображение, встроенное в
PDF (первая страница, под шапкой) и в xlsx (второй лист «График»). Вынесено из `rendering.py`
(было: `_render_funnel_chart`/`_render_monthly_chart` прямо там) в отдельный модуль, как и
предполагает задача — рендер таблиц (openpyxl/xhtml2pdf) не должен тянуть matplotlib, если вид
отчёта без графика.

Каждый вид графика — чистая функция `build_<вид>_figure(dataset) -> Figure`, тестируется без
записи в файл. `matplotlib.figure.Figure` строится напрямую (`Figure(...)`), без `pyplot`:
`pyplot` держит глобальный реестр фигур (`matplotlib.use("Agg")` всё равно нужен один раз на
процесс — он уже выставлен в `rendering.py` до первого импорта `matplotlib.pyplot` где-либо в
приложении, а `Figure`/`FigureCanvasAgg` его не требуют, но и не конфликтуют с ним).

Какой вид графика строить для вида отчёта — `reporting.spec.ReportSpec.chart` (`"funnel"`,
`"monthly"`, `"demand"`, `"progress"`, `"license_status"`), не сам код шаблона: `CHART_BUILDERS`
ниже — единственное место, которое их связывает.

Правила достоверности (методика `dataviz`, раздел B1-8 задачи) — общие для всех графиков этого
модуля:
  * ось значений начинается с нуля (``ax.set_xlim(0, ...)``/``set_ylim(0, ...)``);
  * круговых/«пончиковых» диаграмм и 3D нет — только бары/линии;
  * у каждого столбца/конца линии есть подпись значения (``ax.text``/``ax.annotate``);
  * пустой набор данных — сообщение на холсте, а не пустые оси (``_no_data_figure``);
  * внизу — подпись «Сформировано ДД.ММ.ГГГГ ЧЧ:МM» (``_footer``), момент берётся из
    ``dataset.generated_at`` (момент рендера, не момент запроса).
"""

from __future__ import annotations

import datetime as dt
import io
from collections.abc import Callable
from typing import Any

import matplotlib

# B1-8: явный безголовый backend, как и раньше в `rendering.py` — на сервере нет дисплея/Tkinter.
# `Figure`/`FigureCanvasAgg` ниже не нуждаются в нём для своего собственного рендера (объектный
# режим сам выбирает Agg-рендерер при `savefig(..., format="png")`), но `matplotlib.use(...)`
# должен быть вызван до ЛЮБОГО импорта `pyplot` где-либо в процессе — выставляем здесь на
# случай, если какой-то будущий код модуля всё же импортирует `pyplot`.
matplotlib.use("Agg")
from matplotlib.figure import Figure  # noqa: E402 — backend должен быть выбран до этого импорта
from PIL import Image  # noqa: E402

from app.modules.reporting.builders import ReportDataset

__all__ = [
    "CHART_BUILDERS",
    "RenderNotSupportedError",
    "build_demand_figure",
    "build_funnel_figure",
    "build_license_status_figure",
    "build_monthly_figure",
    "build_progress_figure",
    "figure_to_png",
    "progress_buckets",
    "render_png",
]


class RenderNotSupportedError(Exception):
    """PNG запрошен для вида графика, которого нет в `CHART_BUILDERS` — рассинхрон между
    `ReportSpec.chart` и этим модулем, а не ошибка пользовательского ввода (её отсекает
    `output_formats` шаблона до вызова рендера)."""


#: Палитра и marks-спеки — методика визуализации (skill `dataviz`,
#: `references/palette.md`/`marks-and-anatomy.md`), перенесены из `rendering.py` буквально:
#: тон/контраст уже провалидированы её собственным скриптом (`validate_palette.js`).
_SURFACE = "#fcfcfb"
_INK_PRIMARY = "#0b0b0b"
_INK_SECONDARY = "#52514e"
_INK_MUTED = "#898781"
_GRIDLINE = "#e1e0d9"
_BASELINE = "#c3c2b7"
#: lost/parked (воронка) — вне цепочки конверсии, серый тон вместо категориального/ordinal —
#: отличие несёт в первую очередь подпись («без конверсии»), не только цвет (B1-8, п.8: «до
#: проверки палитры скриптом различие несут подписи данных, а не только цвет»).
_MUTED_GRAY = "#aba89d"

#: Ordinal-рамп (позиция в последовательности — воронка/рейтинг — буквальный пример ordinal-job
#: в color-formula.md): один тон, монотонная светлота.
_ORDINAL_BLUE_STEPS = [
    "#86b6ef",
    "#6da7ec",
    "#5598e7",
    "#3987e5",
    "#2a78d6",
    "#256abf",
    "#1c5cab",
    "#184f95",
    "#104281",
    "#0d366b",
]

#: Категориальные слоты 1/2/3 в фиксированном порядке (palette.md) — никогда не переставляются.
_CATEGORICAL = {"blue": "#2a78d6", "orange": "#eb6834", "aqua": "#1baf7a"}


def _ordinal_ramp(n: int) -> list[str]:
    """N цветов, равномерно растянутых по `_ORDINAL_BLUE_STEPS`."""
    if n <= 0:
        return []
    if n == 1:
        return [_ORDINAL_BLUE_STEPS[len(_ORDINAL_BLUE_STEPS) // 2]]
    last = len(_ORDINAL_BLUE_STEPS) - 1
    return [_ORDINAL_BLUE_STEPS[round(i * last / (n - 1))] for i in range(n)]


def _format_value(value: float) -> str:
    """Табличные числа с разрядным пробелом (marks-and-anatomy.md: «round to clean numbers...
    thousands-comma'd» — пробел, не запятая: та же русская типографская норма, что раздел 2
    «денежные суммы» уже применяет к суммам)."""
    return f"{value:,.0f}".replace(",", " ")


def _cell(row: list[Any], index: int, default: Any = None) -> Any:
    """Безопасный доступ к ячейке строки `dataset.rows` по позиции — часть графиков читает
    колонки, которых нет в упрощённых тестовых строках (например, у воронки «Дошли до шага»
    — только у настоящего `build_deal_funnel`, не у минимального фикстура в тесте PNG)."""
    return row[index] if index < len(row) and row[index] is not None else default


def _style_axes(ax: Any) -> None:
    """Общий «тихий» chrome (marks-and-anatomy.md): убираем рамку графика, оставляем только
    нижнюю ось базовой линией, подписи — приглушённым тоном."""
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(_BASELINE)
    ax.tick_params(colors=_INK_MUTED, labelsize=9)


def _footer(fig: Figure, dataset: ReportDataset | None) -> None:
    """«Внизу подпись "Сформировано ДД.ММ.ГГГГ ЧЧ:ММ"» (B1-8, честность графиков) — момент
    рендера (`dataset.generated_at`), не момент открытия файла читателем."""
    generated_at = dataset.generated_at if dataset is not None else dt.datetime.now(dt.UTC)
    fig.text(
        0.01,
        0.01,
        f"Сформировано {generated_at.strftime('%d.%m.%Y %H:%M')}",
        fontsize=7,
        color=_INK_MUTED,
    )


def _no_data_figure(title: str, *, figsize: tuple[float, float] = (8.5, 3.2)) -> Figure:
    """«Нет данных — сообщение на холсте, а не пустые оси» (B1-8, п.2)."""
    fig = Figure(figsize=figsize)
    ax = fig.add_subplot(111)
    ax.set_title(title, color=_INK_PRIMARY, fontsize=13, loc="left", pad=12)
    ax.text(
        0.5,
        0.5,
        "Нет данных за выбранный период",
        ha="center",
        va="center",
        fontsize=11,
        color=_INK_MUTED,
        transform=ax.transAxes,
    )
    ax.set_xticks([])
    ax.set_yticks([])
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)
    _footer(fig, None)
    return fig


def figure_to_png(fig: Figure, *, compact: bool = False) -> bytes:
    """`Figure.savefig` работает без глобального состояния `pyplot` — фигура не регистрируется
    ни в каком реестре и не требует `plt.close()` (в отличие от `pyplot.subplots()`, который
    был здесь раньше, до выноса в этот модуль).

    `compact=True` — 256-цветная палитра вместо RGBA: у плоских диаграмм (заливки + сглаженный
    текст) файл втрое меньше при неотличимой картинке. Нужно там, где PNG потом едет по сети
    (лист «График» xlsx кладётся в SeaweedFS: на стенде реплика через WireGuard и время `PUT`
    растёт с размером тела почти линейно, секунды на сотнях килобайт)."""
    buffer = io.BytesIO()
    fig.patch.set_facecolor(_SURFACE)
    fig.savefig(buffer, format="png", dpi=130, bbox_inches="tight", facecolor=_SURFACE)
    if not compact:
        return buffer.getvalue()
    buffer.seek(0)
    with Image.open(buffer) as image:
        palette = image.convert("RGB").quantize(
            colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE
        )
    packed = io.BytesIO()
    palette.save(packed, format="PNG", optimize=True)
    return packed.getvalue()


# =============================================================================
# deal_funnel
# =============================================================================


def build_funnel_figure(dataset: ReportDataset) -> Figure:
    """Воронка (B1-8, п.3): горизонтальные бары с наложением — длина бара «Всего прошло»
    (светлый тон), поверх узкий тёмный бар «Сейчас в статусе» (подмножество, не отдельная
    величина). Статусы lost/parked (у них `build_deal_funnel` не определяет «Дошли до шага» —
    позиция 5, `reached_out`, всегда `None`) отделены линией, серые, без подписи конверсии.

    Позиции строки — контракт `build_deal_funnel` (`builders.py`): 0 статус, 1 «Сейчас в
    статусе», 2 «Всего прошло», 3 «Конверсия шага, %», 5 «Дошли до шага» (`None` у lost/parked).
    Старые/упрощённые тестовые строки короче — `_cell` отдаёт разумные запасные значения, а не
    падает `IndexError`."""
    rows = dataset.rows
    if not rows:
        return _no_data_figure(dataset.title)

    labels = [str(_cell(row, 0, "")) for row in rows]
    currently_in = [float(_cell(row, 1, 0) or 0) for row in rows]
    # Нет отдельной колонки «Всего прошло» (старые тестовые строки) — бар показывает одну
    # величину, как до этой задачи: `passed` совпадает с `currently_in`.
    passed = [float(_cell(row, 2, cur) or cur) for row, cur in zip(rows, currently_in, strict=True)]
    conversion = [_cell(row, 3) for row in rows]
    # `reached_out` (позиция 5) — `None` у lost/parked по построению `build_deal_funnel`;
    # отсутствующая позиция (старые тестовые строки) трактуется как «основной шаг» (нет данных,
    # чтобы отделить группу, группировка просто не рисуется).
    is_main = [len(row) <= 5 or row[5] is not None for row in rows]

    n = len(labels)
    main_colors = iter(_ordinal_ramp(sum(is_main)))
    colors = [next(main_colors) if m else _MUTED_GRAY for m in is_main]

    # barh рисует снизу вверх — разворачиваем все параллельные списки один раз здесь, чтобы
    # первый шаг воронки оказался вверху графика, как читатель ожидает.
    labels = labels[::-1]
    passed = passed[::-1]
    currently_in = currently_in[::-1]
    conversion = conversion[::-1]
    is_main = is_main[::-1]
    colors = colors[::-1]

    height = max(3.2, 0.5 * n + 1.3)
    fig = Figure(figsize=(8.5, height))
    ax = fig.add_subplot(111)
    y_pos = list(range(n))

    ax.barh(
        y_pos,
        passed,
        height=0.62,
        color=colors,
        alpha=0.45,
        edgecolor=_SURFACE,
        linewidth=2,
        zorder=2,
    )
    ax.barh(
        y_pos, currently_in, height=0.30, color=colors, edgecolor=_SURFACE, linewidth=1, zorder=3
    )

    max_value = max(passed, default=0)
    ax.set_xlim(0, max_value * 1.22 if max_value else 1)
    for y, value, conv, main in zip(y_pos, passed, conversion, is_main, strict=True):
        if main and conv is not None:
            text = f"{_format_value(value)} · {float(conv):.0f}% от предыдущего"
        else:
            text = _format_value(value)
        ax.text(
            value + max_value * 0.02 if max_value else 0.02,
            y,
            text,
            va="center",
            ha="left",
            fontsize=9,
            color=_INK_PRIMARY,
            zorder=4,
        )

    # Граница между группой основных статусов и lost/parked (B1-8, п.3: «отделены чертой»).
    for index in range(1, n):
        if is_main[index] != is_main[index - 1]:
            ax.axhline(index - 0.5, color=_GRIDLINE, linewidth=1, linestyle="--", zorder=1)

    ax.set_yticks(y_pos)
    ax.set_yticklabels(labels, color=_INK_SECONDARY)
    ax.set_title(dataset.title, color=_INK_PRIMARY, fontsize=13, loc="left", pad=12)
    ax.set_xlabel(
        "Всего прошло (светлый тон) и сейчас в статусе (тёмный)", color=_INK_SECONDARY, fontsize=9
    )
    ax.grid(axis="x", color=_GRIDLINE, linewidth=1, linestyle="-", zorder=0)
    ax.set_axisbelow(True)
    _style_axes(ax)
    ax.spines["bottom"].set_visible(False)
    _footer(fig, dataset)
    fig.tight_layout()
    return fig


# =============================================================================
# monthly_dynamics
# =============================================================================


def build_monthly_figure(dataset: ReportDataset) -> Figure:
    """Динамика по месяцам (B1-8): линии «Создано/Выиграно/Проиграно», каждый месяц окна есть
    на оси (данные B1-7 уже не пропускают пустой месяц — пропуск виден как ноль, не как дырка в
    оси)."""
    rows = dataset.rows
    if not rows:
        return _no_data_figure(dataset.title)

    months = [str(_cell(row, 0, "")) for row in rows]
    series = [
        ("Создано", [float(_cell(row, 1, 0) or 0) for row in rows], _CATEGORICAL["blue"]),
        ("Выиграно", [float(_cell(row, 2, 0) or 0) for row in rows], _CATEGORICAL["orange"]),
        ("Проиграно", [float(_cell(row, 3, 0) or 0) for row in rows], _CATEGORICAL["aqua"]),
    ]
    fig = Figure(figsize=(8.5, 4.6))
    ax = fig.add_subplot(111)
    x = range(len(months))

    max_value = max((v for _n, values, _c in series for v in values), default=0)
    for name, values, color in series:
        ax.plot(
            x,
            values,
            color=color,
            linewidth=2,
            marker="o",
            markersize=8,
            markerfacecolor=color,
            markeredgecolor=_SURFACE,
            markeredgewidth=2,
            label=name,
            zorder=3,
        )
        if values:
            ax.annotate(
                f"{name}: {_format_value(values[-1])}",
                (x[-1], values[-1]),
                xytext=(8, 0),
                textcoords="offset points",
                va="center",
                fontsize=9,
                color=_INK_SECONDARY,
            )

    ax.set_ylim(0, max_value * 1.15 if max_value else 1)
    ax.set_title(dataset.title, color=_INK_PRIMARY, fontsize=13, loc="left", pad=12)
    ax.set_xticks(list(x))
    rotation = 30 if len(months) > 8 else 0
    ax.set_xticklabels(months, rotation=rotation, ha="right" if rotation else "center")
    ax.grid(axis="y", color=_GRIDLINE, linewidth=1, linestyle="-", zorder=0)
    ax.set_axisbelow(True)
    _style_axes(ax)
    legend = ax.legend(loc="upper left", frameon=False, fontsize=9, labelcolor=_INK_SECONDARY)
    for handle in legend.legend_handles:
        if handle is not None and hasattr(handle, "set_markeredgecolor"):
            handle.set_markeredgecolor(_SURFACE)
    _footer(fig, dataset)
    fig.tight_layout()
    return fig


# =============================================================================
# product_demand
# =============================================================================

#: «Показаны первые 15» (B1-8, таблица «Диаграммы по отчётам») — независимо от `top` отчёта
#: (параметр builder'а, по умолчанию 20): график специально уже, чем таблица, три панели на 20+
#: позициях перестают читаться.
_DEMAND_CHART_LIMIT = 15


def build_demand_figure(dataset: ReportDataset) -> Figure:
    """Рейтинг востребованности (B1-8/B1-9): три панели с общей осью названий — «Заявки»,
    «Обучающиеся», «Потоков», каждая со своей шкалой от нуля (шкалы не сопоставимы между собой
    напрямую — об этом подпись на графике).

    Позиции строки — контракт `build_product_demand`: 1 название (продукт либо направление),
    3 заявки, 5 обучающиеся, 6 потоки — одни и те же позиции на уровне `product` и `direction`
    (различается только позиция 2: направление или число продуктов)."""
    rows = dataset.rows
    if not rows:
        return _no_data_figure(dataset.title, figsize=(11, 3.2))

    shown = rows[:_DEMAND_CHART_LIMIT]
    labels = [str(_cell(row, 1, "")) for row in shown][::-1]
    applications = [float(_cell(row, 3, 0) or 0) for row in shown][::-1]
    learners = [float(_cell(row, 5, 0) or 0) for row in shown][::-1]
    streams = [float(_cell(row, 6, 0) or 0) for row in shown][::-1]

    height = max(3.6, 0.42 * len(labels) + 1.7)
    fig = Figure(figsize=(11, height))
    axes = fig.subplots(1, 3, sharey=True)
    panels = (
        ("Заявки", applications, _CATEGORICAL["blue"]),
        ("Обучающиеся", learners, _CATEGORICAL["orange"]),
        ("Потоков", streams, _CATEGORICAL["aqua"]),
    )
    y_pos = list(range(len(labels)))
    for ax, (name, values, color) in zip(axes, panels, strict=True):
        ax.barh(y_pos, values, height=0.6, color=color, edgecolor=_SURFACE, linewidth=1.5, zorder=2)
        max_value = max(values, default=0)
        ax.set_xlim(0, max_value * 1.22 if max_value else 1)
        for y, value in zip(y_pos, values, strict=True):
            ax.text(
                value + max_value * 0.02 if max_value else 0.02,
                y,
                _format_value(value),
                va="center",
                ha="left",
                fontsize=8,
                color=_INK_PRIMARY,
            )
        ax.set_title(name, fontsize=10, color=_INK_SECONDARY, loc="left")
        ax.grid(axis="x", color=_GRIDLINE, linewidth=1, zorder=0)
        ax.set_axisbelow(True)
        _style_axes(ax)
        ax.spines["bottom"].set_visible(False)

    axes[0].set_yticks(y_pos)
    axes[0].set_yticklabels(labels, color=_INK_SECONDARY, fontsize=8)

    fig.suptitle(dataset.title, x=0.01, ha="left", fontsize=13, color=_INK_PRIMARY)
    caption = "Шкалы панелей независимы"
    if len(rows) > _DEMAND_CHART_LIMIT:
        caption += f"; показаны первые {_DEMAND_CHART_LIMIT} из {len(rows)}"
    fig.text(0.01, 0.955, caption, fontsize=8, color=_INK_MUTED)
    _footer(fig, dataset)
    fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.93))
    return fig


# =============================================================================
# learning_progress
# =============================================================================

#: Корзины прогресса (B1-8/B1-10) — границы включительные по верхней стороне, кроме «100%»
#: отдельно (99% и 100% не должны смешиваться — «завершил» значимо отличается от «почти»).
_PROGRESS_BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("0%", 0, 0),
    ("1–25%", 1, 25),
    ("26–50%", 26, 50),
    ("51–75%", 51, 75),
    ("76–99%", 76, 99),
    ("100%", 100, 100),
)


def progress_buckets(values: list[int]) -> list[tuple[str, int]]:
    """Чистая функция (B1-10, п.5): раскладывает значения `progress_pct` по шести корзинам.
    Не трогает БД/сессию — тестируется отдельно от SQL, как `compute_demand_index`."""
    counts = {label: 0 for label, _lo, _hi in _PROGRESS_BUCKETS}
    for value in values:
        for label, low, high in _PROGRESS_BUCKETS:
            if low <= value <= high:
                counts[label] += 1
                break
    return [(label, counts[label]) for label, _lo, _hi in _PROGRESS_BUCKETS]


def build_progress_figure(dataset: ReportDataset) -> Figure:
    """Прогресс обучения (B1-8/B1-10): столбцы по корзинам прогресса, подпись «n = N записей,
    средний прогресс X%». Данные — `dataset.extra["progress_values"]` (B1-8: не зависят от
    выбранных `columns` — `progress_pct` может быть не выбран пользователем для таблицы, график
    всё равно должен его видеть)."""
    values: list[int] = list((dataset.extra or {}).get("progress_values") or [])
    if not values:
        return _no_data_figure(dataset.title)

    buckets = progress_buckets(values)
    labels = [label for label, _count in buckets]
    counts = [float(count) for _label, count in buckets]
    colors = _ordinal_ramp(len(labels))

    fig = Figure(figsize=(8.5, 4.4))
    ax = fig.add_subplot(111)
    x = list(range(len(labels)))
    ax.bar(x, counts, color=colors, edgecolor=_SURFACE, linewidth=1.5, zorder=2)
    max_value = max(counts, default=0)
    ax.set_ylim(0, max_value * 1.2 if max_value else 1)
    for xi, value in zip(x, counts, strict=True):
        ax.text(
            xi,
            value + max_value * 0.02 if max_value else 0.02,
            _format_value(value),
            ha="center",
            va="bottom",
            fontsize=9,
            color=_INK_PRIMARY,
        )

    avg_progress = round(sum(values) / len(values))
    ax.set_title(dataset.title, color=_INK_PRIMARY, fontsize=13, loc="left", pad=12)
    fig.text(
        0.01,
        0.955,
        f"n = {len(values)} записей, средний прогресс {avg_progress}%",
        fontsize=9,
        color=_INK_SECONDARY,
    )
    ax.set_xticks(x)
    ax.set_xticklabels(labels, color=_INK_SECONDARY)
    ax.grid(axis="y", color=_GRIDLINE, linewidth=1, zorder=0)
    ax.set_axisbelow(True)
    _style_axes(ax)
    _footer(fig, dataset)
    fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.93))
    return fig


# =============================================================================
# license_register
# =============================================================================


def _bar_panel(ax: Any, title: str, counts: dict[str, int]) -> None:
    labels = list(counts.keys())
    values = [float(v) for v in counts.values()]
    colors = _ordinal_ramp(len(labels))
    x = list(range(len(labels)))
    ax.bar(x, values, color=colors, edgecolor=_SURFACE, linewidth=1.5, zorder=2)
    max_value = max(values, default=0)
    ax.set_ylim(0, max_value * 1.22 if max_value else 1)
    for xi, value in zip(x, values, strict=True):
        ax.text(
            xi,
            value + max_value * 0.02 if max_value else 0.02,
            _format_value(value),
            ha="center",
            va="bottom",
            fontsize=8,
            color=_INK_PRIMARY,
        )
    rotate = len(labels) > 4
    ax.set_xticks(x)
    ax.set_xticklabels(
        labels,
        color=_INK_SECONDARY,
        rotation=20 if rotate else 0,
        ha="right" if rotate else "center",
    )
    ax.set_title(title, fontsize=10, color=_INK_SECONDARY, loc="left")
    ax.grid(axis="y", color=_GRIDLINE, linewidth=1, zorder=0)
    ax.set_axisbelow(True)
    _style_axes(ax)


def build_license_status_figure(dataset: ReportDataset) -> Figure:
    """Реестр лицензий (B1-8/B1-11): две панели — число договоров по статусу передачи и по году
    окончания. Данные — `dataset.extra["by_status"]`/`["by_year"]` (B1-8: builder кладёт их до
    выбора колонок, график не зависит от `columns`)."""
    extra = dataset.extra or {}
    by_status: dict[str, int] = dict(extra.get("by_status") or {})
    by_year: dict[str, int] = dict(extra.get("by_year") or {})
    if not by_status and not by_year:
        return _no_data_figure(dataset.title, figsize=(11, 3.2))

    fig = Figure(figsize=(11, 4.6))
    ax1, ax2 = fig.subplots(1, 2)
    _bar_panel(ax1, "По статусу передачи", by_status)
    # Год — лексикографическая сортировка совпадает с числовой (все года — строки одной длины,
    # «Без срока» по алфавиту кириллицы идёт позже цифр).
    _bar_panel(ax2, "По году окончания", dict(sorted(by_year.items())))

    fig.suptitle(dataset.title, x=0.01, ha="left", fontsize=13, color=_INK_PRIMARY)
    total = sum(by_status.values()) or sum(by_year.values())
    fig.text(0.01, 0.95, f"n = {total} договоров", fontsize=9, color=_INK_SECONDARY)
    _footer(fig, dataset)
    fig.tight_layout(rect=(0.0, 0.0, 1.0, 0.88))
    return fig


#: Единственное место, которое связывает `ReportSpec.chart` с функцией построения (B1-8, п.1):
#: `CHART_KINDS` (`reporting.rendering`, производный от `ReportSpec.chart`, B1-2) просто проверяет
#: наличие ключа здесь по коду шаблона через `reporting.spec.get_spec(...).chart`.
CHART_BUILDERS: dict[str, Callable[[ReportDataset], Figure]] = {
    "funnel": build_funnel_figure,
    "monthly": build_monthly_figure,
    "demand": build_demand_figure,
    "progress": build_progress_figure,
    "license_status": build_license_status_figure,
}


def render_png(dataset: ReportDataset, *, chart_kind: str, compact: bool = False) -> bytes:
    builder = CHART_BUILDERS.get(chart_kind)
    if builder is None:
        raise RenderNotSupportedError(f"Для вида графика {chart_kind!r} функция не определена")
    return figure_to_png(builder(dataset), compact=compact)
