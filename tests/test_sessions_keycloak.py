"""Контроль сессий в режиме Bearer: список строится по сессиям Keycloak (CRM: обращение №35).

Без cookie-сессии в Redis панель показывала «других сессий нет», хотя пользователь вошёл на
нескольких устройствах. Тесты не ходят в сеть: Keycloak-клиент подменён."""

from __future__ import annotations

import pytest

from app.core import deps
from app.core.security import TokenClaims
from app.modules.identity import router_me
from app.modules.identity.router_me import keycloak_session_info
from tests.conftest import TEST_DATABASE_URL, _make_user, run

pytestmark_db = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

KC_SESSIONS = [
    {
        "id": "kc-a",
        "ipAddress": "10.0.0.1",
        "start": 1_700_000_000_000,
        "lastAccess": 1_700_000_600_000,
    },
    {
        "id": "kc-b",
        "ipAddress": "10.0.0.2",
        "start": 1_700_100_000_000,
        "lastAccess": 1_700_100_600_000,
    },
]


def test_keycloak_session_is_shown_like_a_server_one() -> None:
    info = keycloak_session_info(KC_SESSIONS[0], current_state="kc-a")
    assert info.sid == "kc-kc-a"
    assert info.is_current is True
    assert info.ip == "10.0.0.1"
    assert info.created_at.startswith("2023-11-14")
    assert keycloak_session_info(KC_SESSIONS[1], current_state="kc-a").is_current is False
    assert keycloak_session_info(KC_SESSIONS[1], current_state=None).is_current is False


def _bearer(client, user, state: str, monkeypatch) -> None:
    async def fake_decode(token: str) -> TokenClaims:
        return TokenClaims(
            subject=user.keycloak_id,
            raw={"sub": user.keycloak_id},
            email=user.email,
            full_name=user.full_name,
            roles=frozenset({user.role}),
            session_state=state,
        )

    monkeypatch.setattr(deps, "decode_access_token", fake_decode)
    client.cookies.clear()
    client.headers["Authorization"] = "Bearer test"


@pytestmark_db
class TestBearerSessions:
    def test_list_shows_both_logins_and_marks_the_current(self, client, monkeypatch) -> None:
        user = run(client, _make_user, "KAM")

        async def _list(_kc_id: str) -> list[dict]:
            return KC_SESSIONS

        monkeypatch.setattr(router_me.keycloak_client, "list_user_sessions", _list)
        _bearer(client, user, "kc-b", monkeypatch)

        items = client.get("/api/me/sessions").json()["items"]

        assert [i["sid"] for i in items] == ["kc-kc-a", "kc-kc-b"]
        assert [i["is_current"] for i in items] == [False, True]

    def test_other_login_can_be_ended(self, client, monkeypatch) -> None:
        user = run(client, _make_user, "KAM")
        ended: list[str] = []

        async def _list(_kc_id: str) -> list[dict]:
            return KC_SESSIONS

        async def _delete(kc_session_id: str) -> None:
            ended.append(kc_session_id)

        monkeypatch.setattr(router_me.keycloak_client, "list_user_sessions", _list)
        monkeypatch.setattr(router_me.keycloak_client, "delete_session", _delete)
        _bearer(client, user, "kc-b", monkeypatch)

        assert client.delete("/api/me/sessions/kc-kc-a").status_code == 200
        assert ended == ["kc-a"]

    def test_a_session_that_is_not_yours_is_not_found(self, client, monkeypatch) -> None:
        user = run(client, _make_user, "KAM")
        ended: list[str] = []

        async def _list(_kc_id: str) -> list[dict]:
            return KC_SESSIONS

        async def _delete(kc_session_id: str) -> None:
            ended.append(kc_session_id)

        monkeypatch.setattr(router_me.keycloak_client, "list_user_sessions", _list)
        monkeypatch.setattr(router_me.keycloak_client, "delete_session", _delete)
        _bearer(client, user, "kc-b", monkeypatch)

        assert client.delete("/api/me/sessions/kc-someone-else").status_code == 404
        assert ended == []

    def test_keycloak_down_gives_an_empty_list_not_an_error(self, client, monkeypatch) -> None:
        user = run(client, _make_user, "KAM")

        async def _boom(_kc_id: str) -> list[dict]:
            raise RuntimeError("keycloak is down")

        monkeypatch.setattr(router_me.keycloak_client, "list_user_sessions", _boom)
        _bearer(client, user, "kc-b", monkeypatch)

        response = client.get("/api/me/sessions")

        assert response.status_code == 200
        assert response.json()["items"] == []


@pytestmark_db
class TestTerminateOthers:
    def test_bearer_ends_every_login_except_the_current(self, client, monkeypatch) -> None:
        user = run(client, _make_user, "KAM")
        ended: list[str] = []

        async def _list(_kc_id: str) -> list[dict]:
            return KC_SESSIONS

        async def _delete(kc_session_id: str) -> None:
            ended.append(kc_session_id)

        monkeypatch.setattr(router_me.keycloak_client, "list_user_sessions", _list)
        monkeypatch.setattr(router_me.keycloak_client, "delete_session", _delete)
        _bearer(client, user, "kc-b", monkeypatch)

        response = client.post("/api/me/sessions/terminate-others")

        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True, "terminated": 1}
        assert ended == ["kc-a"]  # текущая kc-b не тронута

    def test_repeat_with_nothing_left_is_a_noop(self, client, monkeypatch) -> None:
        user = run(client, _make_user, "KAM")

        async def _list(_kc_id: str) -> list[dict]:
            return [KC_SESSIONS[1]]

        monkeypatch.setattr(router_me.keycloak_client, "list_user_sessions", _list)
        _bearer(client, user, "kc-b", monkeypatch)

        response = client.post("/api/me/sessions/terminate-others")

        assert response.status_code == 200
        assert response.json()["terminated"] == 0

    def test_cookie_mode_keeps_the_current_and_ends_the_others_in_keycloak(
        self, client, monkeypatch
    ) -> None:
        from app.modules.identity.session_store import session_store
        from tests.conftest import authenticate

        logouts: list[str] = []
        deleted_kc: list[str] = []

        async def _logout(refresh_token: str) -> None:
            logouts.append(refresh_token)

        async def _list(_kc_id: str) -> list[dict]:
            # вход, которого нет среди серверных; после удаления Keycloak его уже не отдаёт
            return [] if deleted_kc else [KC_SESSIONS[0]]

        async def _delete(kc_session_id: str) -> None:
            deleted_kc.append(kc_session_id)

        monkeypatch.setattr(router_me.keycloak_client, "logout", _logout)
        monkeypatch.setattr(router_me.keycloak_client, "list_user_sessions", _list)
        monkeypatch.setattr(router_me.keycloak_client, "delete_session", _delete)
        user = run(client, _make_user, "KAM")
        client.headers["X-CSRF-Token"] = authenticate(client, user)

        async def _second() -> None:
            await session_store.create(
                user_id=user.id,
                keycloak_id=user.keycloak_id,
                access_token="a",
                refresh_token="refresh-second",
                id_token=None,
                kc_session_state=None,
                ip="127.0.0.1",
                user_agent="pytest",
            )

        run(client, _second)

        response = client.post("/api/me/sessions/terminate-others")

        assert response.status_code == 200, response.text
        assert response.json()["terminated"] == 2
        assert logouts == ["refresh-second"]
        assert deleted_kc == ["kc-a"]
        left = client.get("/api/me/sessions").json()["items"]
        assert [i["is_current"] for i in left] == [True]  # текущая жива
        assert client.post("/api/me/sessions/terminate-others").json()["terminated"] == 0

    def test_audit_event_is_written(self, client, monkeypatch) -> None:
        from sqlalchemy import select

        from app.core.db import session_scope
        from app.modules.audit.models import AuditLog

        user = run(client, _make_user, "KAM")

        async def _list(_kc_id: str) -> list[dict]:
            return KC_SESSIONS

        async def _delete(_kc_session_id: str) -> None:
            return None

        monkeypatch.setattr(router_me.keycloak_client, "list_user_sessions", _list)
        monkeypatch.setattr(router_me.keycloak_client, "delete_session", _delete)
        _bearer(client, user, "kc-b", monkeypatch)
        client.post("/api/me/sessions/terminate-others")

        async def _count() -> int:
            async with session_scope() as session:
                rows = await session.execute(
                    select(AuditLog).where(
                        AuditLog.entity_id == user.id, AuditLog.action == "SESSION_TERMINATED"
                    )
                )
                return len(rows.scalars().all())

        assert run(client, _count) == 1
