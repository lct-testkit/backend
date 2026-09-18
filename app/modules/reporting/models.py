"""Модели отчётности и дашбордов (new_spec §4.13, §7.9).

До этого спринта модуль был пустым пакетом: `reporting_max_concurrent`,
`reports_link_ttl_minutes`, `reports_retention_days` в `app/core/config.py` и
`Permission.REPORT_READ`/`REPORT_CREATE` уже существовали (раздел 4/17), а
`AttachmentCategory.REPORT` (`app/modules/files/models.py`) уже был заведён —
тот же приём предварительного резервирования, что и нулевые FK-колонки в
предыдущих спринтах (`Deal.active_signature_document_id` до модуля `signing`
и т.д.), только на этот раз про конфигурацию и права, а не про колонки.

Четыре таблицы:

* `report_templates` — код отчёта + `query_def jsonb`. Раздел 4.13 не
  описывает язык запросов для отчётов (в отличие от, например, DSL guard-
  условий workflow, раздел 8), поэтому `query_def` хранит только
  `{"kind": "<code>"}` — диспетчер на фиксированный набор python-функций в
  `reporting.builders.REPORT_BUILDERS`, а не произвольно интерпретируемый
  запрос. Новый вид отчёта добавляется кодом (новый builder + семя), не
  runtime-конфигурацией — то же соотношение «код регистрирует поведение,
  JSONB хранит метаданные», что `workflow_transitions.actions` уже
  демонстрирует для DSL-действий.
* `report_jobs` — одна задача = один запуск отчёта в одном формате.
  `file_id` — `ON DELETE SET NULL`, не `RESTRICT` по умолчанию для репозитория
  (раздел 2): ретеншен-задача (`reporting.tasks.expire_report_files`,
  раздел 4.13 «готовые файлы автоудаляются через 7 дней») удаляет именно
  файл, а не запись о том, что отчёт когда-то строился — та же логика, что
  `notification_deliveries.notification_id` уже использует для похожего
  «переживает удаление родителя» случая (спринт 7).
* `dashboards`/`dashboard_widgets` — только раскладка и ссылка на отчёт
  (`config.template_code`/`config.params`), не отдельный конвейер данных:
  фронтенд получает содержимое виджета тем же `POST /api/reports`, что и
  обычный отчёт (раздел 4.13 явно не описывает отдельный API для данных
  дашборда, а сами дашборды на Svelte+uPlot — вне этого бэкенд-репозитория).
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UuidPkMixin, VersionMixin


class ReportFormat(StrEnum):
    XLSX = "xlsx"
    PDF = "pdf"
    PNG = "png"


class ReportJobStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class DashboardWidgetType(StrEnum):
    REPORT_TABLE = "report_table"
    REPORT_CHART = "report_chart"
    STAT_TILE = "stat_tile"


class ReportTemplate(UuidPkMixin, TimestampMixin, Base):
    __tablename__ = "report_templates"
    __table_args__ = (UniqueConstraint("code", name="uq_report_templates_code"),)

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    query_def: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    allowed_roles: Mapped[list[str]] = mapped_column(
        ARRAY(String(16)), nullable=False, server_default=text("'{}'")
    )
    default_params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    output_formats: Mapped[list[str]] = mapped_column(
        ARRAY(String(8)), nullable=False, server_default=text("'{}'")
    )
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))


class ReportJob(UuidPkMixin, Base):
    __tablename__ = "report_jobs"
    __table_args__ = (
        Index("ix_report_jobs_requested_by_created", "requested_by", "created_at"),
        # Раздел 4.13: конкурентность воркеров ограничена семафором (10).
        # `reporting.tasks.sweep_report_jobs` считает текущие `processing` по
        # этому же частичному индексу перед тем, как забрать новые `queued`.
        Index(
            "ix_report_jobs_pending",
            "status",
            postgresql_where=text("status IN ('queued','processing')"),
        ),
        CheckConstraint("format IN ('xlsx','pdf','png')", name="report_jobs_format_valid"),
        CheckConstraint(
            "status IN ('queued','processing','completed','failed')",
            name="report_jobs_status_valid",
        ),
    )

    template_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    format: Mapped[str] = mapped_column(String(8), nullable=False)
    requested_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="queued")
    progress_pct: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # SET NULL — см. докстринг модуля: переживает удаление файла ретеншеном.
    file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("files.id", ondelete="SET NULL"), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False, index=True
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Dashboard(UuidPkMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "dashboards"
    __table_args__ = (Index("ix_dashboards_owner", "owner_id"),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    is_shared: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    layout: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )


class DashboardWidget(UuidPkMixin, TimestampMixin, Base):
    __tablename__ = "dashboard_widgets"
    __table_args__ = (
        Index("ix_dashboard_widgets_dashboard", "dashboard_id"),
        CheckConstraint(
            "widget_type IN ('report_table','report_chart','stat_tile')",
            name="dashboard_widgets_type_valid",
        ),
    )

    dashboard_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dashboards.id", ondelete="CASCADE"), nullable=False
    )
    widget_type: Mapped[str] = mapped_column(String(24), nullable=False)
    # {"template_code": "deal_funnel", "params": {...}} — см. докстринг модуля.
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    position: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
