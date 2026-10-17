"""B1-4: формат xls (`xlwt`/BIFF8). Чистые функции рендера — без БД, тем же приёмом, что
`TestXlsxRendering`/`TestPdfRendering` в `tests/test_reporting.py`. Сквозной тест через API —
в `TestXlsEndToEnd` (настоящая Postgres обязательна, см. докстринг `tests/conftest.py`).
"""

from __future__ import annotations

import datetime as dt

import pytest
import xlrd

from app.modules.reporting.builders import ReportDataset
from app.modules.reporting.rendering import CONTENT_TYPES, render_report
from app.modules.reporting.rendering_xls import ReportTooLargeError, render_xls, sheet_title
from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

pytestmark_db = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _dataset(**kwargs) -> ReportDataset:
    defaults: dict = {
        "title": "Тестовый отчёт",
        "columns": ["Вуз", "Создана"],
        "rows": [["МГТУ им. Баумана ₽", dt.date(2026, 1, 5)]],
        "kinds": ["text", "date"],
    }
    defaults.update(kwargs)
    return ReportDataset(**defaults)


class TestXlsSignatureAndRoundTrip:
    def test_starts_with_ole2_signature(self) -> None:
        content = render_xls(_dataset())
        assert content[:8].hex() == "d0cf11e0a1b11ae1"

    def test_xlrd_reads_back_headers_and_cyrillic_with_currency_sign(self) -> None:
        content = render_xls(_dataset())
        book = xlrd.open_workbook(file_contents=content)
        sheet = book.sheet_by_index(0)
        assert sheet.cell_value(0, 0) == "Вуз"
        assert sheet.cell_value(0, 1) == "Создана"
        assert sheet.cell_value(1, 0) == "МГТУ им. Баумана ₽"

    def test_date_cell_reads_back_as_a_real_date_not_a_string(self) -> None:
        content = render_xls(_dataset())
        book = xlrd.open_workbook(file_contents=content)
        sheet = book.sheet_by_index(0)
        raw = sheet.cell_value(1, 1)
        assert isinstance(raw, float)  # xlrd хранит даты как число Excel, не строку
        parsed = xlrd.xldate_as_datetime(raw, book.datemode)
        assert parsed.date() == dt.date(2026, 1, 5)

    def test_aware_datetime_is_converted_to_naive_utc_not_rejected(self) -> None:
        aware = dt.datetime(2026, 1, 5, 10, 30, tzinfo=dt.UTC)
        content = render_xls(_dataset(columns=["Когда"], rows=[[aware]], kinds=["datetime"]))
        book = xlrd.open_workbook(file_contents=content)
        sheet = book.sheet_by_index(0)
        parsed = xlrd.xldate_as_datetime(sheet.cell_value(1, 0), book.datemode)
        assert parsed == dt.datetime(2026, 1, 5, 10, 30)


class TestXlsSafetyLimits:
    def test_cell_longer_than_limit_is_truncated_not_crashing(self) -> None:
        huge = "A" * 40_000
        content = render_xls(_dataset(columns=["Текст"], rows=[[huge]], kinds=["text"]))
        book = xlrd.open_workbook(file_contents=content)
        value = book.sheet_by_index(0).cell_value(1, 0)
        assert len(value) <= 32_767

    def test_formula_like_text_is_escaped_with_a_leading_apostrophe(self) -> None:
        content = render_xls(_dataset(columns=["Название"], rows=[["=1+1"]], kinds=["text"]))
        book = xlrd.open_workbook(file_contents=content)
        value = book.sheet_by_index(0).cell_value(1, 0)
        assert value == "'=1+1"

    def test_row_count_over_the_limit_raises_before_writing(self) -> None:
        rows = [[f"row {i}"] for i in range(65_536)]
        with pytest.raises(ReportTooLargeError):
            render_xls(_dataset(columns=["X"], rows=rows, kinds=["text"]))

    def test_row_count_at_the_limit_is_accepted(self) -> None:
        rows = [[f"row {i}"] for i in range(65_535)]
        content = render_xls(_dataset(columns=["X"], rows=rows, kinds=["text"]))
        assert content[:8].hex() == "d0cf11e0a1b11ae1"


class TestSheetTitleSanitization:
    def test_forbidden_characters_are_replaced(self) -> None:
        assert ":" not in sheet_title("Отчёт: янв/фев*2026?")
        assert "/" not in sheet_title("Отчёт: янв/фев*2026?")

    def test_name_is_truncated_to_31_characters(self) -> None:
        assert len(sheet_title("Очень длинное название отчёта на тридцать с лишним знаков")) <= 31

    def test_empty_title_falls_back_to_a_default(self) -> None:
        assert sheet_title("") == "Отчёт"


class TestRenderReportDispatchesXls:
    def test_xls_format_dispatches_to_render_xls(self) -> None:
        content = render_report(_dataset(), format="xls", template_code="kam_summary")
        assert content[:8].hex() == "d0cf11e0a1b11ae1"

    def test_content_type_is_registered(self) -> None:
        assert CONTENT_TYPES["xls"] == "application/vnd.ms-excel"


@pytestmark_db
class TestXlsEndToEnd:
    """B1-4, приёмка: `POST /api/reports` с `format=xls` для реального шаблона даёт `completed`,
    а файл в подменённом хранилище действительно читается `xlrd` (не просто «не упало»)."""

    def _ensure_template(self, client) -> None:
        from sqlalchemy import select as sa_select

        from app.core.db import session_scope
        from app.modules.reporting.models import ReportTemplate

        async def _ensure() -> None:
            async with session_scope() as session:
                existing = await session.scalar(
                    sa_select(ReportTemplate.id).where(ReportTemplate.code == "sla_compliance")
                )
                if existing is not None:
                    return
                session.add(
                    ReportTemplate(
                        code="sla_compliance",
                        name="Соблюдение SLA",
                        description="Доля сделок в норме/под угрозой/с нарушением SLA.",
                        query_def={"kind": "sla_compliance"},
                        allowed_roles=[],
                        default_params={},
                        output_formats=["xlsx", "xls", "pdf"],
                        is_active=True,
                    )
                )

        run(client, _ensure)

    def _stub_s3(self, monkeypatch) -> dict[str, bytes]:
        import app.modules.reporting.service as reporting_service

        captured: dict[str, bytes] = {}

        async def fake_ensure_bucket(bucket: str) -> None:
            return None

        async def fake_upload(*, bucket: str, key: str, body: bytes, content_type: str) -> None:
            captured["body"] = body

        monkeypatch.setattr(reporting_service, "ensure_bucket", fake_ensure_bucket)
        monkeypatch.setattr(reporting_service, "upload_object_bytes", fake_upload)
        return captured

    def test_post_reports_with_xls_format_gives_a_readable_file(self, client, monkeypatch) -> None:
        self._ensure_template(client)
        captured = self._stub_s3(monkeypatch)
        kam = run(client, _make_user, "KAM")
        client.headers["X-CSRF-Token"] = authenticate(client, kam)

        response = client.post(
            "/api/reports", json={"template_code": "sla_compliance", "format": "xls"}
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == "completed"

        content = captured["body"]
        assert content[:8].hex() == "d0cf11e0a1b11ae1"
        book = xlrd.open_workbook(file_contents=content)
        sheet = book.sheet_by_index(0)
        assert sheet.cell_value(0, 0) == "Состояние"
        assert sheet.nrows == 5  # заголовок + 4 состояния SLA
