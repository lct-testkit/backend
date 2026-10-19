"""P-1 (QA 20.10): «Последний вход» в профиле не обновлялся при входе по паролю напрямую
(Bearer, демо-стенд): отметку ставил только OIDC-callback. Теперь — один раз на сессию
Keycloak, а не на каждый запрос."""

from __future__ import annotations

import pytest

from app.core import deps
from app.core.security import TokenClaims
from tests.conftest import TEST_DATABASE_URL, _make_user, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _bearer(client, user, monkeypatch, *, sid: str) -> None:
    async def fake_decode(token: str) -> TokenClaims:
        return TokenClaims(
            subject=user.keycloak_id,
            raw={"sub": user.keycloak_id, "sid": sid, "exp": 4_102_444_800},
            email=user.email,
            full_name=user.full_name,
            roles=frozenset({user.role}),
            session_state=sid,
            expires_at=4_102_444_800,
        )

    monkeypatch.setattr(deps, "decode_access_token", fake_decode)
    client.cookies.clear()
    client.headers["Authorization"] = "Bearer test"


async def _stamp(user_id):
    from app.core.db import session_scope
    from app.modules.identity.models import User

    async with session_scope() as session:
        row = await session.get(User, user_id)
        return row.last_login_at, row.updated_at, row.version


def test_bearer_login_sets_last_login_once_per_kc_session(client, monkeypatch) -> None:
    user = run(client, _make_user, "KAM")
    assert run(client, _stamp, user.id)[0] is None

    _bearer(client, user, monkeypatch, sid="kc-session-1")
    assert client.get("/api/me").status_code == 200
    first, updated_at, version = run(client, _stamp, user.id)
    assert first is not None
    # Не трогаем updated_at/version: иначе токены «устаревают» от каждого входа.
    assert run(client, _stamp, user.id)[1:] == (updated_at, version)

    # Следующие запросы той же сессии отметку не двигают.
    assert client.get("/api/me").status_code == 200
    assert run(client, _stamp, user.id)[0] == first

    # Новый вход (другая сессия Keycloak) — новая отметка.
    _bearer(client, user, monkeypatch, sid="kc-session-2")
    assert client.get("/api/me").status_code == 200
    second = run(client, _stamp, user.id)[0]
    assert second is not None and second > first
