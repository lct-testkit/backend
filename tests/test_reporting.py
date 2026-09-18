"""Тесты модуля отчётности и дашбордов (спринт 8, new_spec §4.13/§7.9).

Как и `tests/test_notifications.py`/`tests/test_signing.py`, здесь нет
поднятых PostgreSQL/Redis: покрываются чистые функции — рендеринг xlsx/pdf/
png из готового `ReportDataset` (реально запускает openpyxl/xhtml2pdf/
matplotlib и проверяет магические байты результата, тем же приёмом, что
`test_signing.py` уже применяет к `html_to_pdf`), CSV-инъекция в xlsx,
парсинг параметров, содержимое сида (кросс-проверка с
`reporting.builders.REPORT_BUILDERS`/`rendering.CHART_KINDS` — чтобы шаблон,
обещающий `png`, не оказался без зарегистрированного графика) и права.
`build_learning_progress` — единственный builder, не трогающий БД вообще
(честная заглушка, см. его докстринг), поэтому вызван по-настоящему.
Остальные builders (реальные SQL-запросы с RBAC-скоупом) и материализованное
представление проверены вживую против настоящего Postgres в этой же сессии,
не как pytest-тест.
"""

from __future__ import annotations

import pytest

from app.core.errors import ValidationError
from app.core.permissions import Permission, has_permission
from app.modules.identity.models import Role
from app.modules.reporting.builders import (
    REPORT_BUILDERS,
    REPORT_ESTIMATORS,
    ReportDataset,
    _parse_int,
    _parse_uuid,
    build_learning_progress,
)
from app.modules.reporting.rendering import (
    CHART_KINDS,
    CONTENT_TYPES,
    RenderNotSupportedError,
    _xlsx_cell,
    render_pdf,
    render_png,
    render_report,
    render_xlsx,
)
from app.modules.reporting.seed import _DEFAULT_TEMPLATES
from app.modules.reporting.service import SYNC_ROW_THRESHOLD


def _sample_dataset(rows: list[list] | None = None) -> ReportDataset:
    return ReportDataset(
        title="Тестовый отчёт",
        columns=["Колонка А", "Колонка Б"],
        rows=rows if rows is not None else [["Значение 1", 42], ["Значение 2", None]],
        note="Пояснение",
    )


class TestXlsxRendering:
    def test_produces_valid_zip_container(self) -> None:
        content = render_xlsx(_sample_dataset())
        assert content.startswith(b"PK")  # xlsx — zip-контейнер

    def test_none_cell_becomes_empty_string(self) -> None:
        assert _xlsx_cell(None) == ""

    def test_numeric_and_bool_cells_pass_through_unchanged(self) -> None:
        assert _xlsx_cell(42) == 42
        assert _xlsx_cell(3.5) == 3.5
        assert _xlsx_cell(True) is True

    def test_non_primitive_falls_back_to_str(self) -> None:
        assert _xlsx_cell(("x", "y")) == "('x', 'y')"


class TestCsvInjectionProtection:
    @pytest.mark.parametrize("value", ["=cmd|'/c calc'!A1", "+1+1", "-1-1", "@SUM(A1)"])
    def test_leading_formula_char_gets_escaped(self, value: str) -> None:
        cell = _xlsx_cell(value)
        assert cell == f"'{value}"

    def test_normal_text_untouched(self) -> None:
        assert _xlsx_cell("Организация «Ростелеком»") == "Организация «Ростелеком»"


class TestPdfRendering:
    def test_produces_valid_pdf_with_cyrillic_content(self) -> None:
        content = render_pdf(_sample_dataset())
        assert content.startswith(b"%PDF")

    def test_empty_rows_still_renders(self) -> None:
        content = render_pdf(ReportDataset(title="Пусто", columns=["A"], rows=[]))
        assert content.startswith(b"%PDF")


class TestPngRendering:
    def test_funnel_chart_renders_valid_png(self) -> None:
        dataset = ReportDataset(
            title="Воронка",
            columns=["Статус", "Сейчас", "Прошло", "Конверсия", "Дни"],
            rows=[["Новый", 10, 10, 100.0, 1.0], ["В работе", 5, 8, 80.0, 2.0]],
        )
        content = render_png(dataset, template_code="deal_funnel")
        assert content.startswith(b"\x89PNG\r\n\x1a\n")

    def test_monthly_chart_renders_valid_png(self) -> None:
        dataset = ReportDataset(
            title="Динамика",
            columns=["Месяц", "Создано", "Выиграно", "Проиграно", "Сумма"],
            rows=[["2026-01", 5, 2, 1, 100000.0]],
        )
        content = render_png(dataset, template_code="monthly_dynamics")
        assert content.startswith(b"\x89PNG\r\n\x1a\n")

    def test_unsupported_kind_raises_not_fake_image(self) -> None:
        with pytest.raises(RenderNotSupportedError):
            render_png(_sample_dataset(), template_code="kam_summary")


class TestRenderDispatch:
    def test_xlsx_format_dispatches_correctly(self) -> None:
        content = render_report(_sample_dataset(), format="xlsx", template_code="kam_summary")
        assert content.startswith(b"PK")

    def test_pdf_format_dispatches_correctly(self) -> None:
        content = render_report(_sample_dataset(), format="pdf", template_code="kam_summary")
        assert content.startswith(b"%PDF")

    def test_png_format_dispatches_correctly(self) -> None:
        dataset = ReportDataset(title="Воронка", columns=["Статус", "Сейчас"], rows=[["Новый", 1]])
        content = render_report(dataset, format="png", template_code="deal_funnel")
        assert content.startswith(b"\x89PNG\r\n\x1a\n")

    def test_unknown_format_raises(self) -> None:
        with pytest.raises(RenderNotSupportedError):
            render_report(_sample_dataset(), format="csv", template_code="kam_summary")


class TestParamParsing:
    def test_parse_uuid_accepts_valid_uuid_string(self) -> None:
        value = _parse_uuid({"workflow_id": "550e8400-e29b-41d4-a716-446655440000"}, "workflow_id")
        assert str(value) == "550e8400-e29b-41d4-a716-446655440000"

    def test_parse_uuid_returns_none_when_absent(self) -> None:
        assert _parse_uuid({}, "workflow_id") is None

    def test_parse_uuid_rejects_garbage(self) -> None:
        with pytest.raises(ValidationError):
            _parse_uuid({"workflow_id": "not-a-uuid"}, "workflow_id")

    def test_parse_int_clamps_to_bounds(self) -> None:
        assert _parse_int({"limit": 99999}, "limit", default=500, minimum=1, maximum=5000) == 5000
        assert _parse_int({"limit": -5}, "limit", default=500, minimum=1, maximum=5000) == 1

    def test_parse_int_uses_default_when_absent(self) -> None:
        assert _parse_int({}, "months", default=12, minimum=1, maximum=36) == 12

    def test_parse_int_rejects_non_numeric(self) -> None:
        with pytest.raises(ValidationError):
            _parse_int({"limit": "many"}, "limit", default=500, minimum=1, maximum=5000)


class TestLearningProgressStub:
    async def test_returns_empty_dataset_with_honest_note_not_fake_data(self) -> None:
        # Единственный builder, не трогающий сессию/принципала вообще — см.
        # докстринг модуля и `build_learning_progress` самой.
        dataset = await build_learning_progress(None, None, {})  # type: ignore[arg-type]
        assert dataset.rows == []
        assert dataset.note is not None
        assert "LMS" in dataset.note


class TestBuilderRegistry:
    def test_eight_builders_match_new_spec_4_13_literal_list(self) -> None:
        assert len(REPORT_BUILDERS) == 8

    def test_only_stuck_deals_has_a_real_row_estimator(self) -> None:
        # Раздел 4.13: агрегаты структурно малы (статусы/регионы/КАМы/
        # месяцы/причины отказов) и всегда идут по лёгкому пути — только
        # листинг `stuck_deals` может реально превысить порог.
        assert set(REPORT_ESTIMATORS) == {"stuck_deals"}

    def test_sync_threshold_matches_spec_literal_number(self) -> None:
        assert SYNC_ROW_THRESHOLD == 1000


class TestSeedTemplates:
    def test_eight_templates_seeded(self) -> None:
        assert len(_DEFAULT_TEMPLATES) == 8

    def test_no_duplicate_codes(self) -> None:
        codes = [row[0] for row in _DEFAULT_TEMPLATES]
        assert len(codes) == len(set(codes))

    def test_every_kind_has_a_registered_builder(self) -> None:
        for code, _name, _desc, kind, *_ in _DEFAULT_TEMPLATES:
            assert kind in REPORT_BUILDERS, f"{code!r}: нет builder'а для kind={kind!r}"

    def test_every_seeded_output_format_is_renderable(self) -> None:
        for code, _name, _desc, _kind, _roles, _params, formats in _DEFAULT_TEMPLATES:
            for fmt in formats:
                assert fmt in CONTENT_TYPES, f"{code!r}: формат {fmt!r} не в CONTENT_TYPES"

    def test_png_only_seeded_for_kinds_with_a_registered_chart(self) -> None:
        for code, _name, _desc, kind, _roles, _params, formats in _DEFAULT_TEMPLATES:
            if "png" in formats:
                assert kind in CHART_KINDS, f"{code!r} обещает png, но графика для {kind!r} нет"

    def test_kam_summary_restricted_to_head_and_admin_per_matrix_5(self) -> None:
        row = next(r for r in _DEFAULT_TEMPLATES if r[0] == "kam_summary")
        assert set(row[4]) == {"HEAD", "ADMIN"}

    def test_every_body_free_of_undefined_kind(self) -> None:
        kinds = {row[3] for row in _DEFAULT_TEMPLATES}
        assert kinds == set(REPORT_BUILDERS)


class TestReportPermissions:
    @pytest.mark.parametrize("role", [Role.KAM, Role.HEAD, Role.ADMIN])
    def test_granted_to_business_roles(self, role: Role) -> None:
        assert has_permission(role.value, Permission.REPORT_READ)
        assert has_permission(role.value, Permission.REPORT_CREATE)

    @pytest.mark.parametrize("role", [Role.AUDITOR, Role.INTEGRATION])
    def test_denied_to_auditor_and_integration(self, role: Role) -> None:
        assert not has_permission(role.value, Permission.REPORT_CREATE)
        assert not has_permission(role.value, Permission.REPORT_READ)
