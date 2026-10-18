"""B9-9: контракт результирующего json-файла отчёта (rtk_requiriments.md §6 п.4). Пример из
B1-5 и генератор схемы — без БД; параметризация по шаблонам отчёта с форматом `json` — нужна
настоящая Postgres (`tests/conftest.py`)."""

from __future__ import annotations

import json
import uuid

import pytest
from pydantic import ValidationError

from app.modules.reporting.builders import ReportDataset
from app.modules.reporting.rendering import render_json
from app.modules.reporting.result_schema import REPORT_RESULT_SCHEMA_VERSION, ReportResultV1
from tests.conftest import TEST_DATABASE_URL, run

pytestmark_db = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _dataset(**kwargs) -> ReportDataset:
    defaults: dict = {
        "title": "Причины отказов",
        "columns": ["Причина", "Сделок"],
        "rows": [["Дорого", 5], ["Не ответили", 3]],
        "keys": None,
        "kinds": ["text", "int"],
    }
    defaults.update(kwargs)
    return ReportDataset(**defaults)


class TestReportResultV1:
    def test_the_b1_5_example_passes_validation(self) -> None:
        content = render_json(
            _dataset(),
            template_code="loss_reasons",
            job_id=uuid.uuid4(),
            params={"date_from": "2026-01-01"},
            tz_name="Europe/Moscow",
        )
        result = ReportResultV1.model_validate_json(content)
        assert result.schema_ == REPORT_RESULT_SCHEMA_VERSION
        assert result.report.code == "loss_reasons"
        assert result.totals.row_count == 2

    def test_the_schema_key_round_trips_through_the_alias(self) -> None:
        content = render_json(
            _dataset(), template_code="loss_reasons", job_id=None, params={}, tz_name="UTC"
        )
        payload = json.loads(content)
        assert payload["schema"] == "rtk-crm-report/1"
        # Модель не добавляет служебное поле `schema_` в сериализованный вид.
        assert "schema_" not in payload

    def test_missing_schema_field_is_rejected(self) -> None:
        payload = {
            "report": {
                "code": "x",
                "title": "x",
                "job_id": None,
                "generated_at": "2026-01-01T00:00:00+00:00",
                "timezone": "UTC",
            },
            "params": {},
            "columns": [],
            "rows": [],
            "totals": {"row_count": 0, "total_rows": 0, "truncated": False},
            "note": None,
        }
        with pytest.raises(ValidationError):
            ReportResultV1.model_validate(payload)

    def test_an_unknown_top_level_field_does_not_break_parsing(self) -> None:
        """Совместимость вперёд (правило версий, docstring `result_schema.py`): клиент на этой
        версии модели не падает на поле, которого она не знает."""
        content = render_json(
            _dataset(), template_code="loss_reasons", job_id=None, params={}, tz_name="UTC"
        )
        payload = json.loads(content)
        payload["future_field_from_a_later_minor_version"] = {"anything": True}
        result = ReportResultV1.model_validate(payload)
        assert result.schema_ == REPORT_RESULT_SCHEMA_VERSION

    def test_generated_schema_matches_the_committed_file(self) -> None:
        from tools.export_report_schema import TARGET, render

        assert TARGET.exists(), f"{TARGET} не найден — выполните export_report_schema.py"
        assert TARGET.read_text(encoding="utf-8") == render(), (
            "docs/schemas/rtk-crm-report-1.schema.json устарела: выполните "
            "`python tools/export_report_schema.py` и закоммитьте результат"
        )


@pytestmark_db
class TestEveryJsonTemplateProducesAValidResult:
    """Параметризация по шаблонам из `_DEFAULT_TEMPLATES`, у которых `json` среди
    `output_formats` — каждый builder вызывается с ADMIN без фильтров (агрегаты и листинги
    этого модуля не требуют заранее заведённых сделок, пустая выборка — такой же валидный
    результат) и прогоняется через `render_json` → `ReportResultV1.model_validate_json`."""

    def _principal(self):
        from app.core.security import Principal

        return Principal(
            user_id=uuid.uuid4(),
            keycloak_id="k",
            role="ADMIN",
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

    # `deal_funnel` исключён: билдер требует опубликованную воронку по умолчанию
    # (`_resolve_workflow_id`), которой на общей тестовой БД может не быть в зависимости от
    # порядка прогона — остальные десять шаблонов этого модуля работают и на пустой выборке,
    # для проверки формы файла (а не бизнес-данных, это покрывают другие тесты) достаточно их.
    @pytest.mark.parametrize(
        "template_code",
        [
            "kam_summary",
            "region_summary",
            "loss_reasons",
            "sla_compliance",
            "monthly_dynamics",
            "stuck_deals",
            "deal_register",
            "learning_progress",
            "product_demand",
            "license_register",
        ],
    )
    def test_builder_output_round_trips_through_render_json(
        self, client, template_code: str
    ) -> None:
        from app.core.db import session_scope
        from app.modules.reporting.builders import REPORT_BUILDERS
        from app.modules.reporting.spec import REPORT_SPECS

        spec = REPORT_SPECS[template_code]
        if "json" not in _output_formats_for(template_code):
            pytest.skip(f"{template_code}: json не входит в output_formats сида")

        async def _build():
            builder = REPORT_BUILDERS[spec.code]
            async with session_scope() as session:
                return await builder(session, self._principal(), {})

        dataset = run(client, _build)
        content = render_json(
            dataset, template_code=template_code, job_id=uuid.uuid4(), params={}, tz_name="UTC"
        )
        result = ReportResultV1.model_validate_json(content)
        assert result.report.code == template_code


def _output_formats_for(template_code: str) -> list[str]:
    from app.modules.reporting.seed import _DEFAULT_TEMPLATES

    for code, _name, _desc, _kind, _roles, _params, formats in _DEFAULT_TEMPLATES:
        if code == template_code:
            return formats
    return []
