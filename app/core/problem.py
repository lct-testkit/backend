"""RFC 7807 Problem Details — единственный формат ошибок наружу.

Раздел 2 спецификации: в теле обязательны `type`, `title`, `status`,
`detail`, `instance`, `request_id`, `code`; при необходимости — массив
`errors`. Стектрейсы наружу не отдаются никогда.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.exc import DataError, DBAPIError, IntegrityError
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

# SQLSTATE нарушений, которые вызвал сам клиент запросом: ссылка на связанные данные, дубль
# по уникальному индексу, пустое обязательное поле и значение вне CHECK. Последние два — дыра
# в валидации на границе API (сервис должен был ответить 422 с полем сам), но отвечать на них
# «Внутренняя ошибка» нечестно: виноват запрос, а не сервер, и клиенту нужно знать, что править.
_PG_FOREIGN_KEY_VIOLATION = "23503"
_PG_UNIQUE_VIOLATION = "23505"
_PG_NOT_NULL_VIOLATION = "23502"
_PG_CHECK_VIOLATION = "23514"

# Ошибки, после которых запрос можно повторить: взаимная блокировка, сбой сериализации,
# таймаут ожидания блокировки и отмена по statement_timeout. Это состояние БД, а не дефект
# запроса, поэтому 503 с `Retry-After`, а не 500.
_PG_RETRYABLE = frozenset({"40P01", "40001", "55P03", "57014"})


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
        # `mask_mapping` типизирована широко (`object -> object`, рекурсия по произвольным
        # структурам), но для входа-словаря всегда возвращает словарь — проверка вместо
        # `type: ignore`, чтобы мы не потеряли случай, когда это правда перестанет быть так.
        masked_extra = mask_mapping(extra)
        if isinstance(masked_extra, dict):
            body.update(masked_extra)
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


def _capitalize(text: str) -> str:
    return text[:1].upper() + text[1:] if text else text


def _number(value: object) -> str:
    """Граница числа без хвоста `.0` и лишних нулей: `0`, `999999999999.99`."""
    text = str(value)
    return text.rstrip("0").rstrip(".") if "." in text else text


def _expected_values(expected: object) -> str:
    """`'a', 'b' or 'c'` из pydantic -> `a, b, c`."""
    return str(expected).replace("' or '", "', '").replace("'", "")


# Тексты проверки на границе API по-русски: pydantic отдаёт английские фразы, а причина поля
# показывается пользователю под полем формы. Своя причина (`PydanticCustomError`, `ValueError` из
# валидатора) уже по-русски — ей добавляем только заглавную букву и убираем служебный префикс.
_BUILTIN_REASONS: dict[str, str] = {
    "missing": "Обязательное поле",
    "string_type": "Нужен текст",
    "int_type": "Нужно целое число",
    "int_parsing": "Нужно целое число",
    "int_from_float": "Нужно целое число",
    "int_parsing_size": "Число слишком велико",
    "float_type": "Нужно число",
    "float_parsing": "Нужно число",
    "decimal_type": "Нужно число",
    "decimal_parsing": "Нужно число",
    "finite_number": "Нужно конечное число",
    "bool_type": "Нужно значение «да» или «нет»",
    "bool_parsing": "Нужно значение «да» или «нет»",
    "date_type": "Некорректная дата",
    "date_parsing": "Некорректная дата",
    "date_from_datetime_parsing": "Некорректная дата",
    "datetime_type": "Некорректные дата и время",
    "datetime_parsing": "Некорректные дата и время",
    "datetime_from_date_parsing": "Некорректные дата и время",
    "uuid_type": "Некорректный идентификатор",
    "uuid_parsing": "Некорректный идентификатор",
    "enum": "Недопустимое значение",
    "list_type": "Нужен список",
    "dict_type": "Нужен объект",
    "model_type": "Нужен объект",
    "model_attributes_type": "Нужен объект",
    "json_invalid": "Некорректный JSON",
    "json_type": "Некорректный JSON",
    "extra_forbidden": "Лишнее поле",
    "url_parsing": "Некорректный адрес",
    "url_type": "Некорректный адрес",
    "url_scheme": "Недопустимый вид адреса",
    "bytes_type": "Нужны данные",
    "none_required": "Поле должно быть пустым",
    "unique_items": "Значения не должны повторяться",
}


def reason_text(error: dict[str, Any]) -> str:
    """Причина ошибки поля для человека: по-русски и с заглавной буквы."""
    kind = str(error.get("type", ""))
    message = str(error.get("msg", ""))
    ctx = error.get("ctx") or {}
    if kind == "string_too_short":
        return f"Слишком коротко: не меньше {ctx.get('min_length')} символов"
    if kind == "string_too_long":
        return f"Слишком длинно: не больше {ctx.get('max_length')} символов"
    if kind == "too_short":
        return f"Слишком мало элементов: не меньше {ctx.get('min_length')}"
    if kind == "too_long":
        return f"Слишком много элементов: не больше {ctx.get('max_length')}"
    if kind in {"greater_than_equal", "less_than_equal", "greater_than", "less_than"}:
        words = {
            "greater_than_equal": ("ge", "не меньше"),
            "less_than_equal": ("le", "не больше"),
            "greater_than": ("gt", "больше"),
            "less_than": ("lt", "меньше"),
        }[kind]
        return f"Значение должно быть {words[1]} {_number(ctx.get(words[0]))}"
    if kind == "literal_error":
        return f"Допустимые значения: {_expected_values(ctx.get('expected'))}"
    if kind in _BUILTIN_REASONS:
        return _BUILTIN_REASONS[kind]
    if kind == "value_error":
        message = message.removeprefix("Value error, ")
    elif kind == "assertion_error":
        message = message.removeprefix("Assertion failed, ")
    return _capitalize(message)


def _field_errors(errors: Sequence[Any]) -> list[FieldError]:
    return [
        FieldError(field=_loc_to_field(e["loc"]), reason=reason_text(e), code=e["type"])
        for e in errors
    ]


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
    errors = _field_errors(exc.errors())
    return problem_response(
        code=ErrorCode.VALIDATION,
        detail="Запрос не прошёл валидацию",
        instance=str(request.url.path),
        errors=errors,
        request=request,
    )


async def pydantic_error_handler(request: Request, exc: PydanticValidationError) -> JSONResponse:
    errors = _field_errors(exc.errors())
    return problem_response(
        code=ErrorCode.VALIDATION,
        detail="Внутренняя схема данных не прошла валидацию",
        instance=str(request.url.path),
        errors=errors,
        request=request,
    )


# Starlette отдаёт английские фразы («Not Found», «Method Not Allowed»): человеку — по-русски.
_HTTP_DETAILS = {
    400: "Некорректный запрос",
    404: "Ресурс не найден",
    405: "Для этого адреса такой способ обращения не поддерживается",
    406: "Такой формат ответа не поддерживается",
    408: "Время ожидания запроса истекло",
    413: "Слишком большой запрос",
    414: "Слишком длинный адрес запроса",
    415: "Такой тип данных не поддерживается",
}


def _http_detail(status: int, detail: str, code: ErrorCode) -> str:
    if any("а" <= ch.lower() <= "я" or ch.lower() == "ё" for ch in detail):
        return detail
    return _HTTP_DETAILS.get(status) or ERROR_CATALOG[code].title


async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = _STATUS_TO_CODE.get(exc.status_code, ErrorCode.INTERNAL)
    return problem_response(
        code=code,
        status=exc.status_code,
        detail=_http_detail(exc.status_code, str(exc.detail), code),
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


def _pg_cause(exc: DBAPIError) -> object | None:
    """Исходное исключение драйвера (у asyncpg — с `column_name`, `constraint_name`)."""
    return getattr(exc.orig, "__cause__", None)


async def integrity_error_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    """Страховка для нарушений ограничений БД, которые не перехватил сервис:
    дубль и ссылка на связанные данные — 409, пустое обязательное поле и значение вне CHECK —
    422, а не «Внутренняя ошибка». Сервис по-прежнему обязан проверять сам и отвечать точнее
    (404/422 с полем): здесь только последний рубеж. Имя ограничения и значения ключа уходят
    в лог, но не в ответ — в них бывают ПДн и устройство схемы (имя колонки NOT NULL — это
    имя поля запроса, его клиенту показать можно)."""
    sqlstate = getattr(exc.orig, "pgcode", None)
    cause = _pg_cause(exc)
    errors: list[FieldError] | None = None
    if sqlstate == _PG_UNIQUE_VIOLATION:
        code, detail = ErrorCode.DUPLICATE, "Запись с такими данными уже существует"
    elif sqlstate == _PG_FOREIGN_KEY_VIOLATION:
        code = ErrorCode.ENTITY_IN_USE
        if (exc.statement or "").lstrip().upper().startswith("DELETE"):
            detail = "Объект используется в других записях и не может быть удалён"
        else:
            detail = "Запись ссылается на несуществующий объект"
    elif sqlstate == _PG_NOT_NULL_VIOLATION:
        code, detail = ErrorCode.VALIDATION, "Обязательное поле не заполнено"
        column = getattr(cause, "column_name", None)
        if column:
            errors = [
                FieldError(field=str(column), reason="Обязательное поле, оставить пустым нельзя")
            ]
    elif sqlstate == _PG_CHECK_VIOLATION:
        code, detail = ErrorCode.VALIDATION, "Значение не допускается правилами данных"
    else:
        return await unhandled_error_handler(request, exc)

    logger.warning(
        "integrity_conflict",
        sqlstate=sqlstate,
        constraint=getattr(cause, "constraint_name", None),
        path=request.url.path,
        method=request.method,
    )
    return problem_response(
        code=code, detail=detail, instance=str(request.url.path), errors=errors, request=request
    )


async def data_error_handler(request: Request, exc: DataError) -> JSONResponse:
    """Значение не влезло в колонку или не разобралось (слишком длинная строка, число вне
    диапазона, битая дата): вина запроса, а не сервера — 422, как и на границе схемы."""
    logger.warning(
        "data_error",
        sqlstate=getattr(exc.orig, "pgcode", None),
        path=request.url.path,
        method=request.method,
    )
    return problem_response(
        code=ErrorCode.VALIDATION,
        detail="Значение поля не подходит по длине, диапазону или формату",
        instance=str(request.url.path),
        request=request,
    )


async def database_error_handler(request: Request, exc: DBAPIError) -> JSONResponse:
    """Остальные ошибки БД: временные (взаимная блокировка, ожидание блокировки) — 503 с
    просьбой повторить, прочие — обычный необработанный сбой."""
    sqlstate = getattr(exc.orig, "pgcode", None) or getattr(exc.orig, "sqlstate", None)
    if sqlstate in _PG_RETRYABLE:
        logger.warning(
            "database_retryable", sqlstate=sqlstate, path=request.url.path, method=request.method
        )
        return problem_response(
            code=ErrorCode.DEPENDENCY_UNAVAILABLE,
            detail="База данных временно занята другими операциями, повторите запрос",
            instance=str(request.url.path),
            headers={"Retry-After": "1"},
            request=request,
        )
    return await unhandled_error_handler(request, exc)


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(PydanticValidationError, pydantic_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, http_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(IntegrityError, integrity_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(DataError, data_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(DBAPIError, database_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_error_handler)
