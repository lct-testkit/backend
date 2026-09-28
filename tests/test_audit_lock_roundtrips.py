"""Регрессия: под глобальным advisory-локом цепочки аудита — не больше двух запросов (и COMMIT).

Контекст (перф-диагностика 28.09, живой стенд, 50 RPS, сценарий «комментарий»): в очереди за
локом `AuditService._lock_chain()` стояло 15–47 сессий, p50/p95 запроса — 240/690 мс при пороге
300. Сами запросы под локом стоят доли миллисекунды; время набегало от числа обращений между ними:
после каждого `await` управление возвращается в цикл событий с десятками других корутин, и каждое
обращение стоило около 3 мс, а лок держат все остальные писатели. Раньше после захвата лока шли
возврат `lock_timeout`, чтение головы и чтение времени тремя отдельными запросами (плюс INSERT и
UPDATE — пять; шестое обращение — COMMIT). Теперь после лока — один запрос (возврат `lock_timeout`,
голова, время) и один `WITH ins AS (INSERT ...) UPDATE ...` (запись + указатель головы).
"""

from __future__ import annotations

import pytest

from tests.conftest import TEST_DATABASE_URL, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

_MAX_STATEMENTS_UNDER_LOCK = 2


async def _statements_after_lock() -> list[str]:
    from sqlalchemy import event

    from app.core.db import get_engine, get_session_factory
    from app.modules.audit.service import AuditService

    seen: list[str] = []

    def spy(conn, cursor, statement, parameters, context, executemany):
        seen.append(" ".join(statement.split()))

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", spy)
    try:
        factory = get_session_factory()
        async with factory() as session:
            await AuditService(session).record("LOCK_ROUNDTRIPS_TEST", entity_type="hash_test")
            await session.commit()
    finally:
        event.remove(engine, "before_cursor_execute", spy)

    lock_index = next(i for i, sql in enumerate(seen) if "pg_advisory_xact_lock" in sql)
    return seen[lock_index + 1 :]


def test_record_runs_at_most_two_statements_under_the_chain_lock(client) -> None:
    after_lock = run(client, _statements_after_lock)

    assert len(after_lock) <= _MAX_STATEMENTS_UNDER_LOCK, after_lock
    # Первое обращение под локом читает голову и время одним запросом, а не двумя.
    assert "audit_chain_head" in after_lock[0]
    assert "clock_timestamp()" in after_lock[0]
    # Запись и продвижение указателя головы — одним запросом, а не INSERT + UPDATE.
    assert "INSERT INTO audit_log" in after_lock[-1]
    assert "UPDATE audit_chain_head" in after_lock[-1]
