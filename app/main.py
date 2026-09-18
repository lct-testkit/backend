"""Точка входа API.

Модульный монолит: один деплойный артефакт, внутри — пакеты identity, crm,
workflow, catalog, reporting, integration, notification, audit, signing, admin.
Кросс-модульные вызовы идут только через сервисные интерфейсы.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.docs import attach_docs
from app.api.health import router as health_router
from app.core.config import get_settings
from app.core.db import dispose_engine
from app.core.logging import configure_logging
from app.core.problem import register_exception_handlers
from app.core.redis_client import close_redis
from app.core.security import jwks_cache
from app.middleware.request_context import RequestContextMiddleware
from app.modules.admin.router import router as admin_router
from app.modules.catalog.router import (
    contacts_router,
    custom_field_defs_router,
    directions_router,
    holidays_router,
    loss_reasons_router,
    organizations_router,
    products_router,
    regions_router,
)
from app.modules.crm.router import comments_router, deals_router, tasks_router
from app.modules.files.router import attachments_router, files_router
from app.modules.identity.router_admin import router as admin_users_router
from app.modules.identity.router_auth import router as auth_router
from app.modules.identity.router_me import router as me_router
from app.modules.imports.router import import_jobs_router, import_presets_router
from app.modules.notification.router import (
    me_notification_prefs_router,
    notification_templates_admin_router,
    notifications_router,
)
from app.modules.notification.service import RealNotificationService, register_notification_service
from app.modules.registry.router import org_lookup_router, registry_admin_router
from app.modules.signing.public_router import public_signing_router, public_verify_router
from app.modules.signing.router import (
    edm_agreements_router,
    signature_documents_router,
    signature_requests_router,
    signature_templates_router,
    signatures_router,
)
from app.modules.workflow.router import router as workflow_router

logger = structlog.get_logger(__name__)

# Старт приложения не должен ждать Keycloak дольше этого времени.
JWKS_WARMUP_TIMEOUT = 5.0

OPENAPI_DESCRIPTION = """
Бэкенд CRM ИТ Школы Ростелекома.

**Соглашения (раздел 2 спецификации)**

* Все ошибки — RFC 7807 Problem Details с внутренним кодом вида `CRM-XXYY`.
* Идентификаторы — UUIDv7, в JSON строками.
* Даты — ISO 8601 с часовым поясом, внутри хранятся и отдаются в UTC.
* Деньги — строка `"150000.00"` плюс отдельное поле `currency`.
* Списки — курсорная пагинация: `limit` (максимум 100) и непрозрачный `cursor`,
  ответ содержит `items` и `next_cursor`.
* Обновления — оптимистичная блокировка через `If-Match` и поле `version`.
* Создание ресурсов и необратимые операции принимают `Idempotency-Key`.
* `request_id` проходит через логи, аудит и тело ошибки, отдаётся в `X-Request-Id`.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    logger.info(
        "api_starting",
        profile=settings.app_profile,
        version=settings.app_version,
    )

    # До этого спринта здесь ничего не было, и весь `notify_user` в 7 модулях
    # тихо уходил в логирующую заглушку (см. `notification/service.py`).
    register_notification_service(RealNotificationService())

    # Прогреваем JWKS, чтобы первый запрос не платил за поход в Keycloak.
    # Жёсткий общий таймаут обязателен: httpx ограничивает соединение, но
    # разрешение имени уходит в системный резолвер, и при недоступном или
    # медленном DNS (VPN, закрытый контур с частичной настройкой) старт
    # приложения зависал бы на минуты вместо секунд.
    try:
        await asyncio.wait_for(jwks_cache.refresh(), timeout=JWKS_WARMUP_TIMEOUT)
    except TimeoutError:
        logger.warning("jwks_warmup_timeout", timeout=JWKS_WARMUP_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        # Keycloak может подниматься дольше API: не валим старт, /health/ready покажет.
        logger.warning("jwks_warmup_failed", error=type(exc).__name__)

    try:
        yield
    finally:
        logger.info("api_stopping")
        await dispose_engine()
        await close_redis()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)

    app = FastAPI(
        title="CRM ИТ Школы Ростелекома — API",
        version=settings.app_version,
        description=OPENAPI_DESCRIPTION,
        lifespan=lifespan,
        # Схема нужна фронтенду всегда (DoD раздела 21); в prod закрывается
        # только интерактивный Swagger UI.
        openapi_url=f"{settings.api_prefix}/openapi.json" if settings.expose_openapi else None,
        # Стандартный Swagger тянет JS с CDN и содержит инлайн-скрипт —
        # Caddy это блокирует, страница белая. Свой UI подключается ниже.
        docs_url=None,
        redoc_url=None,
        # Ошибки отдаются только как Problem Details, поэтому стандартные
        # ответы FastAPI по валидации переопределены обработчиками.
        responses={},
    )
    # swagger-ui-bundle 4.x понимает только OpenAPI 3.0.x; FastAPI по умолчанию
    # генерирует 3.1.0, и /api/docs показывает «valid version field».
    app.openapi_version = "3.0.3"

    app.add_middleware(RequestContextMiddleware)

    # Единая точка входа — Caddy, поэтому CORS нужен только для локальной разработки.
    if not settings.is_prod:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173", "http://localhost:3000"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["X-Request-Id"],
        )

    register_exception_handlers(app)
    attach_docs(app, settings)

    # Технические ручки вне /api: их опрашивают Docker, Caddy и Prometheus.
    app.include_router(health_router)

    app.include_router(auth_router, prefix=settings.api_prefix)
    app.include_router(me_router, prefix=settings.api_prefix)
    app.include_router(admin_users_router, prefix=settings.api_prefix)
    app.include_router(admin_router, prefix=settings.api_prefix)
    app.include_router(workflow_router, prefix=settings.api_prefix)
    app.include_router(deals_router, prefix=settings.api_prefix)
    app.include_router(comments_router, prefix=settings.api_prefix)
    app.include_router(tasks_router, prefix=settings.api_prefix)
    app.include_router(organizations_router, prefix=settings.api_prefix)
    app.include_router(contacts_router, prefix=settings.api_prefix)
    app.include_router(products_router, prefix=settings.api_prefix)
    app.include_router(directions_router, prefix=settings.api_prefix)
    app.include_router(loss_reasons_router, prefix=settings.api_prefix)
    app.include_router(holidays_router, prefix=settings.api_prefix)
    app.include_router(custom_field_defs_router, prefix=settings.api_prefix)
    app.include_router(regions_router, prefix=settings.api_prefix)
    app.include_router(files_router, prefix=settings.api_prefix)
    app.include_router(attachments_router, prefix=settings.api_prefix)
    app.include_router(org_lookup_router, prefix=settings.api_prefix)
    app.include_router(registry_admin_router, prefix=settings.api_prefix)
    app.include_router(import_jobs_router, prefix=settings.api_prefix)
    app.include_router(import_presets_router, prefix=settings.api_prefix)
    app.include_router(signature_documents_router, prefix=settings.api_prefix)
    app.include_router(signature_requests_router, prefix=settings.api_prefix)
    app.include_router(signature_templates_router, prefix=settings.api_prefix)
    app.include_router(signatures_router, prefix=settings.api_prefix)
    app.include_router(edm_agreements_router, prefix=settings.api_prefix)
    app.include_router(notifications_router, prefix=settings.api_prefix)
    app.include_router(me_notification_prefs_router, prefix=settings.api_prefix)
    app.include_router(notification_templates_admin_router, prefix=settings.api_prefix)
    # Публичные ручки подписания/проверки — без сессии и без Idempotency-Key,
    # поэтому отдельный префикс `/public`, а не `/api` (dop.md §10.10).
    app.include_router(public_signing_router, prefix=settings.public_prefix)
    app.include_router(public_verify_router, prefix=settings.public_prefix)

    return app


app = create_app()
