"""Поведенческие тесты `GET /api/auth/login` и `POST /api/auth/callback` (B9-11).

Раньше реальный протокол (state/nonce/PKCE, обмен кода на токены, сверка id_token и
access_token) не проверялся вообще: `tests/conftest.py::authenticate` заводит сессию в Redis
напрямую и подменяет `decode_access_token` — удобно для остальных тестов, но сам вход через
Keycloak остаётся непокрытым. Keycloak в сети не участвует: `KeycloakClient.exchange_code` и
декодеры токенов (`app.core.security.decode_id_token`/`decode_access_token`) подменены тем же
приёмом, что уже применяет `tests/test_sessions_keycloak.py`."""

from __future__ import annotations

import json

import pytest

from app.core.errors import ErrorCode
from app.core.security import TokenClaims
from app.modules.identity import router_auth
from app.modules.identity.keycloak import TokenResponse
from tests.conftest import TEST_DATABASE_URL, _make_user, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


async def _redis_get(key: str) -> str | None:
    from app.core.redis_client import require_redis

    redis = await require_redis()
    return await redis.get(key)


def _start_login(client, *, next_url: str | None = None) -> tuple[str, dict]:
    """Проходит `GET /api/auth/login` и возвращает (state, содержимое ключа в Redis)."""
    params = {"next": next_url} if next_url else {}
    response = client.get("/api/auth/login", params=params, follow_redirects=False)
    assert response.status_code == 307, response.text
    location = response.headers["location"]
    query = dict(part.split("=", 1) for part in location.split("?", 1)[1].split("&"))
    state = query["state"]
    raw = run(client, _redis_get, router_auth._state_key(state))
    assert raw is not None, "login должен сохранить state в Redis"
    return state, json.loads(raw)


def _patch_token_exchange(monkeypatch, *, subject: str, nonce_ok: bool = True) -> None:
    """Подменяет обмен кода и оба декодера — тем же приёмом, что `test_sessions_keycloak.py`
    уже применяет к `decode_access_token` (`monkeypatch.setattr` на функцию в модуле, а не на
    реальный HTTP-клиент)."""

    async def fake_exchange_code(*, code: str, redirect_uri: str, code_verifier: str | None):
        assert code == "auth-code-1"
        return TokenResponse(
            access_token="at-1",
            refresh_token="rt-1",
            id_token="idt-1",
            expires_in=300,
            session_state="kc-session-1",
            raw={},
        )

    async def fake_decode_id_token(token: str, *, nonce: str | None):
        assert token == "idt-1"
        if not nonce_ok:
            from app.core.errors import AppError

            raise AppError(ErrorCode.UNAUTHENTICATED, "nonce не совпадает")
        return {"sub": subject}

    async def fake_decode_access_token(token: str) -> TokenClaims:
        assert token == "at-1"
        return TokenClaims(
            subject=subject,
            raw={"sub": subject},
            email="user@rt-it-school.ru",
            full_name="Тестовый Пользователь",
            roles=frozenset({"KAM"}),
        )

    monkeypatch.setattr(router_auth.keycloak_client, "exchange_code", fake_exchange_code)
    monkeypatch.setattr(router_auth, "decode_id_token", fake_decode_id_token)
    monkeypatch.setattr(router_auth, "decode_access_token", fake_decode_access_token)


class TestLogin:
    def test_redirects_to_keycloak_with_state_nonce_and_pkce_in_redis(self, client) -> None:
        state, stored = _start_login(client, next_url="/deals/42")
        assert state
        assert stored["nonce"]
        assert stored["code_verifier"]
        assert stored["next"] == "/deals/42"

    def test_unsafe_next_is_not_stored_literally(self, client) -> None:
        # `safe_next_path` — существующая защита от открытого редиректа; здесь только
        # проверяем, что `login` действительно пропускает `next` через неё, а не берёт как есть.
        _state, stored = _start_login(client, next_url="https://evil.example/phish")
        assert stored["next"] != "https://evil.example/phish"


class TestCallback:
    def test_unknown_state_is_rejected(self, client) -> None:
        response = client.post(
            "/api/auth/callback", json={"code": "auth-code-1", "state": "never-issued"}
        )
        assert response.status_code == 401, response.text
        assert response.json()["code"] == "CRM-1101"

    def test_happy_path_sets_session_and_csrf_cookies_and_consumes_state_once(
        self, client, monkeypatch
    ) -> None:
        user = run(client, _make_user, "KAM")
        _patch_token_exchange(monkeypatch, subject=user.keycloak_id)
        state, _stored = _start_login(client)

        response = client.post("/api/auth/callback", json={"code": "auth-code-1", "state": state})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["user"]["id"] == str(user.id)
        assert body["csrf_token"]
        assert "crm_sid" in response.cookies
        assert "crm_csrf" in response.cookies

        # Состояние одноразовое (`redis.getdel` в `_complete_login`) — защита от login-CSRF
        # повтором того же callback-запроса.
        replay = client.post("/api/auth/callback", json={"code": "auth-code-1", "state": state})
        assert replay.status_code == 401, replay.text
        assert replay.json()["code"] == "CRM-1101"

    def test_nonce_mismatch_is_rejected(self, client, monkeypatch) -> None:
        user = run(client, _make_user, "KAM")
        _patch_token_exchange(monkeypatch, subject=user.keycloak_id, nonce_ok=False)
        state, _stored = _start_login(client)

        response = client.post("/api/auth/callback", json={"code": "auth-code-1", "state": state})
        assert response.status_code == 401, response.text
        assert response.json()["code"] == "CRM-1101"

    def test_id_token_and_access_token_subject_mismatch_is_rejected(
        self, client, monkeypatch
    ) -> None:
        """new_spec §3.1 п.4: id_token и access_token обязаны описывать одного субъекта —
        иначе можно подсунуть чужой access_token к своему успешному id_token."""
        user = run(client, _make_user, "KAM")
        _patch_token_exchange(monkeypatch, subject=user.keycloak_id)

        # access_token ссылается на другого субъекта, чем id_token.
        async def fake_decode_access_token_mismatched(token: str) -> TokenClaims:
            return TokenClaims(
                subject="someone-else",
                raw={"sub": "someone-else"},
                email="other@rt-it-school.ru",
                full_name="Другой",
                roles=frozenset({"KAM"}),
            )

        monkeypatch.setattr(router_auth, "decode_access_token", fake_decode_access_token_mismatched)
        state, _stored = _start_login(client)

        response = client.post("/api/auth/callback", json={"code": "auth-code-1", "state": state})
        assert response.status_code == 401, response.text
        assert response.json()["code"] == "CRM-1101"
