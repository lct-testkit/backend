"""Описания шаблонов отчётов без ссылок на внутренние документы

В галерее «Отчёты» у двух шаблонов в описании торчали «(раздел 4.13)» и «(new_spec §4.14)»: пользователю эти
ссылки ни о чём не говорят. Сид заводит шаблон один раз и существующую строку не трогает, поэтому уже
развёрнутые стенды исправляет эта миграция; описание, которое администратор успел поменять вручную, не
затрагивается (обновляется только точное старое значение).

Revision ID: 0022_report_descriptions
Revises: 0021_external_lookup_flag
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_report_descriptions"
down_revision: str | None = "0021_external_lookup_flag"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (код шаблона, прежнее описание, новое описание)
_CHANGES = (
    (
        "deal_funnel",
        "Конверсия и среднее время между шагами воронки (раздел 4.13).",
        "Конверсия и среднее время между шагами воронки.",
    ),
    (
        "learning_progress",
        "Данные из LMS. Интеграция не реализована — отчёт пуст (new_spec §4.14).",
        "Данные из LMS. Интеграция с LMS не собирает эти данные — отчёт пока пуст.",
    ),
)


def _apply(pairs: Sequence[tuple[str, str, str]]) -> None:
    for code, old, new in pairs:
        op.execute(
            sa.text(
                "UPDATE report_templates SET description = :new "
                "WHERE code = :code AND description = :old"
            ).bindparams(code=code, old=old, new=new)
        )


def upgrade() -> None:
    _apply(_CHANGES)


def downgrade() -> None:
    _apply([(code, new, old) for code, old, new in _CHANGES])
