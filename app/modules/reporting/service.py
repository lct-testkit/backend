"""Сервисный слой отчётности и дашбордов (new_spec §4.13, §7.9).

`ReportJobService.create` реализует буквально раздел 4.13: «Синхронно отдаём
только "лёгкие" отчёты (< 1 000 строк, < 2 с). Тяжёлые: `POST /reports` →
`report_job` в очередь → поллинг статуса → готовый файл в S3». Решение
принимается по `REPORT_ESTIMATORS` (см. `reporting.builders`) — если для
вида отчёта нет оценщика, он структурно лёгкий (агрегат на несколько
десятков строк) и генерируется сразу же, в рамках текущей транзакции
запроса.

`generate()` не перехватывает исключения сама: обработка ошибок специфична
контексту вызова.
  * `create()` (sync-путь) — исключение долетает до обычного обработчика
    ошибок FastAPI (422/500), вся транзакция запроса откатывается вместе со
    строкой `report_jobs` — от невалидных параметров не остаётся осиротевшей
    `queued`-записи.
  * `reporting.tasks.sweep_report_jobs` (async-путь, воркер) — там нет
    HTTP-слоя, которому можно вернуть ошибку, поэтому сама задача ловит
    исключение и переводит job в `failed` через `mark_failed`.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import (
    AppError,
    ErrorCode,
    FieldError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
    VersionConflictError,
)
from app.core.ids import uuid7
from app.core.security import Principal
from app.core.storage import ensure_bucket, generate_presigned_get, upload_object_bytes
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.files.models import Attachment, AttachmentCategory, File, FileStatus
from app.modules.reporting.builders import REPORT_BUILDERS, REPORT_ESTIMATORS
from app.modules.reporting.models import (
    Dashboard,
    DashboardWidget,
    ReportJob,
    ReportJobStatus,
    ReportTemplate,
)
from app.modules.reporting.rendering import CONTENT_TYPES, render_report

#: Раздел 4.13: «< 1 000 строк, < 2 с» — граница между sync- и async-путём.
SYNC_ROW_THRESHOLD = 1000


class ReportTemplateService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def list_query(self, principal: Principal) -> Select[tuple[ReportTemplate]]:
        stmt = select(ReportTemplate).where(ReportTemplate.is_active.is_(True))
        if not principal.is_admin:
            # Пустой allowed_roles = доступен всем с правом report:read (тот
            # же принцип, что `workflow_transitions.allowed_roles` уже
            # использует: пустой список — не «никому», а «без ограничения»).
            stmt = stmt.where(
                or_(
                    func.cardinality(ReportTemplate.allowed_roles) == 0,
                    ReportTemplate.allowed_roles.any(principal.role),
                )
            )
        return stmt.order_by(ReportTemplate.code)

    async def get_or_404(self, code: str) -> ReportTemplate:
        template = await self._session.scalar(
            select(ReportTemplate).where(ReportTemplate.code == code)
        )
        if template is None:
            raise NotFoundError("Шаблон отчёта", code)
        return template


class ReportJobService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    async def get_or_404(self, job_id: uuid.UUID) -> ReportJob:
        job = await self._session.get(ReportJob, job_id)
        if job is None:
            raise NotFoundError("Задача отчёта", job_id)
        return job

    def ensure_read_access(self, job: ReportJob, principal: Principal) -> None:
        # Раздел 4.13: отчёт уже отражает скоуп того, кто его запросил
        # (`deal_scope_clause` применён в момент генерации) — второй,
        # более широкой роли на чтение чужого готового отчёта не нужно,
        # HEAD строит свой собственный отчёт того же вида при необходимости.
        if job.requested_by != principal.user_id and not principal.is_admin:
            raise ForbiddenError(
                "Отчёт доступен только тому, кто его запросил, либо администратору"
            )

    def list_query(
        self, principal: Principal, *, template_code: str | None = None
    ) -> Select[tuple[ReportJob]]:
        stmt = select(ReportJob)
        if not principal.is_admin:
            stmt = stmt.where(ReportJob.requested_by == principal.user_id)
        if template_code:
            stmt = stmt.where(ReportJob.template_code == template_code)
        return stmt.order_by(ReportJob.created_at.desc(), ReportJob.id.desc())

    def _ensure_role_allowed(self, template: ReportTemplate, principal: Principal) -> None:
        if principal.is_admin:
            return
        if template.allowed_roles and principal.role not in template.allowed_roles:
            raise ForbiddenError(f"Отчёт {template.code!r} недоступен вашей роли")

    async def create(self, principal: Principal, payload: Any) -> ReportJob:
        template = await ReportTemplateService(self._session).get_or_404(payload.template_code)
        if not template.is_active:
            raise NotFoundError("Шаблон отчёта", payload.template_code)
        self._ensure_role_allowed(template, principal)
        if payload.format not in template.output_formats:
            raise ValidationError(
                f"Формат {payload.format!r} недоступен для отчёта {template.code!r}",
                [
                    FieldError(
                        field="format",
                        reason=f"допустимо: {', '.join(template.output_formats)}",
                    )
                ],
            )
        kind = template.query_def.get("kind", template.code)
        if kind not in REPORT_BUILDERS:
            raise AppError(ErrorCode.VALIDATION, f"Неизвестный вид отчёта: {kind!r}")

        params = {**template.default_params, **(payload.params or {})}

        job = ReportJob(
            template_code=template.code,
            params=params,
            format=payload.format,
            requested_by=principal.user_id,
            status=ReportJobStatus.QUEUED.value,
        )
        self._session.add(job)
        await self._session.flush()

        estimator = REPORT_ESTIMATORS.get(kind)
        is_light = True
        if estimator is not None:
            row_estimate = await estimator(self._session, principal, params)
            is_light = row_estimate <= SYNC_ROW_THRESHOLD

        if is_light:
            await self.generate(job, principal)
        # Иначе остаётся `queued` — заберёт `reporting.tasks.sweep_report_jobs`.
        return job

    async def generate(self, job: ReportJob, principal: Principal) -> None:
        template = await ReportTemplateService(self._session).get_or_404(job.template_code)
        kind = template.query_def.get("kind", template.code)
        builder = REPORT_BUILDERS[kind]

        job.status = ReportJobStatus.PROCESSING.value
        await self._session.flush()

        dataset = await builder(self._session, principal, job.params)
        content = render_report(dataset, format=job.format, template_code=kind)

        settings = get_settings()
        bucket = settings.s3_bucket_reports
        await ensure_bucket(bucket)
        file_id = uuid7()
        storage_key = f"{job.id}/{file_id}.{job.format}"
        await upload_object_bytes(
            bucket=bucket,
            key=storage_key,
            body=content,
            content_type=CONTENT_TYPES[job.format],
        )

        file = File(
            id=file_id,
            storage_key=storage_key,
            bucket=bucket,
            original_filename=f"{dataset.title}.{job.format}",
            mime_type=CONTENT_TYPES[job.format],
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            status=FileStatus.READY.value,
            uploaded_by=job.requested_by,
            refcount=1,
        )
        self._session.add(file)
        # Отдельный flush перед Attachment: File/Attachment не связаны ORM
        # relationship (полиморфная связь по entity_type/entity_id) — тот же
        # найденный вживую в спринте 5 баг гонки flush-порядка, см.
        # imports.service._write_error_report.
        await self._session.flush()
        self._session.add(
            Attachment(
                file_id=file.id,
                entity_type="report_job",
                entity_id=job.id,
                category=AttachmentCategory.REPORT.value,
                uploaded_by=job.requested_by,
            )
        )

        job.status = ReportJobStatus.COMPLETED.value
        job.file_id = file.id
        job.row_count = len(dataset.rows)
        job.finished_at = dt.datetime.now(dt.UTC)
        job.expires_at = job.finished_at + dt.timedelta(days=settings.reports_retention_days)
        await self._session.flush()

        await self._audit.record(
            AuditAction.REPORT_EXPORTED,
            entity_type="report_job",
            entity_id=job.id,
            changes={
                "template_code": {"old": None, "new": job.template_code},
                "format": {"old": None, "new": job.format},
                "row_count": {"old": None, "new": job.row_count},
            },
        )

    async def mark_failed(self, job: ReportJob, error: str) -> None:
        job.status = ReportJobStatus.FAILED.value
        job.error = error[:2000]
        job.finished_at = dt.datetime.now(dt.UTC)
        await self._session.flush()
        await self._audit.record(
            AuditAction.REPORT_FAILED,
            entity_type="report_job",
            entity_id=job.id,
            changes={"error": {"old": None, "new": error[:500]}},
        )

    async def download(self, job: ReportJob, principal: Principal) -> tuple[str, dt.datetime]:
        self.ensure_read_access(job, principal)
        if job.status != ReportJobStatus.COMPLETED.value or job.file_id is None:
            raise AppError(ErrorCode.VALIDATION, "Отчёт ещё не готов")
        file = await self._session.get(File, job.file_id)
        if file is None or file.status != FileStatus.READY.value:
            raise AppError(ErrorCode.VALIDATION, "Файл отчёта недоступен (истёк срок хранения)")

        settings = get_settings()
        ttl_seconds = settings.reports_link_ttl_minutes * 60
        url = await generate_presigned_get(
            bucket=file.bucket,
            key=file.storage_key,
            expires_seconds=ttl_seconds,
            filename=file.original_filename,
        )
        expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=ttl_seconds)
        return url, expires_at


# =============================================================================
# Дашборды
# =============================================================================


class DashboardService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    def list_query(self, principal: Principal) -> Select[tuple[Dashboard]]:
        stmt = select(Dashboard)
        if not principal.is_admin:
            stmt = stmt.where(
                or_(Dashboard.owner_id == principal.user_id, Dashboard.is_shared.is_(True))
            )
        return stmt.order_by(Dashboard.created_at.desc(), Dashboard.id.desc())

    async def get_or_404(self, dashboard_id: uuid.UUID) -> Dashboard:
        dashboard = await self._session.get(Dashboard, dashboard_id)
        if dashboard is None:
            raise NotFoundError("Дашборд", dashboard_id)
        return dashboard

    def ensure_read_access(self, dashboard: Dashboard, principal: Principal) -> None:
        if (
            dashboard.owner_id != principal.user_id
            and not dashboard.is_shared
            and not principal.is_admin
        ):
            raise ForbiddenError("Дашборд недоступен")

    def ensure_write_access(self, dashboard: Dashboard, principal: Principal) -> None:
        if dashboard.owner_id != principal.user_id and not principal.is_admin:
            raise ForbiddenError("Изменять дашборд может только владелец или администратор")

    async def create(self, principal: Principal, payload: Any) -> Dashboard:
        dashboard = Dashboard(
            name=payload.name,
            owner_id=principal.user_id,
            is_shared=payload.is_shared,
            layout=payload.layout or {},
        )
        self._session.add(dashboard)
        await self._session.flush()
        await self._audit.record(
            AuditAction.DASHBOARD_CREATED,
            entity_type="dashboard",
            entity_id=dashboard.id,
            changes={"name": {"old": None, "new": dashboard.name}},
        )
        return dashboard

    async def update(
        self, dashboard: Dashboard, payload: Any, *, expected_version: int
    ) -> Dashboard:
        if dashboard.version != expected_version:
            raise VersionConflictError(dashboard.version, {"name": dashboard.name})
        data = payload.model_dump(exclude_unset=True)
        changes: dict[str, dict[str, Any]] = {}
        for key, value in data.items():
            old = getattr(dashboard, key)
            if old == value:
                continue
            changes[key] = {"old": old, "new": value}
            setattr(dashboard, key, value)
        if not changes:
            return dashboard
        dashboard.version += 1
        await self._session.flush()
        await self._audit.record(
            AuditAction.DASHBOARD_UPDATED,
            entity_type="dashboard",
            entity_id=dashboard.id,
            changes=changes,
        )
        return dashboard

    async def delete(self, dashboard: Dashboard) -> None:
        dashboard_id = dashboard.id
        await self._session.delete(dashboard)
        await self._session.flush()
        await self._audit.record(
            AuditAction.DASHBOARD_DELETED, entity_type="dashboard", entity_id=dashboard_id
        )


class DashboardWidgetService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    async def get_or_404(self, widget_id: uuid.UUID) -> DashboardWidget:
        widget = await self._session.get(DashboardWidget, widget_id)
        if widget is None:
            raise NotFoundError("Виджет дашборда", widget_id)
        return widget

    def list_query(self, dashboard_id: uuid.UUID) -> Select[tuple[DashboardWidget]]:
        return (
            select(DashboardWidget)
            .where(DashboardWidget.dashboard_id == dashboard_id)
            .order_by(DashboardWidget.created_at)
        )

    async def add(self, dashboard: Dashboard, payload: Any) -> DashboardWidget:
        widget = DashboardWidget(
            dashboard_id=dashboard.id,
            widget_type=payload.widget_type,
            config=payload.config,
            position=payload.position or {},
        )
        self._session.add(widget)
        await self._session.flush()
        await self._audit.record(
            AuditAction.DASHBOARD_WIDGET_ADDED,
            entity_type="dashboard",
            entity_id=dashboard.id,
            changes={
                "widget_id": {"old": None, "new": str(widget.id)},
                "widget_type": {"old": None, "new": widget.widget_type},
            },
        )
        return widget

    async def update(self, widget: DashboardWidget, payload: Any) -> DashboardWidget:
        data = payload.model_dump(exclude_unset=True)
        changes: dict[str, dict[str, Any]] = {}
        for key, value in data.items():
            old = getattr(widget, key)
            if old == value:
                continue
            changes[key] = {"old": old, "new": value}
            setattr(widget, key, value)
        if not changes:
            return widget
        await self._session.flush()
        await self._audit.record(
            AuditAction.DASHBOARD_WIDGET_UPDATED,
            entity_type="dashboard",
            entity_id=widget.dashboard_id,
            changes={"widget_id": {"old": None, "new": str(widget.id)}, **changes},
        )
        return widget

    async def remove(self, widget: DashboardWidget) -> None:
        dashboard_id = widget.dashboard_id
        widget_id = widget.id
        await self._session.delete(widget)
        await self._session.flush()
        await self._audit.record(
            AuditAction.DASHBOARD_WIDGET_REMOVED,
            entity_type="dashboard",
            entity_id=dashboard_id,
            changes={"widget_id": {"old": str(widget_id), "new": None}},
        )
