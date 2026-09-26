"""Подписи: уникальность по запросу и индекс по хэшу звена (отчёт внешнего тестирования)

* **`signatures.request_id` уникален.** Подпись на запрос одна, но индекс по этому столбцу был
  обычным: двойная отправка кода при гонке давала вторую подпись, лишние файлы и события.
  Гонку закрывает замок строки запроса (`SignatureRequestService._lock_for_action`), уникальный
  индекс — последний рубеж на уровне БД.

  `signatures` неизменяема (триггер запрещает UPDATE и DELETE), поэтому уже найденные дубли ни
  удалить, ни объединить нельзя, а обычный `UNIQUE` на такой базе просто упал бы. Миграция сначала
  ищет лишние строки (вторую и далее подпись одного запроса). Дублей нет — строится полный
  уникальный индекс. Дубли есть — индекс частичный и обходит именно эти строки по `id`: первая
  подпись каждого запроса остаётся под контролем, любая новая тоже (третью подпись запроса с уже
  имеющимся дублем БД отвергнет). Проще был бы частичный индекс «только строки новее миграции» по
  `created_at`, но он не защитил бы запрос со старой подписью от новой; поэтому исключение
  адресное. Прежний обычный индекс `ix_signatures_request` остаётся: запрос по `request_id` без
  условия частичный индекс не обслуживает.
* **`ix_signatures_hash`.** Проверка подписи ищет предыдущее звено цепочки по `prev_hash`.

Revision ID: 0020_signature_unique_request
Revises: 0019_audit_sla_hardening
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_signature_unique_request"
down_revision: str | None = "0019_audit_sla_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    # Лишние подписи одного запроса: первая по времени остаётся «основной», остальные исключаются.
    surplus = (
        bind.execute(
            sa.text(
                "SELECT id FROM ("
                "  SELECT id, row_number() OVER ("
                "    PARTITION BY request_id ORDER BY created_at, id) AS n"
                "  FROM signatures"
                ") ranked WHERE n > 1"
            )
        )
        .scalars()
        .all()
    )
    if surplus:
        # UUID из БД: в SQL подставляются как литералы (значения из `str(uuid)`, не ввод).
        excluded = ", ".join(f"'{row_id}'" for row_id in surplus)
        op.create_index(
            "uq_signatures_request_id",
            "signatures",
            ["request_id"],
            unique=True,
            postgresql_where=sa.text(f"id NOT IN ({excluded})"),
        )
    else:
        op.create_index("uq_signatures_request_id", "signatures", ["request_id"], unique=True)
    op.create_index("ix_signatures_hash", "signatures", ["hash"])


def downgrade() -> None:
    op.drop_index("ix_signatures_hash", table_name="signatures")
    op.drop_index("uq_signatures_request_id", table_name="signatures")
