"""Контракт OpenAPI: схема описывает то, что API делает на самом деле.

Проверяются вещи, которые расходятся с кодом молча: способы аутентификации
(Bearer и cookie-сессия с CSRF), публичные ручки, формат ошибок RFC 7807 и
`Idempotency-Key`. Тесты не требуют PostgreSQL, Redis и Keycloak.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.api.openapi import build_openapi, is_public_path
from app.core.config import get_settings
from app.core.csrf import SAFE_METHODS
from app.core.errors import ErrorCode, FieldError
from app.core.problem import PROBLEM_CONTENT_TYPE, build_problem, problem_response
from app.main import OPENAPI_DESCRIPTION, create_app

_METHODS = ("get", "put", "post", "delete", "options", "head", "patch")

# Ручки, которые сегодня действительно применяют `Idempotency-Key` (`core/idempotency.py` и
# вебхук CMS). Новая ручка с ключом — осознанное решение: добавьте её сюда и в описание API.
IDEMPOTENT_OPERATIONS = {
    ("post", "/api/contacts"),
    ("post", "/api/deals"),
    ("post", "/api/organizations"),
    ("post", "/api/deals/{deal_id}/comments"),
    ("post", "/api/tasks"),
    ("post", "/api/attachments"),
    ("post", "/api/v1/integrations/cms/leads"),
}

STANDARD_ERROR_CODES = ("400", "401", "403", "404", "409", "422", "429", "500")


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    settings = get_settings()
    return build_openapi(create_app(), settings)


def _operations(schema: dict[str, Any]):
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            if method in _METHODS:
                yield path, method, operation


def _resolve(schema: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    ref = response.get("$ref")
    if ref is None:
        return response
    node: Any = schema
    for part in ref.removeprefix("#/").split("/"):
        node = node[part]
    return node


class TestSecuritySchemes:
    def test_bearer_cookie_and_csrf_are_described(self, schema: dict[str, Any]) -> None:
        settings = get_settings()
        schemes = schema["components"]["securitySchemes"]

        assert schemes["BearerAuth"]["scheme"] == "bearer"
        assert schemes["SessionCookie"] == {
            **schemes["SessionCookie"],
            "type": "apiKey",
            "in": "cookie",
            "name": settings.session_cookie_name,
        }
        assert schemes["CsrfToken"] == {
            **schemes["CsrfToken"],
            "type": "apiKey",
            "in": "header",
            "name": settings.csrf_header_name,
        }

    def test_mutating_operations_require_csrf_together_with_the_cookie(
        self, schema: dict[str, Any]
    ) -> None:
        operation = schema["paths"]["/api/deals"]["post"]

        assert operation["security"] == [
            {"BearerAuth": []},
            {"SessionCookie": [], "CsrfToken": []},
        ]

    def test_safe_operations_inherit_bearer_or_cookie(self, schema: dict[str, Any]) -> None:
        operation = schema["paths"]["/api/deals"]["get"]

        assert "security" not in operation
        assert schema["security"] == [{"BearerAuth": []}, {"SessionCookie": []}]

    def test_no_authenticated_operation_lists_csrf_for_safe_methods(
        self, schema: dict[str, Any]
    ) -> None:
        for path, method, operation in _operations(schema):
            if method.upper() in SAFE_METHODS:
                assert "CsrfToken" not in json.dumps(operation.get("security", [])), path


class TestPublicOperations:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("get", "/health/live"),
            ("get", "/health/ready"),
            ("get", "/api/auth/login"),
            ("post", "/api/auth/callback"),
            ("post", "/api/auth/backchannel-logout"),
            ("get", "/api/auth/invite/{token}"),
            ("get", "/public/sign/{token}"),
            ("post", "/public/sign/{token}/sign"),
            ("get", "/public/verify/{signature_id}"),
            ("post", "/api/v1/integrations/cms/leads"),
            ("post", "/api/v1/integrations/bitrix/webhook"),
            ("post", "/api/v1/integrations/lms/progress"),
        ],
    )
    def test_public_routes_need_no_credentials(
        self, schema: dict[str, Any], method: str, path: str
    ) -> None:
        assert schema["paths"][path][method]["security"] == []

    def test_logout_takes_an_optional_session(self, schema: dict[str, Any]) -> None:
        # Выход без cookie — не ошибка, поэтому сессия необязательна.
        operation = schema["paths"]["/api/auth/logout"]["post"]

        assert operation["security"] == [{"SessionCookie": []}, {}]

    def test_public_list_matches_the_routes_that_skip_authentication(
        self, schema: dict[str, Any]
    ) -> None:
        """Ручка без `get_principal` не имеет заголовка `authorization` в схеме.

        Так схема не расходится с кодом: новая открытая ручка вне списка публичных
        (или закрытая — внутри него) роняет этот тест, а не остаётся тихой дырой в
        контракте.
        """
        settings = get_settings()
        for path, method, operation in _operations(schema):
            takes_auth = any(
                p["in"] == "header" and p["name"].lower() == "authorization"
                for p in operation.get("parameters", [])
            )
            if path == f"{settings.api_prefix}/auth/logout":
                continue
            assert takes_auth != is_public_path(path, settings), f"{method.upper()} {path}"


class TestProblemDetails:
    def test_problem_schema_covers_what_the_server_sends(self, schema: dict[str, Any]) -> None:
        body = build_problem(
            code=ErrorCode.VALIDATION,
            status=422,
            title="t",
            detail="d",
            instance="/api/x",
            errors=[FieldError(field="title", reason="r", code="missing")],
            request_id="rid",
        )
        problem = schema["components"]["schemas"]["Problem"]
        field_error = schema["components"]["schemas"]["ProblemFieldError"]

        assert set(problem["required"]) <= set(body)
        assert set(body) <= set(problem["properties"])
        for item in body["errors"]:  # type: ignore[attr-defined]
            assert set(field_error["required"]) <= set(item)
            assert set(item) <= set(field_error["properties"])

    def test_a_real_problem_response_has_the_documented_shape(self, schema: dict[str, Any]) -> None:
        response = problem_response(code=ErrorCode.NOT_FOUND, detail="нет", instance="/api/x")
        body = json.loads(response.body)
        problem = schema["components"]["schemas"]["Problem"]

        assert response.media_type == PROBLEM_CONTENT_TYPE
        assert set(problem["required"]) <= set(body)

    def test_examples_are_valid_problems(self, schema: dict[str, Any]) -> None:
        problem = schema["components"]["schemas"]["Problem"]
        for name, response in schema["components"]["responses"].items():
            example = response["content"][PROBLEM_CONTENT_TYPE]["example"]
            assert set(problem["required"]) <= set(example), name
            assert example["code"].startswith("CRM-"), name

    def test_every_operation_declares_the_shared_error_responses(
        self, schema: dict[str, Any]
    ) -> None:
        for path, method, operation in _operations(schema):
            responses = operation["responses"]
            for code in ("429", "500"):
                assert code in responses, f"{method.upper()} {path}: нет {code}"
            for code, raw in responses.items():
                if int(code) < 400:
                    continue
                content = _resolve(schema, raw).get("content", {})
                assert PROBLEM_CONTENT_TYPE in content, f"{method.upper()} {path}: {code}"
                assert content[PROBLEM_CONTENT_TYPE]["schema"] == {
                    "$ref": "#/components/schemas/Problem"
                }

    def test_authenticated_operations_declare_401_and_403(self, schema: dict[str, Any]) -> None:
        operation = schema["paths"]["/api/deals"]["get"]

        assert {"401", "403"} <= set(operation["responses"])

    def test_mutations_declare_conflict_and_validation(self, schema: dict[str, Any]) -> None:
        operation = schema["paths"]["/api/deals"]["post"]

        assert set(STANDARD_ERROR_CODES) <= set(operation["responses"])

    def test_fastapi_validation_error_schema_is_gone(self, schema: dict[str, Any]) -> None:
        # API отдаёт 422 как Problem: схема HTTPValidationError только вводила бы в заблуждение.
        assert "HTTPValidationError" not in schema["components"]["schemas"]
        assert "HTTPValidationError" not in json.dumps(schema["paths"])

    def test_handwritten_error_responses_are_kept(self, schema: dict[str, Any]) -> None:
        responses = schema["paths"]["/api/v1/integrations/cms/leads"]["post"]["responses"]

        assert "CRM-1701" in responses["401"]["description"]
        assert "256 КиБ" in responses["413"]["description"]
        assert PROBLEM_CONTENT_TYPE in responses["413"]["content"]

    def test_success_responses_are_untouched(self, schema: dict[str, Any]) -> None:
        response = schema["paths"]["/api/deals/{deal_id}/transition"]["post"]["responses"]["200"]

        assert response["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/TransitionResponse"
        }


class TestIdempotencyKey:
    def test_the_header_is_documented_exactly_where_the_code_reads_it(
        self, schema: dict[str, Any]
    ) -> None:
        documented = {
            (method, path)
            for path, method, operation in _operations(schema)
            if any(p["name"] == "Idempotency-Key" for p in operation.get("parameters", []))
        }

        assert documented == IDEMPOTENT_OPERATIONS

    def test_the_header_explains_its_rules(self, schema: dict[str, Any]) -> None:
        for method, path in IDEMPOTENT_OPERATIONS:
            parameters = schema["paths"][path][method]["parameters"]
            header = next(p for p in parameters if p["name"] == "Idempotency-Key")
            assert "255" in header["description"], path

    @pytest.mark.parametrize(("method", "path"), sorted(IDEMPOTENT_OPERATIONS))
    def test_the_api_description_lists_every_idempotent_route(self, method: str, path: str) -> None:
        # Общее описание раньше обещало ключ «всем создающим ручкам», а принимали его четыре.
        assert path in OPENAPI_DESCRIPTION, f"{method.upper()} {path}"
