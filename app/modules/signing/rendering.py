"""Рендеринг документов ПЭП: Jinja2 → HTML → PDF, штамп, протокол, QR.

dop.md §10.4 называет WeasyPrint, но он тянет системные Pango/Cairo/
GDK-Pixbuf — это ломает принцип `Dockerfile` («никаких системных пакетов»,
сборка не должна зависеть от зеркал Debian при развёртывании в закрытом
контуре). `xhtml2pdf` даёт тот же результат (реальный PDF из HTML, реальный
`sha256`) на чистых manylinux-колёсах (`reportlab`, `pypdf`, `Pillow`) —
осознанная замена инструмента при сохранении требования спеки, см.
`pyproject.toml`.

Кириллица PDF-шрифтами по умолчанию (Helvetica и т.д.) не поддерживается
никаким из вариантов одинаково: нужен встроенный шрифт с покрытием кириллицы.
`app/assets/fonts/DejaVuSans{,-Bold}.ttf` (лицензия Bitstream Vera, см.
`DEJAVU_LICENSE.txt` рядом) подключается через `@font-face`, чтобы работать
одинаково в контейнере (там системных шрифтов нет вовсе) и локально.
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import jinja2
import qrcode
from pypdf import PdfReader, PdfWriter
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from xhtml2pdf import pisa

_ASSETS_DIR = Path(__file__).resolve().parent.parent.parent / "assets" / "fonts"
FONT_REGULAR_PATH = _ASSETS_DIR / "DejaVuSans.ttf"
FONT_BOLD_PATH = _ASSETS_DIR / "DejaVuSans-Bold.ttf"

_FONT_FAMILY = "DejaVuSans"
_REGISTERED_CANVAS_FONT = False


class RenderError(Exception):
    """PDF не удалось построить — html/шаблон повреждены."""


def _font_face_css() -> str:
    return f"""
    @font-face {{
        font-family: "{_FONT_FAMILY}";
        src: url("{FONT_REGULAR_PATH.as_uri()}");
    }}
    @font-face {{
        font-family: "{_FONT_FAMILY}";
        font-weight: bold;
        src: url("{FONT_BOLD_PATH.as_uri()}");
    }}
    body, table, div, p, span, h1, h2, h3, h4 {{ font-family: "{_FONT_FAMILY}"; }}
    """


def _deny_external_resources(uri: str, _rel: str) -> str:
    """`link_callback` для xhtml2pdf: наши шаблоны не должны тянуть внешние
    ресурсы (тот же принцип, что строгий CSP публичной страницы подписания,
    dop.md §10.11) — разрешены только два вложенных шрифта.
    """
    allowed = {FONT_REGULAR_PATH.as_uri(), FONT_BOLD_PATH.as_uri()}
    if uri in allowed:
        return uri.removeprefix("file://")
    raise RenderError(f"Внешний ресурс запрещён в шаблоне документа: {uri!r}")


def render_template_html(body_template: str, context: Mapping[str, Any]) -> str:
    """Jinja2 → HTML. `autoescape=True`: данные сделки/организации приходят от
    пользователей и не должны интерпретироваться как разметка."""
    env = jinja2.Environment(autoescape=True)
    template = env.from_string(body_template)
    body = template.render(**context)
    return f"<html><head><style>{_font_face_css()}</style></head><body>{body}</body></html>"


def html_to_pdf(html: str) -> bytes:
    buffer = io.BytesIO()
    result = pisa.CreatePDF(html, dest=buffer, link_callback=_deny_external_resources)
    if result.err:
        raise RenderError(f"Не удалось построить PDF из HTML (err={result.err})")
    return buffer.getvalue()


def render_signature_document(body_template: str, context: Mapping[str, Any]) -> bytes:
    """Полный путь: шаблон + данные → готовый PDF (dop.md §10.4 фаза 1 п.1)."""
    return html_to_pdf(render_template_html(body_template, context))


def _ensure_canvas_font_registered() -> None:
    global _REGISTERED_CANVAS_FONT
    if _REGISTERED_CANVAS_FONT:
        return
    pdfmetrics.registerFont(TTFont(_FONT_FAMILY, str(FONT_REGULAR_PATH)))
    pdfmetrics.registerFont(TTFont(f"{_FONT_FAMILY}-Bold", str(FONT_BOLD_PATH)))
    _REGISTERED_CANVAS_FONT = True


def _make_qr_image(url: str) -> ImageReader:
    img = qrcode.make(url, border=1)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return ImageReader(buf)


def apply_signature_stamp(pdf_bytes: bytes, *, lines: list[str], verify_url: str) -> bytes:
    """Накладывает штамп подписания на последнюю страницу (dop.md §10.4 п.20):
    «Документ подписан простой электронной подписью / ФИО / дата /
    идентификатор подписи / хэш» + QR на страницу проверки.

    Штамп рисуется поверх существующей последней страницы (`merge_page`), а
    не добавляется отдельной страницей — оригинальная разметка документа не
    сдвигается, что важно для многостраничных договоров. Страницы сразу
    переносятся в `PdfWriter` (`clone_from`), а `merge_page` вызывается на
    странице, уже принадлежащей writer'у: вызов на «отвязанной» странице
    ридера устарел и будет удалён в pypdf 7.0.

    Раздел 3.7/9: документ на подпись — файл, полученный из S3 или только
    что отрендеренный, а не ввод пользователя постранично, поэтому пустой
    или повреждённый PDF здесь — признак порчи объекта в хранилище, а не
    штатный кейс валидации формы; оборачиваем в `RenderError`, а не отдаём
    наружу исключение конкретной библиотеки чтения PDF.
    """
    _ensure_canvas_font_registered()
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        if not reader.pages:
            raise RenderError("Документ не содержит страниц")
        last_page = reader.pages[-1]
        width = float(last_page.mediabox.width)
        height = float(last_page.mediabox.height)
    except RenderError:
        raise
    except Exception as exc:  # noqa: BLE001 — любая ошибка чтения PDF считается одной и той же
        raise RenderError(f"Не удалось прочитать документ для штампа: {exc}") from exc

    box_height = 18 + 14 * len(lines)
    margin = 20
    qr_size = min(box_height - 10, 70)

    overlay_buffer = io.BytesIO()
    c = canvas.Canvas(overlay_buffer, pagesize=(width, height))
    c.setLineWidth(0.75)
    c.rect(margin, margin, width - 2 * margin, box_height)
    c.setFont(_FONT_FAMILY, 8)
    text_x = margin + 8
    text_y = margin + box_height - 14
    for line in lines:
        c.drawString(text_x, text_y, line)
        text_y -= 13
    c.drawImage(
        _make_qr_image(verify_url),
        width - margin - qr_size - 8,
        margin + (box_height - qr_size) / 2,
        width=qr_size,
        height=qr_size,
        mask="auto",
    )
    c.save()
    overlay_buffer.seek(0)
    overlay_page = PdfReader(overlay_buffer).pages[0]

    writer = PdfWriter(clone_from=reader)
    writer.pages[-1].merge_page(overlay_page)

    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def render_protocol_pdf(
    *,
    document_title: str,
    content_hash: str,
    entries: list[dict[str, Any]],
    verify_url: str,
) -> bytes:
    """Протокол подписания (dop.md §10.4 п.19): кто, когда, с какого IP,
    каким способом, хэш документа, QR на страницу проверки. Отдельный файл
    от самого документа — так его можно передать стороне, не имеющей права
    видеть содержимое договора, но имеющей право проверить факт подписания.
    """
    _ensure_canvas_font_registered()
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(595, 842))  # A4 в pt
    width = 595
    y = 800

    c.setFont(f"{_FONT_FAMILY}-Bold", 14)
    c.drawString(40, y, "Протокол подписания")
    y -= 22
    c.setFont(_FONT_FAMILY, 10)
    c.drawString(40, y, f"Документ: {document_title}")
    y -= 16
    c.drawString(40, y, f"Хэш документа (sha256): {content_hash}")
    y -= 26

    for entry in entries:
        c.setFont(f"{_FONT_FAMILY}-Bold", 10)
        c.drawString(40, y, str(entry.get("signer_display", "")))
        y -= 14
        c.setFont(_FONT_FAMILY, 9)
        for label, key in (
            ("Способ", "method"),
            ("Подписано", "signed_at"),
            ("IP", "ip"),
            ("User-Agent", "user_agent"),
            ("Идентификатор подписи", "signature_id"),
        ):
            value = entry.get(key)
            if value:
                c.drawString(52, y, f"{label}: {value}")
                y -= 12
        y -= 10
        if y < 120:
            c.showPage()
            c.setFont(_FONT_FAMILY, 9)
            y = 800

    qr_size = 90
    c.drawImage(
        _make_qr_image(verify_url),
        width - 40 - qr_size,
        40,
        width=qr_size,
        height=qr_size,
        mask="auto",
    )
    c.setFont(_FONT_FAMILY, 8)
    c.drawString(40, 60, "Проверка подлинности:")
    c.drawString(40, 48, verify_url)

    c.save()
    return buffer.getvalue()


def render_erasure_act_pdf(
    *,
    subject_type: str,
    subject_display: str,
    request_id: str,
    legal_basis: str,
    executed_at_iso: str,
    responsible_display: str,
    categories_erased: list[str],
    categories_retained: list[dict[str, str]],
) -> bytes:
    """Акт об уничтожении ПДн (new_spec §4.8.4 шаг 6, dop.md §10.7).

    Тот же инструмент, что `render_protocol_pdf` (reportlab-канва напрямую,
    без Jinja2-шаблона): это не документ, который администратор
    настраивает под свой брендбук, а фиксированная по составу доказательная
    форма — заказчик обязан предъявить её при проверке Роскомнадзора «как
    есть». `categories_retained` — не украшение: dop.md §10.7 прямо требует
    указывать, какие категории данных сохранены и на каком основании
    (пример: подписи — ст. 6 ч. 1 п. 5, 7 152-ФЗ), иначе акт выглядит как
    полное уничтожение там, где часть данных законно осталась.
    """
    _ensure_canvas_font_registered()
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=(595, 842))  # A4 в pt
    y = 800

    subject_labels = {
        "user": "сотрудник",
        "contact": "контакт",
        # dop.md §11.8: ИП — субъект удаления наравне с контактом, не
        # сведения о юрлице, поэтому в акте это отдельная, честная метка,
        # а не тихо «контакт» по умолчанию.
        "organization": "индивидуальный предприниматель",
    }
    c.setFont(f"{_FONT_FAMILY}-Bold", 14)
    c.drawString(40, y, "Акт об уничтожении персональных данных")
    y -= 22
    c.setFont(_FONT_FAMILY, 10)
    for label, value in (
        ("Субъект", f"{subject_display} ({subject_labels.get(subject_type, subject_type)})"),
        ("Запрос", request_id),
        ("Правовое основание", legal_basis),
        ("Дата исполнения", executed_at_iso),
        ("Ответственный", responsible_display),
    ):
        c.drawString(40, y, f"{label}: {value}")
        y -= 16
    y -= 10

    def _ensure_space(min_y: int = 100) -> None:
        nonlocal y
        if y < min_y:
            c.showPage()
            c.setFont(_FONT_FAMILY, 10)
            y = 800

    c.setFont(f"{_FONT_FAMILY}-Bold", 11)
    c.drawString(40, y, "Уничтоженные категории данных:")
    y -= 16
    c.setFont(_FONT_FAMILY, 9)
    for category in categories_erased:
        _ensure_space()
        c.drawString(52, y, f"— {category}")
        y -= 13
    y -= 14

    _ensure_space()
    c.setFont(f"{_FONT_FAMILY}-Bold", 11)
    c.drawString(40, y, "Сохранённые категории данных и основание сохранения:")
    y -= 16
    c.setFont(_FONT_FAMILY, 9)
    if categories_retained:
        for item in categories_retained:
            _ensure_space()
            c.drawString(52, y, f"— {item['category']}: {item['legal_basis']}")
            y -= 13
    else:
        _ensure_space()
        c.drawString(52, y, "— нет: все категории уничтожены полностью")
        y -= 13

    c.save()
    return buffer.getvalue()
