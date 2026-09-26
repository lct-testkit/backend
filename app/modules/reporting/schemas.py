"""Схемы отчётности и дашбордов (new_spec §4.13, §7.9)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

FormatLiteral = Literal["xlsx", "pdf", "png"]
JobStatusLiteral = Literal["queued", "processing", "completed", "failed"]
WidgetTypeLiteral = Literal["report_table", "report_chart", "stat_tile"]


# =============================================================================
# Шаблоны отчётов (GET /api/report-templates)
# =============================================================================


class ReportTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    description: str | None
    allowed_roles: list[str]
    default_params: dict[str, Any]
    output_formats: list[FormatLiteral]
    is_active: bool


class ReportTemplateListResponse(BaseModel):
    items: list[ReportTemplateOut]


# =============================================================================
# Задачи отчётов (POST/GET /api/reports, .../download)
# =============================================================================


class ReportJobCreateRequest(BaseModel):
    template_code: NonEmptyStr = Field(max_length=64)
    format: FormatLiteral
    params: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Параметры вида отчёта, накладываются поверх `default_params` шаблона. "
            "Специфичные для вида (например `months`/`limit`/`workflow_id`) — "
            "см. `GET /report-templates`. Общие для П1 (rtk_requiriments.md разд. 4, "
            "ФТ.1/ФТ.4), поддержаны почти всеми видами, кроме `learning_progress`: "
            "`date_from`/`date_to` (YYYY-MM-DD, период включительно), "
            "`organization_ids`/`direction_ids`/`product_ids`/`owner_ids` "
            "(списки UUID). Выгрузка для LMS (`lms_users_upload`, только xlsx) принимает "
            "`product_id`, `stream_number`, `status_codes` (по умолчанию `payment_contract` и "
            "`lms_enrollment`) и `date_from`/`date_to` по дате создания сделки."
        ),
    )


class ReportJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    template_code: str
    format: FormatLiteral
    status: JobStatusLiteral
    progress_pct: int
    row_count: int | None
    error: str | None
    requested_by: uuid.UUID
    created_at: dt.datetime
    finished_at: dt.datetime | None
    expires_at: dt.datetime | None


class ReportJobListResponse(BaseModel):
    items: list[ReportJobOut]
    next_cursor: str | None = None


class ReportDownloadResponse(BaseModel):
    url: str
    expires_at: dt.datetime


class ReportDataOut(BaseModel):
    """rtk_requiriments.md разд. 6.4 («возможность формирования результирующего
    json-файла»; см. также backend-issues.md #19): тот же `ReportDataset`,
    что рендерится в xlsx/pdf/png, отданный как JSON — для дашбордов и
    клиентских диаграмм. Строится заново по параметрам задания при каждом
    запросе (свежие данные), не создаёт файл в S3 и не пишет `REPORT_EXPORTED`
    — это чтение, не выгрузка (`reporting.service.ReportJobService.get_data`)."""

    title: str
    columns: list[str]
    rows: list[list[Any]]
    note: str | None = None
    generated_at: dt.datetime
    row_count: int


# =============================================================================
# Дашборды (/api/dashboards)
# =============================================================================


class DashboardCreateRequest(BaseModel):
    name: NonEmptyStr = Field(max_length=255)
    is_shared: bool = False
    layout: dict[str, Any] = Field(default_factory=dict)


class DashboardUpdateRequest(BaseModel):
    name: NonEmptyStr | None = Field(default=None, max_length=255)
    is_shared: bool | None = None
    layout: dict[str, Any] | None = None


class DashboardOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    owner_id: uuid.UUID
    is_shared: bool
    layout: dict[str, Any]
    version: int
    created_at: dt.datetime
    updated_at: dt.datetime


class DashboardListResponse(BaseModel):
    items: list[DashboardOut]
    next_cursor: str | None = None


class DashboardWidgetCreateRequest(BaseModel):
    widget_type: WidgetTypeLiteral
    config: dict[str, Any]
    position: dict[str, Any] = Field(default_factory=dict)


class DashboardWidgetUpdateRequest(BaseModel):
    widget_type: WidgetTypeLiteral | None = None
    config: dict[str, Any] | None = None
    position: dict[str, Any] | None = None


class DashboardWidgetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    dashboard_id: uuid.UUID
    widget_type: WidgetTypeLiteral
    config: dict[str, Any]
    position: dict[str, Any]
    created_at: dt.datetime


class DashboardWidgetListResponse(BaseModel):
    items: list[DashboardWidgetOut]
