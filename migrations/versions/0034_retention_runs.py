"""B6-6: retention_runs — журнал запусков очистки по сроку хранения (152-ФЗ ст. 5 ч. 7)

Область этой задачи в этой итерации сознательно сужена (см. отчёт): реализованы категории
без затрагивания неизменяемых/архивных данных — security_events, OTP-коды подписи,
уведомления, незавершённые загрузки, физически удалённые файлы. Архивация audit_log в S3 с
DETACH PARTITION и SECURITY DEFINER функциями (самая рискованная и трудоёмкая часть разбора
задачи — работа с неизменяемым журналом аудита) НЕ реализована в этой миграции: таблица
`audit_archive_manifest` и функции `drop_audit_log_partition`/`register_audit_archive`
остаются открытым пунктом.

Revision ID: 0034_retention_runs
Revises: 0033_signature_on_signed
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0034_retention_runs"
down_revision: str | None = "0033_signature_on_signed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "retention_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("deleted_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("triggered_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_retention_runs"),
        sa.CheckConstraint(
            "status IN ('completed','failed')", name="ck_retention_runs_retention_runs_status_valid"
        ),
    )
    op.create_index("ix_retention_runs_category", "retention_runs", ["category"])
    op.create_index("ix_retention_runs_created_at", "retention_runs", ["created_at"])


def downgrade() -> None:
    op.drop_table("retention_runs")
