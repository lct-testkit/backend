"""B6-7: contact_learner_profiles — колонки под шифрование полей в приложении

Поля ПДн учащегося (`EncryptedText`/`EncryptedDate`, `app/core/field_crypto.py`) хранят
Fernet-токен (`gAAAAA...`, urlsafe base64) в колонке `text`: токен заметно длиннее исходного
значения (примерно в 1,4 раза плюс порядка 100 байт служебных данных), поэтому старые
ограниченные типы (`varchar(N)`, `date`) не вмещают его.

Переносятся только поля из `app.core.masking.LEARNER_PII_KEYS` плюс `passport_issued_at` и
`diploma_issued_at` (те же даты, что и `birth_date`, не входящие в маскирование, но хранящие
ПДн). Существующие значения НЕ перешифровываются этой миграцией — они остаются читаемым
открытым текстом внутри `text`-колонки до `python -m tools.encrypt_learner_profiles
encrypt-existing`; `EncryptedText`/`EncryptedDate` отличают токен Fernet от обычной строки по
префиксу и читают и то, и то.

Откат по датам безопасен только пока строки не зашифрованы (`USING col::date` не распарсит
Fernet-токен) — `downgrade()` явно проверяет это и отказывает, если найдена хоть одна
зашифрованная строка, вместо того чтобы тихо уронить ошибку приведения типов посреди
транзакции.

Revision ID: 0029_learner_profile_crypto
Revises: 0028_file_scan_result_len
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0029_learner_profile_crypto"
down_revision: str | None = "0028_file_scan_result_len"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TEXT_COLUMNS = (
    ("snils", sa.String(14)),
    ("passport_series", sa.String(8)),
    ("passport_number", sa.String(16)),
    ("passport_issued_by", sa.String(512)),
    ("passport_dept_code", sa.String(16)),
    ("reg_region", sa.String(255)),
    ("reg_city", sa.String(255)),
    ("reg_street", sa.String(255)),
    ("reg_house", sa.String(64)),
    ("reg_apartment", sa.String(64)),
    ("reg_zip", sa.String(16)),
    ("diploma_number", sa.String(64)),
    ("diploma_series", sa.String(64)),
    ("diploma_reg_number", sa.String(64)),
    ("diploma_surname", sa.String(128)),
)

_DATE_COLUMNS = ("passport_issued_at", "birth_date", "diploma_issued_at")

_TABLE = "contact_learner_profiles"


def upgrade() -> None:
    for name, existing_type in _TEXT_COLUMNS:
        op.alter_column(_TABLE, name, type_=sa.Text(), existing_type=existing_type)
    for name in _DATE_COLUMNS:
        op.alter_column(
            _TABLE,
            name,
            type_=sa.Text(),
            existing_type=sa.Date(),
            postgresql_using=f"to_char({name}, 'YYYY-MM-DD')",
        )


def downgrade() -> None:
    bind = op.get_bind()
    all_columns = [name for name, _ in _TEXT_COLUMNS] + list(_DATE_COLUMNS)
    encrypted_sql = " OR ".join(f"{name} LIKE 'gAAAA%'" for name in all_columns)
    encrypted = bind.execute(
        sa.text(f"SELECT count(*) FROM {_TABLE} WHERE {encrypted_sql}")  # noqa: S608
    ).scalar_one()
    if encrypted:
        raise RuntimeError(
            "downgrade 0029 отказан: в contact_learner_profiles есть зашифрованные значения "
            "(похожи на Fernet-токен) — сначала выполните "
            "`python -m tools.encrypt_learner_profiles decrypt-existing`, иначе `::date` "
            "упадёт, а усечение текстовых колонок до старой длины необратимо испортит токен."
        )
    for name in _DATE_COLUMNS:
        op.alter_column(
            _TABLE,
            name,
            type_=sa.Date(),
            existing_type=sa.Text(),
            postgresql_using=f"{name}::date",
        )
    for name, existing_type in _TEXT_COLUMNS:
        op.execute(
            sa.text(
                f"UPDATE {_TABLE} SET {name} = left({name}, :len) "  # noqa: S608
                f"WHERE {name} IS NOT NULL"
            ).bindparams(len=existing_type.length)
        )
        op.alter_column(_TABLE, name, type_=existing_type, existing_type=sa.Text())
