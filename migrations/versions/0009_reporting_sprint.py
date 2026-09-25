"""Спринт 8: отчётность и дашборды (new_spec §4.13, §7.9)

Создаёт четыре таблицы (`report_templates`, `report_jobs`, `dashboards`,
`dashboard_widgets`) и одно материализованное представление
(`mv_deal_status_summary`) — раздел 3.4/4.13: «материализованные
представления PostgreSQL с REFRESH CONCURRENTLY по расписанию» для быстрого
(ADMIN-скоуп) пути отчёта «Воронка по статусам», обновляется
`reporting.tasks.refresh_report_materialized_views` каждые 5 минут.
`REFRESH ... CONCURRENTLY` требует уникальный индекс на представлении —
заведён по `status_id` (одна строка представления на статус воронки).

`report_jobs.file_id` — `ON DELETE SET NULL`, а не `RESTRICT` по умолчанию
для репозитория: ретеншен-задача (`reporting.tasks.expire_report_files`,
раздел 4.13 «готовые файлы автоудаляются через 7 дней») удаляет файл, а
запись о том, что отчёт когда-то строился, остаётся — тот же приём, что
`notification_deliveries.notification_id` уже использует (спринт 7).

Никакая существующая таблица не меняется: `Permission.REPORT_READ`/
`REPORT_CREATE`, `reports_max_concurrent`/`reports_link_ttl_minutes`/
`reports_retention_days` (`app/core/config.py`) и
`AttachmentCategory.REPORT` (`app/modules/files/models.py`) уже существовали
до этого спринта — резервирование конфигурации и прав вперёд, тот же приём,
что нулевые FK-колонки в предыдущих спринтах, только на этот раз про
конфигурацию, а не про колонки.

Revision ID: 0009_reporting_sprint
Revises: 0008_notification_sprint
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_reporting_sprint"
down_revision: str | None = "0008_notification_sprint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "report_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("query_def", postgresql.JSONB(), nullable=False),
        sa.Column(
            "allowed_roles",
            postgresql.ARRAY(sa.String(16)),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "default_params",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "output_formats",
            postgresql.ARRAY(sa.String(8)),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_report_templates"),
        sa.UniqueConstraint("code", name="uq_report_templates_code"),
    )
    op.create_index("ix_report_templates_created_at", "report_templates", ["created_at"])

    op.create_table(
        "report_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("template_code", sa.String(64), nullable=False),
        sa.Column(
            "params", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("format", sa.String(8), nullable=False),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("progress_pct", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_report_jobs"),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name="fk_report_jobs_requested_by_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["files.id"],
            name="fk_report_jobs_file_id_files",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint("format IN ('xlsx','pdf','png')", name="ck_report_jobs_format_valid"),
        sa.CheckConstraint(
            "status IN ('queued','processing','completed','failed')",
            name="ck_report_jobs_status_valid",
        ),
    )
    op.create_index("ix_report_jobs_template_code", "report_jobs", ["template_code"])
    op.create_index("ix_report_jobs_created_at", "report_jobs", ["created_at"])
    op.create_index(
        "ix_report_jobs_requested_by_created", "report_jobs", ["requested_by", "created_at"]
    )
    op.create_index(
        "ix_report_jobs_pending",
        "report_jobs",
        ["status"],
        postgresql_where=sa.text("status IN ('queued','processing')"),
    )

    op.create_table(
        "dashboards",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("is_shared", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "layout", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_dashboards"),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name="fk_dashboards_owner_id_users",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_dashboards_owner", "dashboards", ["owner_id"])
    op.create_index("ix_dashboards_created_at", "dashboards", ["created_at"])

    op.create_table(
        "dashboard_widgets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dashboard_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("widget_type", sa.String(24), nullable=False),
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column(
            "position", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_dashboard_widgets"),
        sa.ForeignKeyConstraint(
            ["dashboard_id"],
            ["dashboards.id"],
            name="fk_dashboard_widgets_dashboard_id_dashboards",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "widget_type IN ('report_table','report_chart','stat_tile')",
            name="ck_dashboard_widgets_widget_type_valid",
        ),
    )
    op.create_index("ix_dashboard_widgets_dashboard", "dashboard_widgets", ["dashboard_id"])
    op.create_index("ix_dashboard_widgets_created_at", "dashboard_widgets", ["created_at"])

    # Раздел 3.4/4.13 — см. докстринг модуля. Два подзапроса агрегируются
    # независимо перед join'ом на `workflow_statuses`, чтобы не считать
    # cartesian-произведение "сейчас в статусе" × "история переходов".
    op.execute(
        """
        CREATE MATERIALIZED VIEW mv_deal_status_summary AS
        SELECT
            ws.id AS status_id,
            ws.workflow_id AS workflow_id,
            COALESCE(cur.currently_in_count, 0) AS currently_in_count,
            COALESCE(hist.entered_count, 0) AS entered_count,
            hist.avg_duration_in_prev AS avg_duration_in_prev
        FROM workflow_statuses ws
        LEFT JOIN (
            SELECT status_id, count(*) AS currently_in_count
            FROM deals
            WHERE deleted_at IS NULL
            GROUP BY status_id
        ) cur ON cur.status_id = ws.id
        LEFT JOIN (
            SELECT
                to_status_id,
                count(DISTINCT deal_id) AS entered_count,
                avg(duration_in_prev) AS avg_duration_in_prev
            FROM deal_status_history
            GROUP BY to_status_id
        ) hist ON hist.to_status_id = ws.id
        """
    )
    # Уникальный индекс — обязателен для REFRESH MATERIALIZED VIEW CONCURRENTLY.
    op.execute(
        "CREATE UNIQUE INDEX ix_mv_deal_status_summary_status ON mv_deal_status_summary (status_id)"
    )


def downgrade() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS mv_deal_status_summary")
    op.drop_table("dashboard_widgets")
    op.drop_table("dashboards")
    op.drop_table("report_jobs")
    op.drop_table("report_templates")
