"""B6-10: user_invites.purpose различает приглашение и самостоятельный сброс пароля

До этой задачи `user_invites` обслуживала только приглашение нового сотрудника:
`GET /api/auth/invite/{token}` лишь проверял ссылку, а задать пароль по ней было нельзя
(ссылка работала только как канал письма Keycloak, которого в закрытом контуре может не
быть). Новая колонка `purpose` ('invite' | 'reset') даёт одной и той же таблице (один
токен — один хэш, одна проверка одноразовости) обслуживать и приглашение, и ссылку сброса
пароля (`POST /api/admin/users/{id}/reset-password`), не путая их при отзыве
(`IdentityService.revoke_invites(..., purpose=...)`).

Revision ID: 0046_b6_user_invite_purpose
Revises: 0045_learning_progress_report
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046_b6_user_invite_purpose"
down_revision: str | None = "0045_learning_progress_report"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user_invites",
        sa.Column("purpose", sa.String(16), nullable=False, server_default="invite"),
    )
    op.create_check_constraint(
        "user_invites_purpose_valid",
        "user_invites",
        "purpose IN ('invite','reset')",
    )


def downgrade() -> None:
    op.drop_constraint("user_invites_purpose_valid", "user_invites", type_="check")
    op.drop_column("user_invites", "purpose")
