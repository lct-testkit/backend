"""Оптимистичная блокировка: версия строки занимается одним UPDATE, а не проверкой в Python.

Сравнение `entity.version != expected_version` в коде сервиса ловит только «запоздавший»
запрос: два запроса, которые прочитали строку одной версии, оба проходят проверку и оба
пишут — последний молча затирает первого (потерянное обновление). `claim_version`
переносит проверку в саму запись: `UPDATE ... SET version = version + 1 WHERE id = :id AND
version = :expected`. Второй запрос встаёт на блокировке строки, дожидается коммита первого,
перечитывает условие и не находит строку — 409 CRM-1002, а не тихая перезапись. Строка
остаётся заблокированной до конца транзакции, поэтому остальные изменения запроса ложатся
поверх той версии, которую он проверил.

Вызывать надо ДО изменений полей (или хотя бы до `flush`): при `autoflush=False` ничего
не уходит в БД раньше времени, а конфликт обнаруживается прежде, чем запрос успеет
что-нибудь записать.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.core.errors import VersionConflictError


async def claim_version(
    session: AsyncSession,
    entity: Any,
    expected_version: int | None = None,
    *,
    conflicting: dict[str, object] | None = None,
) -> int:
    """Атомарно увеличивает `version` строки и возвращает новое значение.

    Без `expected_version` версия просто растёт (массовые операции и системные правки, у
    которых нет версии от клиента) — но и тогда приращение относительное, а не
    «прочитанное + 1»: параллельная правка не откатывает счётчик назад.
    """
    model = type(entity)
    stmt = update(model).where(model.id == entity.id)
    if expected_version is not None:
        stmt = stmt.where(model.version == expected_version)
    stmt = (
        stmt.values(version=model.version + 1)
        .returning(model.version)
        .execution_options(synchronize_session=False)
    )
    new_version = (await session.execute(stmt)).scalar_one_or_none()
    if new_version is None:
        current = await session.scalar(select(model.version).where(model.id == entity.id))
        raise VersionConflictError(
            int(current) if current is not None else int(expected_version or 0), conflicting
        )
    # Не через присваивание: атрибут не должен стать «грязным» и уйти в БД вторым UPDATE.
    set_committed_value(entity, "version", int(new_version))
    return int(new_version)
