"""`app.core.field_crypto`: `EncryptedText`/`EncryptedDate` для полей ПДн учащегося (B6-7).

Без БД — чистые проверки шифрования/расшифровки и переходного периода (старый открытый текст
читается как есть, до `tools/encrypt_learner_profiles.py encrypt-existing`)."""

from __future__ import annotations

import datetime as dt

import pytest
from cryptography.fernet import Fernet

from app.core import field_crypto
from app.core.config import get_settings
from app.core.errors import AppError
from app.core.field_crypto import EncryptedDate, EncryptedText, decrypt_text, encrypt_text


@pytest.fixture
def with_key(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("FIELD_ENCRYPTION_KEY", key)
    monkeypatch.delenv("FIELD_ENCRYPTION_KEY_PREV", raising=False)
    get_settings.cache_clear()
    field_crypto.reset_field_crypto_cache()
    yield key
    get_settings.cache_clear()
    field_crypto.reset_field_crypto_cache()


@pytest.fixture
def without_key(monkeypatch):
    monkeypatch.delenv("FIELD_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("FIELD_ENCRYPTION_KEY_PREV", raising=False)
    get_settings.cache_clear()
    field_crypto.reset_field_crypto_cache()
    yield
    get_settings.cache_clear()
    field_crypto.reset_field_crypto_cache()


class _Dialect:
    """Заглушка диалекта: `process_bind_param`/`process_result_value` его не используют."""


class TestTextRoundTrip:
    def test_round_trip(self, with_key) -> None:
        assert decrypt_text(encrypt_text("112-233-445 95")) == "112-233-445 95"

    def test_two_encryptions_of_the_same_value_differ(self, with_key) -> None:
        a = encrypt_text("112-233-445 95")
        b = encrypt_text("112-233-445 95")
        assert a != b  # Fernet-токен несёт случайный nonce

    def test_ciphertext_does_not_contain_the_plaintext(self, with_key) -> None:
        assert "112-233-445" not in encrypt_text("112-233-445 95")

    def test_without_a_key_value_is_stored_as_before(self, without_key) -> None:
        assert encrypt_text("112-233-445 95") == "112-233-445 95"
        assert decrypt_text("112-233-445 95") == "112-233-445 95"

    def test_legacy_plaintext_is_readable_with_a_key(self, with_key) -> None:
        """Переходный период: значение без признака токена — старый открытый текст."""
        assert decrypt_text("старое-открытое-значение") == "старое-открытое-значение"

    def test_encrypted_value_without_the_key_is_an_explicit_error(
        self, with_key, monkeypatch
    ) -> None:
        token = encrypt_text("112-233-445 95")
        monkeypatch.delenv("FIELD_ENCRYPTION_KEY")
        get_settings.cache_clear()
        field_crypto.reset_field_crypto_cache()
        with pytest.raises(AppError):
            decrypt_text(token)

    def test_old_key_is_readable_with_prev_and_not_without_it(self, monkeypatch) -> None:
        old_key = Fernet.generate_key().decode()
        monkeypatch.setenv("FIELD_ENCRYPTION_KEY", old_key)
        get_settings.cache_clear()
        field_crypto.reset_field_crypto_cache()
        token = encrypt_text("112-233-445 95")

        new_key = Fernet.generate_key().decode()
        monkeypatch.setenv("FIELD_ENCRYPTION_KEY", new_key)
        monkeypatch.setenv("FIELD_ENCRYPTION_KEY_PREV", old_key)
        get_settings.cache_clear()
        field_crypto.reset_field_crypto_cache()
        assert decrypt_text(token) == "112-233-445 95"

        monkeypatch.delenv("FIELD_ENCRYPTION_KEY_PREV")
        get_settings.cache_clear()
        field_crypto.reset_field_crypto_cache()
        with pytest.raises(AppError):
            decrypt_text(token)

    def test_corrupted_token_is_an_error_without_leaking_the_value(self, with_key) -> None:
        token = encrypt_text("112-233-445 95")
        corrupted = token[:-4] + "xxxx"
        with pytest.raises(AppError) as exc:
            decrypt_text(corrupted)
        assert "112-233-445" not in str(exc.value)


class TestEncryptedTypes:
    def test_text_type_encrypts_on_bind_and_decrypts_on_result(self, with_key) -> None:
        col = EncryptedText()
        bound = col.process_bind_param("значение", _Dialect())
        assert bound is not None and bound.startswith("gAAAA")
        assert col.process_result_value(bound, _Dialect()) == "значение"

    def test_text_type_passes_through_none(self, with_key) -> None:
        col = EncryptedText()
        assert col.process_bind_param(None, _Dialect()) is None
        assert col.process_result_value(None, _Dialect()) is None

    def test_date_type_round_trip(self, with_key) -> None:
        col = EncryptedDate()
        value = dt.date(1990, 5, 17)
        bound = col.process_bind_param(value, _Dialect())
        assert bound is not None and bound.startswith("gAAAA")
        assert col.process_result_value(bound, _Dialect()) == value

    def test_date_type_rejects_non_date(self, with_key) -> None:
        col = EncryptedDate()
        with pytest.raises(TypeError):
            col.process_bind_param("17.05.1990", _Dialect())

    def test_date_type_reads_legacy_iso_plaintext(self, with_key) -> None:
        """Переходный период: старая колонка `date` после ALTER COLUMN хранит ISO-строку без
        токена (миграция 0029 не перешифровывает существующие значения)."""
        col = EncryptedDate()
        assert col.process_result_value("1990-05-17", _Dialect()) == dt.date(1990, 5, 17)

    def test_without_a_key_types_store_plaintext(self, without_key) -> None:
        text_col = EncryptedText()
        assert text_col.process_bind_param("значение", _Dialect()) == "значение"
        date_col = EncryptedDate()
        bound = date_col.process_bind_param(dt.date(1990, 5, 17), _Dialect())
        assert bound == "1990-05-17"
        assert date_col.process_result_value(bound, _Dialect()) == dt.date(1990, 5, 17)


def test_invalid_key_is_an_explicit_error_at_startup(monkeypatch) -> None:
    """B6-8: невалидный Fernet-ключ должен ронять старт процесса (field_validator в `Settings`),
    а не первое шифрование."""
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,  # type: ignore[arg-type]
            app_profile="dev",
            database_url="postgresql+asyncpg://crm:crm@db:5432/crm",
            redis_url="redis://redis:6379/0",
            keycloak_url="http://kc:8080/auth",
            keycloak_realm="crm",
            keycloak_client_id="crm-bff",
            keycloak_client_secret="secret",
            s3_endpoint_url="http://s3:8333",
            s3_access_key="a",
            s3_secret_key="b",
            crm_app_password="crm_app_password",
            signature_server_secret="test-secret",
            field_encryption_key="не-fernet-ключ",
        )
