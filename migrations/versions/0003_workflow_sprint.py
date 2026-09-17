"""Спринт 2: конструктор воронок

Создаёт таблицы раздела 5.4: workflows, workflow_statuses,
workflow_transitions, sla_rules, status_mapping_jobs.

Расширение относительно спецификации: `workflows.published_graph` —
снимок графа на момент публикации (см. docstring `app/modules/workflow/models.py`).
Без снимка правка черновика немедленно меняла бы правила для сделок, уже
идущих по воронке.

Revision ID: 0003_workflow_sprint
Revises: 0002_identity_sprint
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_workflow_sprint"
down_revision: str | None = "0002_identity_sprint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _create_workflows()
    _create_workflow_statuses()
    _create_workflow_transitions()
    _create_sla_rules()
    _create_status_mapping_jobs()


def downgrade() -> None:
    op.drop_table("status_mapping_jobs")
    op.drop_table("sla_rules")
    op.drop_table("workflow_transitions")
    op.drop_table("workflow_statuses")
    op.drop_table("workflows")


def _create_workflows() -> None:
    op.create_table(
        "workflows",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("deal_type", sa.String(8), nullable=False),
        sa.Column("state", sa.String(16), server_default="draft", nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("graph_hash", sa.String(64), nullable=True),
        sa.Column("published_graph", postgresql.JSONB(), nullable=True),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_workflows"),
        sa.UniqueConstraint("code", name="uq_workflows_code"),
        sa.CheckConstraint("deal_type IN ('b2b','b2c')", name="ck_workflows_workflows_deal_type_valid"),
        sa.CheckConstraint(
            "state IN ('draft','published','archived')", name="ck_workflows_workflows_state_valid"
        ),
        sa.CheckConstraint(
            "state <> 'published' OR (published_graph IS NOT NULL AND graph_hash IS NOT NULL)",
            name="ck_workflows_workflows_published_has_graph",
        ),
    )
    op.create_index("ix_workflows_created_at", "workflows", ["created_at"])
    op.create_index("ix_workflows_deal_type_state", "workflows", ["deal_type", "state"])
    op.create_index(
        "uq_workflows_default_per_deal_type",
        "workflows",
        ["deal_type"],
        unique=True,
        postgresql_where=sa.text("is_default AND state = 'published'"),
    )


def _create_workflow_statuses() -> None:
    op.create_table(
        "workflow_statuses",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workflow_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("type", sa.String(16), server_default="intermediate", nullable=False),
        sa.Column("color", sa.String(16), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "required_fields", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("is_archived", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("replaced_by_status_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_workflow_statuses"),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflows.id"],
            name="fk_workflow_statuses_workflow_id_workflows",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["replaced_by_status_id"],
            ["workflow_statuses.id"],
            name="fk_workflow_statuses_replaced_by_status_id_workflow_statuses",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "workflow_id", "code", name="uq_workflow_statuses_workflow_id_code"
        ),
        sa.CheckConstraint(
            "type IN ('initial','intermediate','won','lost','parked')",
            name="ck_workflow_statuses_workflow_statuses_type_valid",
        ),
        sa.CheckConstraint(
            "is_archived = false OR archived_at IS NOT NULL",
            name="ck_workflow_statuses_workflow_statuses_archived_at_present",
        ),
        sa.CheckConstraint(
            "replaced_by_status_id <> id",
            name="ck_workflow_statuses_workflow_statuses_replacement_self",
        ),
    )
    op.create_index("ix_workflow_statuses_created_at", "workflow_statuses", ["created_at"])
    op.create_index("ix_workflow_statuses_workflow_id", "workflow_statuses", ["workflow_id"])
    op.create_index(
        "ix_workflow_statuses_workflow_sort", "workflow_statuses", ["workflow_id", "sort_order"]
    )
    op.create_index(
        "uq_workflow_statuses_initial",
        "workflow_statuses",
        ["workflow_id"],
        unique=True,
        postgresql_where=sa.text("type = 'initial'"),
    )


def _create_workflow_transitions() -> None:
    op.create_table(
        "workflow_transitions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workflow_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_status_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("to_status_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "allowed_roles", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column(
            "conditions", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("actions", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("requires_comment", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_workflow_transitions"),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflows.id"],
            name="fk_workflow_transitions_workflow_id_workflows",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["from_status_id"],
            ["workflow_statuses.id"],
            name="fk_workflow_transitions_from_status_id_workflow_statuses",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["to_status_id"],
            ["workflow_statuses.id"],
            name="fk_workflow_transitions_to_status_id_workflow_statuses",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "workflow_id",
            "from_status_id",
            "to_status_id",
            name="uq_workflow_transitions_workflow_id_from_status_id_to_status_id",
        ),
        sa.CheckConstraint(
            "from_status_id <> to_status_id",
            name="ck_workflow_transitions_workflow_transitions_no_self_loop",
        ),
    )
    op.create_index("ix_workflow_transitions_created_at", "workflow_transitions", ["created_at"])
    op.create_index(
        "ix_workflow_transitions_workflow_id", "workflow_transitions", ["workflow_id"]
    )
    op.create_index("ix_workflow_transitions_from", "workflow_transitions", ["from_status_id"])


def _create_sla_rules() -> None:
    op.create_table(
        "sla_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workflow_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("max_duration", postgresql.INTERVAL(), nullable=False),
        sa.Column("warn_threshold_pct", sa.SmallInteger(), server_default=sa.text("80"), nullable=False),
        sa.Column("escalate_to_role", sa.String(32), nullable=True),
        sa.Column("escalate_to_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "channels",
            postgresql.JSONB(),
            server_default=sa.text("'[\"in_app\"]'::jsonb"),
            nullable=False,
        ),
        sa.Column("count_business_days", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_sla_rules"),
        sa.ForeignKeyConstraint(
            ["workflow_id"], ["workflows.id"], name="fk_sla_rules_workflow_id_workflows", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["status_id"],
            ["workflow_statuses.id"],
            name="fk_sla_rules_status_id_workflow_statuses",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "warn_threshold_pct BETWEEN 1 AND 100",
            name="ck_sla_rules_sla_rules_warn_threshold_valid",
        ),
        sa.CheckConstraint(
            "max_duration > interval '0'", name="ck_sla_rules_sla_rules_duration_positive"
        ),
    )
    op.create_index("ix_sla_rules_created_at", "sla_rules", ["created_at"])
    op.create_index("ix_sla_rules_workflow_id", "sla_rules", ["workflow_id"])
    op.create_index(
        "uq_sla_rules_status_active",
        "sla_rules",
        ["status_id"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )


def _create_status_mapping_jobs() -> None:
    op.create_table(
        "status_mapping_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workflow_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_status_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "mapping_rules", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("affected_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("processed_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("failed_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("initiated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("report", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_status_mapping_jobs"),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflows.id"],
            name="fk_status_mapping_jobs_workflow_id_workflows",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["from_status_id"],
            ["workflow_statuses.id"],
            name="fk_status_mapping_jobs_from_status_id_workflow_statuses",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','completed','failed')",
            name="ck_status_mapping_jobs_status_mapping_jobs_status_valid",
        ),
    )
    op.create_index("ix_status_mapping_jobs_created_at", "status_mapping_jobs", ["created_at"])
    op.create_index(
        "ix_status_mapping_jobs_workflow_status",
        "status_mapping_jobs",
        ["workflow_id", "from_status_id"],
    )
    op.create_index(
        "uq_status_mapping_jobs_active",
        "status_mapping_jobs",
        ["from_status_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending','running')"),
    )
