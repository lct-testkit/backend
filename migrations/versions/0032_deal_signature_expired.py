"""B7 (мелкая правка): deal_status_history.reason получает 'signature_expired'

Истечение срока подписания делило причину с отказом (`signature_rejected`) — в журнале
сделки это разные события (`_apply_deal_signature_outcome` теперь принимает `reason`
явно). Имя constraint в БД ищется динамически (`pg_constraint`), а не берётся из истории
миграций: naming convention Alembic оборачивает голое имя из `CheckConstraint` ровно один
раз при `create_all()`, но при ручной миграции 0004 итоговое имя в БД получило двойную
обёртку с хэш-суффиксом (тот же сюрприз, что уже описан в 0015_organization_licenses.py).

Revision ID: 0032_deal_signature_expired
Revises: 0031_signature_reminders
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0032_deal_signature_expired"
down_revision: str | None = "0031_signature_reminders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CLEAN_NAME = "deal_status_history_reason_valid"
_NEW_CHECK = (
    "reason IN ('manual','workflow_migration','integration','auto',"
    "'signature_rejected','signature_expired')"
)
_OLD_CHECK = "reason IN ('manual','workflow_migration','integration','auto','signature_rejected')"


def _find_check_constraint(table: str, column_substring: str) -> str:
    """Имя CHECK-constraint'а на `table`, чьё условие упоминает `column_substring`, по факту
    в БД — не по тому, что "должно быть" по истории миграций."""
    bind = op.get_bind()
    return bind.execute(
        sa.text(
            "SELECT con.conname FROM pg_constraint con "
            "JOIN pg_class rel ON rel.oid = con.conrelid "
            "WHERE rel.relname = :table AND con.contype = 'c' "
            "AND pg_get_constraintdef(con.oid) LIKE :pattern"
        ),
        {"table": table, "pattern": f"%{column_substring}%"},
    ).scalar_one()


def upgrade() -> None:
    old_name = _find_check_constraint("deal_status_history", "signature_rejected")
    op.execute(f'ALTER TABLE deal_status_history DROP CONSTRAINT "{old_name}"')
    op.create_check_constraint(_CLEAN_NAME, "deal_status_history", _NEW_CHECK)


def downgrade() -> None:
    old_name = _find_check_constraint("deal_status_history", "signature_rejected")
    op.execute(f'ALTER TABLE deal_status_history DROP CONSTRAINT "{old_name}"')
    op.create_check_constraint(_CLEAN_NAME, "deal_status_history", _OLD_CHECK)
