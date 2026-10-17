"""CLI для управления шифрованием ПДн профиля учащегося (B6-7).

    python -m tools.encrypt_learner_profiles encrypt-existing  # зашифровать открытый текст
    python -m tools.encrypt_learner_profiles rotate            # старый ключ -> новый
    python -m tools.encrypt_learner_profiles decrypt-existing  # расшифровать обратно (откат)
    python -m tools.encrypt_learner_profiles check              # сосчитать строки открытым текстом

Работает пачками по `_BATCH_SIZE` строк (`contact_id`), с `FOR UPDATE SKIP LOCKED` на каждой
пачке — параллельная работа с профилями (`PUT .../learner-profile`) не блокируется надолго.
Читает и пишет значения колонок через явный SQL (`sa.text`), а не через ORM/`EncryptedText`:
инструмент сам решает, шифровать конкретное значение или расшифровать, а не полагается на то,
что сделает `TypeDecorator` при обычном чтении/записи. Вывод — только счётчики, значения полей
никогда не печатаются.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

import sqlalchemy as sa

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_TABLE = "contact_learner_profiles"

_TEXT_COLUMNS = (
    "snils",
    "passport_series",
    "passport_number",
    "passport_issued_by",
    "passport_dept_code",
    "reg_region",
    "reg_city",
    "reg_street",
    "reg_house",
    "reg_apartment",
    "reg_zip",
    "diploma_number",
    "diploma_series",
    "diploma_reg_number",
    "diploma_surname",
)
_DATE_COLUMNS = ("passport_issued_at", "birth_date", "diploma_issued_at")
_ALL_COLUMNS = _TEXT_COLUMNS + _DATE_COLUMNS

_TOKEN_PREFIX = "gAAAA"
_BATCH_SIZE = 500


def _is_token(value: str | None) -> bool:
    return value is not None and value.startswith(_TOKEN_PREFIX)


async def _batched_contact_ids(session: AsyncSession, predicate_sql: str) -> list[list[object]]:
    """Все `contact_id`, подходящие под `predicate_sql`, разбитые на пачки по `_BATCH_SIZE`.

    Пачки собираются одним запросом вперёд (без удержания курсора): строки берутся `FOR UPDATE
    SKIP LOCKED` только в момент собственно обработки пачки, не здесь."""
    rows = (
        await session.execute(
            sa.text(f"SELECT contact_id FROM {_TABLE} WHERE {predicate_sql} ORDER BY contact_id")  # noqa: S608
        )
    ).all()
    ids = [r[0] for r in rows]
    return [ids[i : i + _BATCH_SIZE] for i in range(0, len(ids), _BATCH_SIZE)]


async def _process_batch(
    batch_ids: list[object], transform: Callable[[str, str], str | None]
) -> int:
    """Обрабатывает одну пачку `contact_id` в своей транзакции: `FOR UPDATE SKIP LOCKED`, затем
    для каждой строки — `transform(column, raw_value) -> new_value | None` (`None` — не менять).
    Возвращает число изменённых строк."""
    from app.core.db import session_scope

    changed = 0
    async with session_scope() as session:
        locked = (
            await session.execute(
                sa.text(
                    f"SELECT contact_id FROM {_TABLE} WHERE contact_id = ANY(:ids) "  # noqa: S608
                    "FOR UPDATE SKIP LOCKED"
                ),
                {"ids": batch_ids},
            )
        ).all()
        locked_ids = [r[0] for r in locked]
        if not locked_ids:
            return 0
        rows = (
            await session.execute(
                sa.text(
                    f"SELECT contact_id, {', '.join(_ALL_COLUMNS)} FROM {_TABLE} "  # noqa: S608
                    "WHERE contact_id = ANY(:ids)"
                ),
                {"ids": locked_ids},
            )
        ).all()
        for row in rows:
            contact_id, *values = row
            updates: dict[str, str | None] = {}
            for col, value in zip(_ALL_COLUMNS, values, strict=True):
                if value is None:
                    continue
                new_value = transform(col, value)
                if new_value is not None and new_value != value:
                    updates[col] = new_value
            if updates:
                set_clause = ", ".join(f"{col} = :{col}" for col in updates)
                await session.execute(
                    sa.text(f"UPDATE {_TABLE} SET {set_clause} WHERE contact_id = :id"),  # noqa: S608
                    {**updates, "id": contact_id},
                )
                changed += 1
    return changed


async def encrypt_existing() -> int:
    """Шифрует все значения, которые пока лежат открытым текстом. Идемпотентно: уже
    зашифрованные (признак `gAAAA`) не трогает."""
    from app.core.db import session_scope
    from app.core.field_crypto import encrypt_text, encryption_enabled

    if not encryption_enabled():
        print("FIELD_ENCRYPTION_KEY не задан — нечем шифровать", file=sys.stderr)
        return 0

    predicate = " OR ".join(
        f"({col} IS NOT NULL AND {col} NOT LIKE '{_TOKEN_PREFIX}%')" for col in _ALL_COLUMNS
    )
    async with session_scope() as session:
        batches = await _batched_contact_ids(session, predicate)

    def _encrypt(_column: str, value: str) -> str | None:
        return None if _is_token(value) else encrypt_text(value)

    total = 0
    for batch in batches:
        total += await _process_batch(batch, _encrypt)
    print(total)
    return total


async def decrypt_existing() -> int:
    """Расшифровывает всё обратно в открытый текст — только для откатa миграции 0029/0030."""
    from app.core.db import session_scope
    from app.core.field_crypto import decrypt_text, encryption_enabled

    if not encryption_enabled():
        print("FIELD_ENCRYPTION_KEY не задан — нечем расшифровать", file=sys.stderr)
        return 0

    predicate = " OR ".join(f"{col} LIKE '{_TOKEN_PREFIX}%'" for col in _ALL_COLUMNS)
    async with session_scope() as session:
        batches = await _batched_contact_ids(session, predicate)

    def _decrypt(_column: str, value: str) -> str | None:
        return decrypt_text(value) if _is_token(value) else None

    total = 0
    for batch in batches:
        total += await _process_batch(batch, _decrypt)
    print(total)
    return total


async def rotate() -> int:
    """Перешифровывает Fernet-токены: расшифровывает (активным или `_PREV` ключом через
    `MultiFernet`) и шифрует заново активным ключом. После успешного прогона и `check == 0` со
    старым ключом `FIELD_ENCRYPTION_KEY_PREV` можно убрать из окружения."""
    from app.core.db import session_scope
    from app.core.field_crypto import decrypt_text, encrypt_text, encryption_enabled

    if not encryption_enabled():
        print("FIELD_ENCRYPTION_KEY не задан — нечего ротировать", file=sys.stderr)
        return 0

    predicate = " OR ".join(f"{col} LIKE '{_TOKEN_PREFIX}%'" for col in _ALL_COLUMNS)
    async with session_scope() as session:
        batches = await _batched_contact_ids(session, predicate)

    def _reencrypt(_column: str, value: str) -> str | None:
        if not _is_token(value):
            return None
        return encrypt_text(decrypt_text(value))

    total = 0
    for batch in batches:
        total += await _process_batch(batch, _reencrypt)
    print(total)
    return total


async def check() -> int:
    """Считает строки с хотя бы одним полем открытым текстом. Возвращает это число (0 — чисто);
    используется и как код завершения для скриптов (`echo $?`)."""
    from app.core.db import session_scope

    predicate = " OR ".join(
        f"({col} IS NOT NULL AND {col} NOT LIKE '{_TOKEN_PREFIX}%')" for col in _ALL_COLUMNS
    )
    async with session_scope() as session:
        count = (
            await session.execute(
                sa.text(f"SELECT count(*) FROM {_TABLE} WHERE {predicate}")  # noqa: S608
            )
        ).scalar_one()
    print(f"plaintext_rows={count}")
    return int(count)


_COMMANDS = {
    "encrypt-existing": encrypt_existing,
    "decrypt-existing": decrypt_existing,
    "rotate": rotate,
    "check": check,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=sorted(_COMMANDS))
    args = parser.parse_args(argv)
    result = asyncio.run(_COMMANDS[args.command]())
    return 0 if args.command != "check" else (1 if result else 0)


if __name__ == "__main__":
    raise SystemExit(main())
