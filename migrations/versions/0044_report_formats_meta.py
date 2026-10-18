"""Форматы отчётов xls/json и result_meta заданий (B1-4, B1-5, B1-12)

Общая миграция для трёх задач спринта B1 (документ `backend-sprints.md`, «Итог спринта B1», п.4:
«B1-4 (xls). Вместе с ним создаётся одна миграция ...; B1-5, B1-8, B1-10, B1-12 и B1-15 дописывают
в неё свои строки до слияния, чтобы у Alembic была одна голова»):

* B1-4: формат `xls` (Функц.4/Функц.8 называют его первым из трёх, следом за xlsx/pdf) —
  `xlwt`-рендер (`reporting.rendering_xls`), читается `xlrd`. Лимит 65 535 строк проверяется в
  коде (`ReportJobService`/`render_xls`), не в БД.
* B1-5: формат `json` (rtk_requiriments.md §6 п.4: «возможность формирования результирующего
  json-файла») — тот же датасет, что и `GET /reports/{id}/data`, но файлом с записью в аудит.
* B1-12: `report_jobs.result_meta jsonb` — заголовки колонок/total_rows/truncated/data_as_of/
  timezone на момент генерации, для `ReportJobOut.result_meta` (контракт заданий).

CHECK-constraint `report_jobs_format_valid` не может быть переименован/изменён `ALTER` —
Postgres не поддерживает `ALTER CONSTRAINT ... CHECK`, только `DROP`+`CREATE`. Имя в реальной БД
не совпадает с литералом в модели (`ck_report_jobs_ck_report_jobs_format_valid` — naming
convention `0009` применилась к имени, которое уже содержало префикс `ck_`, задвоив его), поэтому
имя не угадывается, а находится по тексту условия (`_find_check_constraint`, тот же приём, что
уже применяет `0015_organization_licenses.py` для `import_jobs_entity_type_valid`).

`output_formats` существующих шаблонов: `xls` получают все, у кого уже есть `xlsx`, кроме
`lms_users_upload` (там только xlsx по шаблону заказчика — ПДн целиком, см. `FILE_ONLY_REPORTS`).
`json` получают все активные шаблоны, кроме `lms_users_upload` (тот же повод: `/data` для него и
так недоступен, `ReportJobService.get_data` отдаёт `ForbiddenError`).

Revision ID: 0044_report_formats_meta
Revises: 0043_license_term_years_integer
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0044_report_formats_meta"
down_revision: str | None = "0043_license_term_years_integer"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_FORMATS = "format IN ('xlsx','pdf','png')"
_NEW_FORMATS = "format IN ('xlsx','xls','pdf','png','json')"


def _find_check_constraint(table: str, column_substring: str) -> str:
    """Имя CHECK-constraint'а на `table`, чьё условие упоминает `column_substring`, по факту в
    БД — не по тому, что "должно быть" по истории миграций. Та же функция, что и в
    `0015_organization_licenses.py` (миграции этого репозитория её не импортируют друг у друга —
    каждая самодостаточна и не зависит от соседних файлов)."""
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
    return name


def upgrade() -> None:
    old_name = _find_check_constraint("report_jobs", "xlsx")
    op.execute(f'ALTER TABLE report_jobs DROP CONSTRAINT "{old_name}"')
    op.create_check_constraint("report_jobs_format_valid", "report_jobs", _NEW_FORMATS)

    op.add_column("report_jobs", sa.Column("result_meta", postgresql.JSONB(), nullable=True))

    op.execute(
        "UPDATE report_templates SET output_formats = array_append(output_formats, 'xls') "
        "WHERE 'xlsx' = ANY(output_formats) AND NOT ('xls' = ANY(output_formats)) "
        "AND code <> 'lms_users_upload'"
    )
    op.execute(
        "UPDATE report_templates SET output_formats = array_append(output_formats, 'json') "
        "WHERE NOT ('json' = ANY(output_formats)) AND code <> 'lms_users_upload'"
    )


def downgrade() -> None:
    op.execute("UPDATE report_templates SET output_formats = array_remove(output_formats, 'json')")
    op.execute("UPDATE report_templates SET output_formats = array_remove(output_formats, 'xls')")

    op.drop_column("report_jobs", "result_meta")

    # Откат предполагает, что заданий с format IN ('xls','json') к этому моменту уже нет —
    # иначе новый CHECK не создастся (см. докстринг модуля); это тот же риск, что и в B1-4.
    new_name = _find_check_constraint("report_jobs", "xlsx")
    op.execute(f'ALTER TABLE report_jobs DROP CONSTRAINT "{new_name}"')
    op.create_check_constraint("report_jobs_format_valid", "report_jobs", _OLD_FORMATS)
