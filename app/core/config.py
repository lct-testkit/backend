"""Конфигурация приложения.

Полный список переменных окружения зафиксирован в разделе 17 спецификации.
Приложение не стартует без APP_PROFILE и обязательных строк подключения.
Секреты никогда не логируются и не отдаются наружу в открытом виде.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AppProfile = Literal["dev", "demo", "prod"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Профиль ---------------------------------------------------------
    app_profile: AppProfile
    app_name: str = "rtk-crm-api"
    app_version: str = "0.1.0"
    api_prefix: str = "/api"
    public_prefix: str = "/public"
    base_url: str = "http://localhost:8080"

    # --- Хранилища -------------------------------------------------------
    database_url: str
    kc_database_url: str | None = None
    redis_url: str
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_echo: bool = False

    # --- Keycloak --------------------------------------------------------
    # Публичный адрес: из него формируется issuer токенов и ссылка, по которой
    # браузер уходит на страницу входа.
    keycloak_url: str
    # Адрес для серверных вызовов внутри контура (JWKS, token, Admin API).
    # В docker-compose это http://keycloak:8080/auth, снаружи он недоступен.
    # Если не задан, используется публичный адрес.
    keycloak_internal_url: str | None = None
    keycloak_realm: str
    keycloak_client_id: str
    keycloak_client_secret: SecretStr
    keycloak_admin_client_id: str | None = None
    keycloak_admin_client_secret: SecretStr | None = None
    keycloak_jwks_ttl: int = 3600
    keycloak_verify_audience: bool = True

    # --- S3 / SeaweedFS --------------------------------------------------
    # MinIO использовать запрещено: раздел «Стек» спецификации.
    s3_endpoint_url: str
    s3_access_key: SecretStr
    s3_secret_key: SecretStr
    s3_region: str = "us-east-1"
    s3_bucket_files: str = "files"
    s3_bucket_imports: str = "imports"
    s3_bucket_reports: str = "reports"
    s3_bucket_signatures: str = "signatures"
    s3_bucket_registry: str = "registry"

    # --- Сессии и токены -------------------------------------------------
    session_ttl: int = 43200
    session_idle_timeout: int = 3600
    session_cookie_name: str = "crm_sid"
    access_token_ttl: int = 300
    refresh_token_ttl: int = 1800

    # --- Файлы -----------------------------------------------------------
    files_max_size_bytes: int = 52428800
    deal_files_max_size_bytes: int = 524288000
    allowed_file_extensions: str = "png,jpeg,jpg,pdf,zip,gz,gzip,rar,doc,docx,xls,xlsx"

    # --- Отчёты ----------------------------------------------------------
    reports_max_concurrent: int = 10
    reports_link_ttl_minutes: int = 15
    reports_retention_days: int = 7

    # --- Импорт ----------------------------------------------------------
    import_max_rows: int = 50000
    import_max_file_size_bytes: int = 52428800
    import_batch_size: int = 500

    # --- Автоподстановка по ИНН -----------------------------------------
    org_lookup_rate_limit_per_min: int = 30
    external_org_lookup_enabled: bool = False

    # --- ПЭП -------------------------------------------------------------
    public_sign_rate_limit_per_min: int = 10
    signature_token_ttl_days: int = 7
    signature_otp_ttl_seconds: int = 300
    signature_otp_max_attempts: int = 3
    signature_server_secret: SecretStr
    signature_key_version: int = 1

    # --- Интеграции ------------------------------------------------------
    cms_webhook_secret_ref: str | None = None
    lms_base_url: str | None = None
    lms_auth_ref: str | None = None
    bitrix_connector_enabled: bool = False

    # --- LLM -------------------------------------------------------------
    llm_enabled: bool = False
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_timeout_seconds: int = 30

    # --- Прочее ----------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = True
    idempotency_ttl_seconds: int = 86400
    pagination_max_limit: int = 100
    consent_policy_version: str = "1.0"
    docs_enabled: bool = True

    @field_validator("database_url", "kc_database_url", mode="after")
    @classmethod
    def _require_async_driver(cls, value: str | None) -> str | None:
        if value and value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+asyncpg://", 1)
        return value

    @property
    def is_prod(self) -> bool:
        return self.app_profile == "prod"

    @property
    def allowed_extensions(self) -> frozenset[str]:
        return frozenset(
            ext.strip().lower().lstrip(".")
            for ext in self.allowed_file_extensions.split(",")
            if ext.strip()
        )

    @property
    def keycloak_issuer(self) -> str:
        """Значение claim `iss`. Всегда публичный адрес, независимо от того,
        каким путём API обратился к Keycloak."""
        return f"{self.keycloak_url.rstrip('/')}/realms/{self.keycloak_realm}"

    @property
    def _keycloak_internal_realm(self) -> str:
        base = (self.keycloak_internal_url or self.keycloak_url).rstrip("/")
        return f"{base}/realms/{self.keycloak_realm}"

    @property
    def keycloak_admin_base(self) -> str:
        base = (self.keycloak_internal_url or self.keycloak_url).rstrip("/")
        return f"{base}/admin/realms/{self.keycloak_realm}"

    @property
    def keycloak_jwks_url(self) -> str:
        return f"{self._keycloak_internal_realm}/protocol/openid-connect/certs"

    @property
    def keycloak_token_url(self) -> str:
        return f"{self._keycloak_internal_realm}/protocol/openid-connect/token"

    @property
    def keycloak_discovery_url(self) -> str:
        return f"{self._keycloak_internal_realm}/.well-known/openid-configuration"

    @property
    def keycloak_auth_url(self) -> str:
        # Браузерный редирект: только публичный адрес.
        return f"{self.keycloak_issuer}/protocol/openid-connect/auth"

    @property
    def keycloak_logout_url(self) -> str:
        return f"{self._keycloak_internal_realm}/protocol/openid-connect/logout"

    # Swagger в prod закрыт, если не включён явно.
    @property
    def expose_docs(self) -> bool:
        return self.docs_enabled and not self.is_prod


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Синглтон настроек. Падение на старте лучше, чем работа с половиной конфига."""
    return Settings()  # type: ignore[call-arg]


settings_dep = get_settings
