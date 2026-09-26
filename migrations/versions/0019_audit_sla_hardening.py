"""Аудит, SLA и «заморозка» сделок: версия хэша аудита, порог эскалации, parked не закрывает сделку

Три независимых изменения по итогам внешнего тестирования:

* **`audit_log.hash_version`.** Хэш записи аудита раньше не покрывал роль актора, подмену
  личности, IP и User-Agent — их можно было поправить в БД, а цепочка не заметила бы. Состав
  хэшируемых полей расширен, но старые записи неизменяемы и пересчитаны быть не могут, поэтому
  версия хранится в самой записи: 1 (по умолчанию) — исходный состав, 2 — расширенный.
  `verify_chain` проверяет каждую запись по её версии.
* **SLA: `sla_rules.escalate_threshold_pct` и `deals.sla_escalated_at`.** Эскалация считалась
  константой (150%) и срабатывала лишь в момент смены состояния, то есть практически никогда.
  Порог теперь свойство правила (по умолчанию прежние 150%), а факт уже отправленной эскалации
  запоминается на сделке, чтобы её не слать повторно на каждом проходе.
* **`parked` не терминален.** Статус «заморожена» ставил `closed_at`, из-за чего сделку нельзя было
  возобновить и она пропадала из «открытых». `closed_at` у уже замороженных сделок снимается:
  возврат из parked теперь обычный переход, а время паузы копится в `sla_paused_total`.

Revision ID: 0019_audit_sla_hardening
Revises: 0018_revoke_definer_public
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_audit_sla_hardening"
down_revision: str | None = "0018_revoke_definer_public"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- audit_log --------------------------------------------------------
    # Партиционированная таблица: столбец добавляется на родителя и наследуется партициями.
    # Постоянное значение по умолчанию не переписывает таблицу (PG 11+) и не трогает триггеры
    # неизменяемости, срабатывающие на UPDATE/DELETE.
    op.add_column(
        "audit_log",
        sa.Column("hash_version", sa.SmallInteger(), server_default=sa.text("1"), nullable=False),
    )

    # --- SLA -----------------------------------------------------------------
    op.add_column(
        "sla_rules",
        sa.Column(
            "escalate_threshold_pct",
            sa.SmallInteger(),
            server_default=sa.text("150"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "sla_rules_escalate_threshold_valid",
        "sla_rules",
        "escalate_threshold_pct BETWEEN 100 AND 1000",
    )
    op.add_column("deals", sa.Column("sla_escalated_at", sa.DateTime(timezone=True), nullable=True))

    # --- parked: не закрытие ----------------------------------------------------
    op.execute(
        """
        UPDATE deals SET closed_at = NULL
        FROM workflow_statuses s
        WHERE s.id = deals.status_id AND s.type = 'parked' AND deals.closed_at IS NOT NULL
        """
    )


def downgrade() -> None:
    # Возврат `closed_at` замороженным сделкам не восстанавливает исходные значения (их не
    # сохраняли): считаем закрытием момент последнего входа в статус.
    op.execute(
        """
        UPDATE deals SET closed_at = deals.status_changed_at
        FROM workflow_statuses s
        WHERE s.id = deals.status_id AND s.type = 'parked' AND deals.closed_at IS NULL
        """
    )
    op.drop_column("deals", "sla_escalated_at")
    op.drop_constraint("sla_rules_escalate_threshold_valid", "sla_rules", type_="check")
    op.drop_column("sla_rules", "escalate_threshold_pct")
    op.drop_column("audit_log", "hash_version")
