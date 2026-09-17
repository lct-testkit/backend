"""Сквозные проверки цепочки аутентификации.

Эти тесты поднимают приложение целиком и требуют настоящую PostgreSQL:
без неё нечем проверить транзакционность аудита, отложенную запись отказов
и реакцию на блокировку. Redis подменяется in-memory реализацией,
Keycloak — подставным декодером токена.

Запуск:

    createdb crm_test
    DATABASE_URL=postgresql+asyncpg://user@localhost:5432/crm_test alembic upgrade head
    TEST_DATABASE_URL=postgresql+asyncpg://user@localhost:5432/crm_test pytest tests/

Без `TEST_DATABASE_URL` тесты пропускаются, поэтому обычный прогон
остаётся полностью офлайновым.

Асинхронные помощники выполняются через `client.portal`: у TestClient свой
событийный цикл, и движок БД с Redis обязаны жить именно в нём.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from typing import Any

import pytest

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

fakeredis = pytest.importorskip("fakeredis", reason="нужен fakeredis для подмены Redis")


def run(client, func, *args: Any) -> Any:
    """Выполняет корутину в событийном цикле TestClient."""
    return client.portal.call(func, *args)


@pytest.fixture
def client(monkeypatch):
    """Приложение с подменёнными Redis и проверкой токена."""
    os.environ.setdefault("APP_PROFILE", "dev")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL or ""
    os.environ["REDIS_URL"] = "redis://127.0.0.1:6379/0"
    # Только loopback и только IP: имя хоста уходит в системный резолвер, и
    # под VPN или в закрытом контуре разрешение имени зависает дольше любого
    # таймаута httpx. Порт 9 (discard) отказывает в соединении мгновенно.
    os.environ["KEYCLOAK_URL"] = "http://127.0.0.1:9/auth"
    os.environ.setdefault("KEYCLOAK_REALM", "crm")
    os.environ.setdefault("KEYCLOAK_CLIENT_ID", "crm-bff")
    os.environ.setdefault("KEYCLOAK_CLIENT_SECRET", "secret")
    os.environ["S3_ENDPOINT_URL"] = "http://127.0.0.1:9"
    os.environ.setdefault("S3_ACCESS_KEY", "a")
    os.environ.setdefault("S3_SECRET_KEY", "b")
    os.environ.setdefault("SIGNATURE_SERVER_SECRET", "test-secret")

    from fastapi.testclient import TestClient

    from app.core import redis_client
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    monkeypatch.setattr(
        redis_client, "_client", fakeredis.aioredis.FakeRedis(decode_responses=True)
    )

    with TestClient(create_app()) as test_client:
        yield test_client


async def _make_user(role: str = "KAM", status: str = "active"):
    """Создаёт пользователя напрямую в БД: Keycloak в тестах не участвует."""
    from app.core.db import session_scope
    from app.modules.identity.models import User

    async with session_scope() as session:
        user = User(
            keycloak_id=str(uuid.uuid4()),
            email=f"{uuid.uuid4().hex[:12]}@rt-it-school.ru",
            full_name="Иванов Иван Иванович",
            role=role,
            status=status,
            consent_version="1.0",
        )
        session.add(user)
        await session.flush()
        session.expunge(user)
        return user


async def _open_session(user) -> str:
    from app.modules.identity.session_store import session_store

    stored = await session_store.create(
        user_id=user.id,
        keycloak_id=user.keycloak_id,
        access_token="access",
        refresh_token=None,
        id_token=None,
        kc_session_state=None,
        ip="127.0.0.1",
        user_agent="pytest",
    )
    return stored.sid


def authenticate(client, user) -> str:
    """Кладёт сессию в Redis и подменяет декодер токена. Возвращает CSRF-токен."""
    from app.core import deps
    from app.core.csrf import new_csrf_token
    from app.core.security import TokenClaims

    async def fake_decode(token: str) -> TokenClaims:
        return TokenClaims(
            subject=user.keycloak_id,
            raw={"sub": user.keycloak_id},
            email=user.email,
            full_name=user.full_name,
            roles=frozenset({user.role}),
        )

    deps.decode_access_token = fake_decode  # type: ignore[assignment]

    sid = run(client, _open_session, user)
    csrf = new_csrf_token()
    client.cookies.set("crm_sid", sid)
    client.cookies.set("crm_csrf", csrf)
    return csrf


class TestAuthChain:
    def test_unauthenticated_request_is_problem_json(self, client) -> None:
        response = client.get("/api/me")
        assert response.status_code == 401
        body = response.json()
        assert body["code"] == "CRM-1101"
        assert response.headers["content-type"].startswith("application/problem+json")
        assert body["request_id"]

    def test_health_live_is_open(self, client) -> None:
        assert client.get("/health/live").status_code == 200

    def test_session_gives_access(self, client) -> None:
        user = run(client, _make_user)
        authenticate(client, user)
        response = client.get("/api/me")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["role"] == "KAM"
        assert body["consent_required"] is False
        assert body["password_change_required"] is False

    def test_mutating_request_without_csrf_is_rejected(self, client) -> None:
        user = run(client, _make_user)
        authenticate(client, user)
        client.cookies.delete("crm_csrf")
        response = client.post(
            "/api/me/consent",
            json={"policy_version": "1.0", "policy_text_hash": "a" * 64},
        )
        assert response.status_code == 403
        assert response.json()["code"] == "CRM-1107"

    def test_mutating_request_with_csrf_passes(self, client) -> None:
        user = run(client, _make_user)
        csrf = authenticate(client, user)
        response = client.post(
            "/api/me/consent",
            json={"policy_version": "1.0", "policy_text_hash": "a" * 64},
            headers={"X-CSRF-Token": csrf},
        )
        assert response.status_code == 201, response.text


class TestScopeAndAudit:
    def test_kam_cannot_read_admin_users(self, client) -> None:
        user = run(client, _make_user, "KAM")
        authenticate(client, user)
        response = client.get("/api/admin/users")
        assert response.status_code == 403
        assert response.json()["code"] == "CRM-1102"

    def test_denied_access_is_written_to_audit(self, client) -> None:
        """Отказ пишется отдельной транзакцией уже после ответа.

        Раньше он писался вложенной транзакцией при удерживаемом
        advisory-локе цепочки аудита — это давало взаимную блокировку.
        """
        from sqlalchemy import func, select

        from app.core.db import session_scope
        from app.modules.audit.models import AuditLog

        user = run(client, _make_user, "KAM")
        authenticate(client, user)

        async def count_denied() -> int:
            async with session_scope() as session:
                return int(
                    (
                        await session.execute(
                            select(func.count(AuditLog.id)).where(
                                AuditLog.action == "ACCESS_DENIED",
                                AuditLog.actor_id == user.id,
                            )
                        )
                    ).scalar_one()
                )

        before = run(client, count_denied)
        assert client.get("/api/admin/users").status_code == 403
        assert run(client, count_denied) == before + 1

    def test_admin_sees_user_list(self, client) -> None:
        admin = run(client, _make_user, "ADMIN")
        authenticate(client, admin)
        response = client.get("/api/admin/users?limit=5")
        assert response.status_code == 200, response.text
        payload = response.json()
        assert "items" in payload and "next_cursor" in payload

    def test_audit_chain_stays_valid(self, client) -> None:
        admin = run(client, _make_user, "ADMIN")
        authenticate(client, admin)
        response = client.get("/api/admin/audit/verify-chain?limit=200")
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is True

    def test_audit_list_is_available_to_admin(self, client) -> None:
        admin = run(client, _make_user, "ADMIN")
        authenticate(client, admin)
        response = client.get("/api/admin/audit?limit=5")
        assert response.status_code == 200, response.text
        assert "items" in response.json()


class TestSessionLifecycle:
    def test_blocked_user_loses_access_immediately(self, client) -> None:
        from sqlalchemy import update

        from app.core.cache import invalidate_principal
        from app.core.db import session_scope
        from app.modules.identity.models import User

        user = run(client, _make_user)
        authenticate(client, user)
        assert client.get("/api/me").status_code == 200

        async def block() -> None:
            async with session_scope() as session:
                await session.execute(
                    update(User).where(User.id == user.id).values(status="blocked")
                )
            await invalidate_principal(user.id, keycloak_id=user.keycloak_id)

        run(client, block)
        response = client.get("/api/me")
        assert response.status_code == 403
        assert response.json()["code"] == "CRM-1104"

    def test_idle_session_is_dropped(self, client) -> None:
        from app.modules.identity.session_store import session_store

        user = run(client, _make_user)
        authenticate(client, user)
        sid = client.cookies["crm_sid"]

        async def age_session() -> None:
            stored = await session_store.get(sid)
            assert stored is not None
            stored.last_seen_at = (
                dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
            ).isoformat()
            await session_store.update(stored)

        run(client, age_session)
        response = client.get("/api/me")
        assert response.status_code == 401
        assert response.json()["code"] == "CRM-1101"

    def test_session_list_hides_tokens(self, client) -> None:
        user = run(client, _make_user)
        authenticate(client, user)
        response = client.get("/api/me/sessions")
        assert response.status_code == 200
        items = response.json()["items"]
        assert items and items[0]["is_current"] is True
        assert "access_token" not in items[0]
