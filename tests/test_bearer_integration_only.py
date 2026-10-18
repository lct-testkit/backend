"""B6-10: в prod (`bearer_auth_mode == "integration_only"`) вход по `Authorization: Bearer`
разрешён только роли INTEGRATION — и по роли из JWT (отказ до JIT-provisioning), и по
фактической роли пользователя (токен мог быть выпущен до смены роли).

Профиль prod в тестах не поднимается (он требует боевых секретов): режим подменяется
свойством `Settings.bearer_auth_mode`."""

from __future__ import annotations

import pytest

from app.core import deps
from app.core.config import Settings
from app.core.security import TokenClaims
from tests.conftest import TEST_DATABASE_URL, _make_user, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _set_mode(monkeypatch, mode: str) -> None:
    monkeypatch.setattr(Settings, "bearer_auth_mode", property(lambda self: mode))


def _bearer_with_claimed_role(client, user, claimed_role: str, monkeypatch) -> None:
    async def fake_decode(token: str) -> TokenClaims:
        return TokenClaims(
            subject=user.keycloak_id,
            raw={"sub": user.keycloak_id},
            email=user.email,
            full_name=user.full_name,
            roles=frozenset({claimed_role}),
            session_state="kc-test",
        )

    monkeypatch.setattr(deps, "decode_access_token", fake_decode)
    client.cookies.clear()
    client.headers["Authorization"] = "Bearer test"


class TestIntegrationOnlyBearer:
    def test_a_human_role_is_refused(self, client, monkeypatch) -> None:
        user = run(client, _make_user, "KAM")
        _set_mode(monkeypatch, "integration_only")
        _bearer_with_claimed_role(client, user, "KAM", monkeypatch)

        response = client.get("/api/me")

        assert response.status_code == 401, response.text

    def test_the_service_role_is_let_in(self, client, monkeypatch) -> None:
        user = run(client, _make_user, "INTEGRATION")
        _set_mode(monkeypatch, "integration_only")
        _bearer_with_claimed_role(client, user, "INTEGRATION", monkeypatch)

        response = client.get("/api/me")

        assert response.status_code == 200, response.text

    def test_a_stale_integration_claim_does_not_let_a_changed_role_in(
        self, client, monkeypatch
    ) -> None:
        """Пользователь в БД уже KAM (кэш прав это знает), а токен несёт устаревшую роль
        INTEGRATION и claim `perm_epoch` не содержит: проверка только по JWT его бы пропустила."""
        user = run(client, _make_user, "KAM")
        _set_mode(monkeypatch, "all")
        _bearer_with_claimed_role(client, user, "KAM", monkeypatch)
        assert client.get("/api/me").status_code == 200  # прогрев кэша прав ролью KAM

        _set_mode(monkeypatch, "integration_only")
        _bearer_with_claimed_role(client, user, "INTEGRATION", monkeypatch)
        response = client.get("/api/me")

        assert response.status_code == 401, response.text
