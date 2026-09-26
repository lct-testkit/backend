"""Ответственные за продукты: связь контакт — продукт (каталог «Вендоры»).

`GET /api/products/{id}/contacts`, `GET /api/contacts/{id}/products`,
`PUT`/`DELETE /api/products/{id}/contacts/{contact_id}`. Читать связи могут все менеджеры (контакты
в ответе маскированы), писать — только роль с правом записи каталога. Настоящая Postgres
обязательна — см. `tests/conftest.py`.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import login
from tests.people_helpers import (
    audit_entries,
    create_contact,
    create_product,
    unique_email,
    unique_phone,
)

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _link(client, product_id: str, contact_id: str, **body):
    return client.put(f"/api/products/{product_id}/contacts/{contact_id}", json=body)


def _soft_delete(client, model_name: str, entity_id: str) -> None:
    async def _delete() -> None:
        from app.core.db import session_scope
        from app.modules.catalog import models

        async with session_scope() as session:
            entity = await session.get(getattr(models, model_name), uuid.UUID(entity_id))
            assert entity is not None
            entity.deleted_at = dt.datetime.now(dt.UTC)

    run(client, _delete)


def _vendor_world(client) -> tuple[dict, dict, str, str]:
    """ADMIN заводит контакт с телефоном и email и продукт, назначает контакт ответственным.
    Возвращает (продукт, контакт, телефон, email) в исходном виде."""
    login(client, "ADMIN")
    phone, email = unique_phone(), unique_email("vendor")
    contact = create_contact(client, phone=phone, email=email)
    product = create_product(client)
    linked = _link(client, product["id"], contact["id"])
    assert linked.status_code == 200, linked.text
    return product, contact, phone, email


class TestLinkAndRead:
    def test_admin_links_a_contact_and_it_is_readable_from_both_sides(self, client) -> None:
        login(client, "ADMIN")
        contact = create_contact(client)
        product = create_product(client)

        linked = _link(client, product["id"], contact["id"], role="responsible")
        assert linked.status_code == 200, linked.text
        assert linked.json()["role"] == "responsible"
        assert linked.json()["contact"]["id"] == contact["id"]

        by_product = client.get(f"/api/products/{product['id']}/contacts")
        assert by_product.status_code == 200, by_product.text
        assert [(i["contact"]["id"], i["role"]) for i in by_product.json()["items"]] == [
            (contact["id"], "responsible")
        ]
        by_contact = client.get(f"/api/contacts/{contact['id']}/products")
        assert by_contact.status_code == 200, by_contact.text
        assert [(i["product"]["id"], i["role"]) for i in by_contact.json()["items"]] == [
            (product["id"], "responsible")
        ]

    def test_body_may_be_empty_but_an_unknown_role_is_refused(self, client) -> None:
        login(client, "ADMIN")
        contact, product = create_contact(client), create_product(client)

        assert _link(client, product["id"], contact["id"]).json()["role"] == "responsible"
        refused = _link(client, product["id"], contact["id"], role="boss")
        assert refused.status_code == 422, refused.text

    def test_repeating_the_link_is_idempotent_and_audited_once(self, client) -> None:
        login(client, "ADMIN")
        contact, product = create_contact(client), create_product(client)

        for _ in range(3):
            assert _link(client, product["id"], contact["id"]).status_code == 200

        listed = client.get(f"/api/products/{product['id']}/contacts").json()["items"]
        assert len(listed) == 1
        assert len(audit_entries(client, "CONTACT_PRODUCT_LINKED", product["id"])) == 1

    def test_several_products_and_several_contacts(self, client) -> None:
        login(client, "ADMIN")
        first, second = create_contact(client), create_contact(client)
        one, two = create_product(client), create_product(client)
        for product_id, contact_id in (
            (one["id"], first["id"]),
            (one["id"], second["id"]),
            (two["id"], first["id"]),
        ):
            assert _link(client, product_id, contact_id).status_code == 200

        assert len(client.get(f"/api/products/{one['id']}/contacts").json()["items"]) == 2
        assert len(client.get(f"/api/contacts/{first['id']}/products").json()["items"]) == 2
        assert len(client.get(f"/api/contacts/{second['id']}/products").json()["items"]) == 1

    def test_contact_products_carry_the_vendor_name(self, client) -> None:
        login(client, "ADMIN")
        vendor = client.post(
            "/api/organizations",
            json={"name": f"Вендор {uuid.uuid4().hex[:10]}", "org_type": "company"},
        ).json()
        contact = create_contact(client)
        product = create_product(client, vendor_id=vendor["id"])
        assert _link(client, product["id"], contact["id"]).status_code == 200

        item = client.get(f"/api/contacts/{contact['id']}/products").json()["items"][0]
        assert item["product"]["vendor_name"] == vendor["name"]

    def test_deleted_contacts_and_products_are_not_listed(self, client) -> None:
        login(client, "ADMIN")
        kept, gone = create_contact(client), create_contact(client)
        product, gone_product = create_product(client), create_product(client)
        for contact in (kept, gone):
            assert _link(client, product["id"], contact["id"]).status_code == 200
        assert _link(client, gone_product["id"], kept["id"]).status_code == 200
        _soft_delete(client, "Contact", gone["id"])
        _soft_delete(client, "Product", gone_product["id"])

        by_product = client.get(f"/api/products/{product['id']}/contacts").json()["items"]
        assert [i["contact"]["id"] for i in by_product] == [kept["id"]]
        by_contact = client.get(f"/api/contacts/{kept['id']}/products").json()["items"]
        assert [i["product"]["id"] for i in by_contact] == [product["id"]]

    def test_unknown_product_or_contact_is_a_404(self, client) -> None:
        login(client, "ADMIN")
        contact, product = create_contact(client), create_product(client)
        missing = str(uuid.uuid4())

        assert client.get(f"/api/products/{missing}/contacts").status_code == 404
        assert client.get(f"/api/contacts/{missing}/products").status_code == 404
        assert _link(client, missing, contact["id"]).status_code == 404
        assert _link(client, product["id"], missing).status_code == 404
        assert client.delete(f"/api/products/{missing}/contacts/{contact['id']}").status_code == 404


class TestUnlink:
    def test_delete_removes_the_link(self, client) -> None:
        product, contact, _phone, _email = _vendor_world(client)

        deleted = client.delete(f"/api/products/{product['id']}/contacts/{contact['id']}")
        assert deleted.status_code == 204, deleted.text
        assert client.get(f"/api/products/{product['id']}/contacts").json()["items"] == []
        assert client.get(f"/api/contacts/{contact['id']}/products").json()["items"] == []

        # Повторное снятие — уже нечего снимать.
        again = client.delete(f"/api/products/{product['id']}/contacts/{contact['id']}")
        assert again.status_code == 404, again.text


class TestVisibilityAndRoles:
    @pytest.mark.parametrize("role", ["KAM", "HEAD"])
    def test_managers_see_vendor_contacts_through_the_link_masked(self, client, role) -> None:
        product, contact, phone, email = _vendor_world(client)
        untouched = create_contact(client)  # контакт без связи менеджеру не виден

        login(client, role)
        listed = client.get(f"/api/products/{product['id']}/contacts")
        assert listed.status_code == 200, listed.text
        [item] = listed.json()["items"]
        assert item["role"] == "responsible"
        shown = item["contact"]
        assert shown["id"] == contact["id"]
        assert shown["phone"] == f"+7 (9**) ***-**-{phone[-2:]}"
        assert shown["email"] == f"{email[0]}***@example.ru"

        # Через связь виден и сам контакт, и его продукты; без связи контакт скрыт.
        assert client.get(f"/api/contacts/{contact['id']}").status_code == 200
        products = client.get(f"/api/contacts/{contact['id']}/products").json()["items"]
        assert [i["product"]["id"] for i in products] == [product["id"]]
        assert client.get(f"/api/contacts/{untouched['id']}").status_code == 404
        assert client.get(f"/api/contacts/{untouched['id']}/products").status_code == 404

    def test_kam_cannot_write_links(self, client) -> None:
        product, contact, _phone, _email = _vendor_world(client)
        other = create_contact(client)

        login(client, "KAM")
        put = _link(client, product["id"], other["id"])
        assert put.status_code == 403, put.text
        assert put.json()["code"] == "CRM-1102"
        delete = client.delete(f"/api/products/{product['id']}/contacts/{contact['id']}")
        assert delete.status_code == 403, delete.text

        login(client, "ADMIN")
        items = client.get(f"/api/products/{product['id']}/contacts").json()["items"]
        assert [i["contact"]["id"] for i in items] == [contact["id"]]

    def test_auditor_has_no_access(self, client) -> None:
        product, contact, _phone, _email = _vendor_world(client)

        login(client, "AUDITOR")
        assert client.get(f"/api/products/{product['id']}/contacts").status_code == 403
        assert client.get(f"/api/contacts/{contact['id']}/products").status_code == 403


class TestAudit:
    def test_link_and_unlink_are_audited_against_the_product(self, client) -> None:
        product, contact, _phone, _email = _vendor_world(client)

        [linked] = audit_entries(client, "CONTACT_PRODUCT_LINKED", product["id"])
        assert linked["entity_type"] == "product"
        assert linked["changes"] == {
            "contact_id": {"old": None, "new": contact["id"]},
            "role": {"old": None, "new": "responsible"},
        }

        assert (
            client.delete(f"/api/products/{product['id']}/contacts/{contact['id']}").status_code
            == 204
        )
        [unlinked] = audit_entries(client, "CONTACT_PRODUCT_UNLINKED", product["id"])
        assert unlinked["changes"] == {
            "contact_id": {"old": contact["id"], "new": None},
            "role": {"old": "responsible", "new": None},
        }
