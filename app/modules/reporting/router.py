"""Роутер отчётности и дашбордов (new_spec §4.13, §7.9, §8)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, status

from app.core.deps import DbSession, IfMatch, Pagination, require_permission
from app.core.errors import NotFoundError
from app.core.pagination import Page, keyset_before
from app.core.permissions import Permission
from app.core.security import Principal
from app.modules.reporting.models import Dashboard, DashboardWidget, ReportJob
from app.modules.reporting.schemas import (
    DashboardCreateRequest,
    DashboardListResponse,
    DashboardOut,
    DashboardUpdateRequest,
    DashboardWidgetCreateRequest,
    DashboardWidgetListResponse,
    DashboardWidgetOut,
    DashboardWidgetUpdateRequest,
    ReportDataOut,
    ReportDownloadResponse,
    ReportJobCreateRequest,
    ReportJobListResponse,
    ReportJobOut,
    ReportTemplateListResponse,
    ReportTemplateOut,
)
from app.modules.reporting.service import (
    DashboardService,
    DashboardWidgetService,
    ReportJobService,
    ReportTemplateService,
)

reports_router = APIRouter(prefix="/reports", tags=["reporting"])
report_templates_router = APIRouter(prefix="/report-templates", tags=["reporting"])
dashboards_router = APIRouter(prefix="/dashboards", tags=["reporting"])

# Раздел 5: «Отчёты по себе» — KAM/HEAD/ADMIN; AUDITOR/INTEGRATION не имеют
# report:read/report:create (см. `core.permissions`) — скоуп внутри отчёта
# и так решает, что «по себе» значит для конкретной роли.
ReportRead = Annotated[Principal, Depends(require_permission(Permission.REPORT_READ))]
ReportCreate = Annotated[Principal, Depends(require_permission(Permission.REPORT_CREATE))]


# =============================================================================
# Шаблоны отчётов
# =============================================================================


@report_templates_router.get(
    "", summary="Доступные виды отчётов", response_model=ReportTemplateListResponse
)
async def list_report_templates(
    session: DbSession, principal: ReportRead
) -> ReportTemplateListResponse:
    stmt = ReportTemplateService(session).list_query(principal)
    rows = (await session.execute(stmt)).scalars().all()
    return ReportTemplateListResponse(items=[ReportTemplateOut.model_validate(r) for r in rows])


# =============================================================================
# Задачи отчётов
# =============================================================================


@reports_router.post(
    "",
    summary="Запустить отчёт",
    response_model=ReportJobOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_report(
    payload: ReportJobCreateRequest, session: DbSession, principal: ReportCreate
) -> ReportJobOut:
    job = await ReportJobService(session).create(principal, payload)
    return ReportJobOut.model_validate(job)


@reports_router.get("", summary="Мои отчёты", response_model=ReportJobListResponse)
async def list_reports(
    session: DbSession, page: Pagination, principal: ReportRead
) -> ReportJobListResponse:
    service = ReportJobService(session)
    stmt = service.list_query(principal)
    cursor = page.decoded_cursor
    if cursor:
        stmt = stmt.where(keyset_before(ReportJob.created_at, ReportJob.id, cursor))
    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())
    built = Page.build(rows, limit=page.limit, serializer=ReportJobOut.model_validate)
    return ReportJobListResponse(items=built.items, next_cursor=built.next_cursor)


@reports_router.get("/{report_id}", summary="Статус отчёта", response_model=ReportJobOut)
async def get_report(
    session: DbSession, principal: ReportRead, report_id: Annotated[uuid.UUID, Path()]
) -> ReportJobOut:
    service = ReportJobService(session)
    job = await service.get_or_404(report_id)
    service.ensure_read_access(job, principal)
    return ReportJobOut.model_validate(job)


@reports_router.get(
    "/{report_id}/data",
    summary="Данные отчёта в JSON",
    description=(
        "Тот же набор данных, что рендерится в xlsx/pdf/png, в виде "
        "`{columns, rows}` — для виджетов дашбордов и клиентских диаграмм. "
        "Строится заново по параметрам задания при каждом запросе (свежие "
        "данные), не создаёт файл в S3 и не пишет событие аудита "
        "`REPORT_EXPORTED` — это чтение, не выгрузка. Права — как у "
        "`GET /api/reports/{report_id}`."
    ),
    response_model=ReportDataOut,
)
async def get_report_data(
    session: DbSession, principal: ReportRead, report_id: Annotated[uuid.UUID, Path()]
) -> ReportDataOut:
    service = ReportJobService(session)
    job = await service.get_or_404(report_id)
    dataset = await service.get_data(job, principal)
    return ReportDataOut(
        title=dataset.title,
        columns=dataset.columns,
        rows=dataset.rows,
        note=dataset.note,
        generated_at=dataset.generated_at,
        row_count=len(dataset.rows),
    )


@reports_router.get(
    "/{report_id}/download", summary="Ссылка на файл отчёта", response_model=ReportDownloadResponse
)
async def download_report(
    session: DbSession, principal: ReportRead, report_id: Annotated[uuid.UUID, Path()]
) -> ReportDownloadResponse:
    service = ReportJobService(session)
    job = await service.get_or_404(report_id)
    url, expires_at = await service.download(job, principal)
    return ReportDownloadResponse(url=url, expires_at=expires_at)


# =============================================================================
# Дашборды
# =============================================================================


@dashboards_router.post(
    "", summary="Создать дашборд", response_model=DashboardOut, status_code=status.HTTP_201_CREATED
)
async def create_dashboard(
    payload: DashboardCreateRequest, session: DbSession, principal: ReportCreate
) -> DashboardOut:
    dashboard = await DashboardService(session).create(principal, payload)
    return DashboardOut.model_validate(dashboard)


@dashboards_router.get("", summary="Мои дашборды", response_model=DashboardListResponse)
async def list_dashboards(
    session: DbSession, page: Pagination, principal: ReportRead
) -> DashboardListResponse:
    service = DashboardService(session)
    stmt = service.list_query(principal)
    cursor = page.decoded_cursor
    if cursor:
        stmt = stmt.where(keyset_before(Dashboard.created_at, Dashboard.id, cursor))
    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())
    built = Page.build(rows, limit=page.limit, serializer=DashboardOut.model_validate)
    return DashboardListResponse(items=built.items, next_cursor=built.next_cursor)


@dashboards_router.get("/{dashboard_id}", summary="Дашборд", response_model=DashboardOut)
async def get_dashboard(
    session: DbSession, principal: ReportRead, dashboard_id: Annotated[uuid.UUID, Path()]
) -> DashboardOut:
    service = DashboardService(session)
    dashboard = await service.get_or_404(dashboard_id)
    service.ensure_read_access(dashboard, principal)
    return DashboardOut.model_validate(dashboard)


@dashboards_router.patch("/{dashboard_id}", summary="Обновить дашборд", response_model=DashboardOut)
async def update_dashboard(
    payload: DashboardUpdateRequest,
    session: DbSession,
    principal: ReportCreate,
    if_match: IfMatch,
    dashboard_id: Annotated[uuid.UUID, Path()],
) -> DashboardOut:
    service = DashboardService(session)
    dashboard = await service.get_or_404(dashboard_id)
    service.ensure_write_access(dashboard, principal)
    dashboard = await service.update(dashboard, payload, expected_version=if_match)
    return DashboardOut.model_validate(dashboard)


@dashboards_router.delete(
    "/{dashboard_id}", summary="Удалить дашборд", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_dashboard(
    session: DbSession, principal: ReportCreate, dashboard_id: Annotated[uuid.UUID, Path()]
) -> None:
    service = DashboardService(session)
    dashboard = await service.get_or_404(dashboard_id)
    service.ensure_write_access(dashboard, principal)
    await service.delete(dashboard)


# --- Виджеты -----------------------------------------------------------------


@dashboards_router.post(
    "/{dashboard_id}/widgets",
    summary="Добавить виджет",
    response_model=DashboardWidgetOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_widget(
    payload: DashboardWidgetCreateRequest,
    session: DbSession,
    principal: ReportCreate,
    dashboard_id: Annotated[uuid.UUID, Path()],
) -> DashboardWidgetOut:
    dashboard_service = DashboardService(session)
    dashboard = await dashboard_service.get_or_404(dashboard_id)
    dashboard_service.ensure_write_access(dashboard, principal)
    widget = await DashboardWidgetService(session).add(dashboard, payload)
    return DashboardWidgetOut.model_validate(widget)


@dashboards_router.get(
    "/{dashboard_id}/widgets",
    summary="Виджеты дашборда",
    response_model=DashboardWidgetListResponse,
)
async def list_widgets(
    session: DbSession, principal: ReportRead, dashboard_id: Annotated[uuid.UUID, Path()]
) -> DashboardWidgetListResponse:
    dashboard_service = DashboardService(session)
    dashboard = await dashboard_service.get_or_404(dashboard_id)
    dashboard_service.ensure_read_access(dashboard, principal)
    stmt = DashboardWidgetService(session).list_query(dashboard_id)
    rows = (await session.execute(stmt)).scalars().all()
    return DashboardWidgetListResponse(items=[DashboardWidgetOut.model_validate(w) for w in rows])


def _ensure_widget_belongs(widget: DashboardWidget, dashboard: Dashboard) -> None:
    if widget.dashboard_id != dashboard.id:
        raise NotFoundError("Виджет дашборда", widget.id)


@dashboards_router.patch(
    "/{dashboard_id}/widgets/{widget_id}",
    summary="Обновить виджет",
    response_model=DashboardWidgetOut,
)
async def update_widget(
    payload: DashboardWidgetUpdateRequest,
    session: DbSession,
    principal: ReportCreate,
    dashboard_id: Annotated[uuid.UUID, Path()],
    widget_id: Annotated[uuid.UUID, Path()],
) -> DashboardWidgetOut:
    dashboard_service = DashboardService(session)
    dashboard = await dashboard_service.get_or_404(dashboard_id)
    dashboard_service.ensure_write_access(dashboard, principal)
    widget_service = DashboardWidgetService(session)
    widget = await widget_service.get_or_404(widget_id)
    _ensure_widget_belongs(widget, dashboard)
    widget = await widget_service.update(widget, payload)
    return DashboardWidgetOut.model_validate(widget)


@dashboards_router.delete(
    "/{dashboard_id}/widgets/{widget_id}",
    summary="Удалить виджет",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_widget(
    session: DbSession,
    principal: ReportCreate,
    dashboard_id: Annotated[uuid.UUID, Path()],
    widget_id: Annotated[uuid.UUID, Path()],
) -> None:
    dashboard_service = DashboardService(session)
    dashboard = await dashboard_service.get_or_404(dashboard_id)
    dashboard_service.ensure_write_access(dashboard, principal)
    widget_service = DashboardWidgetService(session)
    widget = await widget_service.get_or_404(widget_id)
    _ensure_widget_belongs(widget, dashboard)
    await widget_service.remove(widget)
