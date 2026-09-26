"""Записи, которые не дают физически удалить строку: внешние ключи без каскада.

Жёсткое удаление (152-ФЗ, режим C) допустимо, только если на строку никто не ссылается.
Проверять это списком таблиц, написанным вручную, нельзя: список устаревает с каждой новой
миграцией, а забытая таблица обнаруживается уже на `DELETE` — после внешних необратимых шагов
(учётка в Keycloak, акт уничтожения) и повторяется при каждой попытке. Здесь список берётся из
метаданных ORM: все внешние ключи на нужную колонку, кроме `CASCADE` и `SET NULL/DEFAULT`.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import Base

_NON_BLOCKING = ("CASCADE", "SET NULL", "SET DEFAULT")


async def restrict_dependents(
    session: AsyncSession, table_name: str, row_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Сколько записей в каких таблицах ссылается на `table_name.id = row_id` внешним ключом,
    который не каскадится. Самоссылки самой строки не считаются."""
    blockers: list[dict[str, Any]] = []
    for table in Base.metadata.tables.values():
        for fk in table.foreign_keys:
            if fk.column.table.name != table_name or fk.column.name != "id":
                continue
            if (fk.ondelete or "NO ACTION").upper() in _NON_BLOCKING:
                continue
            stmt = select(func.count()).select_from(table).where(fk.parent == row_id)
            if table.name == table_name:
                stmt = stmt.where(table.c.id != row_id)
            count = int((await session.scalar(stmt)) or 0)
            if count:
                blockers.append(
                    {
                        "code": f"dependent_{table.name}",
                        "detail": f"Есть связанные записи ({table.name}.{fk.parent.name})",
                        "count": count,
                    }
                )
    return blockers
