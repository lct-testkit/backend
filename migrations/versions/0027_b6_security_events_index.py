"""B6-2: частичный индекс под выборку «только важное» в security_events

`GET /admin/security-events?min_severity=warning` — основной путь просмотра журнала
безопасности (администратор не читает `info` построчно). Частичный индекс по
`(severity, created_at)` с условием `severity <> 'info'` держит этот запрос быстрым, не раздувая
индекс записями `info`, которых большинство.

Revision ID: 0027_b6_security_events_index
Revises: 0026_b2_attachment_status_link
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Имя короче привычной схемы "0027_b6_<описание>" намеренно: alembic_version.version_num —
# varchar(32), и полное описание задачи в самом id ("…_severity_index") не поместилось бы.
revision: str = "0027_b6_security_events_index"
down_revision: str | None = "0026_b2_attachment_status_link"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_security_events_severity_created",
        "security_events",
        ["severity", "created_at"],
        postgresql_where=sa.text("severity <> 'info'"),
    )


def downgrade() -> None:
    op.drop_index("ix_security_events_severity_created", table_name="security_events")
