"""Кэш принципала (`cache:perm:*`, `cache:kcid:*`) пишется после коммита (A#25,
A#28, B#30 из `frontend/docs/backend-issues.md`).

JIT-пользователь создаётся в транзакции первого же запроса. Если запрос
отклонён (например, 403 CRM-1105 «нужно согласие» от защищённой ручки), строка
`users` откатывается вместе с ним — а кэш, записанный до коммита, остаётся и
указывает на несуществующего пользователя: `GET /api/me` отвечал 500
`NoResultFound` до истечения TTL. Настоящая Postgres обязательна — см.
`tests/conftest.py`.
"""

from __future__ import annotations

import types
import uuid

import pytest

from app.core.cache import key_keycloak_map
from app.core.db import run_after_commit, session_scope
from app.core.redis_client import get_redis, key_permissions
from tests.conftest import TEST_DATABASE_URL, authenticate, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _new_employee(client) -> types.SimpleNamespace:
    """Сотрудник, которого в БД ещё нет: первый же запрос заводит его JIT."""
    employee = types.SimpleNamespace(
        id=uuid.uuid4(),
        keycloak_id=str(uuid.uuid4()),
        email=f"{uuid.uuid4().hex[:12]}@rt-it-school.ru",
        full_name="Новый Сотрудник",
        role="KAM",
    )
    authenticate(client, employee)
    return employee


def _cached_user_id(client, keycloak_id: str) -> str | None:
    async def _read() -> str | None:
        return await get_redis().get(key_keycloak_map(keycloak_id))

    return run(client, _read)


class TestJitUserAndPrincipalCache:
    def test_rejected_first_request_leaves_no_ghost_in_the_cache(self, client) -> None:
        employee = _new_employee(client)

        # Защищённая ручка: согласия на ПДн у нового пользователя ещё нет.
        denied = client.get("/api/deals")
        assert denied.status_code == 403, denied.text
        assert denied.json()["code"] == "CRM-1105"
        assert _cached_user_id(client, employee.keycloak_id) is None

        me = client.get("/api/me")
        assert me.status_code == 200, me.text
        assert me.json()["email"] == employee.email

    def test_cache_is_written_once_the_request_committed(self, client) -> None:
        employee = _new_employee(client)

        assert client.get("/api/me").status_code == 200

        user_id = _cached_user_id(client, employee.keycloak_id)
        assert user_id is not None

        async def _permissions_cached() -> bool:
            return bool(await get_redis().get(key_permissions(user_id)))

        assert run(client, _permissions_cached)


class TestRunAfterCommit:
    async def test_action_runs_after_a_successful_commit(self) -> None:
        calls: list[str] = []

        async def action() -> None:
            calls.append("done")

        async with session_scope() as session:
            run_after_commit(session, action)
            assert calls == []  # транзакция ещё открыта
        assert calls == ["done"]

    async def test_action_is_skipped_when_the_request_fails(self) -> None:
        calls: list[str] = []

        async def action() -> None:
            calls.append("done")

        async def rejected_request() -> None:
            async with session_scope() as session:
                run_after_commit(session, action)
                raise RuntimeError("запрос отклонён")

        with pytest.raises(RuntimeError):
            await rejected_request()
        assert calls == []

    async def test_failing_action_does_not_break_the_committed_request(self) -> None:
        async def broken() -> None:
            raise ConnectionError("Redis недоступен")

        async with session_scope() as session:
            run_after_commit(session, broken)
