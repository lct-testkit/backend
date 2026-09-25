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
        "database_url": "postgresql+asyncpg://crm_app:x@db:5432/crm",
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
