"""Health-checks и метрики (раздел 6.13).

`/health/live` отвечает, пока жив процесс: его использует Docker и Caddy.
`/health/ready` проверяет БД, Redis, Keycloak JWKS, SeaweedFS и очередь —
и отдаёт 503, если критичная зависимость недоступна.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from fastapi import APIRouter, Response, status
from prometheus_client import CONTENT_TYPE_LATEST

from app.core.config import get_settings
from app.core.db import check_database
from app.core.dependency_metrics import DependencyMetricsRefresher
from app.core.errors import DependencyStatus
from app.core.metrics import dependency_up, render_metrics
from app.core.redis_client import check_queue, check_redis
from app.core.security import jwks_cache
from app.core.storage import check_storage
from app.modules.identity.keycloak import keycloak_client

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["health"])

# Без этих зависимостей приложение не может обслуживать запросы.
CRITICAL = frozenset({"postgres", "redis"})

# Ключ — имя, под которым проверка отдаёт результат (`DependencyStatus.name`, метка `dependency`).
# Лямбды, а не сами функции: проверки подменяются в тестах по имени в этом модуле.
_dependency_metrics = DependencyMetricsRefresher(
    {
        "postgres": lambda: check_database(),
        "redis": lambda: check_redis(),
        "keycloak_jwks": lambda: jwks_cache.check(),
        "seaweedfs": lambda: check_storage(),
        "queue": lambda: check_queue(),
    }
)


@router.get(
    "/health/live",
    summary="Liveness",
    description="Проверяет, что процесс жив. Роль: доступно без аутентификации.",
)
async def live() -> dict[str, str]:
    settings = get_settings()
    return {
        "status": "ok",
        "app": settings.app_name,
        "version": settings.app_version,
        "profile": settings.app_profile,
    }


@router.get(
    "/health/ready",
    summary="Readiness",
    description=(
        "Проверяет PostgreSQL, Redis, Keycloak JWKS, SeaweedFS и очередь arq. "
        "Возвращает 503, если недоступна критичная зависимость. "
        "Роль: доступно без аутентификации."
    ),
)
async def ready(response: Response) -> dict[str, Any]:
    checks: list[DependencyStatus] = list(
        await asyncio.gather(
            check_database(),
            check_redis(),
            jwks_cache.check(),
            check_storage(),
            check_queue(),
        )
    )

    for check in checks:
        dependency_up.labels(dependency=check.name).set(1 if check.ok else 0)

    degraded = [c.name for c in checks if not c.ok]
    critical_down = [name for name in degraded if name in CRITICAL]

    if critical_down:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        overall = "unavailable"
    elif degraded:
        # Остальное деградирует предсказуемо, но трафик принимать можно.
        overall = "degraded"
    else:
        overall = "ok"

    return {
        "status": overall,
        "dependencies": [
            {
                "name": c.name,
                "ok": c.ok,
                "latency_ms": c.latency_ms,
                **({"error": c.error} if c.error else {}),
                **({"details": c.details} if c.details else {}),
            }
            for c in checks
        ],
    }


@router.get(
    "/metrics",
    summary="Prometheus-метрики",
    description=(
        "RED-метрики, длина очереди, время импорта, нарушения SLA и hit-rate кэша. "
        "Перед ответом обновляет `crm_dependency_up` и `crm_queue_depth` (проверки с таймаутом "
        "1 с, результат кэшируется на 5 с). "
        "Роль: доступно без аутентификации внутри контура."
    ),
    include_in_schema=False,
)
async def metrics() -> Response:
    try:
        await _dependency_metrics.refresh()
    except Exception:  # noqa: BLE001 — метрики отдаём, даже если проверки зависимостей сломались
        logger.warning("dependency_metrics_refresh_failed", exc_info=True)
    return Response(content=render_metrics(), media_type=CONTENT_TYPE_LATEST)


@router.get(
    "/health/keycloak",
    summary="Диагностика Keycloak",
    description="Проверяет OIDC discovery Keycloak. Роль: доступно без аутентификации.",
    include_in_schema=False,
)
async def keycloak_health() -> dict[str, Any]:
    check = await keycloak_client.check()
    return {
        "name": check.name,
        "ok": check.ok,
        "latency_ms": check.latency_ms,
        "error": check.error,
    }
