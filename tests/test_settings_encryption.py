"""Секретные системные настройки хранятся зашифрованными (если задан `SETTINGS_ENCRYPTION_KEY`).

Раньше `is_secret` лишь маскировал значение в ответе API: в таблице секрет лежал открытым текстом.
Без ключа поведение прежнее (открытый текст + предупреждение в логе) — включение шифрования не
ломает уже развёрнутые стенды.
"""

from __future__ import annotations

import uuid

import pytest
from cryptography.fernet import Fernet

from app.core.errors import AppError
from app.modules.admin import setting_crypto
from app.modules.admin.setting_crypto import (
    decrypt_value,
    encrypt_value,
    is_encrypted,
    reset_secrets_config,
)
from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import login


@pytest.fixture
def with_key(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", key)
    reset_secrets_config()
    yield key
    reset_secrets_config()


@pytest.fixture
def without_key(monkeypatch):
    monkeypatch.delenv("SETTINGS_ENCRYPTION_KEY", raising=False)
    monkeypatch.setattr(
        setting_crypto,
        "_config",
        lambda: setting_crypto._SecretsConfig(settings_encryption_key=None),
    )


class TestCrypto:
    def test_round_trip_keeps_any_json_value(self, with_key) -> None:
        for value in ("токен-абв", {"a": [1, 2, {"b": None}]}, 42, ["x"], True):
            stored = encrypt_value(value)
            assert is_encrypted(stored)
            assert decrypt_value(stored) == value

    def test_ciphertext_does_not_contain_the_plaintext(self, with_key) -> None:
        stored = encrypt_value("очень-секретный-токен")
        assert "очень-секретный-токен" not in str(stored)

    def test_without_a_key_the_value_is_stored_as_before(self, without_key) -> None:
        assert encrypt_value("plain") == "plain"
        assert decrypt_value("plain") == "plain"

    def test_legacy_plaintext_is_readable_with_a_key(self, with_key) -> None:
        assert decrypt_value("старое-открытое-значение") == "старое-открытое-значение"
        assert decrypt_value({"обычный": "json"}) == {"обычный": "json"}

    def test_encrypted_value_without_the_key_is_an_error_not_garbage(
        self, with_key, monkeypatch
    ) -> None:
        stored = encrypt_value("x")
        monkeypatch.delenv("SETTINGS_ENCRYPTION_KEY")
        reset_secrets_config()
        with pytest.raises(AppError):
            decrypt_value(stored)

    def test_wrong_key_is_an_error(self, with_key, monkeypatch) -> None:
        stored = encrypt_value("x")
        monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", Fernet.generate_key().decode())
        reset_secrets_config()
        with pytest.raises(AppError):
            decrypt_value(stored)

    def test_encrypting_twice_does_not_double_wrap(self, with_key) -> None:
        once = encrypt_value("x")
        assert encrypt_value(once) == once

    def test_garbage_key_is_an_explicit_error(self, monkeypatch) -> None:
        monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", "не-ключ")
        reset_secrets_config()
        try:
            with pytest.raises(AppError):
                encrypt_value("x")
        finally:
            reset_secrets_config()


pytestmark_db = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _stored_value(client, key: str):
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.admin.models import SystemSetting

    async def _load():
        async with session_scope() as session:
            return await session.scalar(select(SystemSetting.value).where(SystemSetting.key == key))

    return run(client, _load)


@pytestmark_db
class TestSecretSettingsOverHttp:
    def test_secret_is_encrypted_in_the_table_and_masked_in_the_api(self, client, with_key) -> None:
        login(client)
        key = f"test.enc.{uuid.uuid4().hex[:8]}"

        response = client.put(
            f"/api/admin/system-settings/{key}",
            json={"value": "супер-секрет-123", "is_secret": True},
        )

        assert response.status_code == 200, response.text
        assert response.json()["value"] == "********"
        stored = _stored_value(client, key)
        assert is_encrypted(stored)
        assert "супер-секрет-123" not in str(stored)
        assert decrypt_value(stored) == "супер-секрет-123"
        listed = client.get("/api/admin/system-settings", params={"prefix": key}).json()["items"]
        assert listed[0]["value"] == "********"

    def test_non_secret_setting_stays_plain(self, client, with_key) -> None:
        login(client)
        key = f"test.plain.{uuid.uuid4().hex[:8]}"
        client.put(f"/api/admin/system-settings/{key}", json={"value": "открыто"})
        assert _stored_value(client, key) == "открыто"

    def test_without_a_key_a_secret_is_stored_as_before(self, client, without_key) -> None:
        login(client)
        key = f"test.nokey.{uuid.uuid4().hex[:8]}"

        response = client.put(
            f"/api/admin/system-settings/{key}", json={"value": "как-раньше", "is_secret": True}
        )

        assert response.status_code == 200, response.text
        assert _stored_value(client, key) == "как-раньше"

    def test_dropping_the_secret_flag_stores_the_new_value_in_the_clear(
        self, client, with_key
    ) -> None:
        login(client)
        key = f"test.unflag.{uuid.uuid4().hex[:8]}"
        client.put(f"/api/admin/system-settings/{key}", json={"value": "один", "is_secret": True})

        client.put(f"/api/admin/system-settings/{key}", json={"value": "два", "is_secret": False})

        assert _stored_value(client, key) == "два"

    def test_rewriting_the_same_secret_is_not_reported_as_a_change(self, client, with_key) -> None:
        from sqlalchemy import select

        from app.core.db import session_scope
        from app.modules.audit.models import AuditLog

        login(client)
        key = f"test.same.{uuid.uuid4().hex[:8]}"
        client.put(f"/api/admin/system-settings/{key}", json={"value": "тот-же", "is_secret": True})
        client.put(f"/api/admin/system-settings/{key}", json={"value": "тот-же"})

        async def _entries() -> list[dict]:
            async with session_scope() as session:
                rows = await session.scalars(
                    select(AuditLog)
                    .where(AuditLog.action == "SYSTEM_SETTING_CHANGED")
                    .order_by(AuditLog.created_at)
                )
                return [
                    r.changes or {}
                    for r in rows
                    if (r.changes or {}).get("key", {}).get("new") == key
                ]

        entries = run(client, _entries)
        assert len(entries) == 2
        assert "value" not in entries[1]  # шифротекст другой, а значение то же: изменения нет

    def test_existing_plaintext_secrets_can_be_encrypted_in_place(
        self, client, without_key
    ) -> None:
        login(client)
        key = f"test.legacy.{uuid.uuid4().hex[:8]}"
        client.put(f"/api/admin/system-settings/{key}", json={"value": "легаси", "is_secret": True})
        assert _stored_value(client, key) == "легаси"

        # Ключ появился позже.
        import os

        os.environ["SETTINGS_ENCRYPTION_KEY"] = Fernet.generate_key().decode()
        setting_crypto._config = lambda: setting_crypto._SecretsConfig()  # type: ignore[assignment]
        try:
            changed = run(client, setting_crypto.encrypt_existing_secrets)
            assert changed >= 1
            stored = _stored_value(client, key)
            assert is_encrypted(stored) and decrypt_value(stored) == "легаси"
            # Идемпотентно.
            assert run(client, setting_crypto.encrypt_existing_secrets) == 0
        finally:
            del os.environ["SETTINGS_ENCRYPTION_KEY"]
