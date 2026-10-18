"""B9-5: планка качества схемы — 0 операций без `description`, 0 латинских `summary`.

Не дублирует `test_openapi_contract.py` (коды ошибок, Problem Details, `Idempotency-Key`, теги)
и `test_error_codes_documented.py` (каталог кодов): здесь только текстовое качество описаний.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from app.api.openapi import build_openapi
from app.core.config import get_settings
from app.main import create_app

_METHODS = ("get", "put", "post", "delete", "patch")
# Латиница, цифры и обычная пунктуация — без единой кириллической буквы. Используем это, а не
# `str.isascii()`: ascii-строка без букв вовсе (например, пустая) не должна считаться «латинским
# summary» — пропускаем её отдельной проверкой на непустоту.
_LATIN_ONLY = re.compile(r"^[A-Za-z0-9 ,./()\-:]+$")
_HAS_LETTER = re.compile(r"[A-Za-zА-Яа-яЁё]")


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    settings = get_settings()
    return build_openapi(create_app(), settings)


def _operations(schema: dict[str, Any]) -> list[tuple[str, str, dict[str, Any]]]:
    return [
        (method, path, operation)
        for path, methods in schema["paths"].items()
        for method, operation in methods.items()
        if method in _METHODS
    ]


class TestEveryOperationHasADescription:
    def test_no_operation_is_missing_a_description(self, schema: dict[str, Any]) -> None:
        missing = [
            f"{method.upper()} {path}"
            for method, path, operation in _operations(schema)
            if not operation.get("description")
        ]
        assert missing == [], f"{len(missing)} операций без description: {missing}"


class TestNoLatinOnlySummary:
    def test_no_summary_is_written_only_in_latin(self, schema: dict[str, Any]) -> None:
        latin = [
            f"{method.upper()} {path} -> {operation.get('summary')!r}"
            for method, path, operation in _operations(schema)
            if (summary := operation.get("summary"))
            and _HAS_LETTER.search(summary)
            and _LATIN_ONLY.fullmatch(summary)
        ]
        assert latin == [], f"{len(latin)} summary без кириллицы: {latin}"

    def test_every_operation_has_a_non_empty_summary(self, schema: dict[str, Any]) -> None:
        missing = [
            f"{method.upper()} {path}"
            for method, path, operation in _operations(schema)
            if not operation.get("summary")
        ]
        assert missing == [], f"{len(missing)} операций без summary: {missing}"
