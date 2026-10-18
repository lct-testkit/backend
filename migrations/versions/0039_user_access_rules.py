"""B4-5: ограничения видимости Администратора — модель данных

ТЗ, «Администратор п.1» и Функц.11: «разграничение пользователей по доступу
к данным». Таблица `user_access_rules` хранит по одной строке на вуз или
направление, которые Администратор разрешил (`allow`) или запретил (`deny`)
конкретному KAM/HEAD; колонка `users.access_restricted` денормализует «есть
ли у пользователя хоть одна строка», чтобы 99% пользователей без ограничений
не платили ни одним лишним запросом в скоуп-клаузах (B4-6).

Новая таблица подпадает под `ALTER DEFAULT PRIVILEGES ... GRANT SELECT,
INSERT, UPDATE, DELETE ON TABLES TO crm_app` из `0002_identity_sprint`
автоматически — права объекта, созданного ролью-владельцем миграций, не
нужно выставлять заново.

Revision ID: 0039_user_access_rules
Revises: 0038_b5_mapping_routing
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0039_user_access_rules"
down_revision: str | None = "0038_b5_mapping_routing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_access_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("effect", sa.String(length=8), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("direction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["direction_id"], ["directions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("effect IN ('allow','deny')", name="user_access_rules_effect_valid"),
        sa.CheckConstraint(
            "(organization_id IS NOT NULL)::int + (direction_id IS NOT NULL)::int = 1",
            name="user_access_rules_one_target",
        ),
    )
    op.create_index(
        "uq_user_access_rules_org",
        "user_access_rules",
        ["user_id", "organization_id"],
        unique=True,
        postgresql_where=sa.text("organization_id IS NOT NULL"),
    )
    op.create_index(
        "uq_user_access_rules_dir",
        "user_access_rules",
        ["user_id", "direction_id"],
        unique=True,
        postgresql_where=sa.text("direction_id IS NOT NULL"),
    )
    op.create_index("ix_user_access_rules_user", "user_access_rules", ["user_id"])

    op.add_column(
        "users",
        sa.Column(
            "access_restricted", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "access_restricted")
    op.drop_index("ix_user_access_rules_user", table_name="user_access_rules")
    op.drop_index("uq_user_access_rules_dir", table_name="user_access_rules")
    op.drop_index("uq_user_access_rules_org", table_name="user_access_rules")
    op.drop_table("user_access_rules")
