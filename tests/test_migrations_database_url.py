"""D6: `MIGRATIONS_DATABASE_URL` — оверрайд для `alembic` локально вне docker
compose — должен подхватываться из `.env`, а не только из настоящей
переменной окружения процесса.

Раньше `migrations/env.py` читал её напрямую через `os.environ.get(...)`, в
обход `Settings`/`.env` (см. `app/core/config.py`): значение, заданное только
в `.env`, молча игнорировалось, и `alembic upgrade head` шёл по `database_url`
— роли `crm_app`, у которой сознательно нет DDL-прав (0012_audit_role_hardening).
"""

from __future__ import annotations

from app.core.config import Settings

_BASE: dict[str, str] = {
    "app_profile": "dev",
    "database_url": "postgresql+asyncpg://crm_app:crm_app@db:5432/crm",
    "crm_app_password": "crm_app",
    "redis_url": "redis://redis:6379/0",
    "keycloak_url": "http://kc:8080/auth",
    "keycloak_realm": "crm",
    "keycloak_client_id": "crm-bff",
    "keycloak_client_secret": "crm-bff-secret",
    "s3_endpoint_url": "http://s3:8333",
    "s3_access_key": "crm_access",
    "s3_secret_key": "crm_secret_key",
    "signature_server_secret": "change-me-in-prod",
}


def test_migrations_database_url_defaults_to_none() -> None:
    """Без оверрайда поле пустое — `migrations/env.py` берёт `database_url`
    (тот же приём, что `keycloak_internal_url`/`s3_public_endpoint_url`)."""
    settings = Settings(_env_file=None, **_BASE)  # type: ignore[arg-type]
    assert settings.migrations_database_url is None
    assert (settings.migrations_database_url or settings.database_url) == settings.database_url


def test_migrations_database_url_is_a_settings_field_not_raw_environ() -> None:
    """Поле читается через обычный механизм `Settings` (в т.ч. `.env`), а не
    напрямую из `os.environ` — иначе значение из `.env` молча терялось (D6)."""
    override = "postgresql+asyncpg://crm:crm@localhost:5432/crm"
    settings = Settings(
        _env_file=None,  # type: ignore[arg-type]
        migrations_database_url=override,
        **_BASE,
    )
    assert settings.migrations_database_url == override
    assert (settings.migrations_database_url or settings.database_url) == override
