"""Локальный Swagger UI.

Стандартная страница FastAPI тянет CSS/JS с jsdelivr. Caddy это запрещает
(CSP `script-src 'self'`), плюс в закрытом контуре CDN недоступен — в браузере
остаётся белый экран. Здесь ассеты отдаются с нашего же сервера.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from swagger_ui_bundle import swagger_ui_path

from app.core.config import Settings

_SWAGGER_INIT_JS = """window.addEventListener("load", function () {
  window.ui = SwaggerUIBundle({
    url: "/api/openapi.json",
    dom_id: "#swagger-ui",
    layout: "BaseLayout",
    deepLinking: true,
    showExtensions: true,
    showCommonExtensions: true,
    persistAuthorization: true,
    oauth2RedirectUrl: window.location.origin + "/api/docs/oauth2-redirect"
  });
});
"""

_SWAGGER_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>{title}</title>
  <link rel="stylesheet" href="/static/swagger/swagger-ui.css">
  <link rel="icon" type="image/png" href="/static/swagger/favicon-32x32.png">
  <style>html, body {{ margin: 0; background: #fafafa; }}</style>
</head>
<body>
  <div id="swagger-ui"></div>
  <script src="/static/swagger/swagger-ui-bundle.js"></script>
  <script src="/api/docs/swagger-init.js"></script>
</body>
</html>
"""

# Ручки без токена: иначе Swagger потребует Authorize и для /health.
_PUBLIC_PATH_PREFIXES = (
    "/health/",
    "/metrics",
    "/api/auth/login",
    "/api/auth/callback",
    "/api/auth/backchannel-logout",
)


def _is_public_path(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix) for prefix in _PUBLIC_PATH_PREFIXES)


def _install_bearer_auth(app: FastAPI) -> None:
    """Добавляет схему Bearer в OpenAPI — без неё у Swagger нет кнопки Authorize."""

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema

        schema = get_openapi(
            title=app.title,
            version=app.version,
            openapi_version=app.openapi_version,
            description=app.description,
            routes=app.routes,
        )
        components = schema.setdefault("components", {})
        schemes = components.setdefault("securitySchemes", {})
        schemes["BearerAuth"] = {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": (
                "Access token из Keycloak. "
                "Вставьте только сам токен — слово Bearer добавлять не нужно, "
                "Swagger подставит его сам."
            ),
        }
        schema["security"] = [{"BearerAuth": []}]

        for path, methods in schema.get("paths", {}).items():
            if not _is_public_path(path):
                continue
            for method, operation in methods.items():
                if isinstance(operation, dict) and method.lower() in {
                    "get",
                    "post",
                    "put",
                    "patch",
                    "delete",
                    "head",
                    "options",
                }:
                    operation["security"] = []

        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]


def attach_docs(app: FastAPI, settings: Settings) -> None:
    if not settings.expose_docs:
        return

    _install_bearer_auth(app)

    app.mount(
        "/static/swagger",
        StaticFiles(directory=str(swagger_ui_path)),
        name="swagger-ui",
    )

    router = APIRouter(include_in_schema=False)

    @router.get(f"{settings.api_prefix}/docs", response_class=HTMLResponse)
    async def swagger_ui() -> HTMLResponse:
        return HTMLResponse(_SWAGGER_HTML.format(title=app.title))

    @router.get(f"{settings.api_prefix}/docs/swagger-init.js")
    async def swagger_init() -> Response:
        return Response(_SWAGGER_INIT_JS, media_type="application/javascript")

    @router.get(f"{settings.api_prefix}/docs/oauth2-redirect", response_class=HTMLResponse)
    async def swagger_oauth_redirect() -> HTMLResponse:
        redirect = Path(swagger_ui_path) / "oauth2-redirect.html"
        return HTMLResponse(redirect.read_text(encoding="utf-8"))

    app.include_router(router)
