"""B6-10: пароль по ссылке приглашения/сброса (`POST /api/auth/invite/{token}/password`),
результат письма Keycloak при сбросе (`reset-password`) и повторном приглашении (`invite`),
дефолт `ALLOW_BEARER_AUTH=false`.

Keycloak не участвует: `keycloak_client` подменяется точечно (тот же приём, что
`tests/test_identity_admin.py`). Токен — plaintext строка, в БД хранится только её sha256
(`hash_token`), как и в проде.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import functools
import uuid

import pytest

from app.modules.identity.service import hash_token
from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

_BODY = {"new_password": "Длинный-Пароль-2026", "new_password_repeat": "Длинный-Пароль-2026"}


async def _insert_invite(
    *,
    user_id: uuid.UUID,
    token: str,
    purpose: str = "invite",
    minutes_left: int = 60,
    used: bool = False,
    revoked: bool = False,
) -> None:
    from app.core.db import session_scope
    from app.modules.identity.models import UserInvite

    async with session_scope() as session:
        now = dt.datetime.now(dt.UTC)
        session.add(
            UserInvite(
                user_id=user_id,
                token_hash=hash_token(token),
                purpose=purpose,
                expires_at=now + dt.timedelta(minutes=minutes_left),
                used_at=now if used else None,
                revoked_at=now if revoked else None,
            )
        )


def _with_keycloak_user(client) -> object:
    # `_make_user` уже заводит случайный `keycloak_id` (Keycloak в тестах не участвует).
    return run(client, _make_user, "KAM")


def _mock_keycloak(monkeypatch, *, set_password_fails: bool = False):
    from app.core.errors import AppError, ErrorCode, FieldError
    from app.modules.identity import router_auth

    calls: dict[str, list] = {"set_password": [], "update_required_actions": [], "update_user": []}

    async def set_password(keycloak_id: str, *, password: str, temporary: bool = False) -> None:
        calls["set_password"].append((keycloak_id, password, temporary))
        if set_password_fails:
            raise AppError(
                ErrorCode.VALIDATION,
                "Пароль не соответствует политике безопасности",
                errors=[FieldError(field="new_password", reason="не соответствует политике")],
            )

    async def update_required_actions(keycloak_id: str, *, add=(), remove=()) -> None:
        calls["update_required_actions"].append((keycloak_id, tuple(add), tuple(remove)))

    async def update_user(keycloak_id: str, payload: dict) -> None:
        calls["update_user"].append((keycloak_id, dict(payload)))

    monkeypatch.setattr(router_auth.keycloak_client, "set_password", set_password)
    monkeypatch.setattr(
        router_auth.keycloak_client, "update_required_actions", update_required_actions
    )
    monkeypatch.setattr(router_auth.keycloak_client, "update_user", update_user)
    return calls


async def _invite_row(token: str):
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.identity.models import UserInvite

    async with session_scope() as session:
        row = (
            await session.execute(
                select(UserInvite).where(UserInvite.token_hash == hash_token(token))
            )
        ).scalar_one()
        session.expunge(row)
        return row


async def _user_row(user_id: uuid.UUID):
    from app.core.db import session_scope
    from app.modules.identity.models import User

    async with session_scope() as session:
        row = await session.get(User, user_id)
        session.expunge(row)
        return row


class TestSetInvitePassword:
    def test_a_valid_token_sets_the_password_and_burns_the_invite(self, client, monkeypatch):
        calls = _mock_keycloak(monkeypatch)
        user = _with_keycloak_user(client)
        token = "a" * 32
        run(client, functools.partial(_insert_invite, user_id=user.id, token=token))

        response = client.post(f"/api/auth/invite/{token}/password", json=_BODY)

        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert len(calls["set_password"]) == 1
        assert calls["set_password"][0][1] == _BODY["new_password"]
        removed = calls["update_required_actions"][0][2]
        assert set(removed) == {"UPDATE_PASSWORD", "VERIFY_EMAIL"}
        assert calls["update_user"][0][1] == {"emailVerified": True}

        updated_user = run(client, _user_row, user.id)
        assert updated_user.must_change_password is False
        assert updated_user.password_changed_at is not None
        invite = run(client, _invite_row, token)
        assert invite.used_at is not None

        second = client.post(f"/api/auth/invite/{token}/password", json=_BODY)
        assert second.status_code == 404, second.text

    def test_a_password_policy_violation_from_keycloak_is_a_422_without_the_password(
        self, client, monkeypatch, caplog
    ):
        _mock_keycloak(monkeypatch, set_password_fails=True)
        user = _with_keycloak_user(client)
        token = "b" * 32
        run(client, functools.partial(_insert_invite, user_id=user.id, token=token))

        response = client.post(f"/api/auth/invite/{token}/password", json=_BODY)

        assert response.status_code == 422, response.text
        assert _BODY["new_password"] not in response.text
        invite = run(client, _invite_row, token)
        assert invite.used_at is None  # токен не сожжён отказавшей попыткой

    def test_an_unknown_token_gives_a_generic_404(self, client, monkeypatch):
        _mock_keycloak(monkeypatch)

        response = client.post("/api/auth/invite/" + "z" * 32 + "/password", json=_BODY)

        assert response.status_code == 404, response.text

    def test_an_expired_token_gives_the_same_404(self, client, monkeypatch):
        _mock_keycloak(monkeypatch)
        user = _with_keycloak_user(client)
        token = "c" * 32
        run(
            client,
            functools.partial(_insert_invite, user_id=user.id, token=token, minutes_left=-5),
        )

        response = client.post(f"/api/auth/invite/{token}/password", json=_BODY)

        assert response.status_code == 404, response.text

    def test_an_already_used_token_gives_the_same_404(self, client, monkeypatch):
        _mock_keycloak(monkeypatch)
        user = _with_keycloak_user(client)
        token = "d" * 32
        run(client, functools.partial(_insert_invite, user_id=user.id, token=token, used=True))

        response = client.post(f"/api/auth/invite/{token}/password", json=_BODY)

        assert response.status_code == 404, response.text

    def test_a_reset_purpose_token_also_sets_the_password(self, client, monkeypatch):
        _mock_keycloak(monkeypatch)
        user = _with_keycloak_user(client)
        token = "e" * 32
        run(
            client,
            functools.partial(_insert_invite, user_id=user.id, token=token, purpose="reset"),
        )

        response = client.post(f"/api/auth/invite/{token}/password", json=_BODY)

        assert response.status_code == 200, response.text

    def test_setting_the_password_burns_the_other_pending_links_of_the_user(
        self, client, monkeypatch
    ):
        """Приглашение и ссылка сброса живут независимо: вторая, неиспользованная, после смены
        пароля по первой не должна позволять сменить его снова."""
        _mock_keycloak(monkeypatch)
        user = _with_keycloak_user(client)
        invite_token, reset_token = "h" * 32, "i" * 32
        run(
            client,
            functools.partial(_insert_invite, user_id=user.id, token=invite_token),
        )
        run(
            client,
            functools.partial(_insert_invite, user_id=user.id, token=reset_token, purpose="reset"),
        )

        first = client.post(f"/api/auth/invite/{invite_token}/password", json=_BODY)
        second = client.post(f"/api/auth/invite/{reset_token}/password", json=_BODY)

        assert first.status_code == 200, first.text
        assert second.status_code == 404, second.text
        assert run(client, _invite_row, reset_token).used_at is not None

    def test_two_concurrent_requests_on_the_same_token_give_exactly_one_success(
        self, client, monkeypatch
    ):
        calls = _mock_keycloak(monkeypatch)
        user = _with_keycloak_user(client)
        token = "f" * 32
        run(client, functools.partial(_insert_invite, user_id=user.id, token=token))

        async def _one() -> int:
            from httpx import ASGITransport, AsyncClient

            # Тот же экземпляр приложения, что уже поднят фикстурой `client` (не новый
            # `create_app()`): иначе второй клиент работал бы с несвязанным приложением.
            transport = ASGITransport(app=client.app)
            async with AsyncClient(transport=transport, base_url="http://test") as http:
                resp = await http.post(f"/api/auth/invite/{token}/password", json=_BODY)
                return resp.status_code

        async def _both() -> list[int]:
            return list(await asyncio.gather(_one(), _one()))

        results = run(client, _both)

        assert sorted(results) == [200, 404]
        assert len(calls["set_password"]) == 1


class TestResetPasswordLink:
    def _admin(self, client):
        admin = run(client, _make_user, "ADMIN")
        client.headers["X-CSRF-Token"] = authenticate(client, admin)
        return admin

    def test_without_smtp_the_response_carries_a_reset_link(self, client, monkeypatch):
        from app.modules.identity import admin_service

        async def no_email(*_args: object, **_kwargs: object) -> bool:
            return False

        async def noop(*_args: object, **_kwargs: object) -> None:
            return None

        monkeypatch.setattr(admin_service.AdminUserService, "send_reset_email", no_email)
        monkeypatch.setattr(admin_service.keycloak_client, "update_required_actions", noop)
        monkeypatch.setattr(admin_service.keycloak_client, "logout_all_sessions", noop)
        self._admin(client)
        target = _with_keycloak_user(client)

        response = client.post(
            f"/api/admin/users/{target.id}/reset-password",
            json={"reason": "забыл пароль"},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["email_sent"] is False
        assert body["reset_url"] and "/invite/" in body["reset_url"]
        assert body["reset_expires_at"]

    def test_with_smtp_the_response_has_no_link(self, client, monkeypatch):
        from app.modules.identity import admin_service

        async def yes_email(*_args: object, **_kwargs: object) -> bool:
            return True

        async def noop(*_args: object, **_kwargs: object) -> None:
            return None

        # Письмо шлёт сам backend (`send_reset_email`), а не Keycloak.
        monkeypatch.setattr(admin_service.AdminUserService, "send_reset_email", yes_email)
        monkeypatch.setattr(admin_service.keycloak_client, "update_required_actions", noop)
        monkeypatch.setattr(admin_service.keycloak_client, "logout_all_sessions", noop)
        self._admin(client)
        target = _with_keycloak_user(client)

        response = client.post(
            f"/api/admin/users/{target.id}/reset-password",
            json={"reason": "забыл пароль"},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["email_sent"] is True
        assert body["reset_url"] is None

    def test_a_nonexistent_user_gives_the_same_generic_response(self, client, monkeypatch):
        self._admin(client)

        response = client.post(
            f"/api/admin/users/{uuid.uuid4()}/reset-password",
            json={"reason": "забыл пароль"},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["email_sent"] is False
        assert body["reset_url"] is None
        assert body["detail"] == "Если учётная запись существует, сброс запущен"


class TestResendInviteEmailResult:
    def _invited(self, client):
        # `_make_user` уже заводит случайный `keycloak_id`.
        return run(client, _make_user, "KAM", "invited")

    def test_invite_email_sent_reflects_the_actual_smtp_result(self, client, monkeypatch):
        from app.modules.identity import admin_service

        admin = run(client, _make_user, "ADMIN")
        client.headers["X-CSRF-Token"] = authenticate(client, admin)
        target = self._invited(client)

        async def yes_email(*_args: object, **_kwargs: object) -> bool:
            return True

        monkeypatch.setattr(admin_service.AdminUserService, "send_invite_email", yes_email)

        response = client.post(f"/api/admin/users/{target.id}/invite")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["invite_email_sent"] is True
        assert body["invite_url"] is None


class TestBearerDefaultIsOff:
    def test_the_code_default_is_false(self) -> None:
        # Поле читается у класса, а не у сконструированного `Settings()`: любой тест в
        # процессе (включая этот файл) мог уже дёрнуть фикстуру `client`, которая явно
        # заводит `ALLOW_BEARER_AUTH=true` в `os.environ` для тестов, которым нужен Bearer —
        # конструирование `Settings()` подхватило бы это значение, а не дефолт кода.
        from app.core.config import Settings

        assert Settings.model_fields["allow_bearer_auth"].default is False

    def test_bearer_token_is_rejected_outright_when_disabled(self, client, monkeypatch) -> None:
        from app.core.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings, "allow_bearer_auth", False)
        client.cookies.clear()
        client.headers["Authorization"] = "Bearer whatever"

        response = client.get("/api/me")

        assert response.status_code == 401, response.text
