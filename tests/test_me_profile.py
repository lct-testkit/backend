"""Профиль: `PATCH /api/me` (себе) и телефон в `PATCH /api/admin/users/{id}`.

Телефон нужен внутреннему подписанту: код подтверждения уходит по SMS, иначе
только на email. Сквозные тесты на настоящей PostgreSQL (`TEST_DATABASE_URL`) —
см. докстринг `tests/conftest.py`.
"""

from __future__ import annotations

import json
import uuid

import pytest
from pydantic import ValidationError

from app.modules.identity.schemas import MePatchRequest, UserPatchRequest
from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

PHONE = "+7 (999) 123-45-67"


def _login(client, user) -> None:
    csrf = authenticate(client, user)
    client.headers["X-CSRF-Token"] = csrf


async def _load_user(user_id: uuid.UUID):
    from app.core.db import session_scope
    from app.modules.identity.models import User

    async with session_scope() as session:
        user = await session.get(User, user_id)
        session.expunge(user)
        return user


async def _cached_principal(user_id: uuid.UUID) -> dict | None:
    from app.core.redis_client import get_redis, key_permissions

    raw = await get_redis().get(key_permissions(user_id))
    return json.loads(raw) if raw else None


async def _audit_changes(user_id: uuid.UUID) -> list[dict]:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.audit.models import AuditLog

    async with session_scope() as session:
        rows = await session.execute(
            select(AuditLog.changes, AuditLog.actor_id).where(
                AuditLog.action == "USER_UPDATED", AuditLog.entity_id == user_id
            )
        )
        return [{"changes": changes, "actor_id": actor} for changes, actor in rows]


class TestPhoneValidation:
    @pytest.mark.parametrize("schema", [MePatchRequest, UserPatchRequest])
    @pytest.mark.parametrize("phone", ["+7 (999) 123-45-67", "89991234567", "+7 999 123 45 67"])
    def test_accepts_common_formats(self, schema, phone: str) -> None:
        assert schema(phone=phone).phone == phone

    @pytest.mark.parametrize("schema", [MePatchRequest, UserPatchRequest])
    @pytest.mark.parametrize(
        "phone", ["123", "телефон", "+7 999 12", "9" * 16, "+7 999 abc 45 67", ""]
    )
    def test_rejects_what_cannot_be_dialled(self, schema, phone: str) -> None:
        with pytest.raises(ValidationError):
            schema(phone=phone)

    @pytest.mark.parametrize("schema", [MePatchRequest, UserPatchRequest])
    def test_none_clears_the_phone(self, schema) -> None:
        assert schema(phone=None).phone is None


class TestMePatchSchema:
    def test_empty_body_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            MePatchRequest()

    @pytest.mark.parametrize("field", ["role", "email", "status", "team_id", "full_name"])
    def test_only_profile_fields_are_accepted(self, field: str) -> None:
        with pytest.raises(ValidationError):
            MePatchRequest(display_name="Имя", **{field: "x"})

    def test_timezone_cannot_be_cleared(self) -> None:
        with pytest.raises(ValidationError):
            MePatchRequest(timezone=None)

    def test_blank_display_name_clears_it(self) -> None:
        assert MePatchRequest(display_name="   ").display_name is None


pytestmark_db = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


@pytestmark_db
class TestPatchMe:
    def test_user_edits_own_profile(self, client) -> None:
        user = run(client, _make_user, "KAM")
        _login(client, user)

        response = client.patch(
            "/api/me",
            json={"display_name": "Иван П.", "timezone": "Asia/Yekaterinburg", "phone": PHONE},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["display_name"], body["timezone"], body["phone"]) == (
            "Иван П.",
            "Asia/Yekaterinburg",
            PHONE,
        )
        assert body["version"] == user.version + 1
        stored = run(client, _load_user, user.id)
        assert (stored.display_name, stored.timezone, stored.phone) == (
            "Иван П.",
            "Asia/Yekaterinburg",
            PHONE,
        )
        assert client.get("/api/me").json()["phone"] == PHONE

    def test_a_single_field_leaves_the_others_alone(self, client) -> None:
        user = run(client, _make_user, "KAM")
        _login(client, user)

        response = client.patch("/api/me", json={"phone": PHONE})

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["timezone"] == "Europe/Moscow"
        assert body["display_name"] is None

    def test_phone_can_be_cleared(self, client) -> None:
        user = run(client, _make_user, "KAM")
        _login(client, user)
        client.patch("/api/me", json={"phone": PHONE})

        response = client.patch("/api/me", json={"phone": None})

        assert response.status_code == 200, response.text
        assert response.json()["phone"] is None

    def test_principal_cache_is_dropped_so_the_new_name_shows_up(self, client) -> None:
        user = run(client, _make_user, "KAM")
        _login(client, user)
        client.get("/api/me")
        assert run(client, _cached_principal, user.id)["full_name"] == user.full_name

        assert client.patch("/api/me", json={"display_name": "Ваня"}).status_code == 200

        assert run(client, _cached_principal, user.id) is None
        client.get("/api/me")
        assert run(client, _cached_principal, user.id)["full_name"] == "Ваня"

    def test_login_sync_does_not_undo_the_edit(self, client) -> None:
        # Вход подтягивает из токена только роль (`provision_from_claims`).
        user = run(client, _make_user, "KAM")
        _login(client, user)
        client.patch("/api/me", json={"display_name": "Ваня", "phone": PHONE})
        client.cookies.clear()
        _login(client, user)

        body = client.get("/api/me").json()

        assert (body["display_name"], body["phone"]) == ("Ваня", PHONE)

    def test_edit_is_written_to_the_audit_log_without_the_raw_phone(self, client) -> None:
        user = run(client, _make_user, "KAM")
        _login(client, user)
        client.patch("/api/me", json={"phone": PHONE})

        (entry,) = run(client, _audit_changes, user.id)

        assert entry["actor_id"] == user.id
        assert "phone" in entry["changes"]
        assert "123-45-67" not in json.dumps(entry["changes"], ensure_ascii=False)

    @pytest.mark.parametrize("field", ["role", "email", "status", "full_name"])
    def test_privileged_fields_are_refused(self, client, field: str) -> None:
        user = run(client, _make_user, "KAM")
        _login(client, user)

        response = client.patch("/api/me", json={"display_name": "Ваня", field: "ADMIN"})

        assert response.status_code == 422, response.text
        stored = run(client, _load_user, user.id)
        assert (stored.role, stored.display_name) == ("KAM", None)

    def test_empty_body_and_bad_phone_are_refused(self, client) -> None:
        _login(client, run(client, _make_user, "KAM"))

        assert client.patch("/api/me", json={}).status_code == 422
        assert client.patch("/api/me", json={"phone": "12"}).status_code == 422
        assert client.patch("/api/me", json={"timezone": None}).status_code == 422

    def test_admin_cannot_use_it_for_someone_else(self, client) -> None:
        # Ручка правит только вызывающего: чужой профиль — через администрирование.
        admin = run(client, _make_user, "ADMIN")
        other = run(client, _make_user, "KAM")
        _login(client, admin)

        assert client.patch("/api/me", json={"display_name": "Я"}).status_code == 200

        assert run(client, _load_user, other.id).display_name is None
        assert run(client, _load_user, admin.id).display_name == "Я"


@pytestmark_db
class TestAdminPatchesPhone:
    def _patch(self, client, user, body: dict):
        return client.patch(
            f"/api/admin/users/{user.id}", json=body, headers={"If-Match": str(user.version)}
        )

    def test_admin_sets_the_signer_phone(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        signer = run(client, _make_user, "KAM")

        response = self._patch(client, signer, {"phone": PHONE})

        assert response.status_code == 200, response.text
        assert response.json()["phone"] == PHONE
        assert run(client, _load_user, signer.id).phone == PHONE

    def test_admin_change_is_audited_with_a_masked_phone(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        signer = run(client, _make_user, "KAM")
        self._patch(client, signer, {"phone": PHONE})

        (entry,) = run(client, _audit_changes, signer.id)

        assert "phone" in entry["changes"]
        assert "123-45-67" not in json.dumps(entry["changes"], ensure_ascii=False)

    def test_bad_phone_is_refused(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        signer = run(client, _make_user, "KAM")

        assert self._patch(client, signer, {"phone": "abc"}).status_code == 422
        assert run(client, _load_user, signer.id).phone is None
