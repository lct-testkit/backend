"""O-1 (QA 20.10): основной e-mail, телефон и сайт организации проверяются так же строго, как у
контакта. До правки `OrganizationCreate/Update` принимали `str` без валидатора, и в реестр
попадало `main_phone: "звоните"`, `main_email: "not-an-email"`, `website: "сайт"`.

Чистые проверки схем не требуют БД; сквозные (ответ 422 с именем поля, нормализация при записи,
PATCH без этих ключей не трогает старые данные) — с `TEST_DATABASE_URL`, см. `tests/conftest.py`.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.modules.catalog.schemas import (
    OrganizationCreateRequest,
    OrganizationUpdateRequest,
    normalize_website,
)
from tests.conftest import TEST_DATABASE_URL
from tests.crm_helpers import login


class TestNormalizeWebsite:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://example.ru", "https://example.ru"),
            ("http://www.example.ru/path?x=1", "http://www.example.ru/path?x=1"),
            ("example.ru", "https://example.ru"),
            ("  www.mephi.ru/about  ", "https://www.mephi.ru/about"),
            ("example.ru:8080/x", "https://example.ru:8080/x"),
            ("HTTPS://Example.RU", "https://Example.RU"),
            ("https://вуз.рф", "https://вуз.рф"),
            ("вуз.рф", "https://вуз.рф"),
        ],
    )
    def test_valid_addresses(self, raw: str, expected: str) -> None:
        assert normalize_website(raw) == expected

    @pytest.mark.parametrize(
        "raw",
        [
            "сайт",
            "localhost",
            "ftp://example.ru",
            "javascript:alert(1)",
            "mailto:a@b.ru",
            "https://",
            "https://no-dot",
            "exa mple.ru",
            "https://exa mple.ru",
            "example.ru:99999",
            ".ru",
        ],
    )
    def test_invalid_addresses(self, raw: str) -> None:
        assert normalize_website(raw) is None


class TestSchemas:
    def test_create_normalises_all_three(self) -> None:
        org = OrganizationCreateRequest(
            name="Вуз",
            main_email="  Info@Example.RU ",
            main_phone="8 (999) 123-45-67",
            website="example.ru",
        )
        assert org.main_email == "info@example.ru"
        assert org.main_phone == "+79991234567"
        assert org.website == "https://example.ru"

    def test_empty_strings_clear_the_fields(self) -> None:
        patch = OrganizationUpdateRequest(main_email="", main_phone=" ", website="")
        assert patch.main_email is None
        assert patch.main_phone is None
        assert patch.website is None

    @pytest.mark.parametrize(
        ("field", "value", "label"),
        [
            ("main_email", "not-an-email", "Основной e-mail"),
            ("main_phone", "звоните", "Основной телефон"),
            ("website", "сайт", "Сайт"),
        ],
    )
    @pytest.mark.parametrize("schema", [OrganizationCreateRequest, OrganizationUpdateRequest])
    def test_garbage_is_rejected_with_a_russian_field_message(
        self, schema, field: str, value: str, label: str
    ) -> None:
        with pytest.raises(ValidationError) as caught:
            schema(**{"name": "Вуз", field: value})
        (error,) = caught.value.errors()
        assert error["loc"] == (field,)
        assert error["msg"].startswith(label)


pytestmark_db = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


@pytestmark_db
class TestApi:
    def _create(self, client, **fields):
        body = {"name": f"ООО «Вуз {uuid.uuid4().hex[:8]}»", "org_type": "university", **fields}
        return client.post("/api/organizations", json=body)

    def test_create_with_garbage_is_422_and_names_the_fields(self, client) -> None:
        login(client, "KAM")
        response = self._create(
            client, main_phone="звоните", main_email="not-an-email", website="сайт"
        )
        assert response.status_code == 422, response.text
        errors = {item["field"]: item["reason"] for item in response.json()["errors"]}
        assert set(errors) == {"main_phone", "main_email", "website"}
        assert errors["main_phone"].startswith("Основной телефон")
        assert errors["main_email"].startswith("Основной e-mail")
        assert errors["website"].startswith("Сайт")

    def test_create_stores_normalised_values(self, client) -> None:
        login(client, "KAM")
        response = self._create(
            client, main_phone="8 999 123 45 67", main_email="Info@Example.RU", website="example.ru"
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["main_phone"] == "+79991234567"
        assert body["main_email"] == "info@example.ru"
        assert body["website"] == "https://example.ru"

    def test_patch_validates_and_untouched_keys_are_not_rechecked(self, client) -> None:
        login(client, "KAM")
        org = self._create(client, website="example.ru").json()
        bad = client.patch(
            f"/api/organizations/{org['id']}",
            json={"main_email": "nope"},
            headers={"If-Match": str(org["version"])},
        )
        assert bad.status_code == 422, bad.text
        assert bad.json()["errors"][0]["field"] == "main_email"

        good = client.patch(
            f"/api/organizations/{org['id']}",
            json={"students_count": 10},
            headers={"If-Match": str(org["version"])},
        )
        assert good.status_code == 200, good.text
        assert good.json()["website"] == "https://example.ru"

    def test_patch_empty_string_clears_the_field(self, client) -> None:
        login(client, "KAM")
        org = self._create(client, main_email="info@example.ru").json()
        response = client.patch(
            f"/api/organizations/{org['id']}",
            json={"main_email": ""},
            headers={"If-Match": str(org["version"])},
        )
        assert response.status_code == 200, response.text
        assert response.json()["main_email"] is None
