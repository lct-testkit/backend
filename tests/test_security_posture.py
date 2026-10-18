"""B6-11: `GET /admin/security/posture` — что из декларируемых мер реально включено.

Keycloak не участвует: `has_otp`/`realm_posture` подменяются точечно. Redis — `fakeredis`
(кэш на 60 с, `app.core.redis_client.key_security_posture`).
"""

from __future__ import annotations

import pytest

from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

_REALM_SAMPLE = {
    "bruteForceProtected": True,
    "revokeRefreshToken": True,
    "eventsEnabled": False,
    "sslRequired": "external",
}


def _login(client, user) -> None:
    client.headers["X-CSRF-Token"] = authenticate(client, user)


def _mock_keycloak(monkeypatch, *, otp_by_id: dict[str, bool], realm: dict | None = _REALM_SAMPLE):
    from app.modules.identity import admin_service

    async def has_otp(keycloak_id: str) -> bool:
        return otp_by_id.get(keycloak_id, False)

    async def realm_posture() -> dict | None:
        return realm

    monkeypatch.setattr(admin_service.keycloak_client, "has_otp", has_otp)
    monkeypatch.setattr(admin_service.keycloak_client, "realm_posture", realm_posture)


class TestPermissions:
    def test_admin_and_auditor_can_read_head_and_kam_cannot(self, client, monkeypatch) -> None:
        _mock_keycloak(monkeypatch, otp_by_id={})
        for role, expected in (("ADMIN", 200), ("AUDITOR", 200), ("HEAD", 403), ("KAM", 403)):
            _login(client, run(client, _make_user, role))
            response = client.get("/api/admin/security/posture")
            assert response.status_code == expected, (role, response.text)


class TestAdminsWithoutTotp:
    def test_an_admin_without_otp_is_listed_one_with_otp_is_not(self, client, monkeypatch) -> None:
        with_otp = run(client, _make_user, "ADMIN")
        without_otp = run(client, _make_user, "ADMIN")
        _mock_keycloak(
            monkeypatch,
            otp_by_id={with_otp.keycloak_id: True, without_otp.keycloak_id: False},
        )
        reader = run(client, _make_user, "AUDITOR")
        _login(client, reader)

        response = client.get("/api/admin/security/posture")

        assert response.status_code == 200, response.text
        listed = {item["user_id"] for item in response.json()["admins_without_totp"]}
        assert str(without_otp.id) in listed
        assert str(with_otp.id) not in listed

    def test_head_without_otp_is_also_listed(self, client, monkeypatch) -> None:
        head = run(client, _make_user, "HEAD")
        _mock_keycloak(monkeypatch, otp_by_id={head.keycloak_id: False})
        _login(client, run(client, _make_user, "ADMIN"))

        response = client.get("/api/admin/security/posture")

        listed = {item["user_id"] for item in response.json()["admins_without_totp"]}
        assert str(head.id) in listed

    def test_blocked_admins_are_not_counted(self, client, monkeypatch) -> None:
        blocked = run(client, _make_user, "ADMIN", "blocked")
        _mock_keycloak(monkeypatch, otp_by_id={})
        _login(client, run(client, _make_user, "ADMIN"))

        response = client.get("/api/admin/security/posture")

        listed = {item["user_id"] for item in response.json()["admins_without_totp"]}
        assert str(blocked.id) not in listed


class TestRealmUnavailable:
    def test_keycloak_down_gives_a_200_with_an_empty_realm_not_a_500(
        self, client, monkeypatch
    ) -> None:
        run(client, _make_user, "ADMIN")  # шумовая запись: не должна упасть на has_otp
        _mock_keycloak(monkeypatch, otp_by_id={}, realm=None)
        _login(client, run(client, _make_user, "ADMIN"))

        response = client.get("/api/admin/security/posture")

        assert response.status_code == 200, response.text
        assert response.json()["realm"] is None


class TestResponseShape:
    def test_bearer_mode_and_antivirus_and_keys_are_reported(self, client, monkeypatch) -> None:
        _mock_keycloak(monkeypatch, otp_by_id={})
        _login(client, run(client, _make_user, "ADMIN"))

        body = client.get("/api/admin/security/posture").json()

        assert body["bearer_auth_mode"] in {"all", "integration_only", "off"}
        assert set(body["antivirus"]) == {"enabled", "fail_mode", "reachable"}
        assert isinstance(body["audit_hmac_key_set"], bool)
        assert isinstance(body["field_encryption_enabled"], bool)
        assert body["realm"] == {
            "brute_force_protected": True,
            "revoke_refresh_token": True,
            "events_enabled": False,
            "ssl_required": "external",
        }


class TestCaching:
    def test_a_second_call_within_a_minute_does_not_hit_keycloak_again(
        self, client, monkeypatch
    ) -> None:
        # БД не очищается между тестами — считаем прирост вызовов, а не абсолютное число:
        # сколько всего ADMIN/HEAD накопилось к этому моменту в сессии тестов, не важно.
        admin = run(client, _make_user, "ADMIN")
        calls = {"has_otp": 0, "realm_posture": 0}

        from app.modules.identity import admin_service

        async def has_otp(_keycloak_id: str) -> bool:
            calls["has_otp"] += 1
            return True

        async def realm_posture() -> dict:
            calls["realm_posture"] += 1
            return _REALM_SAMPLE

        monkeypatch.setattr(admin_service.keycloak_client, "has_otp", has_otp)
        monkeypatch.setattr(admin_service.keycloak_client, "realm_posture", realm_posture)
        _login(client, admin)

        first = client.get("/api/admin/security/posture")
        after_first = dict(calls)
        second = client.get("/api/admin/security/posture")

        assert first.status_code == second.status_code == 200
        assert first.json() == second.json()
        assert after_first["has_otp"] > 0  # первый запрос действительно сходил в Keycloak
        # Второй запрос обслужен из кэша Redis (60 с) — ни одного нового вызова.
        assert calls == after_first
