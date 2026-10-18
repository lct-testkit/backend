"""B5-3/B5-4: настраиваемый маппинг полей и маршрутизация источника

Функц.5 ТЗ говорит о заборе данных «по утверждённым полям» и о добавлении
заявки «в существующий или новый workflow». До этой задачи оба были зашиты в
код: маппинг — словарь `lead_payload._ALIASES`, маршрут — жёсткий
`deal_type="b2c"` без выбора воронки (`cms.py:383`, BUG-15).

Обе колонки — на `integration_sources`, не отдельная таблица версий: история
изменений уже есть в журнале аудита (`AuditAction.INTEGRATION_MAPPING_UPDATED`/
`INTEGRATION_ROUTING_UPDATED`), а колонки `PATCH .../sources/{code}` не
затрагивает (`IntegrationSourceUpdateRequest` их не содержит) — значит обычный
`PATCH` не может случайно стереть маппинг/маршрут, только выделенные ручки.

Revision ID: 0038_b5_mapping_routing
Revises: 0037_b5_integration_pull_cursor
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0038_b5_mapping_routing"
down_revision: str | None = "0037_b5_integration_pull_cursor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "integration_sources",
        sa.Column("field_mapping", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "integration_sources",
        sa.Column("routing", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("integration_sources", "routing")
    op.drop_column("integration_sources", "field_mapping")
