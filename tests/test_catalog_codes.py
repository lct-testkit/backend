"""C-3 (QA 20.10): одно правило кодов справочников — продукт и направление принимают
латиницу, цифры, «_», «-», «.» (импорт ищет продукт по коду). Прежние коды (`rt-datalake`,
`RT.CRM`) годятся."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.core.normalize import is_valid_catalog_code, slugify_code
from app.modules.catalog.schemas import DirectionCreateRequest, ProductCreateRequest
from app.modules.imports.fields import DIRECTION_FIELDS, PRODUCT_FIELDS, validate_field
from tests.conftest import TEST_DATABASE_URL
from tests.crm_helpers import login

GOOD = ["py-basic", "RT.CRM", "rt_datalake", "qa-auto", "1c", "A", "x" * 64]
BAD = ["код с пробелом!", "кириллица", "с пробелом", "-начинается-с-дефиса", "a/b", "a;b", "x" * 65]


@pytest.mark.parametrize("code", GOOD)
def test_valid_codes(code: str) -> None:
    assert is_valid_catalog_code(code)
    ProductCreateRequest(code=code, name="Продукт")
    DirectionCreateRequest(code=code, name="Направление")


@pytest.mark.parametrize("code", BAD)
def test_invalid_codes_are_rejected(code: str) -> None:
    assert not is_valid_catalog_code(code)
    with pytest.raises(ValidationError):
        ProductCreateRequest(code=code, name="Продукт")
    with pytest.raises(ValidationError):
        DirectionCreateRequest(code=code, name="Направление")


def test_what_slugify_produces_is_always_valid() -> None:
    for title in ["«Базис Dynamix»", "RT.DataLake", "Курсы по ----", "1С:Предприятие", "!!!"]:
        assert is_valid_catalog_code(slugify_code(title)), title


@pytest.mark.parametrize("fields", [PRODUCT_FIELDS, DIRECTION_FIELDS])
def test_import_applies_the_same_rule(fields) -> None:
    spec = next(f for f in fields if f.target == "code")
    assert validate_field(spec, "py-basic") == ("py-basic", None)
    value, error = validate_field(spec, "код с пробелом!")
    assert value is None
    assert error is not None and error.startswith("«Код»: код — латиница")


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL")
def test_api_returns_422_with_the_field(client) -> None:
    login(client, "ADMIN")
    for url in ("/api/products", "/api/directions"):
        response = client.post(url, json={"code": "код с пробелом!", "name": "X"})
        assert response.status_code == 422, response.text
        assert response.json()["errors"][0]["field"] == "code"
    ok = client.post(
        "/api/products", json={"code": f"c3-{uuid.uuid4().hex[:8]}", "name": "Нормальный код"}
    )
    assert ok.status_code == 201, ok.text
