"""B3-2/B3-5/B3-8: опции разбора книги, сводка dry-run, строка/лист в отчёте, два новых типа импорта

Один импорт может читать несколько листов книги с шапкой не в первой строке
(B3-2) — выбор листа/строки заголовка/режима склейки/`fill_down` должен
переживать `PUT .../mapping` и повторный разбор файла при `dry-run`/`apply`,
поэтому он хранится на задании, а не пересчитывается на каждый запрос:

* `import_jobs.options` (jsonb, `NOT NULL DEFAULT '{}'`) — `ImportOptions`
  (`sheet`, `sheets`, `header_row`, `fill_down`, `create_missing_*`).
* `import_presets.options` (jsonb, нулевой — старые пресеты без опций листа).
* `import_row_results.source_row`/`sheet` — номер строки Excel (а не
  порядковый номер среди непустых строк) и имя листа строки результата, для
  понятного отчёта об ошибках (B3-5).
* `import_jobs.summary` (jsonb) — сводка dry-run: сколько строк с каждым
  кодом проблемы и что будет создано/обновлено/пропущено (B3-5), заполняется
  `_dry_run_handled`/`_dry_run`, читается `GET /api/imports/{id}/summary`.

CHECK `import_jobs_entity_type_valid` (сейчас шесть значений, расширен в
`0017_people_import`) получает `direction` и `responsible` — импорт
направлений и списка ответственных за вузы (B3-8), тот же приём, что уже
применялся: имя ограничения ищем через `pg_constraint`, потому что naming
convention и имя в истории миграций расходятся (см. пояснение в `0015`).

Revision ID: 0046_import_sheets_types
Revises: 0045_learning_progress_report
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0046_import_sheets_types"
down_revision: str | None = "0045_learning_progress_report"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_ENTITY_TYPE_CHECK = (
    "entity_type IN ('organization','product','license','vendor_contact','payment','learner')"
)
_NEW_ENTITY_TYPE_CHECK = (
    "entity_type IN ('organization','product','license','vendor_contact','payment','learner',"
    "'direction','responsible')"
)
_CLEAN_CONSTRAINT_NAME = "import_jobs_entity_type_valid"


def _find_check_constraint(table: str, column_substring: str) -> str:
    """Имя CHECK-constraint'а по факту в БД (см. `0017_people_import`: имя из истории миграций и
    имя в БД расходятся из-за naming convention, поэтому ищем через системный каталог)."""
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
    op.add_column(
        "import_jobs",
        sa.Column(
            "options", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
    )
    op.add_column("import_jobs", sa.Column("summary", postgresql.JSONB(), nullable=True))
    op.add_column("import_presets", sa.Column("options", postgresql.JSONB(), nullable=True))
    op.add_column("import_row_results", sa.Column("source_row", sa.Integer(), nullable=True))
    op.add_column(
        "import_row_results", sa.Column("sheet", sa.String(length=255), nullable=True)
    )

    old_name = _find_check_constraint("import_jobs", "entity_type")
    op.execute(f'ALTER TABLE import_jobs DROP CONSTRAINT "{old_name}"')
    op.create_check_constraint(_CLEAN_CONSTRAINT_NAME, "import_jobs", _NEW_ENTITY_TYPE_CHECK)


def downgrade() -> None:
    # Задания новых типов не проходят прежний CHECK — как в 0017, сначала убрать их.
    op.execute("DELETE FROM import_jobs WHERE entity_type IN ('direction','responsible')")
    old_name = _find_check_constraint("import_jobs", "entity_type")
    op.execute(f'ALTER TABLE import_jobs DROP CONSTRAINT "{old_name}"')
    op.create_check_constraint(_CLEAN_CONSTRAINT_NAME, "import_jobs", _OLD_ENTITY_TYPE_CHECK)

    op.drop_column("import_row_results", "sheet")
    op.drop_column("import_row_results", "source_row")
    op.drop_column("import_presets", "options")
    op.drop_column("import_jobs", "summary")
    op.drop_column("import_jobs", "options")
