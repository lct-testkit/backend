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
не как pytest-тест — кроме `TestReportDataEndpoint` (П2) и `TestReportJobs`
(коды CRM-1601/1602/1603, `progress_pct`, фильтр `template_code`): настоящая
Postgres обязательна, тот же приём, что `tests/test_imports.py::
TestLicenseImportEndToEnd` — см. `tests/conftest.py`.
"""

from __future__ import annotations

import datetime as dt
import uuid

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


class TestLearningProgressStub:
    async def test_returns_empty_dataset_with_honest_note_not_fake_data(self) -> None:
        # Единственный builder, не трогающий сессию/принципала вообще — см.
        # докстринг модуля и `build_learning_progress` самой.
        dataset = await build_learning_progress(None, None, {})  # type: ignore[arg-type]
        assert dataset.rows == []
        assert dataset.note is not None
        assert "LMS" in dataset.note


class TestBuilderRegistry:
    def test_eight_builders_match_new_spec_4_13_literal_list_plus_the_lms_upload(self) -> None:
        # Восемь видов раздела 4.13 и девятый — выгрузка учащихся для LMS (`lms_users_upload`).
        assert len(REPORT_BUILDERS) == 9
        assert "lms_users_upload" in REPORT_BUILDERS

    def test_only_stuck_deals_has_a_real_row_estimator(self) -> None:
        # Раздел 4.13: агрегаты структурно малы (статусы/регионы/КАМы/
        # месяцы/причины отказов) и всегда идут по лёгкому пути — только
        # листинг `stuck_deals` может реально превысить порог.
        assert set(REPORT_ESTIMATORS) == {"stuck_deals"}

    def test_sync_threshold_matches_spec_literal_number(self) -> None:
        assert SYNC_ROW_THRESHOLD == 1000


class TestSeedTemplates:
    def test_nine_templates_seeded(self) -> None:
        assert len(_DEFAULT_TEMPLATES) == 9

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
