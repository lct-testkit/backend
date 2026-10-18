"""B4-11 (A-22): отклонённые расхождения реквизитов («Оставить как есть»)

До этой задачи баннер расхождений с ЕГРЮЛ возвращался при каждой сверке (`sweep_registry_drift`),
даже если менеджер уже раз просмотрел его и осознанно оставил своё значение — принять расхождение
(`apply-drift`) было единственным действием. `organizations.drift_dismissed` — отдельный от
`manual_overrides` словарь: если в реестре появится ДРУГОЕ значение (не то, что отклонили), баннер
обязан вернуться.

Revision ID: 0048_b4_org_drift_dismissed
Revises: 0047_b5_api_keys
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0048_b4_org_drift_dismissed"
down_revision: str | None = "0047_b5_api_keys"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column(
            "drift_dismissed",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("organizations", "drift_dismissed")
