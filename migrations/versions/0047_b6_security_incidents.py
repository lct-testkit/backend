"""B6-12: реестр инцидентов безопасности — сроки РКН 24/72 ч (152-ФЗ ст. 21 ч. 3.1)

Таблица `security_incidents` даёт журналу `security_events` (B6-2, B6-3, B6-5, B6-11 —
источники событий) действие: кандидат заводится автоматически, человек подтверждает (от
этого момента считаются сроки), система генерирует тексты уведомлений РКН и считает
дедлайны. `security_incident_seq` — та же схема, что `deal_number_seq` (0004): номер
`INC-{год}-{NNNN}` строится в Python (`IncidentService._next_number`), секвенция только
источник монотонного счётчика.

Revision ID: 0047_b6_security_incidents
Revises: 0046_b6_user_invite_purpose
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0047_b6_security_incidents"
down_revision: str | None = "0046_b6_user_invite_purpose"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "crm_app"


def upgrade() -> None:
    op.execute("CREATE SEQUENCE IF NOT EXISTS security_incident_seq")
    # 0002_identity_sprint выдал USAGE/SELECT только тем секвенциям, что уже существовали на
    # момент той миграции (ALTER DEFAULT PRIVILEGES распространяется на TABLES, не SEQUENCES) —
    # этой секвенции нужен собственный GRANT, иначе nextval() упадёт для роли приложения.
    op.execute(f"GRANT USAGE, SELECT ON SEQUENCE security_incident_seq TO {APP_ROLE}")

    op.create_table(
        "security_incidents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("number", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default=sa.text("'candidate'")
        ),
        sa.Column(
            "severity", sa.String(length=16), nullable=False, server_default=sa.text("'warning'")
        ),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("event_ids", postgresql.JSONB(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("data_categories", postgresql.JSONB(), nullable=True),
        sa.Column("subjects_estimate", sa.Integer(), nullable=True),
        sa.Column("causes", sa.Text(), nullable=True),
        sa.Column("harm", sa.Text(), nullable=True),
        sa.Column("measures", sa.Text(), nullable=True),
        sa.Column("responsible_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("rkn_24_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rkn_72_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rkn_24_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rkn_24_reference", sa.String(length=255), nullable=True),
        sa.Column("rkn_72_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rkn_72_reference", sa.String(length=255), nullable=True),
        sa.Column("subjects_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("investigation_summary", sa.Text(), nullable=True),
        sa.Column("deadline_alerts_sent", postgresql.JSONB(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["responsible_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('candidate','open','rkn_24_sent','rkn_72_sent','closed','dismissed')",
            name="security_incidents_status_valid",
        ),
        sa.CheckConstraint("source IN ('event','manual')", name="security_incidents_source_valid"),
        sa.CheckConstraint(
            "severity IN ('info','warning','critical')",
            name="security_incidents_severity_valid",
        ),
    )
    op.create_index(
        "ix_security_incidents_status_rkn24", "security_incidents", ["status", "rkn_24_due_at"]
    )
    op.create_index("uq_security_incidents_number", "security_incidents", ["number"], unique=True)
    # TimestampMixin.created_at объявлен с index=True — тот же индекс, что у остальных таблиц
    # на этом миксине (audit_log, data_erasure_requests и т.п.).
    op.create_index("ix_security_incidents_created_at", "security_incidents", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_security_incidents_created_at", table_name="security_incidents")
    op.drop_index("uq_security_incidents_number", table_name="security_incidents")
    op.drop_index("ix_security_incidents_status_rkn24", table_name="security_incidents")
    op.drop_table("security_incidents")
    op.execute("DROP SEQUENCE IF EXISTS security_incident_seq")
