"""B1-2: `reporting.spec.REPORT_SPECS`/`validate_params` — реестр отчётов, строгие параметры и
каталог колонок `deal_register`. Без БД: `REPORT_SPECS` и `validate_params` — чистые структуры и
функция над словарём `params`, сам запрос строит `builders._deal_register_query` (проверяется в
`tests/test_reporting_register.py`, настоящая Postgres обязательна).
"""

from __future__ import annotations

import pytest

from app.core.errors import ValidationError
from app.modules.reporting.builders import REGISTER_COLUMNS, REPORT_BUILDERS
from app.modules.reporting.seed import _DEFAULT_TEMPLATES
from app.modules.reporting.spec import REPORT_SPECS, validate_params


class TestSpecRegistryMatchesBuilders:
    def test_every_builder_has_a_spec_and_vice_versa(self) -> None:
        assert set(REPORT_SPECS) == set(REPORT_BUILDERS)

    def test_every_seeded_template_kind_has_a_spec(self) -> None:
        for code, _name, _desc, kind, *_rest in _DEFAULT_TEMPLATES:
            assert kind in REPORT_SPECS, f"{code!r}: нет ReportSpec для kind={kind!r}"


class TestDealRegisterColumnCatalog:
    def test_default_columns_are_the_fixed_ten_in_order(self) -> None:
        # Контракт с дашбордом (widgets.ts:18-24): заголовки не переименовываются и не
        # переставляются, этот список и есть источник истины для фронтенда до появления
        # выбора колонок — ссылка на него закреплена тестом, чтобы случайная правка каталога
        # была замечена здесь, а не на демонстрации.
        default_titles = [c.title for c in REGISTER_COLUMNS if c.default]
        assert default_titles == [
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

    def test_sixteen_columns_total_six_of_them_not_default(self) -> None:
        assert len(REGISTER_COLUMNS) == 16
        assert sum(1 for c in REGISTER_COLUMNS if not c.default) == 6

    def test_new_columns_are_appended_after_the_original_ten(self) -> None:
        keys = [c.key for c in REGISTER_COLUMNS]
        assert keys[:10] == [
            "organization",
            "direction",
            "product",
            "status",
            "owner",
            "number",
            "title",
            "amount",
            "created_at",
            "closed_at",
        ]
        assert set(keys[10:]) == {
            "region",
            "quantity",
            "stream_number",
            "product_total",
            "students_planned",
            "priority",
        }

    def test_deal_register_spec_exposes_the_same_catalog(self) -> None:
        assert REPORT_SPECS["deal_register"].columns == REGISTER_COLUMNS


class TestValidateParamsUnknownKeys:
    def test_unknown_key_is_rejected(self) -> None:
        spec = REPORT_SPECS["deal_register"]
        with pytest.raises(ValidationError):
            validate_params(spec, {"organisation_ids": ["x"]})  # опечатка — не organization_ids

    def test_underscore_prefixed_key_is_rejected(self) -> None:
        spec = REPORT_SPECS["deal_register"]
        with pytest.raises(ValidationError):
            validate_params(spec, {"_seed": "demo"})

    def test_known_common_filters_pass(self) -> None:
        spec = REPORT_SPECS["deal_register"]
        validate_params(
            spec,
            {
                "date_from": "2026-01-01",
                "date_to": "2026-01-31",
                "organization_ids": ["x"],
                "direction_ids": ["y"],
                "product_ids": ["z"],
                "owner_ids": ["w"],
                "limit": 100,
            },
        )

    def test_view_specific_param_is_known_for_its_own_spec_only(self) -> None:
        funnel = REPORT_SPECS["deal_funnel"]
        validate_params(funnel, {"workflow_id": "x", "deal_type": "b2b"})
        with pytest.raises(ValidationError):
            validate_params(REPORT_SPECS["sla_compliance"], {"workflow_id": "x"})

    def test_lms_users_upload_does_not_know_entity_id_lists(self) -> None:
        # Свой формат фильтра (product_id/stream_number), не список product_ids.
        spec = REPORT_SPECS["lms_users_upload"]
        with pytest.raises(ValidationError):
            validate_params(spec, {"product_ids": ["x"]})
        validate_params(spec, {"product_id": "x", "stream_number": 2, "status_codes": ["a"]})


class TestValidateParamsColumns:
    def test_columns_not_supported_by_aggregate_view(self) -> None:
        spec = REPORT_SPECS["sla_compliance"]
        with pytest.raises(ValidationError):
            validate_params(spec, {"columns": ["status"]})

    def test_empty_columns_list_is_allowed_everywhere(self) -> None:
        validate_params(REPORT_SPECS["sla_compliance"], {"columns": []})
        validate_params(REPORT_SPECS["deal_register"], {"columns": []})

    def test_unknown_column_key_is_rejected_with_allowed_list_in_reason(self) -> None:
        spec = REPORT_SPECS["deal_register"]
        with pytest.raises(ValidationError) as exc:
            validate_params(spec, {"columns": ["vuz"]})
        reasons = " ".join(e.reason for e in exc.value.errors or [])
        assert "vuz" in reasons
        assert "organization" in reasons

    def test_duplicate_column_key_is_rejected(self) -> None:
        spec = REPORT_SPECS["deal_register"]
        with pytest.raises(ValidationError):
            validate_params(spec, {"columns": ["organization", "organization"]})

    def test_columns_must_be_a_list(self) -> None:
        spec = REPORT_SPECS["deal_register"]
        with pytest.raises(ValidationError):
            validate_params(spec, {"columns": "organization"})

    def test_valid_subset_passes(self) -> None:
        spec = REPORT_SPECS["deal_register"]
        validate_params(spec, {"columns": ["organization", "product", "owner", "status"]})


class TestValidateParamsDateField:
    def test_date_field_forbidden_for_monthly_dynamics(self) -> None:
        spec = REPORT_SPECS["monthly_dynamics"]
        with pytest.raises(ValidationError):
            validate_params(spec, {"date_field": "created_at"})

    def test_date_field_outside_catalog_is_rejected(self) -> None:
        spec = REPORT_SPECS["deal_register"]
        with pytest.raises(ValidationError) as exc:
            validate_params(spec, {"date_field": "foo"})
        reasons = " ".join(e.reason for e in exc.value.errors or [])
        assert "created_at" in reasons

    def test_date_field_within_catalog_passes(self) -> None:
        spec = REPORT_SPECS["deal_register"]
        validate_params(spec, {"date_field": "closed_at"})

    def test_loss_reasons_only_allows_closed_at(self) -> None:
        spec = REPORT_SPECS["loss_reasons"]
        assert spec.date_fields == ("closed_at",)
        with pytest.raises(ValidationError):
            validate_params(spec, {"date_field": "created_at"})


class TestMultipleErrorsCollectedTogether:
    def test_several_problems_reported_in_one_422(self) -> None:
        spec = REPORT_SPECS["deal_register"]
        with pytest.raises(ValidationError) as exc:
            validate_params(spec, {"vuz": "x", "columns": ["vuz", "vuz"], "date_field": "foo"})
        fields = {e.field for e in exc.value.errors or []}
        assert fields == {"vuz", "columns", "date_field"}


#: Заголовки, которые читает `CHART_PRESETS` фронтенда (документ спринта B1, со ссылкой на
#: `widgets.ts:18-24`). Начиная с B1-3 каждый builder трогает БД даже для заголовков (нужен
#: пояс пользователя — `_load_filters`), поэтому сама проверка живёт в `needs_db`-тесте
#: (`tests/test_reporting_period.py::TestChartPresetHeadersSurvive`), не здесь.
CHART_PRESET_HEADERS: dict[str, set[str]] = {
    "deal_funnel": {"Статус", "Сейчас в статусе", "Всего прошло"},
    "kam_summary": {"КАМ", "В работе", "Выиграно", "Проиграно", "Сумма выигранных"},
    "region_summary": {"Регион", "Сделок", "Сумма выигранных"},
    "loss_reasons": {"Причина"},
    "sla_compliance": {"Состояние"},
    "monthly_dynamics": {"Месяц", "Создано"},
}
