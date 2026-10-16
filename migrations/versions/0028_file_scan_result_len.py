"""B6-4: files.scan_result 32 -> 128 символов

Заглушка `NullAntivirusScanner` писала короткие строки (`clean`, `signature_mismatch`), и
32 символов хватало. Настоящий антивирус (`ClamAvScanner`, протокол INSTREAM) возвращает имя
сигнатуры произвольной длины (например `Win.Test.EICAR_HDB-1` — 21 символ, но встречаются и
заметно длиннее) — без расширения колонки `commit` падал бы с 500 именно на заражённых файлах,
то есть именно там, где меньше всего хочется получить необработанную ошибку.

Откат обрезает уже лежащие значения длиннее 32 символов до 32 — старые записи заглушки короче
этого предела не трогает.

Revision ID: 0028_file_scan_result_len
Revises: 0027_b6_security_events_index
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_file_scan_result_len"
down_revision: str | None = "0027_b6_security_events_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("files", "scan_result", type_=sa.String(128), existing_type=sa.String(32))


def downgrade() -> None:
    op.execute("UPDATE files SET scan_result = left(scan_result, 32) WHERE scan_result IS NOT NULL")
    op.alter_column("files", "scan_result", type_=sa.String(32), existing_type=sa.String(128))
