"""APP_PROFILE=prod не стартует с демо-секретами (config._reject_demo_secrets_in_prod)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings

STRONG = {
    "signature_server_secret": "Zx9-strong-signature-secret-0123456789",
    "keycloak_client_secret": "Kc-strong-client-secret-0123456789",
    "keycloak_admin_client_secret": "Kc-strong-admin-secret-0123456789",
    "s3_access_key": "S3-strong-access-key-0123456789",
    "s3_secret_key": "S3-strong-secret-key-0123456789",
    "crm_app_password": "Db-strong-app-password-0123456789",
}


def _settings(profile: str, **overrides: str) -> Settings:
    base = {
        "app_profile": profile,
        "database_url": "postgresql+asyncpg://crm_app:Db-strong-app-password-0123456789@db:5432/crm",
        "redis_url": "redis://redis:6379/0",
        "keycloak_url": "http://kc:8080/auth",
        "keycloak_realm": "crm",
        "keycloak_client_id": "crm-bff",
        "s3_endpoint_url": "http://s3:8333",
        **STRONG,
        **overrides,
    }
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def test_prod_accepts_strong_secrets() -> None:
    assert _settings("prod").is_prod


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("signature_server_secret", "change-me-in-prod"),
        ("keycloak_client_secret", "crm-bff-secret"),
        ("keycloak_admin_client_secret", "crm-admin-secret"),
        ("s3_access_key", "crm_access"),
        ("s3_secret_key", "crm_secret_key"),
        ("crm_app_password", "crm_app"),
        ("signature_server_secret", "short"),
    ],
)
def test_prod_rejects_demo_or_short_secret(field: str, value: str) -> None:
    with pytest.raises(ValidationError) as exc:
        _settings("prod", **{field: value})
    assert field.upper() in str(exc.value)


@pytest.mark.parametrize("profile", ["dev", "demo"])
def test_non_prod_keeps_demo_secrets(profile: str) -> None:
    settings = _settings(profile, signature_server_secret="change-me-in-prod")
    assert not settings.is_prod


def test_prod_rejects_demo_password_inside_database_url() -> None:
    """Пароль роли приложения сидит и в строке подключения: демо-значение там тоже отвергается."""
    with pytest.raises(ValidationError) as exc:
        _settings("prod", database_url="postgresql+asyncpg://crm_app:crm_app@db:5432/crm")
    assert "DATABASE_URL" in str(exc.value)


def test_prod_rejects_short_audit_hmac_key() -> None:
    with pytest.raises(ValidationError) as exc:
        _settings("prod", audit_hmac_key="short")
    assert "AUDIT_HMAC_KEY" in str(exc.value)


def test_prod_accepts_strong_audit_hmac_key_and_blank_means_unset() -> None:
    assert _settings("prod", audit_hmac_key="Audit-strong-hmac-key-0123456789abcdef").audit_hmac_key
    assert _settings("prod", audit_hmac_key="   ").audit_hmac_key is None


@pytest.mark.parametrize(
    ("env_name", "value"),
    [
        ("POSTGRES_PASSWORD", "crm"),
        ("KEYCLOAK_ADMIN_PASSWORD", "admin"),
        ("CMS_WEBHOOK_SECRET", "short"),
    ],
)
def test_prod_rejects_demo_or_short_secret_from_environment(
    monkeypatch: pytest.MonkeyPatch, env_name: str, value: str
) -> None:
    """Постгрес и Keycloak читают эти пароли не через Settings, а прямо из окружения контейнера —
    но раз переменная долетела и до api, демо-значение там так же недопустимо в prod."""
    monkeypatch.setenv(env_name, value)
    with pytest.raises(ValidationError) as exc:
        _settings("prod")
    assert env_name in str(exc.value)


def test_prod_ignores_absent_or_blank_environment_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Эти три переменные не всегда доходят до контейнера api (например, Helm их туда не
    пробрасывает) — их отсутствие не повод отказывать в запуске, в отличие от полей Settings."""
    for env_name in ("POSTGRES_PASSWORD", "KEYCLOAK_ADMIN_PASSWORD", "CMS_WEBHOOK_SECRET"):
        monkeypatch.delenv(env_name, raising=False)
    assert _settings("prod").is_prod


def test_prod_checks_cms_webhook_secret_at_its_configured_ref_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`CMS_WEBHOOK_SECRET_REF` может называть любую переменную — проверка идёт по ней, не по
    жёстко зашитому имени."""
    monkeypatch.setenv("CUSTOM_CMS_SECRET_VAR", "secret")
    with pytest.raises(ValidationError) as exc:
        _settings("prod", cms_webhook_secret_ref="CUSTOM_CMS_SECRET_VAR")
    assert "CMS_WEBHOOK_SECRET" in str(exc.value)
