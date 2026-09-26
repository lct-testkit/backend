"""Цепочка аудита при параллельных транзакциях: голова выбирается по времени ЗАПИСИ под локом, а не
по времени начала транзакции.

Раньше `created_at` брался из `now()` — времени старта транзакции. Долгая транзакция, начавшаяся
раньше, но получившая лок позже, писала запись «в прошлое» с `prev_hash` более поздней записи:
порядок по `created_at` и порядок хэшей расходились, цепочка рвалась и ветвилась.
"""

from __future__ import annotations

import asyncio
import random

import pytest

from tests.conftest import TEST_DATABASE_URL, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


async def _late_writer_after_early_transaction() -> dict:
    """A открывает транзакцию первой (её `now()` раньше), но пишет в аудит уже после того, как B
    записал и зафиксировал свою запись."""
    from sqlalchemy import text

    from app.core.db import get_session_factory
    from app.modules.audit.service import AuditService

    factory = get_session_factory()
    session_a = factory()
    session_b = factory()
    try:
        await session_a.execute(text("SELECT 1"))  # транзакция A началась
        await asyncio.sleep(0.05)
        await AuditService(session_b).record("CHAIN_TEST_B", entity_type="chain_test")
        await session_b.commit()
        await AuditService(session_a).record("CHAIN_TEST_A", entity_type="chain_test")
        await session_a.commit()
    finally:
        await session_a.close()
        await session_b.close()

    async with factory() as session:
        return await AuditService(session).verify_chain(limit=3)


def test_late_writer_with_an_early_transaction_keeps_the_chain_intact(client) -> None:
    result = run(client, _late_writer_after_early_transaction)
    assert result["ok"], result["problems"]


async def _hammer(writers: int) -> dict:
    from sqlalchemy import text

    from app.core.db import get_session_factory
    from app.modules.audit.service import AuditService

    factory = get_session_factory()

    async def writer(index: int) -> None:
        async with factory() as session:
            await session.execute(text("SELECT 1"))  # старт транзакции задолго до записи
            await asyncio.sleep(random.random() * 0.2)
            await AuditService(session).record(f"CHAIN_TEST_{index}", entity_type="chain_test")
            await session.commit()

    await asyncio.gather(*(writer(i) for i in range(writers)))
    async with factory() as session:
        return await AuditService(session).verify_chain(limit=writers + 1)


def test_parallel_writers_do_not_fork_or_break_the_chain(client) -> None:
    result = run(client, _hammer, 40)
    assert result["ok"], result["problems"][:5]
    assert result["checked"] >= 40
