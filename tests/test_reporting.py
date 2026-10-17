"""Тесты модуля отчётности и дашбордов (спринт 8, new_spec §4.13/§7.9; спринт B1).

Как и `tests/test_notifications.py`/`tests/test_signing.py`, здесь по большей части нет
поднятых PostgreSQL/Redis: покрываются чистые функции — рендеринг xlsx/pdf/
png из готового `ReportDataset` (реально запускает openpyxl/xhtml2pdf/
matplotlib и проверяет магические байты результата, тем же приёмом, что
`test_signing.py` уже применяет к `html_to_pdf`), CSV-инъекция в xlsx,
парсинг параметров, содержимое сида (кросс-проверка с
`reporting.builders.REPORT_BUILDERS`/`rendering.CHART_KINDS` — чтобы шаблон,
обещающий `png`, не оказался без зарегистрированного графика) и права.
Содержимое видов отчёта на настоящей БД (B1-9/B1-10/B1-11) — отдельными файлами, не здесь:
`tests/test_reporting_demand.py` (`product_demand`), `tests/test_reporting_progress.py`
(`learning_progress`), `tests/test_reporting_licenses.py` (`license_register`),
`tests/test_reporting_register.py` (`deal_register`, усечение B1-6), `tests/
test_reporting_seed.py` (`seed_demo_report_history`, B1-14). Остальные builders раздела 4.13
(реальные SQL-запросы с RBAC-скоупом) и материализованное представление проверены вживую
против настоящего Postgres в этой же сессии, не как pytest-тест — кроме
`TestReportDataEndpoint` (П2) и `TestReportJobs` (коды CRM-1601/1602/1603, `progress_pct`,
фильтр `template_code`): настоящая Postgres обязательна, тот же приём, что
`tests/test_imports.py::TestLicenseImportEndToEnd` — см. `tests/conftest.py`.
"""

from __future__ import annotations

import datetime as dt
import io
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.core.errors import ValidationError
from app.core.permissions import Permission, has_permission
from app.modules.crm.models import Deal
from app.modules.identity.models import Role
from app.modules.reporting.builders import (
    REPORT_BUILDERS,
    REPORT_ESTIMATORS,
    ReportDataset,
    ReportFilters,
    _apply_deal_filters,
    _apply_entity_filters,
    _parse_date_range,
    _parse_int,
    _parse_report_filters,
    _parse_uuid,
    _parse_uuid_list,
    build_deal_register,
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
    report_file_name,
)
from app.modules.reporting.seed import _DEFAULT_TEMPLATES
from app.modules.reporting.service import SYNC_ROW_THRESHOLD
from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run


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


class TestXlsxDatesAndLayout:
    """B1-6: раньше `datetime` в xlsx попадал в файл текстом (`_xlsx_cell` приводило всё
    не-число/не-строку к `str(value)`) — здесь проверяется, что ячейка с `kind="datetime"`
    становится настоящей датой Excel с форматом, а не строкой, плюс сопутствующие требования
    качества файла (ширины колонок, закреплённая шапка, автофильтр, альбомная печать)."""

    def _dataset_with_datetime(self) -> ReportDataset:
        aware = dt.datetime(2026, 1, 31, 21, 30, tzinfo=dt.UTC)
        return ReportDataset(
            title="Реестр",
            columns=["Вуз", "Создана"],
            rows=[["МГТУ им. Баумана", aware]],
            keys=["organization", "created_at"],
            kinds=["text", "datetime"],
        )

    def _load(self, content: bytes):
        from openpyxl import load_workbook

        return load_workbook(io.BytesIO(content))

    def test_datetime_cell_is_a_real_date_not_a_string(self) -> None:
        content = render_xlsx(self._dataset_with_datetime(), tz_name="Europe/Moscow")
        sheet = self._load(content).active
        cell = sheet.cell(row=2, column=2)
        assert cell.is_date
        assert cell.number_format == "DD.MM.YYYY HH:MM"
        # 21:30 UTC 31 января = 00:30 1 февраля в Москве (UTC+3) — перевод пояса меняет дату
        # ячейки, не только время.
        assert cell.value == dt.datetime(2026, 2, 1, 0, 30)

    def test_column_widths_are_within_the_documented_bounds(self) -> None:
        content = render_xlsx(self._dataset_with_datetime())
        sheet = self._load(content).active
        for column in ("A", "B"):
            width = sheet.column_dimensions[column].width
            assert 8 <= width <= 60

    def test_header_row_is_frozen_and_bold(self) -> None:
        content = render_xlsx(self._dataset_with_datetime())
        sheet = self._load(content).active
        assert sheet.freeze_panes == "A2"
        assert sheet.cell(row=1, column=1).font.bold is True

    def test_autofilter_covers_the_whole_table(self) -> None:
        content = render_xlsx(self._dataset_with_datetime())
        sheet = self._load(content).active
        assert sheet.auto_filter.ref == "A1:B2"

    def test_print_setup_is_landscape_and_fits_the_page_width(self) -> None:
        content = render_xlsx(self._dataset_with_datetime())
        sheet = self._load(content).active
        assert sheet.page_setup.orientation == "landscape"
        assert sheet.page_setup.fitToWidth == 1
        assert sheet.page_setup.fitToHeight == 0
        assert sheet.print_title_rows == "$1:$1"

    def test_decimal_value_is_a_real_number_not_text(self) -> None:
        dataset = ReportDataset(
            title="Сумма",
            columns=["Сумма, ₽"],
            rows=[[Decimal("150000.50")]],
            kinds=["money"],
        )
        content = render_xlsx(dataset)
        sheet = self._load(content).active
        cell = sheet.cell(row=2, column=1)
        assert cell.value == 150000.50
        assert cell.number_format == "#,##0.00"


class TestReportFileName:
    """B1-6: раньше `f"{dataset.title}.{job.format}"` (`reporting.service.generate`) давало
    одно и то же имя файла при каждой выгрузке — повторное скачивание различалось только
    припиской браузера `(1)`/`(2)`."""

    def test_combines_title_date_and_extension(self) -> None:
        generated_at = dt.datetime(2026, 10, 16, 14, 5, tzinfo=dt.UTC)
        assert report_file_name("Причины отказов", generated_at, "xlsx") == (
            "Причины отказов_2026-10-16.xlsx"
        )

    def test_forbidden_characters_are_replaced(self) -> None:
        generated_at = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        name = report_file_name('Отчёт: янв/фев*2026?<>"', generated_at, "pdf")
        assert all(ch not in name for ch in '\\/:*?"<>')

    def test_empty_title_falls_back_to_a_default(self) -> None:
        generated_at = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        assert report_file_name("", generated_at, "json") == "Отчёт_2026-01-01.json"

    def test_result_is_never_longer_than_the_documented_bound(self) -> None:
        generated_at = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        long_title = "А" * 500
        name = report_file_name(long_title, generated_at, "xlsx")
        assert len(name) <= 100 + len(".xlsx")


class TestPdfRendering:
    def test_produces_valid_pdf_with_cyrillic_content(self) -> None:
        content = render_pdf(_sample_dataset())
        assert content.startswith(b"%PDF")

    def test_empty_rows_still_renders(self) -> None:
        content = render_pdf(ReportDataset(title="Пусто", columns=["A"], rows=[]))
        assert content.startswith(b"%PDF")

    def test_page_is_landscape_a4(self) -> None:
        """B1-6: портретный A4 не помещал 10 колонок — альбомная ориентация через CSS `@page`
        (вариант A из сравнения в задаче, без перехода на reportlab Platypus)."""
        from pypdf import PdfReader

        content = render_pdf(_sample_dataset())
        reader = PdfReader(io.BytesIO(content))
        box = reader.pages[0].mediabox
        assert float(box.width) > float(box.height)


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

    # -- П1: период --------------------------------------------------------

    def test_parse_date_range_accepts_iso_dates(self) -> None:
        date_from, date_to = _parse_date_range({"date_from": "2026-01-01", "date_to": "2026-01-31"})
        assert date_from.isoformat() == "2026-01-01"
        assert date_to.isoformat() == "2026-01-31"

    def test_parse_date_range_both_absent_is_none(self) -> None:
        assert _parse_date_range({}) == (None, None)

    def test_parse_date_range_one_sided_is_allowed(self) -> None:
        date_from, date_to = _parse_date_range({"date_from": "2026-01-01"})
        assert date_from is not None
        assert date_to is None

    def test_parse_date_range_rejects_from_after_to(self) -> None:
        with pytest.raises(ValidationError):
            _parse_date_range({"date_from": "2026-02-01", "date_to": "2026-01-01"})

    def test_parse_date_range_rejects_garbage(self) -> None:
        with pytest.raises(ValidationError):
            _parse_date_range({"date_from": "не дата"})

    # -- П1: списки ID -------------------------------------------------------

    def test_parse_uuid_list_accepts_json_array(self) -> None:
        ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        parsed = _parse_uuid_list({"organization_ids": ids}, "organization_ids")
        assert [str(p) for p in parsed] == ids

    def test_parse_uuid_list_accepts_csv_string(self) -> None:
        a, b = uuid.uuid4(), uuid.uuid4()
        parsed = _parse_uuid_list({"owner_ids": f"{a}, {b}"}, "owner_ids")
        assert parsed == [a, b]

    def test_parse_uuid_list_returns_none_when_absent_or_empty(self) -> None:
        assert _parse_uuid_list({}, "product_ids") is None
        assert _parse_uuid_list({"product_ids": []}, "product_ids") is None

    def test_parse_uuid_list_rejects_garbage(self) -> None:
        with pytest.raises(ValidationError):
            _parse_uuid_list({"direction_ids": ["not-a-uuid"]}, "direction_ids")

    def test_parse_report_filters_collects_all_six_keys(self) -> None:
        org_id = uuid.uuid4()
        filters = _parse_report_filters(
            {"date_from": "2026-01-01", "organization_ids": [str(org_id)]}
        )
        assert filters.date_from is not None
        assert filters.date_to is None
        assert filters.organization_ids == [org_id]
        assert filters.direction_ids is None
        assert filters.has_any() is True

    def test_empty_params_yields_no_filters(self) -> None:
        assert _parse_report_filters({}).has_any() is False


class TestReportFilterClauses:
    """Билдеры выполняют реальные SQL-запросы (нужен Postgres — см. докстринг
    модуля), но композиция WHERE-условий — чистая функция над `Select`,
    проверяется компиляцией в SQL-текст без подключения к БД, тем же
    приёмом, что и остальные тесты этого файла."""

    def _sql(self, stmt) -> str:
        return str(
            stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        )

    def test_no_filters_is_a_no_op(self) -> None:
        stmt = _apply_deal_filters(select(Deal.id), ReportFilters())
        assert self._sql(stmt) == self._sql(select(Deal.id))

    def test_date_range_filters_inclusive_day_boundaries(self) -> None:
        filters = _parse_report_filters({"date_from": "2026-01-01", "date_to": "2026-01-31"})
        sql = self._sql(_apply_deal_filters(select(Deal.id), filters))
        assert "deals.created_at >=" in sql
        assert "deals.created_at <" in sql
        assert "2026-01-01" in sql
        # Верхняя граница исключающая и сдвинута на следующий день, иначе
        # весь `date_to` целиком (со временем > 00:00) выпал бы из отчёта.
        assert "2026-02-01" in sql

    def test_date_filter_uses_custom_column_when_given(self) -> None:
        filters = _parse_report_filters({"date_from": "2026-01-01"})
        sql = self._sql(_apply_deal_filters(select(Deal.id), filters, date_column=Deal.closed_at))
        assert "deals.closed_at >=" in sql
        assert "deals.created_at" not in sql

    def test_organization_and_owner_filters(self) -> None:
        org_id, owner_id = uuid.uuid4(), uuid.uuid4()
        filters = _parse_report_filters(
            {"organization_ids": [str(org_id)], "owner_ids": [str(owner_id)]}
        )
        sql = self._sql(_apply_entity_filters(select(Deal.id), filters))
        assert "deals.organization_id IN" in sql
        assert "deals.owner_id IN" in sql
        assert str(org_id) in sql
        assert str(owner_id) in sql

    def test_product_and_direction_filters_go_through_deal_products(self) -> None:
        product_id, direction_id = uuid.uuid4(), uuid.uuid4()
        filters = _parse_report_filters(
            {"product_ids": [str(product_id)], "direction_ids": [str(direction_id)]}
        )
        sql = self._sql(_apply_entity_filters(select(Deal.id), filters))
        assert "deal_products" in sql
        assert "products.direction_id IN" in sql


class TestBuilderRegistry:
    def test_builders_match_new_spec_4_13_plus_b1_additions(self) -> None:
        # Восемь видов раздела 4.13, плюс выгрузка для LMS (`lms_users_upload`), `deal_register`
        # (буквальное прочтение Функц.4/§4.2, см. докстринг модуля), `product_demand` (B1-9,
        # раздел «Цель» ТЗ) и `license_register` (B1-11, ТЗ §4 п.1). Число — `len(REPORT_SPECS)`
        # (B1-15): растёт с каждым новым видом без правки литерала здесь.
        from app.modules.reporting.spec import REPORT_SPECS

        assert len(REPORT_BUILDERS) == len(REPORT_SPECS)
        assert "lms_users_upload" in REPORT_BUILDERS
        assert "deal_register" in REPORT_BUILDERS
        assert "product_demand" in REPORT_BUILDERS
        assert "license_register" in REPORT_BUILDERS

    def test_only_listings_have_a_real_row_estimator(self) -> None:
        # Раздел 4.13: агрегаты структурно малы (статусы/регионы/КАМы/
        # месяцы/причины отказов, B1-9: продукты/направления) и всегда идут по лёгкому пути —
        # только построчные листинги (`stuck_deals`, `deal_register`, B1-10:
        # `learning_progress`, B1-11: `license_register`) могут реально превысить порог.
        assert set(REPORT_ESTIMATORS) == {
            "stuck_deals",
            "deal_register",
            "learning_progress",
            "license_register",
        }

    def test_sync_threshold_matches_spec_literal_number(self) -> None:
        assert SYNC_ROW_THRESHOLD == 1000


class TestDealRegisterReport:
    """`deal_register` (раздел 3 докстринга модуля) — реестр сделок в разрезе вуза, направления,
    продукта, статуса и ответственного. Сам билдер (`_deal_register_query`) требует настоящую БД
    (`deal_scope_clause`, JOIN'ы на `deal_products`/`products`/`directions`) — как и у его
    ближайшего родственника `stuck_deals`, для этого в файле нет отдельного теста (см. докстринг
    модуля: чистые функции тестируются без БД, остальное — там, где уже поднята инфраструктура ради
    другого теста). Здесь проверяется то, что не требует сессии: порядок колонок и построчное
    отображение результата запроса, включая замену отсутствующих продукта/направления на «—»."""

    async def test_maps_rows_with_dashes_for_missing_product_and_direction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.modules.reporting import builders

        class _Stmt:
            def limit(self, _n: int) -> _Stmt:
                return self

        async def fake_query(
            session: object, principal: object, params: dict, *, columns: list[str]
        ) -> _Stmt:
            return _Stmt()

        class _Result:
            def all(self) -> list[tuple]:
                return [
                    (
                        "ООО «Ромашка»",
                        None,
                        None,
                        "Квалификация",
                        "Иван Иванов",
                        "D-2026-000001",
                        "Курс Python",
                        Decimal("150000.00"),
                        dt.datetime(2026, 1, 5, tzinfo=dt.UTC),
                        None,
                    )
                ]

        class _Session:
            async def execute(self, _stmt: object) -> _Result:
                return _Result()

        monkeypatch.setattr(builders, "_deal_register_query", fake_query)
        dataset = await build_deal_register(_Session(), object(), {})  # type: ignore[arg-type]
        assert dataset.columns == [
            "Вуз",
            "Направление",
            "Продукт",
            "Статус",
            "Ответственный",
            "Номер",
            "Название",
            "Сумма, ₽",
            "Создана",
            "Закрыта",
        ]
        assert dataset.rows == [
            [
                "ООО «Ромашка»",
                "—",
                "—",
                "Квалификация",
                "Иван Иванов",
                "D-2026-000001",
                "Курс Python",
                150000.0,
                dt.datetime(2026, 1, 5, tzinfo=dt.UTC),
                None,
            ]
        ]


class TestSeedTemplates:
    def test_one_template_per_builder(self) -> None:
        # B1-15: число шаблонов растёт с каждым новым видом (B1-9 `product_demand`, B1-11
        # `license_register`) — литерал здесь обновлялся бы каждый раз. Полное соответствие
        # кодов множеству `REPORT_BUILDERS` (не только числа) проверяет соседний тест ниже
        # (`test_every_body_free_of_undefined_kind`, по полю `kind`, не `code` — в этом сиде они
        # совпадают всегда, но формально это разные поля одной строки).
        assert len(_DEFAULT_TEMPLATES) == len(REPORT_BUILDERS)

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


class TestReportDataEndpoint:
    """П2 (rtk_requiriments.md разд. 6.4; frontend/docs/backend-issues.md #19):
    `GET /api/reports/{report_id}/data` отдаёт тот же датасет, что и
    xlsx/pdf, но JSON'ом — без файла в S3, без `REPORT_EXPORTED`. Настоящая
    Postgres обязательна — см. докстринг модуля."""

    pytestmark = pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
    )

    def _kam(self, client) -> None:
        kam = run(client, _make_user, "KAM")
        csrf = authenticate(client, kam)
        client.headers["X-CSRF-Token"] = csrf

    def _stub_s3(self, monkeypatch) -> None:
        """`POST /api/reports` для лёгкого шаблона генерирует файл синхронно
        (`ReportJobService.generate`) и грузит его в S3 — недоступный по
        design (`S3_ENDPOINT_URL=http://127.0.0.1:9` в `tests/conftest.py`,
        тот же приём, что уже используют тесты Redis/Keycloak). `/data` сама
        файл не трогает (это и есть смысл П2), но чтобы вообще получить
        `report_id` со статусом `completed`, сначала нужно пройти обычный
        `POST /api/reports` — тот же приём, что `test_imports.py::
        TestLicenseImportEndToEnd._stub_storage`."""
        import app.modules.reporting.service as reporting_service

        async def fake_ensure_bucket(bucket: str) -> None:
            return None

        async def fake_upload(*, bucket: str, key: str, body: bytes, content_type: str) -> None:
            return None

        monkeypatch.setattr(reporting_service, "ensure_bucket", fake_ensure_bucket)
        monkeypatch.setattr(reporting_service, "upload_object_bytes", fake_upload)

    def _seed_template(self, client) -> None:
        """`POST /api/reports` резолвит `template_code` через `report_templates`
        (`ReportJobService.create`) — таблица пуста на голой БД после
        `alembic upgrade head` (шаблоны заводит отдельно `python -m
        app.modules.reporting.seed`, не входит в `tests/conftest.py`).
        Остальные тесты этого файла самодостаточны (сами заводят пользователей
        через `_make_user`) — заводим и здесь, вместо того чтобы тесту молча
        полагаться на то, что кто-то заранее прогнал сид (`tests/conftest.py`
        документирует только `alembic upgrade head`, без сидов)."""
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
                        output_formats=["xlsx", "pdf"],
                        is_active=True,
                    )
                )

        run(client, _ensure)

    def test_data_endpoint_returns_same_shape_as_the_builder(self, client, monkeypatch) -> None:
        self._stub_s3(monkeypatch)
        self._seed_template(client)
        self._kam(client)
        create = client.post(
            "/api/reports", json={"template_code": "sla_compliance", "format": "xlsx"}
        )
        assert create.status_code == 201, create.text
        job = create.json()
        assert job["status"] == "completed", job  # агрегат — всегда лёгкий путь

        data = client.get(f"/api/reports/{job['id']}/data")
        assert data.status_code == 200, data.text
        body = data.json()
        assert body["columns"] == ["Состояние", "Сделок", "Доля, %"]
        assert body["row_count"] == len(body["rows"])
        # Четыре строки состояний SLA — раздел 4.13, построитель
        # `build_sla_compliance` всегда возвращает все четыре, даже нулевые.
        assert len(body["rows"]) == 4
        assert body["generated_at"]

    def test_data_endpoint_does_not_create_a_file_or_export_audit_event(
        self, client, monkeypatch
    ) -> None:
        from sqlalchemy import func, select

        from app.core.db import session_scope
        from app.modules.audit.models import AuditLog

        self._stub_s3(monkeypatch)
        self._seed_template(client)
        self._kam(client)
        job_id = client.post(
            "/api/reports", json={"template_code": "sla_compliance", "format": "xlsx"}
        ).json()["id"]

        async def _count_exports() -> int:
            async with session_scope() as session:
                return int(
                    (
                        await session.execute(
                            select(func.count(AuditLog.id)).where(
                                AuditLog.action == "REPORT_EXPORTED",
                                AuditLog.entity_id == uuid.UUID(job_id),
                            )
                        )
                    ).scalar_one()
                )

        before = run(client, _count_exports)
        assert client.get(f"/api/reports/{job_id}/data").status_code == 200
        assert client.get(f"/api/reports/{job_id}/data").status_code == 200
        # POST /api/reports само уже написало ровно одно REPORT_EXPORTED
        # (генерация xlsx) — два вызова /data сверх него не добавили ни
        # одного: чтение, не выгрузка.
        assert run(client, _count_exports) == before

    def test_data_endpoint_denies_another_users_report(self, client, monkeypatch) -> None:
        self._stub_s3(monkeypatch)
        self._seed_template(client)
        self._kam(client)
        job_id = client.post(
            "/api/reports", json={"template_code": "sla_compliance", "format": "xlsx"}
        ).json()["id"]

        other_kam = run(client, _make_user, "KAM")
        authenticate(client, other_kam)
        response = client.get(f"/api/reports/{job_id}/data")
        assert response.status_code == 403, response.text


class TestReportJobs:
    """B#17, B#18, B#20 (`frontend/docs/backend-issues.md`): коды CRM-1601/1602/1603,
    `progress_pct` и фильтр `template_code` в `GET /api/reports`. Настоящая Postgres
    обязательна — см. докстринг модуля."""

    pytestmark = pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
    )

    def _kam(self, client):
        kam = run(client, _make_user, "KAM")
        client.headers["X-CSRF-Token"] = authenticate(client, kam)
        return kam

    def _stub_s3(self, monkeypatch) -> None:
        import app.modules.reporting.service as reporting_service

        async def fake_ensure_bucket(bucket: str) -> None:
            return None

        async def fake_upload(*, bucket: str, key: str, body: bytes, content_type: str) -> None:
            return None

        monkeypatch.setattr(reporting_service, "ensure_bucket", fake_ensure_bucket)
        monkeypatch.setattr(reporting_service, "upload_object_bytes", fake_upload)

    def _ensure_templates(self, client, *codes: str) -> None:
        """Лёгкие агрегатные шаблоны: `kind` совпадает с кодом (см. `reporting.seed`)."""
        from sqlalchemy import select as sa_select

        from app.core.db import session_scope
        from app.modules.reporting.models import ReportTemplate

        async def _ensure() -> None:
            async with session_scope() as session:
                for code in codes:
                    existing = await session.scalar(
                        sa_select(ReportTemplate.id).where(ReportTemplate.code == code)
                    )
                    if existing is None:
                        session.add(
                            ReportTemplate(
                                code=code,
                                name=code,
                                query_def={"kind": code},
                                allowed_roles=[],
                                default_params={},
                                output_formats=["xlsx", "pdf"],
                                is_active=True,
                            )
                        )

        run(client, _ensure)

    def _insert_job(self, client, requester, **fields) -> str:
        """Задание отчёта напрямую в БД: так получаются `queued`/`failed`/просроченные."""
        from app.core.db import session_scope
        from app.modules.reporting.models import ReportJob

        async def _create() -> str:
            async with session_scope() as session:
                job = ReportJob(
                    **{
                        "template_code": "sla_compliance",
                        "format": "xlsx",
                        "requested_by": requester.id,
                        "status": "queued",
                        **fields,
                    }
                )
                session.add(job)
                await session.flush()
                return str(job.id)

        return run(client, _create)

    def _download(self, client, job_id: str):
        return client.get(f"/api/reports/{job_id}/download")

    def test_completed_report_has_full_progress(self, client, monkeypatch) -> None:
        self._stub_s3(monkeypatch)
        self._ensure_templates(client, "sla_compliance")
        self._kam(client)

        response = client.post(
            "/api/reports", json={"template_code": "sla_compliance", "format": "xlsx"}
        )
        assert response.status_code == 201, response.text
        assert response.json()["status"] == "completed"
        assert response.json()["progress_pct"] == 100

    def test_download_of_an_unfinished_report_is_not_ready(self, client) -> None:
        kam = self._kam(client)

        for status in ("queued", "processing", "failed"):
            response = self._download(client, self._insert_job(client, kam, status=status))
            assert response.status_code == 409, (status, response.text)
            assert response.json()["code"] == "CRM-1602", status

    def test_download_after_retention_is_gone(self, client) -> None:
        # `expire_report_files` удаляет файл, но оставляет `completed` и `expires_at`.
        kam = self._kam(client)
        job_id = self._insert_job(
            client,
            kam,
            status="completed",
            file_id=None,
            expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=1),
        )

        response = self._download(client, job_id)
        assert response.status_code == 410, response.text
        assert response.json()["code"] == "CRM-1603"

    def test_download_after_expiry_is_gone_even_if_the_file_is_not_swept_yet(self, client) -> None:
        from app.core.db import session_scope
        from app.modules.files.models import File

        async def _ready_file() -> uuid.UUID:
            async with session_scope() as session:
                file = File(
                    storage_key=f"test/{uuid.uuid4()}.xlsx",
                    bucket="reports",
                    original_filename="Отчёт.xlsx",
                    mime_type="application/vnd.ms-excel",
                    size_bytes=10,
                    status="ready",
                )
                session.add(file)
                await session.flush()
                return file.id

        kam = self._kam(client)
        job_id = self._insert_job(
            client,
            kam,
            status="completed",
            file_id=run(client, _ready_file),
            expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=5),
        )

        response = self._download(client, job_id)
        assert response.status_code == 410, response.text
        assert response.json()["code"] == "CRM-1603"

    def test_too_many_unfinished_reports_are_refused(self, client, monkeypatch) -> None:
        from app.core.config import get_settings

        self._stub_s3(monkeypatch)
        self._ensure_templates(client, "sla_compliance")
        kam = self._kam(client)
        for _ in range(get_settings().reports_max_concurrent):
            self._insert_job(client, kam, status="queued")

        request = {"template_code": "sla_compliance", "format": "xlsx"}
        refused = client.post("/api/reports", json=request)
        assert refused.status_code == 429, refused.text
        assert refused.json()["code"] == "CRM-1601"

        # Лимит личный: чужая очередь другому сотруднику не мешает.
        self._kam(client)
        assert client.post("/api/reports", json=request).status_code == 201

    def test_list_is_filtered_by_template_code(self, client, monkeypatch) -> None:
        self._stub_s3(monkeypatch)
        self._ensure_templates(client, "sla_compliance", "loss_reasons")
        self._kam(client)
        for code in ("sla_compliance", "loss_reasons", "sla_compliance"):
            created = client.post("/api/reports", json={"template_code": code, "format": "xlsx"})
            assert created.status_code == 201, created.text

        everything = client.get("/api/reports").json()["items"]
        assert {job["template_code"] for job in everything} == {"sla_compliance", "loss_reasons"}

        filtered = client.get("/api/reports", params={"template_code": "loss_reasons"}).json()
        assert [job["template_code"] for job in filtered["items"]] == ["loss_reasons"]


class TestTemplateTexts:
    """Тексты, которые видит пользователь, не ссылаются на внутренние документы."""

    def test_seed_descriptions_have_no_spec_references(self) -> None:
        from app.modules.reporting.seed import _DEFAULT_TEMPLATES

        for code, _name, description, *_rest in _DEFAULT_TEMPLATES:
            for marker in ("раздел", "new_spec", "§"):
                assert marker not in description, (code, description)


class TestStuckDealsTruncation:
    """B1-6: то же усечение по `limit`, что и `deal_register` (`TestTruncation`,
    `tests/test_reporting_register.py`) — здесь для `stuck_deals`, у которого раньше не было ни
    усечения, ни `keys`/`kinds`. Настоящая Postgres обязательна (своя мини-фабрика: сделки с
    просроченным SLA, никакого общего с `deal_register` мира не нужно)."""

    pytestmark = pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
    )

    async def _make_breached_deals(self, count: int) -> uuid.UUID:
        """Возвращает `owner_id` — тесты строят `Principal` с ролью KAM и этим же
        `user_id`, чтобы скоуп (`deal_scope_clause`) ограничил выдачу только созданными здесь
        сделками. ADMIN без фильтра увидел бы ЛЮБЫЕ просроченные сделки во всей (общей для
        сессии) тестовой БД, включая оставшиеся от других тестов — `total_rows`/`truncated`
        стали бы случайными числами, а не детерминированной проверкой этого теста."""
        from app.core.db import session_scope
        from app.modules.catalog.models import Organization
        from app.modules.identity.models import User
        from app.modules.workflow.models import Workflow, WorkflowStatus

        token = uuid.uuid4().hex[:10]
        async with session_scope() as session:
            owner = User(
                keycloak_id=str(uuid.uuid4()),
                email=f"kam-stuck-{token}@rt-it-school.ru",
                full_name="Застряловa З. З.",
                role="KAM",
                status="active",
                consent_version="1.0",
            )
            session.add(owner)
            workflow = Workflow(code=f"wf-stuck-{token}", name="Воронка просрочки", deal_type="b2b")
            session.add(workflow)
            await session.flush()
            status = WorkflowStatus(
                workflow_id=workflow.id,
                code="negotiation",
                name="Переговоры",
                type="intermediate",
                sort_order=0,
            )
            organization = Organization(
                name=f"ВУЗ просрочки {token}", org_type="university", owner_id=owner.id
            )
            session.add_all([status, organization])
            await session.flush()

            now = dt.datetime.now(dt.UTC)
            for index in range(count):
                session.add(
                    Deal(
                        number=f"D-stuck-{token}-{index}",
                        title=f"Просроченная сделка {index}",
                        deal_type="b2b",
                        workflow_id=workflow.id,
                        status_id=status.id,
                        organization_id=organization.id,
                        owner_id=owner.id,
                        sla_due_at=now - dt.timedelta(days=index + 1),
                        sla_state="breached",
                    )
                )
            await session.flush()
            return owner.id

    def _owner_principal(self, owner_id: uuid.UUID):
        from app.core.security import Principal

        return Principal(
            user_id=owner_id,
            keycloak_id="k",
            role="KAM",
            status="active",
            email=None,
            full_name="x",
            team_id=None,
            manager_id=None,
            perm_epoch=1,
            session_id=None,
            consent_version="1.0",
            must_change_password=False,
            claims=None,  # type: ignore[arg-type]
        )

    def _build(self, client, owner_id: uuid.UUID, params: dict):
        from app.core.db import session_scope
        from app.modules.reporting.builders import build_stuck_deals

        async def _run() -> ReportDataset:
            async with session_scope() as session:
                return await build_stuck_deals(session, self._owner_principal(owner_id), params)

        return run(client, _run)

    def test_limit_below_the_total_marks_truncated(self, client) -> None:
        owner_id = run(client, self._make_breached_deals, 3)
        dataset = self._build(client, owner_id, {"limit": 1})
        assert len(dataset.rows) == 1
        assert dataset.truncated is True
        assert dataset.total_rows == 3
        assert dataset.note is not None and "отчёт усечён по параметру limit" in dataset.note
        assert dataset.keys == [
            "number",
            "title",
            "owner",
            "status",
            "organization",
            "days_overdue",
        ]
        assert dataset.kinds == ["text", "text", "text", "text", "text", "days"]

    def test_limit_above_the_total_skips_the_estimate(self, client) -> None:
        owner_id = run(client, self._make_breached_deals, 2)
        dataset = self._build(client, owner_id, {"limit": 5000})
        assert len(dataset.rows) == 2
        assert dataset.truncated is False
        assert dataset.total_rows is None
