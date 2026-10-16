"""B2-6: привязка вложения к статусу сделки и к записи истории перехода

Функц.3 требует «прикладывания файлов в статусы»: до этой миграции файл был
привязан к сделке целиком, и из истории нельзя было узнать, на каком шаге он
появился. Условие DSL `step_attachments.<категория> exists` (B2-7) засчитывает
только вложения, у которых `status_id` совпадает с текущим статусом сделки —
без этой колонки черновик договора, загруженный на шаге 4, засчитывался бы
как подписанный скан на шаге 6.

Две новые колонки `attachments.status_id`/`status_history_id` (вариант Б из
бэклога: обе необязательные, `ON DELETE RESTRICT` — статус или запись истории
с живыми вложениями нельзя удалить/заархивировать мимо `save_graph`, это уже
перехватывается как CRM-1208). `status_id` — статус сделки на момент
прикладывания (для вложения перехода — статус, из которого уходят);
`status_history_id` — запись истории перехода, с которой файл был передан
(выставляется только один раз, в `DealService.transition`, см. B2-7).

Бэкфилл закрывает уже лежащие вложения сделок: статус берётся как последняя
запись `deal_status_history` не позже `created_at` вложения. У сделок без
истории до даты файла (импорт) `status_id` остаётся `NULL` — это безопасная
сторона ошибки: такой файл просто не засчитается условию `step_attachments.*`.

CHECK `deal_events_type_valid` расширяется значением `FILE_REMOVED` (B2-8):
отвязка файла от сделки должна оставлять след в ленте так же, как и
привязка (`FILE_ADDED`, уже разрешён этим CHECK).

Revision ID: 0026_b2_attachment_status_link
Revises: 0025_import_job_last_error
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_b2_attachment_status_link"
down_revision: str | None = "0025_import_job_last_error"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEAL_EVENTS_CLEAN_CONSTRAINT_NAME = "deal_events_type_valid"
_DEAL_EVENTS_TYPES_WITH_FILE_REMOVED = (
    "'CREATED','OWNER_CHANGED','FIELD_CHANGED','FILE_ADDED','FILE_REMOVED',"
    "'SIGNATURE_REQUESTED','SIGNATURE_SIGNED','SIGNATURE_REJECTED',"
    "'IMPORT_APPLIED','WORKFLOW_MIGRATION'"
)
_DEAL_EVENTS_TYPES_WITHOUT_FILE_REMOVED = (
    "'CREATED','OWNER_CHANGED','FIELD_CHANGED','FILE_ADDED',"
    "'SIGNATURE_REQUESTED','SIGNATURE_SIGNED','SIGNATURE_REJECTED',"
    "'IMPORT_APPLIED','WORKFLOW_MIGRATION'"
)


def _find_check_constraint(table: str, column_substring: str) -> str:
    """Имя CHECK-constraint'а на `table`, чьё условие упоминает
    `column_substring`, по факту в БД (см. приём в `0015_organization_licenses.py`):
    naming convention иногда задваивает суффикс, и угадывать текущее имя по
    истории миграций ненадёжно."""
    bind = op.get_bind()
    name = bind.execute(
        sa.text(
            "SELECT con.conname FROM pg_constraint con "
            "JOIN pg_class rel ON rel.oid = con.conrelid "
            "WHERE rel.relname = :table AND con.contype = 'c' "
            "AND pg_get_constraintdef(con.oid) LIKE :pattern"
        ),
        {"table": table, "pattern": f"%{column_substring}%"},
    ).scalar_one()
    return str(name)


def upgrade() -> None:
    op.add_column(
        "attachments",
        sa.Column("status_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "attachments",
        sa.Column(
            "status_history_id", sa.dialects.postgresql.UUID(as_uuid=True), nullable=True
        ),
    )
    op.create_foreign_key(
        "fk_attachments_status_id_workflow_statuses",
        "attachments",
        "workflow_statuses",
        ["status_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_attachments_status_history_id_deal_status_history",
        "attachments",
        "deal_status_history",
        ["status_history_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_attachments_status_history",
        "attachments",
        ["status_history_id"],
        postgresql_where=sa.text("status_history_id IS NOT NULL"),
    )
    op.create_index(
        "ix_attachments_deal_status",
        "attachments",
        ["entity_id", "status_id"],
        postgresql_where=sa.text("entity_type = 'deal' AND deleted_at IS NULL"),
    )

    # Бэкфилл: статус сделки на момент прикладывания — последняя запись истории
    # не позже created_at вложения (см. докстринг модуля).
    op.execute(
        sa.text(
            "UPDATE attachments a SET status_id = ("
            "  SELECT h.to_status_id FROM deal_status_history h"
            "  WHERE h.deal_id = a.entity_id AND h.changed_at <= a.created_at"
            "  ORDER BY h.changed_at DESC LIMIT 1"
            ") WHERE a.entity_type = 'deal'"
        )
    )

    # `op.drop_constraint(name, ..., type_="check")` пропускает `name` через naming convention
    # ещё раз (тот же гротеск, что уже найден в `0015_organization_licenses.py`: имя
    # задваивается и обрезается до 63 байт с хэш-суффиксом, итоговая строка в БД не
    # существует). Raw SQL по динамически найденному имени — без повторной обработки.
    old_name = _find_check_constraint("deal_events", "FILE_ADDED")
    op.execute(f'ALTER TABLE deal_events DROP CONSTRAINT "{old_name}"')
    op.create_check_constraint(
        _DEAL_EVENTS_CLEAN_CONSTRAINT_NAME,
        "deal_events",
        f"event_type IN ({_DEAL_EVENTS_TYPES_WITH_FILE_REMOVED})",
    )


def downgrade() -> None:
    # Откат отказывает, если в deal_events уже есть строки FILE_REMOVED: их
    # нужно удалить вручную до downgrade (см. риски задачи B2-6/B2-8 в плане
    # спринта) — это осознанно не делается автоматически здесь, чтобы
    # downgrade не стирал данные молча.
    old_name = _find_check_constraint("deal_events", "FILE_ADDED")
    op.execute(f'ALTER TABLE deal_events DROP CONSTRAINT "{old_name}"')
    op.create_check_constraint(
        _DEAL_EVENTS_CLEAN_CONSTRAINT_NAME,
        "deal_events",
        f"event_type IN ({_DEAL_EVENTS_TYPES_WITHOUT_FILE_REMOVED})",
    )

    op.drop_index("ix_attachments_deal_status", table_name="attachments")
    op.drop_index("ix_attachments_status_history", table_name="attachments")
    op.drop_constraint(
        "fk_attachments_status_history_id_deal_status_history", "attachments", type_="foreignkey"
    )
    op.drop_constraint("fk_attachments_status_id_workflow_statuses", "attachments", type_="foreignkey")
    op.drop_column("attachments", "status_history_id")
    op.drop_column("attachments", "status_id")
