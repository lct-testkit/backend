"""Справочник кодов ошибок (B9-4, НФР3): каждый код описан, `GET /api/meta/error-codes`
отдаёт их все. Без БД — модуль чистый (каталог статичен, ручка не трогает сессию/БД).
"""

from __future__ import annotations

import asyncio

import pytest

from app.api.meta import list_error_codes
from app.core.errors import ERROR_CATALOG, ErrorCode
from tests.conftest import TEST_DATABASE_URL


class TestErrorCatalogCompleteness:
    def test_every_error_code_is_in_the_catalog(self) -> None:
        missing = [code for code in ErrorCode if code not in ERROR_CATALOG]
        assert missing == []

    def test_every_catalog_entry_has_a_non_empty_description_and_hint(self) -> None:
        empty = [code.value for code, spec in ERROR_CATALOG.items() if not spec.description.strip()]
        assert empty == [], f"без description: {empty}"
        empty_hints = [code.value for code, spec in ERROR_CATALOG.items() if not spec.hint.strip()]
        assert empty_hints == [], f"без hint: {empty_hints}"

    def test_catalog_has_exactly_51_codes(self) -> None:
        # Число кодов не хардкодится в задаче эпика — но раз сегодня их 51 (как и в самом
        # ErrorCode), тест фиксирует соответствие между enum и каталогом, а не магическое число.
        assert len(ERROR_CATALOG) == len(list(ErrorCode))


class TestErrorCodesEndpoint:
    def test_returns_every_catalog_code_without_a_token(self) -> None:
        # Ручка без аргумента principal/Depends — вызов напрямую эквивалентен запросу без
        # заголовка Authorization и без сессии.
        response = asyncio.run(list_error_codes())
        codes = {item.code for item in response.items}
        assert codes == {code.value for code in ErrorCode}
        assert len(response.items) == len(ERROR_CATALOG)

    def test_each_item_matches_the_catalog_entry(self) -> None:
        response = asyncio.run(list_error_codes())
        by_code = {item.code: item for item in response.items}
        for code, spec in ERROR_CATALOG.items():
            item = by_code[code.value]
            assert item.name == code.name
            assert item.http_status == spec.status
            assert item.title == spec.title
            assert item.description == spec.description
            assert item.hint == spec.hint

    @pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
    )
    def test_real_http_request_without_any_credentials(self, client) -> None:
        # Ни сессии, ни Bearer-токена, ни CSRF — приёмочная команда задачи B9-4 буквально так и
        # проверяет справочник на живом стенде.
        response = client.get("/api/meta/error-codes")
        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == len(ERROR_CATALOG)
