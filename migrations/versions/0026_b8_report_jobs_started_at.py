"""B8-11: `report_jobs.started_at` — когда рендер реально начался

До этой задачи ни один обработчик `app/` не ставил заданий в очередь arq из API:
тяжёлый отчёт лежал `queued` до ближайшего минутного тика `sweep_report_jobs`, а
"processing" никогда не коммитился отдельной транзакцией (вся генерация шла одной
транзакцией с блокировкой `FOR UPDATE SKIP LOCKED`), поэтому снаружи «занято»
было не видно: `GET /api/reports/{id}` в процессе рендера всё ещё отдавал `queued`.

Теперь `render_report_job` (arq-задача) сначала коммитит `status='processing',
started_at=now()` отдельной короткой транзакцией — это и даёт видимость, и
позволяет добору (`sweep_report_jobs`) отличить «реально рендерится» от
«зависло после падения воркера» по возрасту `started_at` (порог 15 минут,
заметно больше таймаута самой задачи в 600 с).

Revision ID: 0026_b8_report_jobs_started_at
Revises: 0025_import_job_last_error
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_b8_report_jobs_started_at"
down_revision: str | None = "0025_import_job_last_error"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("report_jobs", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    # Добор (`sweep_report_jobs`) ищет старые `queued` для повторной постановки в очередь:
    # частичный индекс держит этот запрос дешёвым независимо от числа завершённых заданий.
    op.create_index(
        "ix_report_jobs_queue",
        "report_jobs",
        ["created_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )


def downgrade() -> None:
    op.drop_index("ix_report_jobs_queue", table_name="report_jobs")
    op.drop_column("report_jobs", "started_at")
