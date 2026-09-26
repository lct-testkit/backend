"""Локальный Swagger UI.

Стандартная страница FastAPI тянет CSS/JS с jsdelivr. Caddy это запрещает
(CSP `script-src 'self'`), плюс в закрытом контуре CDN недоступен — в браузере
остаётся белый экран. Здесь ассеты отдаются с нашего же сервера.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from swagger_ui_bundle import swagger_ui_path

from app.api.openapi import install_openapi
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


def attach_docs(app: FastAPI, settings: Settings) -> None:
    # Схема описывает способы аутентификации и формат ошибок всегда, даже когда UI закрыт.
    if settings.expose_openapi:
        install_openapi(app, settings)
    if not settings.expose_docs:
        return

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
