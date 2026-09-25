"""Тесты административных системных ручек (раздел 6.12): настройки и флаги.

Сквозные тесты требуют настоящую PostgreSQL (`TEST_DATABASE_URL`) — см.
докстринг `tests/conftest.py`.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.modules.admin.schemas import SystemSettingPut
from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run


def _admin(client) -> None:
    admin = run(client, _make_user, "ADMIN")
    csrf = authenticate(client, admin)
    client.headers["X-CSRF-Token"] = csrf


async def _stored_value(key: str) -> object:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.admin.models import SystemSetting

    async with session_scope() as session:
        return (
            await session.execute(select(SystemSetting.value).where(SystemSetting.key == key))
        ).scalar_one_or_none()


class TestSecretPlaceholderSchema:
    """`GET /admin/system-settings` подменяет значение секрета маркером `********`;
    отправленный обратно, он затирал бы настоящий секрет."""

    def test_placeholder_is_not_a_value(self) -> None:
        with pytest.raises(ValidationError):
            SystemSettingPut(value="********")

    def test_placeholder_next_to_a_flag_is_still_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SystemSettingPut(value="********", is_secret=True, description="токен")

    @pytest.mark.parametrize("value", ["real-secret", "*******", "********x", 0, None, {"a": 1}])
    def test_anything_else_is_a_value(self, value: object) -> None:
        assert SystemSettingPut(value=value).value == value


class TestPutSystemSetting:
    pytestmark = pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
    )

    def test_placeholder_does_not_overwrite_a_stored_secret(self, client) -> None:
        _admin(client)
        key = f"test.secret.{uuid.uuid4().hex[:8]}"
        created = client.put(
            f"/api/admin/system-settings/{key}", json={"value": "real-secret", "is_secret": True}
        )
        assert created.status_code == 200, created.text

        response = client.put(f"/api/admin/system-settings/{key}", json={"value": "********"})

        assert response.status_code == 422, response.text
        body = response.json()
        assert body["code"] == "CRM-1001"
        assert [error["field"] for error in body["errors"]] == ["value"]
        assert run(client, _stored_value, key) == "real-secret"

    def test_placeholder_is_not_stored_for_a_new_setting(self, client) -> None:
        _admin(client)
        key = f"test.new.{uuid.uuid4().hex[:8]}"

        response = client.put(
            f"/api/admin/system-settings/{key}", json={"value": "********", "is_secret": True}
        )

        assert response.status_code == 422, response.text
        assert run(client, _stored_value, key) is None

    def test_a_real_secret_is_still_masked_in_the_response(self, client) -> None:
        _admin(client)
        key = f"test.masked.{uuid.uuid4().hex[:8]}"

        response = client.put(
            f"/api/admin/system-settings/{key}", json={"value": "token-123", "is_secret": True}
        )

        assert response.status_code == 200, response.text
        assert response.json()["value"] == "********"
        assert run(client, _stored_value, key) == "token-123"
