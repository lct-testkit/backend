"""Вендор продукта и занятые коды (`/api/products`, каталог «Вендоры»).

У продукта появился `vendor_id` — организация-вендор; в выдаче рядом стоит её название
(`vendor_name`, только чтение), а список фильтруется по `?vendor_id=`. Код продукта уникален и среди
удалённых записей (индекс `uq_products_code` не смотрит на `deleted_at`), поэтому код удалённого
продукта — 409 CRM-1301, а не «Внутренняя ошибка». Настоящая Postgres обязательна — см.
`tests/conftest.py`.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _admin(client) -> None:
    admin = run(client, _make_user, "ADMIN")
    client.headers["X-CSRF-Token"] = authenticate(client, admin)


def _vendor(client, name: str | None = None) -> dict:
    response = client.post(
        "/api/organizations",
        json={"name": name or f"Вендор {uuid.uuid4().hex[:10]}", "org_type": "company"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _product(client, **fields):
    return client.post(
        "/api/products",
        json={"code": f"prod-{uuid.uuid4().hex[:10]}", "name": "Продукт вендора", **fields},
    )


def _soft_delete(client, model_name: str, entity_id: str) -> None:
    async def _delete() -> None:
        from app.core.db import session_scope
        from app.modules.catalog import models

        async with session_scope() as session:
            entity = await session.get(getattr(models, model_name), uuid.UUID(entity_id))
            assert entity is not None
            entity.deleted_at = dt.datetime.now(dt.UTC)

    run(client, _delete)


class TestProductVendor:
    def test_product_is_created_with_a_vendor_and_shows_its_name(self, client) -> None:
        _admin(client)
        vendor = _vendor(client)

        created = _product(client, vendor_id=vendor["id"])
        assert created.status_code == 201, created.text
        assert created.json()["vendor_id"] == vendor["id"]
        assert created.json()["vendor_name"] == vendor["name"]

    def test_vendor_name_is_read_only(self, client) -> None:
        _admin(client)

        created = _product(client, vendor_name="Подделка")
        assert created.status_code == 201, created.text
        assert created.json()["vendor_id"] is None
        assert created.json()["vendor_name"] is None

    def test_list_is_filtered_by_vendor_and_carries_the_names(self, client) -> None:
        _admin(client)
        vendor, other = _vendor(client), _vendor(client)
        mine = [_product(client, vendor_id=vendor["id"]).json()["id"] for _ in range(2)]
        _product(client, vendor_id=other["id"])
        _product(client)

        listed = client.get("/api/products", params={"vendor_id": vendor["id"]})
        assert listed.status_code == 200, listed.text
        items = listed.json()["items"]
        assert {item["id"] for item in items} == set(mine)
        assert {item["vendor_name"] for item in items} == {vendor["name"]}

        # Без фильтра название вендора приходит и на общей странице.
        everything = client.get("/api/products", params={"limit": 100}).json()["items"]
        named = {item["id"]: item["vendor_name"] for item in everything}
        assert all(named.get(product_id) == vendor["name"] for product_id in mine)

    def test_unknown_or_deleted_vendor_is_a_404(self, client) -> None:
        _admin(client)

        missing = _product(client, vendor_id=str(uuid.uuid4()))
        assert missing.status_code == 404, missing.text

        gone = _vendor(client)
        _soft_delete(client, "Organization", gone["id"])
        deleted = _product(client, vendor_id=gone["id"])
        assert deleted.status_code == 404, deleted.text
        assert deleted.json()["code"] == "CRM-9004"

    def test_patch_sets_changes_and_clears_the_vendor(self, client) -> None:
        _admin(client)
        first, second = _vendor(client), _vendor(client)
        product = _product(client, vendor_id=first["id"]).json()

        moved = client.patch(
            f"/api/products/{product['id']}",
            json={"vendor_id": second["id"]},
            headers={"If-Match": str(product["version"])},
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["vendor_name"] == second["name"]

        cleared = client.patch(
            f"/api/products/{product['id']}",
            json={"vendor_id": None},
            headers={"If-Match": str(moved.json()["version"])},
        )
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["vendor_id"] is None
        assert cleared.json()["vendor_name"] is None

    def test_patch_to_an_unknown_vendor_is_a_404(self, client) -> None:
        _admin(client)
        product = _product(client).json()

        response = client.patch(
            f"/api/products/{product['id']}",
            json={"vendor_id": str(uuid.uuid4())},
            headers={"If-Match": str(product["version"])},
        )
        assert response.status_code == 404, response.text

    def test_vendor_deleted_later_does_not_block_editing_the_product(self, client) -> None:
        _admin(client)
        vendor = _vendor(client)
        product = _product(client, vendor_id=vendor["id"]).json()
        _soft_delete(client, "Organization", vendor["id"])

        response = client.patch(
            f"/api/products/{product['id']}",
            json={"name": "Переименованный продукт", "vendor_id": vendor["id"]},
            headers={"If-Match": str(product["version"])},
        )
        assert response.status_code == 200, response.text


class TestProductCode:
    def test_code_of_a_deleted_product_is_a_conflict_not_a_500(self, client) -> None:
        _admin(client)
        code = f"prod-{uuid.uuid4().hex[:10]}"
        first = _product(client, code=code)
        assert first.status_code == 201, first.text
        _soft_delete(client, "Product", first.json()["id"])

        again = _product(client, code=code)
        assert again.status_code == 409, again.text
        body = again.json()
        assert body["code"] == "CRM-1301"
        assert body["deleted"] is True
        assert body["errors"][0]["field"] == "code"

    def test_code_of_an_active_product_is_also_a_conflict(self, client) -> None:
        """До этого PR дубль кода активного товара отдавал 422 (просто ошибка валидации),
        а дубль кода УДАЛЁННОГО — 409 CRM-1301 (тест выше): одна и та же проблема двумя
        разными кодами в зависимости от состояния существующей строки. Теперь оба случая
        симметричны — 409 CRM-1301 с полем в errors[], как и для удалённого."""
        _admin(client)
        code = f"prod-{uuid.uuid4().hex[:10]}"
        assert _product(client, code=code).status_code == 201

        again = _product(client, code=code)
        assert again.status_code == 409, again.text
        body = again.json()
        assert body["code"] == "CRM-1301"
        assert body["errors"][0]["field"] == "code"
