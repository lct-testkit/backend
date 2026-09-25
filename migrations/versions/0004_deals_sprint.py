"""Спринт 3: сделки

Создаёт таблицы раздела 5.5: deals, deal_participants, deal_status_history,
deal_events, deal_products, deal_comments, deal_comment_revisions, tasks.

`deals.organization_id`, `deals.contact_id`, `deals.loss_reason_id` и
`deal_products.product_id` — без внешнего ключа: каталог организаций,
контактов, причин отказа и продуктов появится в спринте 4 (см. docstring
`app/modules/crm/models.py`). FK на них добавит миграция того спринта.

`deal_status_history` — append-only, как `audit_log`: роль приложения
получает только `SELECT`/`INSERT` (см. `REVOKE` ниже, тот же приём, что
`0002_identity_sprint` применяет к `audit_log`).

Revision ID: 0004_deals_sprint
Revises: 0003_workflow_sprint
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_deals_sprint"
down_revision: str | None = "0003_workflow_sprint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "crm_app"


def upgrade() -> None:
    op.execute("CREATE SEQUENCE IF NOT EXISTS deal_number_seq")
    # `0002_identity_sprint` grants USAGE/SELECT на все секвенции, что
    # существовали на момент той миграции, но не задаёт ALTER DEFAULT
    # PRIVILEGES для будущих — эта секвенция создана позже и нуждается в
    # собственном GRANT, иначе `nextval()` упадёт для роли приложения.
    op.execute(f"GRANT USAGE, SELECT ON SEQUENCE deal_number_seq TO {APP_ROLE}")
    _create_deals()
    _create_deal_participants()
    _create_deal_status_history()
    _create_deal_events()
    _create_deal_products()
    _create_deal_comments()
    _create_deal_comment_revisions()
    _create_tasks()

    # История переходов — append-only: "изменил статус, но подчистил след"
    # не должно быть возможно даже для роли приложения (раздел 5.5: "история
    # не редактируется").
    op.execute(f"REVOKE UPDATE, DELETE ON deal_status_history FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON deal_status_history TO {APP_ROLE}")


def downgrade() -> None:
    op.drop_table("tasks")
    op.drop_table("deal_comment_revisions")
    op.drop_table("deal_comments")
    op.drop_table("deal_products")
    op.drop_table("deal_events")
    op.drop_table("deal_status_history")
    op.drop_table("deal_participants")
    op.drop_table("deals")
    op.execute("DROP SEQUENCE IF EXISTS deal_number_seq")


def _create_deals() -> None:
    op.create_table(
        "deals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("number", sa.String(32), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("deal_type", sa.String(8), nullable=False),
        sa.Column("workflow_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("currency", sa.String(3), server_default="RUB", nullable=False),
        sa.Column("students_planned", sa.Integer(), nullable=True),
        sa.Column("expected_close_date", sa.Date(), nullable=True),
        sa.Column(
            "status_changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("sla_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sla_state", sa.String(16), server_default="ok", nullable=False),
        sa.Column(
            "sla_paused_total",
            postgresql.INTERVAL(),
            server_default=sa.text("interval '0'"),
            nullable=False,
        ),
        sa.Column("priority", sa.String(16), server_default="normal", nullable=False),
        sa.Column("loss_reason_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "custom_fields",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("source", sa.String(32), nullable=True),
        sa.Column(
            "external_ids",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "owner_unavailable", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("signature_status", sa.String(20), server_default="none", nullable=False),
        sa.Column("active_signature_document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_deals"),
        sa.UniqueConstraint("number", name="uq_deals_number"),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflows.id"],
            name="fk_deals_workflow_id_workflows",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["status_id"],
            ["workflow_statuses.id"],
            name="fk_deals_status_id_workflow_statuses",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name="fk_deals_owner_id_users", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_deals_created_by_users", ondelete="RESTRICT"
        ),
        sa.CheckConstraint("deal_type IN ('b2b','b2c')", name="ck_deals_deals_deal_type_valid"),
        sa.CheckConstraint(
            "deal_type <> 'b2b' OR organization_id IS NOT NULL",
            name="ck_deals_deals_b2b_requires_organization",
        ),
        sa.CheckConstraint(
            "deal_type <> 'b2c' OR contact_id IS NOT NULL",
            name="ck_deals_deals_b2c_requires_contact",
        ),
        sa.CheckConstraint(
            "sla_state IN ('ok','warning','breached','paused')",
            name="ck_deals_deals_sla_state_valid",
        ),
        sa.CheckConstraint(
            "priority IN ('low','normal','high','critical')", name="ck_deals_deals_priority_valid"
        ),
        sa.CheckConstraint(
            "signature_status IN "
            "('none','pending','partially_signed','signed','rejected','expired','void')",
            name="ck_deals_deals_signature_status_valid",
        ),
    )
    op.create_index("ix_deals_created_at", "deals", ["created_at"])
    op.create_index("ix_deals_owner", "deals", ["owner_id"])
    op.create_index("ix_deals_status", "deals", ["status_id"])
    op.create_index("ix_deals_workflow", "deals", ["workflow_id"])
    op.create_index("ix_deals_organization", "deals", ["organization_id"])
    op.create_index("ix_deals_contact", "deals", ["contact_id"])
    op.create_index(
        "ix_deals_sla_due_open",
        "deals",
        ["sla_due_at"],
        postgresql_where=sa.text("closed_at IS NULL AND deleted_at IS NULL"),
    )


def _create_deal_participants() -> None:
    op.create_table(
        "deal_participants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role_in_deal", sa.String(16), nullable=False),
        sa.Column("added_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "added_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_deal_participants"),
        sa.ForeignKeyConstraint(
            ["deal_id"], ["deals.id"], name="fk_deal_participants_deal_id_deals", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_deal_participants_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["added_by"],
            ["users.id"],
            name="fk_deal_participants_added_by_users",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "deal_id", "user_id", "role_in_deal", name="uq_deal_participants_deal_user_role"
        ),
        sa.CheckConstraint(
            "role_in_deal IN ('watcher','co_owner','lawyer','methodist')",
            name="ck_deal_participants_deal_participants_role_valid",
        ),
    )
    op.create_index("ix_deal_participants_deal", "deal_participants", ["deal_id"])
    op.create_index("ix_deal_participants_user", "deal_participants", ["user_id"])


def _create_deal_status_history() -> None:
    op.create_table(
        "deal_status_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_status_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("to_status_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("changed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("transition_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reason", sa.String(24), server_default="manual", nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("duration_in_prev", postgresql.INTERVAL(), nullable=True),
        sa.Column("sla_state_at_change", sa.String(16), nullable=True),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_deal_status_history"),
        sa.ForeignKeyConstraint(
            ["deal_id"],
            ["deals.id"],
            name="fk_deal_status_history_deal_id_deals",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["from_status_id"],
            ["workflow_statuses.id"],
            name="fk_deal_status_history_from_status_id_workflow_statuses",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["to_status_id"],
            ["workflow_statuses.id"],
            name="fk_deal_status_history_to_status_id_workflow_statuses",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["changed_by"],
            ["users.id"],
            name="fk_deal_status_history_changed_by_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["transition_id"],
            ["workflow_transitions.id"],
            name="fk_deal_status_history_transition_id_workflow_transitions",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "reason IN ('manual','workflow_migration','integration','auto','signature_rejected')",
            name="ck_deal_status_history_deal_status_history_reason_valid",
        ),
    )
    op.create_index("ix_deal_status_history_changed_at", "deal_status_history", ["changed_at"])
    op.create_index(
        "ix_deal_status_history_deal_changed", "deal_status_history", ["deal_id", "changed_at"]
    )


def _create_deal_events() -> None:
    op.create_table(
        "deal_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_deal_events"),
        sa.ForeignKeyConstraint(
            ["deal_id"], ["deals.id"], name="fk_deal_events_deal_id_deals", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["users.id"], name="fk_deal_events_actor_id_users", ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "event_type IN ('CREATED','OWNER_CHANGED','FIELD_CHANGED','FILE_ADDED',"
            "'SIGNATURE_REQUESTED','SIGNATURE_SIGNED','SIGNATURE_REJECTED',"
            "'IMPORT_APPLIED','WORKFLOW_MIGRATION')",
            name="ck_deal_events_deal_events_type_valid",
        ),
    )
    op.create_index("ix_deal_events_created_at", "deal_events", ["created_at"])
    op.create_index("ix_deal_events_deal_created", "deal_events", ["deal_id", "created_at"])


def _create_deal_products() -> None:
    op.create_table(
        "deal_products",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quantity", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("price", sa.Numeric(14, 2), nullable=True),
        sa.Column("discount_pct", sa.Numeric(5, 2), server_default=sa.text("0"), nullable=False),
        sa.Column("total", sa.Numeric(14, 2), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_deal_products"),
        sa.ForeignKeyConstraint(
            ["deal_id"], ["deals.id"], name="fk_deal_products_deal_id_deals", ondelete="CASCADE"
        ),
        sa.CheckConstraint("quantity > 0", name="ck_deal_products_deal_products_quantity_positive"),
        sa.CheckConstraint(
            "discount_pct BETWEEN 0 AND 100", name="ck_deal_products_deal_products_discount_valid"
        ),
    )
    op.create_index("ix_deal_products_created_at", "deal_products", ["created_at"])
    op.create_index("ix_deal_products_deal", "deal_products", ["deal_id"])
    op.create_index("ix_deal_products_product", "deal_products", ["product_id"])


def _create_deal_comments() -> None:
    op.create_table(
        "deal_comments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("author_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("body_format", sa.String(16), server_default="plain", nullable=False),
        sa.Column(
            "mentions", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column("is_system", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_internal", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_deal_comments"),
        sa.ForeignKeyConstraint(
            ["deal_id"], ["deals.id"], name="fk_deal_comments_deal_id_deals", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["users.id"],
            name="fk_deal_comments_author_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["deal_comments.id"],
            name="fk_deal_comments_parent_id_deal_comments",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "body_format IN ('plain','markdown')",
            name="ck_deal_comments_deal_comments_format_valid",
        ),
    )
    op.create_index("ix_deal_comments_created_at", "deal_comments", ["created_at"])
    op.create_index("ix_deal_comments_deal_created", "deal_comments", ["deal_id", "created_at"])


def _create_deal_comment_revisions() -> None:
    op.create_table(
        "deal_comment_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("comment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("edited_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "edited_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_deal_comment_revisions"),
        sa.ForeignKeyConstraint(
            ["comment_id"],
            ["deal_comments.id"],
            name="fk_deal_comment_revisions_comment_id_deal_comments",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["edited_by"],
            ["users.id"],
            name="fk_deal_comment_revisions_edited_by_users",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_deal_comment_revisions_comment", "deal_comment_revisions", ["comment_id"])


def _create_tasks() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deal_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("assignee_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(16), server_default="open", nullable=False),
        sa.Column("priority", sa.String(16), server_default="normal", nullable=False),
        sa.Column("auto_created_by_transition_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_tasks"),
        sa.ForeignKeyConstraint(
            ["deal_id"], ["deals.id"], name="fk_tasks_deal_id_deals", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["assignee_id"], ["users.id"], name="fk_tasks_assignee_id_users", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name="fk_tasks_created_by_users", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["completed_by"], ["users.id"], name="fk_tasks_completed_by_users", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["auto_created_by_transition_id"],
            ["workflow_transitions.id"],
            name="fk_tasks_auto_created_by_transition_id_workflow_transitions",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('open','in_progress','done','cancelled')",
            name="ck_tasks_tasks_status_valid",
        ),
        sa.CheckConstraint(
            "priority IN ('low','normal','high','critical')", name="ck_tasks_tasks_priority_valid"
        ),
    )
    op.create_index("ix_tasks_created_at", "tasks", ["created_at"])
    op.create_index("ix_tasks_deal", "tasks", ["deal_id"])
    op.create_index("ix_tasks_assignee", "tasks", ["assignee_id"])
    op.create_index(
        "ix_tasks_deal_open",
        "tasks",
        ["deal_id"],
        postgresql_where=sa.text("status IN ('open','in_progress') AND deleted_at IS NULL"),
    )
    op.create_index(
        "ix_tasks_due_open", "tasks", ["due_at"], postgresql_where=sa.text("status = 'open'")
    )
