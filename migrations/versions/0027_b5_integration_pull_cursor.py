"""B5-2: забор лидов с сайта по API — канал входящих, включатель и курсор забора

Функц.5 ТЗ требует «забирать по API информацию из веб-сайта». До этой задачи
единственным входом заявок с сайта был вебхук (`POST /api/v1/integrations/
cms/leads`); регулярного опроса не было вовсе. Три новые колонки
`integration_sources` описывают забор (включён ли, как часто, что попросили
забрать руками — `pull_request`, и чем закончился последний прогон —
`last_run`), а `inbound_messages.channel` отличает, как заявка попала в
журнал: `webhook` (как раньше, значение по умолчанию для старых строк),
`pull` (эта задача) или `manual` (B5-5, кнопка «Забрать сейчас»).

Revision ID: 0027_b5_integration_pull_cursor
Revises: 0026_b8_report_jobs_started_at
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0027_b5_integration_pull_cursor"
down_revision: str | None = "0026_b8_report_jobs_started_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "inbound_messages",
        sa.Column("channel", sa.String(16), nullable=False, server_default="webhook"),
    )
    op.create_check_constraint(
        "inbound_messages_channel_valid",
        "inbound_messages",
        "channel IN ('webhook','pull','manual')",
    )
    op.add_column(
        "integration_sources",
        sa.Column("pull_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "integration_sources",
        sa.Column(
            "pull_interval_seconds", sa.Integer(), nullable=False, server_default=sa.text("300")
        ),
    )
    op.create_check_constraint(
        "integration_sources_pull_interval_valid",
        "integration_sources",
        "pull_interval_seconds BETWEEN 60 AND 86400",
    )
    # Запрос ручного запуска (B5-5: {requested_at, requested_by, updated_since}) и итог
    # последнего прогона (B5-5: {trigger, started_at, finished_at, fetched, created, ...}).
    op.add_column(
        "integration_sources",
        sa.Column("pull_request", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "integration_sources",
        sa.Column("last_run", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("integration_sources", "last_run")
    op.drop_column("integration_sources", "pull_request")
    op.drop_constraint(
        "integration_sources_pull_interval_valid", "integration_sources", type_="check"
    )
    op.drop_column("integration_sources", "pull_interval_seconds")
    op.drop_column("integration_sources", "pull_enabled")
    op.drop_constraint("inbound_messages_channel_valid", "inbound_messages", type_="check")
    op.drop_column("inbound_messages", "channel")
