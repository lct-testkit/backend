"""Нарушения ограничений БД, которые не перехватил сервис (B#5 из
`frontend/docs/backend-issues.md`): дубль и ссылка на связанные данные — 409
Problem Details, а не «Внутренняя ошибка сервера». Всё остальное (NOT NULL,
CHECK) — дыра в валидации или баг, такая ошибка остаётся 500."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

from app.core.problem import integrity_error_handler
from tests.conftest import TEST_DATABASE_URL
from tests.crm_helpers import login


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/api/test", "headers": []})


def _integrity_error(sqlstate: str, statement: str) -> IntegrityError:
    orig = Exception("нарушено ограничение")
    orig.pgcode = sqlstate  # type: ignore[attr-defined]
    return IntegrityError(statement, {}, orig)


class TestIntegrityErrorHandler:
    async def test_unique_violation_is_a_duplicate_conflict(self) -> None:
        response = await integrity_error_handler(
            _request(), _integrity_error("23505", "INSERT INTO directions (code) VALUES ($1)")
        )
        body = json.loads(response.body)
        assert response.status_code == 409
        assert body["code"] == "CRM-1301"
        assert response.headers["content-type"].startswith("application/problem+json")

    async def test_deleting_a_referenced_row_is_an_in_use_conflict(self) -> None:
        response = await integrity_error_handler(
            _request(), _integrity_error("23503", "DELETE FROM workflow_statuses WHERE id = $1")
        )
        body = json.loads(response.body)
        assert response.status_code == 409
        assert body["code"] == "CRM-1303"
        assert "удал" in body["detail"]

    async def test_dangling_reference_is_a_conflict_too(self) -> None:
        response = await integrity_error_handler(
            _request(), _integrity_error("23503", "INSERT INTO products (direction_id) VALUES ($1)")
        )
        body = json.loads(response.body)
        assert response.status_code == 409
        assert body["code"] == "CRM-1303"
        assert "несуществующ" in body["detail"]

    @pytest.mark.parametrize("sqlstate", ["23502", "23514"])
    async def test_other_violations_stay_internal_errors(self, sqlstate: str) -> None:
        response = await integrity_error_handler(
            _request(), _integrity_error(sqlstate, "INSERT INTO directions (code) VALUES ($1)")
        )
        assert response.status_code == 500
        assert json.loads(response.body)["code"] == "CRM-9000"

    async def test_constraint_details_are_not_leaked(self) -> None:
        response = await integrity_error_handler(
            _request(), _integrity_error("23505", "INSERT INTO users (email) VALUES ($1)")
        )
        assert b"users" not in response.body


@pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)
class TestIntegrityErrorOverHttp:
    def test_missing_reference_is_a_conflict_not_a_500(self, client) -> None:
        # `POST /products` не проверяет направление: FK возвращает нарушение из БД.
        login(client)
        response = client.post(
            "/api/products",
            json={
                "code": f"prod-{uuid.uuid4().hex[:8]}",
                "name": "Продукт с чужим направлением",
                "direction_id": str(uuid.uuid4()),
            },
        )
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "CRM-1303"
        assert response.json()["request_id"]
