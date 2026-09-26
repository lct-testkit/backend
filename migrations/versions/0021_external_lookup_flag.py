"""Флаг функции `external_org_lookup`: внешний поиск организаций по ИНН (публичный поиск ФНС)

Флаг заводится выключенным: закрытому контуру внешний адрес открывает администратор явно
(dop.md §11.1). Строка нужна, чтобы флаг был виден в «Настройки → Флаги» сразу, без ручного
создания; отсутствие строки читается как «выключено» (`registry.providers.resolve_chain`).
Повторный запуск и уже заведённый вручную флаг не затрагиваются.

Revision ID: 0021_external_lookup_flag
Revises: 0020_signature_unique_request
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_external_lookup_flag"
down_revision: str | None = "0020_signature_unique_request"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CODE = "external_org_lookup"
_DESCRIPTION = (
    "Внешний поиск организаций по ИНН и названию (публичный сервис ФНС) после локального "
    "реестра. Включает исходящие запросы во внешнюю сеть."
)


def upgrade() -> None:
    op.execute(
        sa.text(
            "INSERT INTO feature_flags (id, code, is_enabled, description, rollout) "
            "VALUES (gen_random_uuid(), :code, false, :description, 100) "
            "ON CONFLICT (code) DO NOTHING"
        ).bindparams(code=_CODE, description=_DESCRIPTION)
    )


def downgrade() -> None:
    op.execute(
        sa.text("DELETE FROM feature_flags WHERE code = :code AND is_enabled = false").bindparams(
            code=_CODE
        )
    )
