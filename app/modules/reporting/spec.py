"""Единое описание вида отчёта (B1-2): `ReportSpec` собирает в одном месте то, что раньше было
размазано по параллельным словарям модуля `reporting` — `REPORT_BUILDERS`/`REPORT_ESTIMATORS`
(`reporting.builders`), `CHART_KINDS` (`reporting.rendering`), `REPORT_DATA_CATEGORIES`
(`reporting.service`). Та же проблема, которую решает `ReportSpec`, видна в задаче B1-2: опечатка
в имени параметра (`organisation_ids` вместо `organization_ids`) молча давала отчёт без фильтра —
строгая проверка (`validate_params`) ниже отклоняет любой параметр, которого нет в явном описании
вида отчёта, ещё до создания `report_jobs`.

Зависимость направлена в одну сторону: `spec.py` импортирует из `builders.py` (функции builder'ов,
`ColumnDef`, каталог колонок реестра сделок), а не наоборот — старые `REPORT_BUILDERS`/
`REPORT_ESTIMATORS` остаются определены там же, где сами функции (это и позволяет тестам/сервису
продолжать импортировать их оттуда без изменений, как и предполагает план спринта), а
`REPORT_SPECS` здесь просто на них ссылается. `CHART_KINDS` (`reporting.rendering`) и
`REPORT_DATA_CATEGORIES` (`reporting.service`) стали производными от `REPORT_SPECS` — см.
`derived_chart_kinds`/`derived_data_categories` ниже.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.errors import FieldError, ValidationError
from app.modules.reporting.builders import (
    FILE_ONLY_REPORTS,
    LEARNING_PROGRESS_COLUMNS,
    LICENSE_REGISTER_COLUMNS,
    LMS_USERS_UPLOAD,
    REGISTER_COLUMNS,
    REPORT_BUILDERS,
    REPORT_ESTIMATORS,
    ColumnDef,
    ReportBuilder,
    ReportEstimator,
)

#: П1 (rtk_requiriments.md разд. 4, ФТ.1/ФТ.4): шесть общих ключей фильтра, которые понимают
#: большинство builder'ов. `ReportSpec.common_filters` сужает этот набор для видов, которым
#: часть ключей структурно не подходит (`lms_users_upload` — свой `product_id`/`stream_number`,
#: не список продуктов; `license_register`, B1-11 — без `product_ids`/`direction_ids`).
COMMON_FILTER_KEYS: frozenset[str] = frozenset(
    {"date_from", "date_to", "organization_ids", "direction_ids", "product_ids", "owner_ids"}
)
#: Ключи, которые понимает любой вид отчёта с каталогом колонок/датой — не входят в
#: `common_filters` (это не фильтр по сущности), проверяются отдельно в `validate_params`.
_STRUCTURAL_KEYS: frozenset[str] = frozenset({"limit", "columns", "date_field"})


@dataclass(frozen=True, slots=True)
class ReportSpec:
    """Описание одного вида отчёта (`report_templates.query_def.kind`).

    `columns` — пусто означает «выбор колонок не поддерживается» (агрегаты: воронка, SLA,
    динамика, регионы, причины — B1-2, п.3: графики и дашборды опираются на фиксированный
    состав). `date_fields` — пусто означает «параметр `date_field` недопустим вовсе»
    (`monthly_dynamics` — у него уже две серии по двум датам одновременно, B1-3); иначе первое
    значение — умолчание. `params` — ключи `params` сверх общих (`COMMON_FILTER_KEYS` одной из
    `common_filters`, `limit`, `columns`, `date_field`)."""

    code: str
    builder: ReportBuilder
    estimator: ReportEstimator | None = None
    columns: tuple[ColumnDef, ...] = ()
    params: frozenset[str] = field(default_factory=frozenset)
    common_filters: frozenset[str] = COMMON_FILTER_KEYS
    date_fields: tuple[str, ...] = ("created_at",)
    chart: str | None = None
    data_categories: tuple[str, ...] = ()

    @property
    def default_date_field(self) -> str | None:
        return self.date_fields[0] if self.date_fields else None

    @property
    def default_columns(self) -> list[str]:
        return [c.key for c in self.columns if c.default]


REPORT_SPECS: dict[str, ReportSpec] = {
    "deal_funnel": ReportSpec(
        code="deal_funnel",
        builder=REPORT_BUILDERS["deal_funnel"],
        params=frozenset({"workflow_id", "deal_type"}),
        date_fields=("created_at", "closed_at", "updated_at"),
        chart="funnel",
    ),
    "kam_summary": ReportSpec(
        code="kam_summary",
        builder=REPORT_BUILDERS["kam_summary"],
        date_fields=("created_at", "closed_at", "updated_at"),
        data_categories=("ФИО сотрудников (КАМ)",),
    ),
    "region_summary": ReportSpec(
        code="region_summary",
        builder=REPORT_BUILDERS["region_summary"],
        date_fields=("created_at", "closed_at", "updated_at"),
    ),
    "loss_reasons": ReportSpec(
        code="loss_reasons",
        builder=REPORT_BUILDERS["loss_reasons"],
        # Жёстко закреплён в самом builder'е (`date_column=Deal.closed_at`) — «причины отказов за
        # период» читается как «отказы, случившиеся в периоде», не «заведённые в периоде».
        date_fields=("closed_at",),
    ),
    "sla_compliance": ReportSpec(
        code="sla_compliance",
        builder=REPORT_BUILDERS["sla_compliance"],
        date_fields=("created_at", "closed_at", "updated_at"),
    ),
    "monthly_dynamics": ReportSpec(
        code="monthly_dynamics",
        builder=REPORT_BUILDERS["monthly_dynamics"],
        params=frozenset({"months"}),
        date_fields=(),  # B1-3: `date_field` запрещён — две серии (создано/закрыто) сразу.
        chart="monthly",
    ),
    "stuck_deals": ReportSpec(
        code="stuck_deals",
        builder=REPORT_BUILDERS["stuck_deals"],
        estimator=REPORT_ESTIMATORS["stuck_deals"],
        date_fields=("created_at", "closed_at", "updated_at"),
        data_categories=("ФИО сотрудников (владелец сделки)",),
    ),
    "deal_register": ReportSpec(
        code="deal_register",
        builder=REPORT_BUILDERS["deal_register"],
        estimator=REPORT_ESTIMATORS["deal_register"],
        columns=REGISTER_COLUMNS,
        date_fields=("created_at", "closed_at", "updated_at"),
        # B1-12 (заодно с B1-2): раньше `deal_register` не было в каталоге категорий ПДн
        # аудита и уходило в "не классифицировано — считать содержащим ПДн" — реестр несёт
        # только ФИО ответственных сотрудников и названия вузов/сделок, не ПДн учащихся.
        data_categories=(
            "ФИО сотрудников (ответственные по сделкам)",
            "наименования вузов и сделок",
        ),
    ),
    "learning_progress": ReportSpec(
        code="learning_progress",
        builder=REPORT_BUILDERS["learning_progress"],
        estimator=REPORT_ESTIMATORS["learning_progress"],
        columns=LEARNING_PROGRESS_COLUMNS,
        # `limit` — структурный ключ (`_STRUCTURAL_KEYS` этого модуля), не нужен в `params`
        # явно — тот же приём, что у `deal_register` чуть выше.
        # B1-10: период по умолчанию — активность учащегося, не заведение сделки (у заглушки
        # было только `created_at`, которого даже не существовало бы в выборе пользователя).
        date_fields=("last_activity_at", "enrolled_at", "completed_at"),
        data_categories=("ФИО учащихся (инициалы)", "прогресс обучения"),
    ),
    LMS_USERS_UPLOAD: ReportSpec(
        code=LMS_USERS_UPLOAD,
        builder=REPORT_BUILDERS[LMS_USERS_UPLOAD],
        params=frozenset({"product_id", "stream_number", "status_codes"}),
        # Свой формат периода (всегда по дате создания сделки) и нет списков
        # организаций/направлений/продуктов/ответственных — только `date_from`/`date_to`.
        common_filters=frozenset({"date_from", "date_to"}),
        date_fields=("created_at",),
        data_categories=(
            "ФИО, телефон и email учащихся",
            "ПДн учащихся (СНИЛС, паспорт, дата рождения, адрес регистрации, диплом)",
        ),
    ),
    "product_demand": ReportSpec(
        code="product_demand",
        builder=REPORT_BUILDERS["product_demand"],
        # Нет оценщика — та же причина, что у остальных агрегатов этого модуля (kam_summary,
        # region_summary, …): группировка по каталогу продуктов/направлений структурно мала.
        params=frozenset({"level", "top", "sort_by"}),
        date_fields=("created_at", "closed_at", "updated_at"),
        # B1-8 (диаграмма "demand") не сделан в этом черновике — `chart` остаётся `None`, отчёт
        # отдаёт только табличные форматы, пока график не зарегистрирован.
        data_categories=(),
    ),
    "license_register": ReportSpec(
        code="license_register",
        builder=REPORT_BUILDERS["license_register"],
        estimator=REPORT_ESTIMATORS["license_register"],
        columns=LICENSE_REGISTER_COLUMNS,
        params=frozenset({"vendors", "transfer_statuses", "valid_year", "expired"}),
        # Нет продуктов/направлений/ответственных — лицензия привязана к вузу, не к сделке
        # (`vendors`/`transfer_statuses`/`valid_year`/`expired` — свои параметры выше, не из
        # общего набора). `organization_ids` остаётся, потому что реестр и правда по вузам.
        common_filters=frozenset({"date_from", "date_to", "organization_ids"}),
        date_fields=("license_signed_at", "created_at"),
        # B1-8 (диаграмма "license_status") не сделан в этом черновике.
        data_categories=(
            "ФИО менеджеров и ответственных от вуза (маскируются без права contact:reveal)",
        ),
    ),
}

assert set(REPORT_SPECS) == set(  # noqa: S101 — инвариант модуля, не runtime-проверка ввода
    REPORT_BUILDERS
), "REPORT_SPECS должен описывать каждый builder"
assert LMS_USERS_UPLOAD in FILE_ONLY_REPORTS  # noqa: S101 — почему у него нет columns/`/data`


def derived_chart_kinds() -> frozenset[str]:
    """`reporting.rendering.CHART_KINDS` — виды отчёта, у которых определён график (B1-8)."""
    return frozenset(code for code, spec in REPORT_SPECS.items() if spec.chart)


def derived_data_categories() -> dict[str, list[str]]:
    """`reporting.service.REPORT_DATA_CATEGORIES` — категории ПДн для аудита `REPORT_EXPORTED`
    (B1-12). Пустой список — осознанное «без ПДн», отсутствие кода в словаре (не бывает: см.
    assert выше) — защитное «не классифицировано, считать содержащим ПДн»."""
    return {code: list(spec.data_categories) for code, spec in REPORT_SPECS.items()}


def get_spec(code: str) -> ReportSpec | None:
    return REPORT_SPECS.get(code)


def validate_params(spec: ReportSpec, params: dict[str, Any]) -> None:
    """B1-2: вызывается в `ReportJobService.create` после слияния `payload.params` с
    `template.default_params`, до создания записи `report_jobs` — невалидный запрос не оставляет
    осиротевшего задания (вся транзакция откатывается обработчиком ошибок FastAPI).

    Неизвестный ключ, ключ с префиксом `_` (служебный — пишет только сид), `columns` у вида без
    каталога колонок/с неизвестным или повторным ключом, `date_field` вне `spec.date_fields` (в
    т.ч. когда он вовсе не поддерживается) — все эти случаи собираются в ОДИН 422 со списком
    всех найденных проблем, а не падают на первой."""
    errors: list[FieldError] = []
    known_keys = spec.params | spec.common_filters | _STRUCTURAL_KEYS
    for key in params:
        if key.startswith("_"):
            errors.append(FieldError(field=key, reason="служебный параметр недопустим"))
            continue
        if key not in known_keys:
            errors.append(FieldError(field=key, reason="неизвестный параметр"))

    columns_raw = params.get("columns")
    if columns_raw not in (None, []):
        if not spec.columns:
            errors.append(
                FieldError(
                    field="columns",
                    reason="выбор колонок не поддерживается видом отчёта",
                )
            )
        elif not isinstance(columns_raw, list):
            errors.append(FieldError(field="columns", reason="ожидается массив строк"))
        else:
            valid_keys = {c.key for c in spec.columns}
            seen: set[str] = set()
            for raw_key in columns_raw:
                if raw_key not in valid_keys:
                    errors.append(
                        FieldError(
                            field="columns",
                            reason=(
                                f"неизвестный ключ: {raw_key}; допустимо: "
                                f"{', '.join(sorted(valid_keys))}"
                            ),
                        )
                    )
                elif raw_key in seen:
                    errors.append(FieldError(field="columns", reason=f"повтор ключа: {raw_key}"))
                seen.add(raw_key)

    date_field_raw = params.get("date_field")
    if date_field_raw not in (None, ""):
        if not spec.date_fields:
            errors.append(
                FieldError(
                    field="date_field",
                    reason="параметр date_field не поддерживается этим видом отчёта",
                )
            )
        elif date_field_raw not in spec.date_fields:
            errors.append(
                FieldError(
                    field="date_field",
                    reason=f"допустимые значения: {', '.join(spec.date_fields)}",
                )
            )

    if errors:
        raise ValidationError("Некорректные параметры отчёта", errors)
