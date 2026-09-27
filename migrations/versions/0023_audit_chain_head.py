"""Указатель на голову цепочки аудита — синглтон-таблица вместо ORDER BY по audit_log

Перф-диагностика 27.09: `SELECT hash FROM audit_log ORDER BY created_at DESC, id DESC LIMIT 1`
под advisory-локом цепочки (`AuditService._chain_head`) планировался 15-30мс — Postgres
рассматривает constraint exclusion по всем месячным партициям `audit_log` на каждый вызов,
и это время множится на глубину очереди ожидающих лок под нагрузкой (см. V0.4.0-PLAN.md P0-4).
`audit_chain_head` — одна строка, не партиционирована: чтение/запись — доли миллисекунды
независимо от размера `audit_log` и числа партиций.

Бэкафилл — тем же запросом, что раньше делал `_chain_head()`: текущая голова цепочки на
момент миграции (NULL, если audit_log ещё пуст — genesis).

Revision ID: 0023_audit_chain_head
Revises: 0022_report_descriptions
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_audit_chain_head"
down_revision: str | None = "0022_report_descriptions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_chain_head",
        sa.Column("id", sa.Boolean(), nullable=False),
        sa.Column("hash", sa.String(64), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_chain_head"),
        # Единственная строка: `id` — не флаг, а замок против второй строки.
        sa.CheckConstraint("id", name="singleton"),
    )
    op.execute(
        sa.text(
            "INSERT INTO audit_chain_head (id, hash) "
            "VALUES (true, (SELECT hash FROM audit_log ORDER BY created_at DESC, id DESC LIMIT 1))"
        )
    )


def downgrade() -> None:
    op.drop_table("audit_chain_head")
