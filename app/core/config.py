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
    # Только для alembic (`migrations/versions/0012_audit_role_hardening.py`):
    # пароль, которым миграция переиздаёт `ALTER ROLE crm_app ... PASSWORD`.
    # api/worker его не читают — их database_url уже содержит пароль внутри
    # строки подключения (docker-compose.yml даёт сервису `migrate` отдельный
    # DATABASE_URL суперпользователя, т.к. только он умеет DDL).
    crm_app_password: SecretStr
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
    # `s3_endpoint_url` — адрес внутри docker-сети (`http://seaweedfs:8333`),
    # им пользуются только серверные вызовы (ensure_bucket, скачивание
    # объекта для подсчёта sha256 при commit). Presigned PUT/GET-ссылки
    # получает браузер клиента — ему нужен адрес, реально достижимый снаружи
    # контура, единая точка входа которого — Caddy на 443/8443 (раздел 2.2:
    # «наружу опубликован только 443»). Тот же приём, что уже применён к
    # Keycloak (`keycloak_url` публичный, `keycloak_internal_url`
    # внутренний) — здесь просто наоборот, что помечено обязательным полем.
    s3_endpoint_url: str
    s3_public_endpoint_url: str | None = None
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
    # new_spec §3.1: idle-таймаут 30 минут. Сессия без активности умирает
    # раньше абсолютного TTL.
    session_idle_timeout: int = 1800
    session_cookie_name: str = "crm_sid"
    access_token_ttl: int = 300
    refresh_token_ttl: int = 28800
    # За сколько секунд до истечения access-токена обновлять его по refresh.
    access_token_refresh_leeway: int = 30
    # Сессионная cookie защищена SameSite=Lax, но мутирующие методы
    # дополнительно требуют double-submit токен (new_spec §3.1 п.5).
    csrf_cookie_name: str = "crm_csrf"
    csrf_header_name: str = "X-CSRF-Token"
    csrf_enabled: bool = True
    # Прямая аутентификация по `Authorization: Bearer` в обход серверной
    # сессии. Нужна Swagger UI и сервисным учёткам; в prod по умолчанию
    # разрешена только роли INTEGRATION (см. `bearer_auth_mode`).
    allow_bearer_auth: bool = True

    # --- Файлы -----------------------------------------------------------
    files_max_size_bytes: int = 52428800
    deal_files_max_size_bytes: int = 524288000
    # xml/csv — выгрузка ЕГРЮЛ (раздел 5.11) и файлы импорта каталогов
    # (раздел 4.12) идут через тот же общий `files`-конвейер, что и вложения
    # сделок: не отдельная загрузка в обход magic-bytes/антивируса, а те же
    # расширения в общем allowlist.
    allowed_file_extensions: str = "png,jpeg,jpg,pdf,zip,gz,gzip,rar,doc,docx,xls,xlsx,xml,csv"

    # --- Отчёты ----------------------------------------------------------
    reports_max_concurrent: int = 10
    reports_link_ttl_minutes: int = 15
    reports_retention_days: int = 7

    # --- Импорт ----------------------------------------------------------
    # new_spec §4.12, фаза 2: «лимит 50 МБ / 100 000 строк» — 5 000 строк
    # там же (§0, критерий 4) это только демо-цель для DoD, не потолок.
    import_max_rows: int = 100000
    import_max_file_size_bytes: int = 52428800
    import_batch_size: int = 500

    # --- Автоподстановка по ИНН и локальный реестр ЕГРЮЛ ------------------
    org_lookup_rate_limit_per_min: int = 30
    external_org_lookup_enabled: bool = False
    # dop.md §11.7: «раз в 30 дней или при обновлении локального реестра».
    registry_drift_interval_days: int = 30

    # --- ПЭП -------------------------------------------------------------
    public_sign_rate_limit_per_min: int = 10
    signature_token_ttl_days: int = 7
    signature_otp_ttl_seconds: int = 300
    signature_otp_max_attempts: int = 3
    signature_server_secret: SecretStr
    signature_key_version: int = 1

    # --- Уведомления -------------------------------------------------------
    notification_dispatch_batch_size: int = 200
    notification_max_delivery_attempts: int = 5

    # --- Интеграции ------------------------------------------------------
    cms_webhook_secret_ref: str | None = None
    lms_base_url: str | None = None
    lms_auth_ref: str | None = None
    bitrix_connector_enabled: bool = False
    # Секрет живёт преимущественно на `integration_sources.credentials_ref`
    # (раздел 7.8, админ настраивает без деплоя) — это в `.env` только
    # запасной путь для сред, где сид источника ещё не прогнан
    # (`integration.tasks._deliver` сначала смотрит в БД). Значение — имя
    # переменной окружения, которая хранит **весь** секретный префикс
    # входящего вебхука Bitrix24 целиком (`https://{портал}/rest/{user_id}/
    # {webhook_code}`, без отдельного токена — см. `integration/bitrix.py`).
    bitrix_webhook_url_ref: str | None = None
    # Раздел 4.14 не требует рейт-лимит на вебхуки явно (в отличие от
    # dop.md §10.11 для /public/sign/*), но это единственные пути без
    # сессионной аутентификации — тот же принцип защиты по умолчанию.
    integration_webhook_rate_limit_per_min: int = 60

    # --- LLM -------------------------------------------------------------
    llm_enabled: bool = False
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_timeout_seconds: int = 30

    # --- Администрирование пользователей (раздел 6.2, new_spec §4.1–4.8) --
    # Приглашение: одноразовая ссылка, в базе только sha256 токена.
    invite_ttl_hours: int = 72
    invite_resend_interval_seconds: int = 300
    invite_resend_per_day: int = 5
    # Добровольная смена пароля: 5 неудачных попыток → блокировка формы.
    password_change_max_attempts: int = 5
    password_change_lock_seconds: int = 900
    # Grace period режима A перед обезличиванием (new_spec §4.8.2).
    erasure_grace_days: int = 30
    # Срок исполнения запроса субъекта ПДн по ст. 21 152-ФЗ.
    erasure_subject_deadline_days: int = 30
    # new_spec §4.8.1: срок именно для запроса на уничтожение ПДн контакта
    # (ст. 21) короче общего — 7 рабочих дней против 30 календарных для
    # сотрудника. Считается в календарных днях (не рабочих): точный учёт
    # производственного календаря уже есть для SLA сделок (`holidays`,
    # new_spec §4.10) — если здесь понадобится точность день-в-день, это
    # тот же расчёт, а не отдельная реализация.
    erasure_contact_deadline_days: int = 7
    # Подтверждение второго администратора живёт ограниченное время.
    admin_approval_ttl_seconds: int = 86400

    # --- Прочее ----------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = True
    idempotency_ttl_seconds: int = 86400
    pagination_max_limit: int = 100
    # Версия политики по умолчанию. Действующая версия публикуется через
    # system_settings `pdn_policy` и перекрывает это значение.
    consent_policy_version: str = "1.0"
    consent_policy_text_hash: str | None = None
    docs_enabled: bool = True
    # OpenAPI-схема нужна фронтенду и в prod; закрывается только UI.
    openapi_enabled: bool = True

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
    def s3_public_endpoint(self) -> str:
        """Адрес для presigned-ссылок, отдаваемых браузеру. Если публичный
        адрес не задан отдельно (локальные тесты, окружения с одним
        плоским адресом), используется `s3_endpoint_url` как есть — тогда
        presigned-ссылки будут работать только внутри той же сети, что и
        раньше, без изменения поведения для таких сред."""
        return self.s3_public_endpoint_url or self.s3_endpoint_url

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

    # Swagger UI в prod закрыт, если не включён явно.
    @property
    def expose_docs(self) -> bool:
        return self.docs_enabled and not self.is_prod

    @property
    def expose_openapi(self) -> bool:
        """Схема публикуется всегда: на неё опирается контракт с фронтендом
        (раздел 21, Definition of Done). Закрывается только интерактивный UI."""
        return self.openapi_enabled

    @property
    def bearer_auth_mode(self) -> Literal["all", "integration_only", "off"]:
        """Кому разрешён вход по `Authorization: Bearer` без серверной сессии.

        В prod браузерный трафик обязан ходить через BFF-сессию, иначе
        обходится весь контур из new_spec §3.1: токен в JS-контексте,
        отсутствие CSRF-защиты и невозможность завершить сессию.
        """
        if not self.allow_bearer_auth:
            return "off"
        return "integration_only" if self.is_prod else "all"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Синглтон настроек. Падение на старте лучше, чем работа с половиной конфига."""
    return Settings()  # type: ignore[call-arg]


settings_dep = get_settings
