"""Жизненный цикл учётных записей и версия команды (new_spec §4.5, §4.1, §4.6)

**`users.auto_unblock_at`.** `POST /admin/users/{id}/block` принимал
`auto_unblock_at`, но только писал его в аудит и событие безопасности: сохранить
срок было негде, и исполнять его было нечем. Колонка хранит срок блокировки;
задача `identity.tasks.sweep_user_lifecycle` снимает блокировку, когда он
наступает. Частичный индекс — под её запрос (`status = 'blocked' AND
auto_unblock_at <= now()`): заблокированных с заданным сроком единицы, полное
сканирование `users` на каждый тик без него линейно дорожало бы с ростом таблицы.

**`teams.version`.** Оптимистичная блокировка `PATCH /admin/teams/{id}` через
`If-Match` — тот же `VersionMixin`, что у пользователей и шаблонов уведомлений.

Revision ID: 0016_identity_lifecycle
Revises: 0015_organization_licenses
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_identity_lifecycle"
down_revision: str | None = "0015_organization_licenses"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("auto_unblock_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_users_auto_unblock_due",
        "users",
        ["auto_unblock_at"],
        postgresql_where=sa.text("status = 'blocked' AND auto_unblock_at IS NOT NULL"),
    )
    op.add_column(
        "teams",
        sa.Column("version", sa.BigInteger(), nullable=False, server_default=sa.text("1")),
    )


def downgrade() -> None:
    op.drop_column("teams", "version")
    op.drop_index("ix_users_auto_unblock_due", table_name="users")
    op.drop_column("users", "auto_unblock_at")
