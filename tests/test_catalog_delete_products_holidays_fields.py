"""DELETE для продуктов, праздников и определений пользовательских полей (B-9, B-34).

До этой правки у всех трёх не было ручки удаления вовсе (только `is_active`/`PATCH` там, где они
есть). Тесты мирорят `tests/test_catalog.py::TestDeleteDirection`/`TestDeleteLossReason` (тот же
приём проверки 409 CRM-1303 при использовании), но для новых сущностей. Настоящая Postgres
обязательна — см. `tests/conftest.py`.
"""

from __future__ import annotations

import random
import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL
from tests.crm_helpers import create_deal, create_published_workflow, login

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _marker() -> str:
    return uuid.uuid4().hex[:8]


def _unique_date() -> str:
    # Не фиксированная дата: `uq_holidays_date` живёт дольше одного тестового прогона (обычная
    # БД, не транзакция с откатом), повтор конкретного числа ловил бы чужую оставленную запись.
    year, month, day = random.randint(2033, 2089), random.randint(1, 12), random.randint(1, 28)
    return f"{year:04d}-{month:02d}-{day:02d}"


class TestDeleteProduct:
    def _create(self, client, **fields):
        body = {"code": f"prod-{_marker()}", "name": "Продукт на удаление", **fields}
        response = client.post("/api/products", json=body)
        assert response.status_code == 201, response.text
        return response.json()

    def test_deletes_an_unused_product(self, client) -> None:
        login(client, "ADMIN")
        product = self._create(client)

        delete = client.delete(f"/api/products/{product['id']}")
        assert delete.status_code == 204, delete.text

        listing = client.get("/api/products", params={"code": product["code"]}).json()
        assert all(item["id"] != product["id"] for item in listing["items"])

    def test_product_used_by_a_deal_cannot_be_deleted(self, client) -> None:
        login(client, "ADMIN")
        product = self._create(client)
        graph = create_published_workflow(client)
        deal = create_deal(client, graph["workflow"]["id"])

        attach = client.put(
            f"/api/deals/{deal['id']}/products",
            json={"items": [{"product_id": product["id"]}]},
            headers={"If-Match": str(deal["version"])},
        )
        assert attach.status_code == 200, attach.text

        delete = client.delete(f"/api/products/{product['id']}")
        assert delete.status_code == 409, delete.text
        assert delete.json()["code"] == "CRM-1303"

    def test_unknown_product_is_not_found(self, client) -> None:
        login(client, "ADMIN")
        delete = client.delete(f"/api/products/{uuid.uuid4()}")
        assert delete.status_code == 404, delete.text

    def test_kam_cannot_delete_a_product(self, client) -> None:
        login(client, "ADMIN")
        product = self._create(client)

        login(client, "KAM")
        delete = client.delete(f"/api/products/{product['id']}")
        assert delete.status_code == 403, delete.text


class TestDeleteHoliday:
    def _create(self, client, *, date: str, **fields):
        body = {"date": date, "name": "Праздник", **fields}
        response = client.post("/api/holidays", json=body)
        assert response.status_code == 201, response.text
        return response.json()

    def test_deletes_a_holiday(self, client) -> None:
        login(client, "ADMIN")
        date = _unique_date()
        holiday = self._create(client, date=date)

        delete = client.delete(f"/api/holidays/{holiday['id']}")
        assert delete.status_code == 204, delete.text

        listing = client.get("/api/holidays", params={"date_from": date, "date_to": date}).json()
        assert listing["items"] == []

    def test_unknown_holiday_is_not_found(self, client) -> None:
        login(client, "ADMIN")
        delete = client.delete(f"/api/holidays/{uuid.uuid4()}")
        assert delete.status_code == 404, delete.text

    def test_kam_cannot_delete_a_holiday(self, client) -> None:
        login(client, "ADMIN")
        holiday = self._create(client, date=_unique_date())

        login(client, "KAM")
        delete = client.delete(f"/api/holidays/{holiday['id']}")
        assert delete.status_code == 403, delete.text


class TestDeleteCustomFieldDef:
    def _create_field(self, client, entity_type: str = "organization", **fields):
        body = {
            "entity_type": entity_type,
            "code": f"field_{_marker()}",
            "label": "Поле на удаление",
            "field_type": "string",
            **fields,
        }
        response = client.post("/api/custom-field-defs", json=body)
        assert response.status_code == 201, response.text
        return response.json()

    def test_deletes_an_unused_field(self, client) -> None:
        login(client, "ADMIN")
        field = self._create_field(client)

        delete = client.delete(f"/api/custom-field-defs/{field['id']}")
        assert delete.status_code == 204, delete.text

    def test_field_with_a_non_null_value_cannot_be_deleted(self, client) -> None:
        login(client, "ADMIN")
        field = self._create_field(client, entity_type="organization")

        org = client.post(
            "/api/organizations",
            json={
                "name": f"ООО «Со значением {_marker()}»",
                "org_type": "company",
                "custom_fields": {field["code"]: "занято"},
            },
        )
        assert org.status_code == 201, org.text

        delete = client.delete(f"/api/custom-field-defs/{field['id']}")
        assert delete.status_code == 409, delete.text
        assert delete.json()["code"] == "CRM-1303"

    def test_field_never_filled_in_deletes_freely(self, client) -> None:
        login(client, "ADMIN")
        field = self._create_field(client, entity_type="organization")

        # Организация существует, но значение по коду этого поля никогда не задавалось.
        org = client.post(
            "/api/organizations",
            json={"name": f"ООО «Без значения {_marker()}»", "org_type": "company"},
        )
        assert org.status_code == 201, org.text

        delete = client.delete(f"/api/custom-field-defs/{field['id']}")
        assert delete.status_code == 204, delete.text

    def test_contact_type_field_has_no_column_to_check_and_deletes_freely(self, client) -> None:
        login(client, "ADMIN")
        field = self._create_field(client, entity_type="contact")

        delete = client.delete(f"/api/custom-field-defs/{field['id']}")
        assert delete.status_code == 204, delete.text

    def test_unknown_field_is_not_found(self, client) -> None:
        login(client, "ADMIN")
        delete = client.delete(f"/api/custom-field-defs/{uuid.uuid4()}")
        assert delete.status_code == 404, delete.text

    def test_kam_cannot_delete_a_field(self, client) -> None:
        login(client, "ADMIN")
        field = self._create_field(client)

        login(client, "KAM")
        delete = client.delete(f"/api/custom-field-defs/{field['id']}")
        assert delete.status_code == 403, delete.text
