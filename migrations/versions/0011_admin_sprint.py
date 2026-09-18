"""Спринт 10: администрирование — исполнение удаления/обезличивания (new_spec §4.8, §9 шаг 9)

Запросы на удаление (`data_erasure_requests`), их блокеры и подтверждение
вторым администратором уже существовали до этого спринта (модуль `identity`
завёл их вместе с offboarding — та же логика предварительного резервирования,
что описана в докстринге 0010 для `Permission.INTEGRATION_INGEST`). Чего не
было — самого исполнения: `ERASURE_EXECUTED`/`ERASURE_REQUEST_APPROVED`/
`ERASURE_REQUEST_REJECTED` (`app/modules/audit/actions.py`) существовали как
значения enum, но ни разу не вызывались, а `grace_until` (new_spec §4.8.4
шаг 4 — 30 дней на восстановление до необратимого шага) вычислялся роутером
только для ответа API и нигде не сохранялся — фоновой задаче было не по
чему выбирать просроченные запросы.

Эта миграция добавляет ровно то, чего не хватало для исполнения:
  * `data_erasure_requests.grace_until` — момент, когда истекает отсрочка
    режима A и запрос становится исполнимым фоновым сборщиком
    (`identity.tasks.sweep_erasure_requests`);
  * частичный индекс под запрос сборщика (`status IN ('pending','approved')
    AND grace_until <= now()`) — без него полное сканирование таблицы на
    каждый тик cron стало бы линейно дороже с ростом истории запросов.

Revision ID: 0011_admin_sprint
Revises: 0010_integration_sprint
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_admin_sprint"
down_revision: str | None = "0010_integration_sprint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "data_erasure_requests",
        sa.Column("grace_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_data_erasure_requests_grace_due",
        "data_erasure_requests",
        ["grace_until"],
        postgresql_where=sa.text("status IN ('pending','approved')"),
    )


def downgrade() -> None:
    op.drop_index("ix_data_erasure_requests_grace_due", table_name="data_erasure_requests")
    op.drop_column("data_erasure_requests", "grace_until")
