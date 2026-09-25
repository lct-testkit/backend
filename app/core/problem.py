"""RFC 7807 Problem Details — единственный формат ошибок наружу.

Раздел 2 спецификации: в теле обязательны `type`, `title`, `status`,
`detail`, `instance`, `request_id`, `code`; при необходимости — массив
`errors`. Стектрейсы наружу не отдаются никогда.
"""

from __future__ import annotations

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.context import get_request_id
from app.core.errors import ERROR_CATALOG, AppError, ErrorCode, FieldError
from app.core.masking import mask_mapping

logger = structlog.get_logger(__name__)

PROBLEM_CONTENT_TYPE = "application/problem+json"
PROBLEM_TYPE_BASE = "https://crm.rt-it-school.ru/problems"

# Ключ в ASGI scope, через который обработчик передаёт код ошибки в метрики.
ERROR_CODE_SCOPE_KEY = "crm_error_code"

# Дубль request_id в scope. Обработчик необработанных исключений висит на
# ServerErrorMiddleware — снаружи нашего middleware, который к тому моменту
# уже сбросил contextvars. Без этого дубля именно у ответов 500 поле
# request_id оказывалось null, хотя текст просит сообщить его администратору.
REQUEST_ID_SCOPE_KEY = "crm_request_id"

REQUEST_ID_HEADER = "X-Request-Id"

# HTTP-статус -> код каталога для ошибок, которые поднял не наш код (Starlette).
_STATUS_TO_CODE: dict[int, ErrorCode] = {
    401: ErrorCode.UNAUTHENTICATED,
    403: ErrorCode.FORBIDDEN,
    404: ErrorCode.NOT_FOUND,
    409: ErrorCode.DUPLICATE,
    413: ErrorCode.FILE_TOO_LARGE,
    415: ErrorCode.FILE_TYPE_NOT_ALLOWED,
    422: ErrorCode.VALIDATION,
    429: ErrorCode.REPORTS_LIMIT_EXCEEDED,
    503: ErrorCode.DEPENDENCY_UNAVAILABLE,
}

# SQLSTATE нарушений, которые вызвал сам клиент запросом: ссылка на связанные данные и дубль
# по уникальному индексу. NOT NULL и CHECK сюда не входят — это дыра в валидации или баг
# сервиса, и такая ошибка остаётся внутренней (500), а не маскируется под конфликт.
_PG_FOREIGN_KEY_VIOLATION = "23503"
_PG_UNIQUE_VIOLATION = "23505"


def build_problem(
    *,
    code: ErrorCode,
    status: int,
    title: str,
    detail: str,
    instance: str,
    errors: list[FieldError] | None = None,
    extra: dict[str, object] | None = None,
    request_id: str | None = None,
) -> dict[str, object]:
    body: dict[str, object] = {
        "type": f"{PROBLEM_TYPE_BASE}/{code.value.lower()}",
        "title": title,
        "status": status,
        "detail": detail,
        "instance": instance,
        "request_id": request_id or get_request_id(),
        "code": code.value,
    }
    if errors:
        body["errors"] = [
            {"field": e.field, "reason": e.reason, **({"code": e.code} if e.code else {})}
            for e in errors
        ]
    if extra:
        body.update(mask_mapping(extra))  # type: ignore[arg-type]
    return body


def problem_response(
    *,
    code: ErrorCode,
    detail: str,
    instance: str,
    status: int | None = None,
    title: str | None = None,
    errors: list[FieldError] | None = None,
    extra: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
    request: Request | None = None,
) -> JSONResponse:
    spec = ERROR_CATALOG[code]
    resolved_status = status or spec.status
    request_id: str | None = None
    if request is not None:
        # Метрика http_errors_total разбита по коду каталога: middleware
        # читает его из scope уже после того, как обработчик вернул ответ.
        request.scope[ERROR_CODE_SCOPE_KEY] = code.value
        request_id = request.scope.get(REQUEST_ID_SCOPE_KEY)
    body = build_problem(
        code=code,
        status=resolved_status,
        title=title or spec.title,
        detail=detail,
        instance=instance,
        errors=errors,
        extra=extra,
        request_id=request_id,
    )
    resolved_headers = dict(headers or {})
    # У ответов 500 заголовок не успевает поставить middleware: исключение
    # уходит мимо его return. Ставим здесь, чтобы клиент видел request_id
    # и в заголовке, и в теле.
    effective_request_id = body.get("request_id")
    if effective_request_id:
        resolved_headers.setdefault(REQUEST_ID_HEADER, str(effective_request_id))
    return JSONResponse(
        status_code=resolved_status,
        content=body,
        media_type=PROBLEM_CONTENT_TYPE,
        headers=resolved_headers,
    )


def _loc_to_field(loc: tuple[object, ...]) -> str:
    # Отрезаем "body"/"query"/"path": фронтенду нужно имя поля, а не срез запроса.
    parts = [str(p) for p in loc]
    if parts and parts[0] in {"body", "query", "path", "header", "cookie"}:
        parts = parts[1:]
    return ".".join(parts) or "__root__"


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    # Поле называется error_code, а не code: ключ `code` маскируется как
    # секрет (коды OTP и OIDC), и код ошибки превратился бы в звёздочки.
    log = logger.bind(error_code=exc.code.value, status=exc.status, path=request.url.path)
    if exc.status >= 500:
        log.error("app_error", detail=exc.detail)
    else:
        log.info("app_error", detail=exc.detail)
    return problem_response(
        code=exc.code,
        status=exc.status,
        title=exc.title,
        detail=exc.detail,
        instance=str(request.url.path),
        errors=exc.errors,
        extra=exc.extra,
        headers=exc.headers,
        request=request,
    )


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [
        FieldError(field=_loc_to_field(e["loc"]), reason=e["msg"], code=e["type"])
        for e in exc.errors()
    ]
    return problem_response(
        code=ErrorCode.VALIDATION,
        detail="Запрос не прошёл валидацию",
        instance=str(request.url.path),
        errors=errors,
        request=request,
    )


async def pydantic_error_handler(request: Request, exc: PydanticValidationError) -> JSONResponse:
    errors = [
        FieldError(field=_loc_to_field(e["loc"]), reason=e["msg"], code=e["type"])
        for e in exc.errors()
    ]
    return problem_response(
        code=ErrorCode.VALIDATION,
        detail="Внутренняя схема данных не прошла валидацию",
        instance=str(request.url.path),
        errors=errors,
        request=request,
    )


async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = _STATUS_TO_CODE.get(exc.status_code, ErrorCode.INTERNAL)
    return problem_response(
        code=code,
        status=exc.status_code,
        detail=str(exc.detail),
        instance=str(request.url.path),
        headers=getattr(exc, "headers", None),
        request=request,
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Стектрейс уходит только в лог, наружу — обезличенный 500 с кодом.
    logger.exception(
        "unhandled_error",
        path=request.url.path,
        method=request.method,
        error_type=type(exc).__name__,
    )
    return problem_response(
        code=ErrorCode.INTERNAL,
        detail="Внутренняя ошибка сервера. Обратитесь к администратору с request_id.",
        instance=str(request.url.path),
        request=request,
    )


async def integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    """Страховка для нарушений ограничений БД, которые не перехватил сервис:
    дубль и ссылка на связанные данные — 409, а не «Внутренняя ошибка». Сервис
    по-прежнему обязан проверять сам и отвечать точнее (404/422 с полем): здесь
    только последний рубеж. Имя ограничения и значения ключа уходят в лог, но не в
    ответ — в них бывают ПДн и устройство схемы."""
    sqlstate = getattr(exc.orig, "pgcode", None)
    if sqlstate == _PG_UNIQUE_VIOLATION:
        code, detail = ErrorCode.DUPLICATE, "Запись с такими данными уже существует"
    elif sqlstate == _PG_FOREIGN_KEY_VIOLATION:
        code = ErrorCode.ENTITY_IN_USE
        if (exc.statement or "").lstrip().upper().startswith("DELETE"):
            detail = "Объект используется в других записях и не может быть удалён"
        else:
            detail = "Запись ссылается на несуществующий объект"
    else:
        return await unhandled_error_handler(request, exc)

    cause = getattr(exc.orig, "__cause__", None)
    logger.warning(
        "integrity_conflict",
        sqlstate=sqlstate,
        constraint=getattr(cause, "constraint_name", None),
        path=request.url.path,
        method=request.method,
    )
    return problem_response(
        code=code, detail=detail, instance=str(request.url.path), request=request
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(PydanticValidationError, pydantic_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(IntegrityError, integrity_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_error_handler)
