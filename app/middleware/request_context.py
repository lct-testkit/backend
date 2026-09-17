"""Middleware контекста запроса и RED-метрик.

`request_id` берётся из заголовка от Caddy (`X-Request-Id`) или генерируется,
кладётся в contextvar, отдаётся в заголовке ответа и попадает в тело каждой
ошибки Problem Details.
"""

from __future__ import annotations

import time

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.core.context import (
    ClientContext,
    new_request_id,
    reset_context,
    set_client,
    set_request_id,
)
from app.core.metrics import (
    http_errors_total,
    http_request_duration_seconds,
    http_requests_total,
)
from app.core.problem import (
    ERROR_CODE_SCOPE_KEY,
    REQUEST_ID_HEADER,
    REQUEST_ID_SCOPE_KEY,
)

logger = structlog.get_logger(__name__)

FORWARDED_FOR_HEADER = "X-Forwarded-For"

# Шумные технические маршруты не пишем в лог доступа.
_QUIET_PATHS = frozenset({"/health/live", "/health/ready", "/metrics"})


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get(FORWARDED_FOR_HEADER)
    if forwarded:
        # Caddy добавляет цепочку: первый адрес — исходный клиент.
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-Ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else None


def _route_template(request: Request) -> str:
    """Шаблон маршрута вместо конкретного пути, иначе метрики взорвутся по кардинальности."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path or "unmatched"


class RequestContextMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or new_request_id()
        set_request_id(request_id)
        # Дубль в scope переживает сброс contextvars в finally: обработчик
        # необработанных исключений сработает уже после него.
        request.scope[REQUEST_ID_SCOPE_KEY] = request_id
        set_client(
            ClientContext(ip=_client_ip(request), user_agent=request.headers.get("User-Agent"))
        )
        structlog.contextvars.bind_contextvars(request_id=request_id)

        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            elapsed = time.perf_counter() - started
            route = _route_template(request)
            http_requests_total.labels(
                method=request.method, route=route, status=str(status_code)
            ).inc()
            http_request_duration_seconds.labels(method=request.method, route=route).observe(
                elapsed
            )
            if status_code >= 400:
                http_errors_total.labels(
                    route=route,
                    code=request.scope.get(ERROR_CODE_SCOPE_KEY, "unknown"),
                    status=str(status_code),
                ).inc()
            if request.url.path not in _QUIET_PATHS:
                logger.info(
                    "http_request",
                    method=request.method,
                    path=request.url.path,
                    route=route,
                    status=status_code,
                    duration_ms=round(elapsed * 1000, 2),
                )
            structlog.contextvars.clear_contextvars()
            reset_context()
