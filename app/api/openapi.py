"""Схема OpenAPI: способы аутентификации, Problem Details и единые ответы об ошибках.

Схема — контракт с фронтендом и внешними интеграторами, поэтому она обязана
описывать то, что API делает на самом деле, а не то, что FastAPI вывел из
сигнатур ручек:

* аутентификация ходит не только по Bearer, а ещё и по сессионной cookie
  с double-submit CSRF-токеном на мутирующих методах (`core/deps.py`,
  `core/csrf.py`); публичные ручки не требуют ничего;
* ошибки отдаются только как RFC 7807 (`core/problem.py`), а не как
  `HTTPValidationError` из коробки FastAPI;
* правим готовую схему одним проходом, а не каждую ручку: ручек 200+, и
  «забытая» была бы тихим расхождением контракта с кодом.

Существующие операции и схемы ответов не переименовываются: из схемы строится
клиент фронтенда (`frontend/tools/gen-api.mjs`).
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings
from app.core.csrf import SAFE_METHODS
from app.core.errors import ErrorCode
from app.core.problem import PROBLEM_CONTENT_TYPE, PROBLEM_TYPE_BASE, REQUEST_ID_HEADER

_HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

# Входящие вебхуки живут вне `/api/*`-префикса приложения (см. main.py): путь буквальный.
_WEBHOOK_PREFIX = "/api/v1/integrations/"

SCHEME_BEARER = "BearerAuth"
SCHEME_SESSION = "SessionCookie"
SCHEME_CSRF = "CsrfToken"

_PROBLEM_REF = "#/components/schemas/Problem"


class ProblemFieldError(BaseModel):
    """Элемент массива `errors`: что не так с конкретным полем запроса."""

    field: str = Field(
        description="Путь к полю (`items.0.title`) или `__root__` для всего запроса."
    )
    reason: str = Field(description="Человекочитаемая причина.")
    code: str | None = Field(default=None, description="Машинный код проверки (`missing`, ...).")


class Problem(BaseModel):
    """RFC 7807 Problem Details — единственный формат ошибок этого API.

    Content-Type ответа — `application/problem+json`. Помимо перечисленных, тело
    может нести поля, специфичные для ошибки (например `limit` у 429).
    """

    # Тело строит `core.problem.build_problem`; дополнительные поля (`extra` ошибки)
    # добавляются к нему как есть, поэтому additionalProperties открыты.
    model_config = ConfigDict(extra="allow")

    type: str = Field(
        description=f"URI типа проблемы: `{PROBLEM_TYPE_BASE}/crm-xxxx` (код в нижнем регистре)."
    )
    title: str = Field(description="Краткое название ошибки из каталога.")
    status: int = Field(description="HTTP-статус, дублирует статус ответа.")
    detail: str = Field(description="Подробность для человека, без стектрейсов и ПДн.")
    instance: str = Field(description="Путь запроса, на котором возникла ошибка.")
    request_id: str | None = Field(
        description="Сквозной идентификатор запроса (он же `X-Request-Id`): "
        "его называют администратору при 500."
    )
    code: str = Field(
        pattern=r"^CRM-[0-9]{4}$",
        description="Внутренний код из каталога ошибок, `CRM-XXYY`: по нему, а не по "
        "тексту, клиент выбирает поведение.",
    )
    errors: list[ProblemFieldError] | None = Field(
        default=None, description="Ошибки по полям; есть у 422 и у части 409."
    )


def _example(code: ErrorCode, status: int, title: str, detail: str, **more: Any) -> dict[str, Any]:
    return {
        "type": f"{PROBLEM_TYPE_BASE}/{code.value.lower()}",
        "title": title,
        "status": status,
        "detail": detail,
        "instance": "/api/deals",
        "request_id": "0190f3c2-7d6e-7a41-8b2d-5f0a9c3e1b74",
        "code": code.value,
        **more,
    }


# Имя в components.responses -> (HTTP-статус, описание, пример тела, доп. заголовки).
# Статусы и коды взяты из ERROR_CATALOG: пример — реальный код каталога, а не выдумка.
_STANDARD_RESPONSES: dict[str, tuple[str, str, dict[str, Any], dict[str, Any]]] = {
    "400": (
        "BadRequest",
        "Запрос нечитаем: битое тело (multipart, кодировка), нарушен протокол.",
        _example(
            ErrorCode.INTERNAL,
            400,
            "Внутренняя ошибка сервера",
            "There was an error parsing the body",
        ),
        {},
    ),
    "401": (
        "Unauthorized",
        "Нет действующей сессии или токена (CRM-1101); у вебхуков — неверная подпись (CRM-1701).",
        _example(
            ErrorCode.UNAUTHENTICATED,
            401,
            "Пользователь не аутентифицирован",
            "Сессия не найдена: выполните вход",
        ),
        {},
    ),
    "403": (
        "Forbidden",
        "Недостаточно прав, пользователь заблокирован, не пройдена проверка CSRF (CRM-1107), "
        "не дано согласие на ПДн или требуется смена пароля.",
        _example(ErrorCode.FORBIDDEN, 403, "Недостаточно прав", "Недостаточно прав для операции"),
        {},
    ),
    "404": (
        "NotFound",
        "Объект не найден или скрыт от пользователя правами доступа.",
        _example(ErrorCode.NOT_FOUND, 404, "Ресурс не найден", "Сделка не найдена"),
        {},
    ),
    "409": (
        "Conflict",
        "Конфликт состояния: устаревшая версия (`If-Match`, CRM-1002), дубликат, "
        "повтор `Idempotency-Key` с другим телом (CRM-1003), объект используется.",
        _example(
            ErrorCode.VERSION_CONFLICT,
            409,
            "Конфликт версии",
            "Объект изменён другим пользователем",
        ),
        {},
    ),
    "422": (
        "UnprocessableEntity",
        "Запрос не прошёл валидацию или нарушает бизнес-правило; поля — в `errors`.",
        _example(
            ErrorCode.VALIDATION,
            422,
            "Ошибка валидации запроса",
            "Запрос не прошёл валидацию",
            errors=[{"field": "title", "reason": "Field required", "code": "missing"}],
        ),
        {},
    ),
    "429": (
        "TooManyRequests",
        "Превышен лимит частоты запросов (CRM-8429); повторить можно через `Retry-After` секунд.",
        _example(
            ErrorCode.RATE_LIMITED,
            429,
            "Превышен лимит частоты запросов",
            "Слишком много запросов, повторите позже",
            limit=10,
            window_seconds=60,
        ),
        {
            "Retry-After": {
                "description": "Через сколько секунд повторить запрос.",
                "schema": {"type": "string"},
            }
        },
    ),
    "500": (
        "InternalServerError",
        "Необработанный сбой сервера (CRM-9000): деталей нет, нужен `request_id`.",
        _example(
            ErrorCode.INTERNAL,
            500,
            "Внутренняя ошибка сервера",
            "Внутренняя ошибка сервера. Обратитесь к администратору с request_id.",
        ),
        {},
    ),
}


def _problem_response(
    description: str, example: dict[str, Any], headers: dict[str, Any]
) -> dict[str, Any]:
    return {
        "description": description,
        "headers": {
            REQUEST_ID_HEADER: {
                "description": "Сквозной идентификатор запроса, дублирует `request_id` тела.",
                "schema": {"type": "string"},
            },
            **headers,
        },
        "content": {PROBLEM_CONTENT_TYPE: {"schema": {"$ref": _PROBLEM_REF}, "example": example}},
    }


def _security_schemes(settings: Settings) -> dict[str, Any]:
    return {
        SCHEME_BEARER: {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": (
                "Access token из Keycloak. Вставьте только сам токен — слово Bearer "
                "добавлять не нужно, Swagger подставит его сам. Для сервисных вызовов "
                "и Swagger UI; в prod разрешён только роли INTEGRATION, браузер ходит "
                "по серверной сессии (`SessionCookie`)."
            ),
        },
        SCHEME_SESSION: {
            "type": "apiKey",
            "in": "cookie",
            "name": settings.session_cookie_name,
            "description": (
                "Серверная сессия BFF: httpOnly-cookie, её ставит `POST /api/auth/callback` "
                "после входа через Keycloak. Браузер шлёт её сам; в Swagger UI работает, "
                "если вход выполнен в этом же браузере. На POST/PUT/PATCH/DELETE вместе "
                f"с ней нужен заголовок `{settings.csrf_header_name}` (схема `CsrfToken`)."
            ),
        },
        SCHEME_CSRF: {
            "type": "apiKey",
            "in": "header",
            "name": settings.csrf_header_name,
            "description": (
                f"Double-submit CSRF: значение cookie `{settings.csrf_cookie_name}` "
                "(не httpOnly, ставится при входе вместе с сессией и приходит в поле "
                "`csrf_token` ответа `POST /api/auth/callback`) надо вернуть этим заголовком "
                "на POST/PUT/PATCH/DELETE при сессионной аутентификации. Расхождение — "
                "403 CRM-1107. Запросы с Bearer токена не требуют."
            ),
        },
    }


def _public_prefixes(settings: Settings) -> tuple[str, ...]:
    """Ручки без сессии и токена; остальные принимают Bearer или сессию."""
    return (
        "/health/",
        "/metrics",
        f"{settings.public_prefix}/",
        f"{settings.api_prefix}/auth/",
        _WEBHOOK_PREFIX,
    )


def is_public_path(path: str, settings: Settings) -> bool:
    return any(path == p or path.startswith(p) for p in _public_prefixes(settings))


def _security_for(path: str, method: str, settings: Settings) -> list[dict[str, list[str]]] | None:
    """Требования безопасности операции; `None` — унаследовать глобальные."""
    if path == f"{settings.api_prefix}/auth/logout":
        # Выход идемпотентен: без cookie это не ошибка, а с ней сессия гасится.
        return [{SCHEME_SESSION: []}, {}]
    if is_public_path(path, settings):
        return []
    if method.upper() in SAFE_METHODS:
        return None
    return [{SCHEME_BEARER: []}, {SCHEME_SESSION: [], SCHEME_CSRF: []}]


def _standard_codes(
    path: str, method: str, operation: dict[str, Any], settings: Settings
) -> list[str]:
    """Какие из стандартных ошибок операция реально может вернуть."""
    unsafe = method.upper() not in SAFE_METHODS
    public = is_public_path(path, settings)
    has_path_param = any(p.get("in") == "path" for p in operation.get("parameters", []))
    codes = ["429", "500"]
    if not public or path.startswith(_WEBHOOK_PREFIX):
        # У вебхуков 401 — это неверная подпись тела.
        codes.append("401")
    if not public:
        codes.append("403")
    if "requestBody" in operation:
        codes.append("400")
    if has_path_param or unsafe:
        codes.append("404")
    if unsafe:
        codes.append("409")
    # FastAPI уже добавил 422 всем ручкам с параметрами; мутирующим — добавляем сами.
    if unsafe or "422" in operation.get("responses", {}):
        codes.append("422")
    return codes


_IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"

_IDEMPOTENCY_KEY_DESCRIPTION = (
    "Ключ идемпотентности, 8–255 символов. Повтор с тем же ключом и телом возвращает "
    "сохранённый ответ, с другим телом — 409 CRM-1003. Действует в пределах пользователя, "
    "хранится 24 часа."
)
_WEBHOOK_IDEMPOTENCY_KEY_DESCRIPTION = (
    "Обязателен (без него 422): идентификатор доставки, до 255 символов. Повтор с тем же "
    "ключом возвращает итог первой обработки."
)


def _describe_idempotency_key(path: str, operation: dict[str, Any]) -> None:
    """Пояснение к заголовку: у самого параметра в коде описания нет, а условия
    (длина, срок, конфликт) клиенту нужны там же, где он его подставляет."""
    for parameter in operation.get("parameters", []):
        if parameter.get("name") == _IDEMPOTENCY_KEY_HEADER and "description" not in parameter:
            parameter["description"] = (
                _WEBHOOK_IDEMPOTENCY_KEY_DESCRIPTION
                if path.startswith(_WEBHOOK_PREFIX)
                else _IDEMPOTENCY_KEY_DESCRIPTION
            )


def _refs(node: Any) -> set[str]:
    """Имена схем, на которые где-либо ссылается узел."""
    found: set[str] = set()
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.add(ref.rsplit("/", 1)[-1])
        for value in node.values():
            found |= _refs(value)
    elif isinstance(node, list):
        for value in node:
            found |= _refs(value)
    return found


def build_openapi(app: FastAPI, settings: Settings) -> dict[str, Any]:
    schema = get_openapi(
        title=app.title,
        version=app.version,
        openapi_version=app.openapi_version,
        description=app.description,
        routes=app.routes,
    )
    components = schema.setdefault("components", {})
    schemas = components.setdefault("schemas", {})

    components["securitySchemes"] = _security_schemes(settings)
    # Глобально — то, что достаточно для безопасных методов; мутирующим ниже
    # добавляется CSRF-токен, публичным — пустой список.
    schema["security"] = [{SCHEME_BEARER: []}, {SCHEME_SESSION: []}]

    problem = Problem.model_json_schema(ref_template="#/components/schemas/{model}")
    schemas.update(problem.pop("$defs", {}))
    schemas["Problem"] = problem

    named = {
        code: (name, _problem_response(description, example, headers))
        for code, (name, description, example, headers) in _STANDARD_RESPONSES.items()
    }
    components["responses"] = dict(named.values())

    for path, methods in schema.get("paths", {}).items():
        for method, operation in methods.items():
            if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                continue
            override = _security_for(path, method, settings)
            if override is not None:
                operation["security"] = override

            _describe_idempotency_key(path, operation)

            responses = operation.setdefault("responses", {})
            for code in _standard_codes(path, method, operation, settings):
                name = named[code][0]
                current = responses.get(code)
                if code == "422" or current is None:
                    # Вместо HTTPValidationError, которого API не отдаёт.
                    responses[code] = {"$ref": f"#/components/responses/{name}"}
            # Ответы, объявленные на ручке вручную (413, 503, ...), — тоже Problem.
            for code, response in responses.items():
                if code.isdigit() and int(code) >= 400 and "$ref" not in response:
                    response.setdefault(
                        "content", {PROBLEM_CONTENT_TYPE: {"schema": {"$ref": _PROBLEM_REF}}}
                    )

    # Схемы ошибок валидации FastAPI больше никем не используются — не вводим в заблуждение.
    used = _refs(schema.get("paths", {})) | _refs(components.get("responses", {}))
    pending = list(used)
    while pending:
        for ref in _refs(schemas.get(pending.pop(), {})) - used:
            used.add(ref)
            pending.append(ref)
    for name in ("HTTPValidationError", "ValidationError"):
        if name in schemas and name not in used:
            del schemas[name]
    return schema


def install_openapi(app: FastAPI, settings: Settings) -> None:
    """Подменяет генерацию схемы приложения (результат кэшируется, как у FastAPI)."""

    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = build_openapi(app, settings)
        return app.openapi_schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
