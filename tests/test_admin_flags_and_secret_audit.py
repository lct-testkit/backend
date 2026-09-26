"""Флаги фич можно создавать; `null` в PATCH флага — 422; секрет не попадает в аудит.

Найдено внешним тестированием: флаги нельзя было завести (только PATCH существующих, а миграции
и seed их не создавали); `{"is_enabled": null}` давал 500 на NOT NULL; при снятии признака
«секретная» прежнее значение настройки уходило в неизменяемый журнал открытым текстом.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _admin(client) -> None:
    admin = run(client, _make_user, "ADMIN")
    client.headers["X-CSRF-Token"] = authenticate(client, admin)


def _flag_code() -> str:
    return f"flag_{uuid.uuid4().hex[:10]}"


async def _audit_changes_for_setting(key: str) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.audit.models import AuditLog

    async with session_scope() as session:
        rows = await session.scalars(
            select(AuditLog)
            .where(AuditLog.action == "SYSTEM_SETTING_CHANGED")
            .order_by(AuditLog.created_at)
        )
        return [
            row.changes or {}
            for row in rows
            if (row.changes or {}).get("key", {}).get("new") == key
        ]


class TestCreateFeatureFlag:
    def test_created_and_listed(self, client) -> None:
        _admin(client)
        code = _flag_code()

        created = client.post(
            "/api/admin/feature-flags",
            json={"code": code, "is_enabled": True, "rollout": 30, "description": "проверка"},
        )

        assert created.status_code == 201, created.text
        body = created.json()
        assert (body["code"], body["is_enabled"], body["rollout"]) == (code, True, 30)
        listed = client.get("/api/admin/feature-flags?limit=100").json()["items"]
        assert code in {flag["code"] for flag in listed}

    def test_defaults_are_off_and_full_rollout(self, client) -> None:
        _admin(client)

        body = client.post("/api/admin/feature-flags", json={"code": _flag_code()}).json()

        assert (body["is_enabled"], body["rollout"]) == (False, 100)

    def test_duplicate_code_is_409(self, client) -> None:
        _admin(client)
        code = _flag_code()
        assert client.post("/api/admin/feature-flags", json={"code": code}).status_code == 201

        again = client.post("/api/admin/feature-flags", json={"code": code})

        assert again.status_code == 409, again.text
        assert again.json()["code"] == "CRM-1301"

    @pytest.mark.parametrize("code", ["Bad Code", "1abc", "a", "x" * 65, ""])
    def test_bad_code_is_422(self, client, code: str) -> None:
        _admin(client)
        assert client.post("/api/admin/feature-flags", json={"code": code}).status_code == 422

    def test_rollout_is_bounded(self, client) -> None:
        _admin(client)
        response = client.post(
            "/api/admin/feature-flags", json={"code": _flag_code(), "rollout": 101}
        )
        assert response.status_code == 422

    def test_kam_cannot_create(self, client) -> None:
        user = run(client, _make_user, "KAM")
        client.headers["X-CSRF-Token"] = authenticate(client, user)

        response = client.post("/api/admin/feature-flags", json={"code": _flag_code()})

        assert response.status_code == 403


class TestPatchFeatureFlag:
    @pytest.mark.parametrize("field", ["is_enabled", "rollout"])
    def test_null_is_422_not_500(self, client, field: str) -> None:
        _admin(client)
        code = _flag_code()
        client.post("/api/admin/feature-flags", json={"code": code})

        response = client.patch(f"/api/admin/feature-flags/{code}", json={field: None})

        assert response.status_code == 422, response.text

    def test_description_can_be_cleared(self, client) -> None:
        _admin(client)
        code = _flag_code()
        client.post("/api/admin/feature-flags", json={"code": code, "description": "было"})

        response = client.patch(f"/api/admin/feature-flags/{code}", json={"description": None})

        assert response.status_code == 200, response.text
        assert response.json()["description"] is None


class TestSecretSettingAudit:
    def test_old_value_is_not_logged_when_the_secret_flag_is_removed(self, client) -> None:
        _admin(client)
        key = f"test.audit.{uuid.uuid4().hex[:8]}"
        client.put(
            f"/api/admin/system-settings/{key}", json={"value": "старый-секрет", "is_secret": True}
        )

        client.put(
            f"/api/admin/system-settings/{key}",
            json={"value": "новое-значение", "is_secret": False},
        )

        entries = run(client, _audit_changes_for_setting, key)
        assert len(entries) == 2
        dumped = str(entries)
        assert "старый-секрет" not in dumped
        assert "новое-значение" not in dumped

    def test_rotation_of_a_secret_is_visible_but_not_readable(self, client) -> None:
        _admin(client)
        key = f"test.rotate.{uuid.uuid4().hex[:8]}"
        client.put(f"/api/admin/system-settings/{key}", json={"value": "one", "is_secret": True})
        client.put(f"/api/admin/system-settings/{key}", json={"value": "two"})

        entries = run(client, _audit_changes_for_setting, key)

        assert entries[-1]["value"] == {"old": "***", "new": "***"}
        assert "two" not in str(entries[-1])

    def test_plain_setting_keeps_its_values_in_the_audit(self, client) -> None:
        _admin(client)
        key = f"test.plain.{uuid.uuid4().hex[:8]}"
        client.put(f"/api/admin/system-settings/{key}", json={"value": "a"})
        client.put(f"/api/admin/system-settings/{key}", json={"value": "b"})

        entries = run(client, _audit_changes_for_setting, key)

        assert entries[-1]["value"] == {"old": "a", "new": "b"}
