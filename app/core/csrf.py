"""Double-submit CSRF-токен для сессионной аутентификации.

new_spec §3.1 п.5: CSRF закрывается `SameSite=Lax` **плюс** double-submit
токеном на мутирующих методах. Lax сам по себе не покрывает всё: он не
защищает от запросов с того же сайта (поддомен, открытый редирект во
фрейме) и зависит от поведения конкретного браузера.

Токен кладётся в отдельную cookie (не httpOnly — его обязан прочитать
фронтенд) и обязан прийти обратно в заголовке. Запросы с `Authorization:
Bearer` проверку не проходят: у них нет cookie, а значит нет и вектора CSRF.
"""

from __future__ import annotations

import hmac
import secrets

from fastapi import Response

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode

# Методы, которые ничего не меняют, токена не требуют.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def set_csrf_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=token,
        max_age=settings.session_ttl,
        # Читается JavaScript намеренно: это вторая половина double-submit.
        httponly=False,
        secure=settings.app_profile != "dev",
        samesite="lax",
        path="/",
    )


def clear_csrf_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(key=settings.csrf_cookie_name, path="/")


def verify_csrf(*, method: str, cookie_value: str | None, header_value: str | None) -> None:
    """Сверяет cookie и заголовок. Расхождение — CRM-1107."""
    settings = get_settings()
    if not settings.csrf_enabled or method.upper() in SAFE_METHODS:
        return
    if not cookie_value or not header_value:
        raise AppError(
            ErrorCode.CSRF_FAILED,
            "Мутирующий запрос из браузера требует CSRF-токен",
            extra={"header": settings.csrf_header_name},
        )
    if not hmac.compare_digest(cookie_value, header_value):
        raise AppError(ErrorCode.CSRF_FAILED, "CSRF-токен не совпадает с сессионным")
