"""B5-1: настройки источников CMS_BASE_URL/CMS_AUTH_REF/LMS_BASE_URL.

Без адресов и токенов в окружении забор лидов по API (B5-2) и стыковка с LMS
(B5-6) не запустятся вовсе — задача фиксирует только настройки и их разбор.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.modules.integration.security import resolve_secret

_BASE = {
    "app_profile": "dev",
    "database_url": "postgresql+asyncpg://crm_app:x@db:5432/crm",
    "redis_url": "redis://redis:6379/0",
    "keycloak_url": "http://kc:8080/auth",
    "keycloak_realm": "crm",
    "keycloak_client_id": "crm-bff",
    "s3_endpoint_url": "http://s3:8333",
    "signature_server_secret": "Zx9-strong-signature-secret-0123456789",
    "keycloak_client_secret": "Kc-strong-client-secret-0123456789",
    "s3_access_key": "S3-strong-access-key-0123456789",
    "s3_secret_key": "S3-strong-secret-key-0123456789",
    "crm_app_password": "Db-strong-app-password-0123456789",
}


def _settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **{**_BASE, **overrides})  # type: ignore[arg-type]


def test_trailing_slash_is_stripped() -> None:
    s = _settings(cms_base_url="http://mock-cms:4010/", lms_base_url="http://mock-lms:4010/api/")
    assert s.cms_base_url == "http://mock-cms:4010"
    assert s.lms_base_url == "http://mock-lms:4010/api"


def test_empty_string_is_none() -> None:
    s = _settings(cms_base_url="", lms_base_url="")
    assert s.cms_base_url is None
    assert s.lms_base_url is None


def test_defaults_are_unset() -> None:
    s = _settings()
    assert s.cms_base_url is None
    assert s.lms_base_url is None
    assert s.cms_auth_ref == "CMS_AUTH_TOKEN"
    assert s.lms_auth_ref == "LMS_AUTH_TOKEN"
    assert s.integration_pull_page_size == 100
    assert s.integration_pull_max_pages == 20


@pytest.mark.parametrize("value", [0, 501])
def test_pull_page_size_is_bounded(value: int) -> None:
    with pytest.raises(ValidationError):
        _settings(integration_pull_page_size=value)


def test_resolve_secret_reads_the_named_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LMS_AUTH_TOKEN", "secret-value")
    assert resolve_secret("LMS_AUTH_TOKEN") == "secret-value"
    monkeypatch.delenv("LMS_AUTH_TOKEN", raising=False)
    assert resolve_secret("LMS_AUTH_TOKEN") is None
