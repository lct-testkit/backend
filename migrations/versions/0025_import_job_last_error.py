"""`import_jobs.last_error` — причина ухода в статус `failed`

`ImportJobStatus.FAILED` был объявлен, но нигде не присваивался: если партия `apply_batch`/
`rollback_batch` падала вне построчного try/except (сам запрос партии, `finalize_*_if_done`,
обрыв соединения с БД), исключение улетало через `sweep_import_jobs`, а статус задания
оставался как был (`applying`/`rolling_back`) навсегда — пользователь никогда не узнавал, что
импорт сломался. `imports.tasks._run_one_batch` теперь ловит такую ошибку и ставит `failed`;
причина — здесь, тем же приёмом, что `last_error` в `integration.models` для фоновых заданий
интеграций.

Revision ID: 0025_import_job_last_error
Revises: 0024_audit_chain_head_autovacuum
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_import_job_last_error"
down_revision: str | None = "0024_audit_chain_head_autovacuum"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("import_jobs", sa.Column("last_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("import_jobs", "last_error")
