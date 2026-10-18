"""B6-11: обновление access-токена серверной сессии под локом — ожидание победителя гонки,
отличение повторного использования refresh-токена (reuse) от обычного истечения.

Keycloak не участвует: `keycloak_client.refresh` подменяется точечно; Redis — `fakeredis`
(фикстура `client` в `tests/conftest.py`).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager

import pytest

from tests.conftest import TEST_DATABASE_URL, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


async def _new_expired_session():
    from app.modules.identity.session_store import session_store

    session = await session_store.create(
        user_id=uuid.uuid4(),
        keycloak_id=str(uuid.uuid4()),
        access_token="old-access-token",
        refresh_token="old-refresh-token",
        id_token=None,
        kc_session_state=None,
        ip="127.0.0.1",
        user_agent="pytest",
        access_expires_at=int(time.time()) - 10,  # уже нуждается в обновлении
    )
    return session


async def _security_events(user_id: str) -> list[dict]:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.identity.models import SecurityEvent

    async with session_scope() as db:
        rows = await db.execute(
            select(SecurityEvent).where(SecurityEvent.user_id == uuid.UUID(user_id))
        )
        return [{"event_type": r.event_type, "severity": r.severity} for r in rows.scalars()]


class TestWaitingForTheWinner:
    def test_a_winner_writing_the_token_soon_is_picked_up(self, client, monkeypatch) -> None:
        from app.modules.identity import session_store as store_module

        @asynccontextmanager
        async def never_acquires(_resource, *, ttl=15):  # noqa: ARG001
            yield False

        monkeypatch.setattr(store_module, "distributed_lock", never_acquires)

        async def _scene() -> str:
            session = await _new_expired_session()

            async def _winner_writes_soon() -> None:
                await asyncio.sleep(0.3)
                session.access_token = "fresh-access-token"
                session.access_expires_at = int(time.time()) + 300
                await store_module.session_store.update(session)

            waiter = asyncio.ensure_future(store_module.ensure_fresh_access_token(session))
            writer = asyncio.ensure_future(_winner_writes_soon())
            try:
                return await waiter
            finally:
                await writer

        token = run(client, _scene)

        assert token == "fresh-access-token"

    def test_no_winner_at_all_falls_back_to_the_stale_token(self, client, monkeypatch) -> None:
        from app.modules.identity import session_store as store_module

        @asynccontextmanager
        async def never_acquires(_resource, *, ttl=15):  # noqa: ARG001
            yield False

        monkeypatch.setattr(store_module, "distributed_lock", never_acquires)
        # Бюджет ожидания короче теста — иначе 2 с реального времени на тест многовато.
        monkeypatch.setattr(store_module, "_REFRESH_WAIT_BUDGET_SECONDS", 0.3)
        monkeypatch.setattr(store_module, "_REFRESH_WAIT_STEP_SECONDS", 0.1)

        async def _scene() -> str:
            session = await _new_expired_session()
            return await store_module.ensure_fresh_access_token(session)

        token = run(client, _scene)

        assert token == "old-access-token"


class TestRefreshReuseDetection:
    def test_a_reuse_signal_from_keycloak_logs_a_security_event_and_deletes_the_session(
        self, client, monkeypatch
    ) -> None:
        from app.modules.identity import session_store as store_module
        from app.modules.identity.keycloak import TokenRefreshError

        async def fake_refresh(_token: str):
            raise TokenRefreshError(
                keycloak_error="invalid_grant", keycloak_error_description="Stale token"
            )

        async def _scene() -> tuple[str, object]:
            from app.core.errors import UnauthenticatedError
            from app.modules.identity.keycloak import keycloak_client
            from app.modules.identity.session_store import session_store

            monkeypatch.setattr(keycloak_client, "refresh", fake_refresh)
            session = await _new_expired_session()
            try:
                await store_module.ensure_fresh_access_token(session)
            except UnauthenticatedError as exc:
                caught = exc
            else:
                caught = None
            remaining = await session_store.get(session.sid)
            return session.user_id, caught, remaining

        user_id, caught, remaining = run(client, _scene)

        assert caught is not None
        assert remaining is None  # сессия удалена
        events = run(client, _security_events, user_id)
        assert [e["event_type"] for e in events] == ["SESSION_HIJACK_SUSPECT"]
        assert events[0]["severity"] == "warning"

    def test_an_ordinary_expiry_does_not_log_a_security_event(self, client, monkeypatch) -> None:
        from app.modules.identity import session_store as store_module
        from app.modules.identity.keycloak import TokenRefreshError

        async def fake_refresh(_token: str):
            raise TokenRefreshError(
                keycloak_error="invalid_grant", keycloak_error_description="Token is expired"
            )

        async def _scene() -> str:
            from app.core.errors import UnauthenticatedError
            from app.modules.identity.keycloak import keycloak_client

            monkeypatch.setattr(keycloak_client, "refresh", fake_refresh)
            session = await _new_expired_session()
            try:
                await store_module.ensure_fresh_access_token(session)
            except UnauthenticatedError:
                pass
            return session.user_id

        user_id = run(client, _scene)

        events = run(client, _security_events, user_id)
        assert events == []
