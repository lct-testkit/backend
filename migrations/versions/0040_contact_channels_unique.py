"""B4-9: уникальность каналов связи контакта

`contact_channels` заводился без ограничений: один и тот же Telegram или
добавочный телефон можно было записать контакту дважды, а `is_primary` —
выставить нескольким строкам одного типа сразу (A-20). Перед индексами чистим
накопленные дубли: как это сделать безопасно на живом стенде — в риске задачи
(дамп перед применением).

Revision ID: 0040_contact_channels_unique
Revises: 0039_user_access_rules
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040_contact_channels_unique"
down_revision: str | None = "0039_user_access_rules"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Дубль (тот же контакт+тип+значение) — оставляем самый старый, остальные удаляем.
    op.execute(
        """
        DELETE FROM contact_channels a USING contact_channels b
         WHERE a.contact_id = b.contact_id AND a.type = b.type AND a.value = b.value
           AND (a.created_at, a.id) > (b.created_at, b.id)
        """
    )
    # Несколько первичных одного типа — первичным остаётся самый старый.
    op.execute(
        """
        UPDATE contact_channels c SET is_primary = false
         WHERE c.is_primary AND EXISTS (
           SELECT 1 FROM contact_channels p
            WHERE p.contact_id = c.contact_id AND p.type = c.type AND p.is_primary
              AND (p.created_at, p.id) < (c.created_at, c.id))
        """
    )
    op.create_index(
        "uq_contact_channels_value",
        "contact_channels",
        ["contact_id", "type", "value"],
        unique=True,
    )
    op.create_index(
        "uq_contact_channels_primary",
        "contact_channels",
        ["contact_id", "type"],
        unique=True,
        postgresql_where=sa.text("is_primary"),
    )


def downgrade() -> None:
    op.drop_index("uq_contact_channels_primary", table_name="contact_channels")
    op.drop_index("uq_contact_channels_value", table_name="contact_channels")
    # Удалённые дубли не восстанавливаются — см. риски задачи B4-9.
