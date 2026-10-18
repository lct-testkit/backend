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

import json
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


class TestCacheFormatCompatibility:
    """B4-14 (п.8): формат `cache:perm:{id}` должен переживать поэтапную выкладку B4-6
    без 500-к. `CachedPrincipal.restricted` имеет значение по умолчанию — старая запись
    без этого поля читается как `restricted=False`; ошибка разбора (например, от поля,
    которого эта версия кода ещё не знает) ловится в `get_principal_cache`
    (`core/cache.py`) и даёт промах кэша, а не падение запроса."""

    async def _seed(self, keycloak_id: str, user_id: str, raw: dict) -> None:
        redis = get_redis()
        await redis.set(key_keycloak_map(keycloak_id), user_id)
        await redis.set(key_permissions(user_id), json.dumps(raw))

    @staticmethod
    def _base_entry(user_id: str, keycloak_id: str) -> dict:
        return {
            "user_id": user_id,
            "keycloak_id": keycloak_id,
            "role": "KAM",
            "status": "active",
            "email": "x@rt-it-school.ru",
            "full_name": "Сотрудник",
            "team_id": None,
            "manager_id": None,
            "perm_epoch": 1,
            "consent_version": "1.0",
            "must_change_password": False,
        }

    def test_an_entry_without_restricted_reads_as_not_restricted(self, client) -> None:
        from app.core.cache import get_principal_cache

        keycloak_id, user_id = str(uuid.uuid4()), str(uuid.uuid4())
        # Поля `restricted` нет вовсе — запись до выкладки B4-6.
        run(client, self._seed, keycloak_id, user_id, self._base_entry(user_id, keycloak_id))

        cached = run(client, get_principal_cache, keycloak_id)

        assert cached is not None
        assert cached.restricted is False

    def test_an_entry_with_an_unknown_future_field_is_a_cache_miss_not_a_crash(
        self, client
    ) -> None:
        from app.core.cache import get_principal_cache

        keycloak_id, user_id = str(uuid.uuid4()), str(uuid.uuid4())
        future_entry = {
            **self._base_entry(user_id, keycloak_id),
            "restricted": False,
            "some_field_from_a_future_deploy": "x",
        }
        run(client, self._seed, keycloak_id, user_id, future_entry)

        # Реплика со старым кодом (без этого поля в `CachedPrincipal`) не ловит
        # TypeError и не отвечает 500-кой — `get_principal_cache` перехватывает
        # ошибку разбора сам и отдаёт промах кэша.
        assert run(client, get_principal_cache, keycloak_id) is None


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


class TestPasswordChangeDropsStaleCache:
    """`POST /api/me/password` снимает `must_change_password` в БД — запись `cache:perm:*`
    держит прежнее значение и до конца TTL (5 минут) отвечала бы CRM-1106 на бизнес-ручки,
    хотя пароль уже сменён (описание ручки обещает «сбрасывается кэш прав»)."""

    def test_business_endpoints_open_right_after_a_forced_password_change(
        self, client, monkeypatch
    ) -> None:
        from app.modules.identity import router_me
        from tests.conftest import _make_user

        user = run(client, _make_user, "KAM")

        async def _require_password_change() -> None:
            from app.modules.identity.models import User

            async with session_scope() as session:
                stored = await session.get(User, user.id)
                assert stored is not None
                stored.must_change_password = True

        run(client, _require_password_change)
        client.headers["X-CSRF-Token"] = authenticate(client, user)

        # Первый же запрос кладёт в кэш принципала с must_change_password=True.
        blocked = client.get("/api/deals")
        assert blocked.status_code == 403, blocked.text
        assert blocked.json()["code"] == "CRM-1106"

        async def _ok(*args, **kwargs) -> None:
            return None

        for name in ("password_grant", "set_password", "update_required_actions"):
            monkeypatch.setattr(router_me.keycloak_client, name, _ok)
        monkeypatch.setattr(router_me.keycloak_client, "logout_all_sessions", _ok)

        changed = client.post(
            "/api/me/password",
            json={
                "current_password": "Current-Password-1",
                "new_password": "New-Password-12345",
                "new_password_repeat": "New-Password-12345",
            },
        )
        assert changed.status_code == 200, changed.text

        opened = client.get("/api/deals")
        assert opened.status_code == 200, opened.text
