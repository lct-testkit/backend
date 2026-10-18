"""Шаблон «Прогресс обучения»: описание и default_params под настоящую реализацию (B1-10)

До этой задачи `build_learning_progress` (`app/modules/reporting/builders.py`) был честной
заглушкой — таблица `learning_progress` и приём данных LMS (`integration.lms.upsert_progress`)
структурно существовали, но builder их не читал и всегда отдавал пустой результат с текстом
«интеграция не реализована». Сид заводит строку `report_templates` один раз и существующую не
трогает (`reporting.seed.seed_report_templates` — `if existing is not None: continue`), поэтому
уже развёрнутые стенды (в т.ч. тестовую БД этого прогона) обновляет только эта миграция — тот же
приём, что `0022_report_descriptions.py` уже применил к той же строке: обновляется только ТОЧНОЕ
старое значение (администратор, успевший поменять текст вручную, не затрагивается). `output_
formats` эту задачу не меняет — xlsx/xls/pdf/json у `learning_progress` уже есть с миграции
0029 (B1-4/B1-5, формат добавлялся всем активным шаблонам сразу).

Revision ID: 0045_learning_progress_report
Revises: 0044_report_formats_meta
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0045_learning_progress_report"
down_revision: str | None = "0044_report_formats_meta"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_DESCRIPTION = "Данные из LMS. Интеграция с LMS не собирает эти данные — отчёт пока пуст."
_NEW_DESCRIPTION = (
    "Прогресс учащихся по данным LMS: процент прохождения курса, оценка, даты зачисления и "
    "завершения, последняя активность, поток. Пусто и без пометки «не реализована», если "
    "источник lms не присылал данных. Параметры: date_from/date_to, organization_ids, "
    "direction_ids, product_ids, owner_ids, columns, date_field, limit."
)


def upgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE report_templates SET description = :new_description, "
            "default_params = '{\"limit\": 1000}'::jsonb "
            "WHERE code = 'learning_progress' AND description = :old_description "
            "AND default_params = '{}'::jsonb"
        ).bindparams(new_description=_NEW_DESCRIPTION, old_description=_OLD_DESCRIPTION)
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE report_templates SET description = :old_description, "
            "default_params = '{}'::jsonb "
            "WHERE code = 'learning_progress' AND description = :new_description "
            "AND default_params = '{\"limit\": 1000}'::jsonb"
        ).bindparams(old_description=_OLD_DESCRIPTION, new_description=_NEW_DESCRIPTION)
    )
