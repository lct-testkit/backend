"""`Settings.cookies_secure` — Secure на куках по реальной схеме install.sh-адреса (base_url), не по
профилю. Регрессия: было `app_profile != "dev"`, то есть True для demo/prod даже на `--tls off`
с публичным --host (не localhost) — там браузер Secure-куку не примет, сессия не держится.
Баг маскировался на localhost: там http считается "потенциально доверенным" контекстом и
исключение работает, поэтому его не увидели раньше на прогонах офлайн-установки именно на localhost.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings

STRONG = {
    "signature_server_secret": "Zx9-strong-signature-secret-0123456789",
    "keycloak_client_secret": "Kc-strong-client-secret-0123456789",
    "keycloak_admin_client_secret": "Kc-strong-admin-secret-0123456789",
    "s3_access_key": "S3-strong-access-key-0123456789",
    "s3_secret_key": "S3-strong-secret-key-0123456789",
    "crm_app_password": "Db-strong-app-password-0123456789",
}


def _settings(base_url: str, profile: str = "demo") -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[arg-type]
        app_profile=profile,
        base_url=base_url,
        database_url="postgresql+asyncpg://crm_app:Db-strong-app-password-0123456789@db:5432/crm",
        redis_url="redis://redis:6379/0",
        keycloak_url="http://kc:8080/auth",
        keycloak_realm="crm",
        keycloak_client_id="crm-bff",
        s3_endpoint_url="http://s3:8333",
        **STRONG,
    )


@pytest.mark.parametrize("profile", ["demo", "prod"])
def test_off_mode_with_a_real_host_is_not_secure(profile: str) -> None:
    """install.sh --tls off --host 192.168.0.10 (RUNBOOK явно разрешает публичный --host в этом
    режиме) — без этого фикса secure=True тут ломало сессию: браузер не сохраняет Secure-куку
    не по HTTPS и не на localhost."""
    assert _settings("http://192.168.0.10:8080", profile).cookies_secure is False


@pytest.mark.parametrize("profile", ["demo", "prod"])
def test_off_mode_on_localhost_is_also_not_secure(profile: str) -> None:
    """localhost — тоже http, cookies_secure тут False; сессия при этом всё равно работает
    (браузеры считают localhost потенциально доверенным контекстом сами по себе), но настройка
    отражает реальную схему, а не "особый случай локалхоста"."""
    assert _settings("http://localhost:8080", profile).cookies_secure is False


@pytest.mark.parametrize("profile", ["demo", "prod"])
def test_acme_or_internal_mode_is_secure(profile: str) -> None:
    """install.sh --tls acme/internal всегда даёт https:// в base_url."""
    assert _settings("https://crm.example.ru", profile).cookies_secure is True


def test_dev_profile_behaviour_did_not_change() -> None:
    """dev-профиль (локальный uvicorn, обычно http://localhost:8000) — тем же результатом, что и до
    фикса: секьюрность не по профилю, а по факту http/https в base_url."""
    assert _settings("http://localhost:8000", "dev").cookies_secure is False
