"""B4-8: поиск сделок по названию вуза, колонки продукта/направления, множественные
фильтры (`backend-sprints.md` строки 3045-3111).

Требует настоящую Postgres (`TEST_DATABASE_URL`), как и остальные сквозные тесты
модуля CRM. Воронку публикует ADMIN (`workflow:write`), сделки заводит КАМ — тем же
порядком, что `tests/test_deals.py::TestCreateDealOwner`.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import create_contact, create_published_workflow, create_user, login, sign_in

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


async def _make_org(
    name: str, short_name: str | None = None, inn: str | None = None, deleted: bool = False
) -> str:
    from app.core.db import session_scope
    from app.modules.catalog.models import Organization

    async with session_scope() as session:
        org = Organization(
            name=name,
            short_name=short_name,
            inn=inn,
            org_type="university",
            deleted_at=dt.datetime.now(dt.UTC) if deleted else None,
        )
        session.add(org)
        await session.flush()
        return str(org.id)


async def _make_direction(name: str, parent_id: str | None = None) -> str:
    from app.core.db import session_scope
    from app.modules.catalog.models import Direction

    async with session_scope() as session:
        direction = Direction(
            code=f"dir-{uuid.uuid4().hex[:10]}",
            name=name,
            parent_id=uuid.UUID(parent_id) if parent_id else None,
        )
        session.add(direction)
        await session.flush()
        return str(direction.id)


async def _make_product(name: str, direction_id: str | None = None) -> str:
    from app.core.db import session_scope
    from app.modules.catalog.models import Product

    async with session_scope() as session:
        product = Product(
            code=f"prod-{uuid.uuid4().hex[:10]}",
            name=name,
            direction_id=uuid.UUID(direction_id) if direction_id else None,
        )
        session.add(product)
        await session.flush()
        return str(product.id)


def _published_workflow_id(client, *, deal_type: str = "b2b") -> str:
    """ADMIN входит и публикует воронку; КАМ заводится следующим входом."""
    login(client)
    return create_published_workflow(client, deal_type=deal_type)["workflow"]["id"]


def _as_kam(client):
    kam = create_user(client, "KAM")
    sign_in(client, kam)
    return kam


def _create_b2b_deal(client, workflow_id: str, organization_id: str, **overrides):
    payload = {
        "title": overrides.pop("title", "Сделка для поиска"),
        "deal_type": "b2b",
        "workflow_id": workflow_id,
        "organization_id": organization_id,
        **overrides,
    }
    response = client.post("/api/deals", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


async def _soft_delete_org(org_id: str) -> None:
    from app.core.db import session_scope
    from app.modules.catalog.models import Organization

    async with session_scope() as session:
        org = await session.get(Organization, uuid.UUID(org_id))
        org.deleted_at = dt.datetime.now(dt.UTC)


class TestSearchByOrganization:
    def test_finds_deal_by_organization_name_case_insensitive(self, client) -> None:
        workflow_id = _published_workflow_id(client)
        _as_kam(client)
        org_id = run(client, _make_org, "МГТУ им. Баумана")
        _create_b2b_deal(client, workflow_id, org_id, title="Обучение преподавателей")

        response = client.get("/api/deals", params={"q": "мгту"})
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert any(item["organization_id"] == org_id for item in items)

    def test_finds_deal_by_inn_and_short_name(self, client) -> None:
        workflow_id = _published_workflow_id(client)
        _as_kam(client)
        # Случайный ИНН на запуск: уникальный индекс `uq_organizations_inn_active` не
        # пускает повтор фиксированного значения между прогонами на одной БД.
        inn = "16" + str(uuid.uuid4().int)[:8]
        org_id = run(client, _make_org, "Казанский университет", "КФУ", inn)
        _create_b2b_deal(client, workflow_id, org_id)

        by_inn = client.get("/api/deals", params={"q": inn})
        assert org_id in [item["organization_id"] for item in by_inn.json()["items"]]

        by_short = client.get("/api/deals", params={"q": "КФУ"})
        assert org_id in [item["organization_id"] for item in by_short.json()["items"]]

    def test_soft_deleted_organization_not_matched(self, client) -> None:
        workflow_id = _published_workflow_id(client)
        _as_kam(client)
        org_id = run(client, _make_org, "Удалённый Вуз Поиска")
        _create_b2b_deal(client, workflow_id, org_id)
        run(client, _soft_delete_org, org_id)

        response = client.get("/api/deals", params={"q": "Удалённый Вуз Поиска"})
        assert response.json()["items"] == []


class TestSearchEscaping:
    def test_percent_sign_is_literal(self, client) -> None:
        workflow_id = _published_workflow_id(client)
        _as_kam(client)
        org_id = run(client, _make_org, f"Вуз {uuid.uuid4().hex[:6]}")
        with_percent = _create_b2b_deal(
            client, workflow_id, org_id, title="Скидка 100% для партнёра"
        )
        _create_b2b_deal(client, workflow_id, org_id, title="Обычная сделка без скидки")

        response = client.get("/api/deals", params={"q": "100%"})
        ids = [item["id"] for item in response.json()["items"]]
        assert with_percent["id"] in ids
        assert len(ids) == 1

    def test_underscore_is_literal(self, client) -> None:
        workflow_id = _published_workflow_id(client)
        _as_kam(client)
        org_id = run(client, _make_org, f"Вуз {uuid.uuid4().hex[:6]}")
        with_underscore = _create_b2b_deal(
            client, workflow_id, org_id, title="Сделка_с_подчёркиванием"
        )
        _create_b2b_deal(client, workflow_id, org_id, title="Сделка без спецсимволов")

        response = client.get("/api/deals", params={"q": "_"})
        ids = [item["id"] for item in response.json()["items"]]
        # Без экранирования `_` матчил бы вообще любую непустую строку.
        assert ids == [with_underscore["id"]]


class TestMultiValueFilters:
    def test_product_id_repeated_is_union(self, client) -> None:
        workflow_id = _published_workflow_id(client)
        _as_kam(client)
        org_id = run(client, _make_org, f"Вуз {uuid.uuid4().hex[:6]}")
        product_a = run(client, _make_product, "Продукт А")
        product_b = run(client, _make_product, "Продукт Б")
        product_c = run(client, _make_product, "Продукт В")
        deal_a = _create_b2b_deal(client, workflow_id, org_id, products=[{"product_id": product_a}])
        deal_b = _create_b2b_deal(client, workflow_id, org_id, products=[{"product_id": product_b}])
        _create_b2b_deal(client, workflow_id, org_id, products=[{"product_id": product_c}])

        response = client.get(
            "/api/deals", params=[("product_id", product_a), ("product_id", product_b)]
        )
        ids = {item["id"] for item in response.json()["items"]}
        assert ids == {deal_a["id"], deal_b["id"]}

    def test_product_and_direction_filters_intersect(self, client) -> None:
        workflow_id = _published_workflow_id(client)
        _as_kam(client)
        org_id = run(client, _make_org, f"Вуз {uuid.uuid4().hex[:6]}")
        direction_a = run(client, _make_direction, "Python")
        direction_b = run(client, _make_direction, "QA")
        product_a = run(client, _make_product, "Курс Python", direction_a)
        product_b = run(client, _make_product, "Курс QA", direction_b)
        deal_a = _create_b2b_deal(client, workflow_id, org_id, products=[{"product_id": product_a}])
        _create_b2b_deal(client, workflow_id, org_id, products=[{"product_id": product_b}])

        response = client.get(
            "/api/deals", params={"product_id": product_a, "direction_id": direction_a}
        )
        ids = {item["id"] for item in response.json()["items"]}
        assert ids == {deal_a["id"]}

    def test_parent_direction_includes_children(self, client) -> None:
        workflow_id = _published_workflow_id(client)
        _as_kam(client)
        org_id = run(client, _make_org, f"Вуз {uuid.uuid4().hex[:6]}")
        parent = run(client, _make_direction, "ИТ")
        child = run(client, _make_direction, "Разработка", parent)
        product = run(client, _make_product, "Курс разработки", child)
        deal = _create_b2b_deal(client, workflow_id, org_id, products=[{"product_id": product}])

        response = client.get("/api/deals", params={"direction_id": parent})
        ids = {item["id"] for item in response.json()["items"]}
        assert ids == {deal["id"]}


class TestListColumns:
    def test_products_and_status_name_present(self, client) -> None:
        workflow_id = _published_workflow_id(client)
        _as_kam(client)
        org_id = run(client, _make_org, f"Вуз {uuid.uuid4().hex[:6]}")
        direction = run(client, _make_direction, "Python")
        product = run(client, _make_product, "Курс Python", direction)
        deal = _create_b2b_deal(
            client, workflow_id, org_id, products=[{"product_id": product, "quantity": 20}]
        )

        response = client.get("/api/deals", params={"organization_id": org_id})
        item = next(i for i in response.json()["items"] if i["id"] == deal["id"])
        assert item["status_name"]
        assert item["status_type"] == "initial"
        assert item["products_total"] == 1
        assert item["products"][0]["product_id"] == product
        assert item["products"][0]["direction_id"] == direction
        assert item["directions"] == [{"id": direction, "name": "Python"}]

    def test_b2c_deal_without_products_has_empty_lists(self, client) -> None:
        workflow_id = _published_workflow_id(client, deal_type="b2c")
        _as_kam(client)
        contact_id = create_contact(client)
        response = client.post(
            "/api/deals",
            json={
                "title": "B2C без продуктов",
                "deal_type": "b2c",
                "workflow_id": workflow_id,
                "contact_id": contact_id,
            },
        )
        assert response.status_code == 201, response.text
        deal_id = response.json()["id"]

        listed = client.get("/api/deals", params={"contact_id": contact_id})
        item = next(i for i in listed.json()["items"] if i["id"] == deal_id)
        assert item["products"] == []
        assert item["products_total"] == 0
        assert item["directions"] == []

    def test_products_list_capped_at_five(self, client) -> None:
        workflow_id = _published_workflow_id(client)
        _as_kam(client)
        org_id = run(client, _make_org, f"Вуз {uuid.uuid4().hex[:6]}")
        products = [run(client, _make_product, f"Продукт {i}") for i in range(7)]
        deal = _create_b2b_deal(
            client, workflow_id, org_id, products=[{"product_id": p} for p in products]
        )

        response = client.get("/api/deals", params={"organization_id": org_id})
        item = next(i for i in response.json()["items"] if i["id"] == deal["id"])
        assert item["products_total"] == 7
        assert len(item["products"]) == 5


class TestScopeNotBypassed:
    def test_kam_search_does_not_leak_other_kam_deals_via_organization_name(self, client) -> None:
        shared_name = f"Вуз Одно Имя {uuid.uuid4().hex[:6]}"
        workflow_id = _published_workflow_id(client)

        kam_a = _as_kam(client)
        org_a = run(client, _make_org, shared_name)
        deal_a = _create_b2b_deal(client, workflow_id, org_a)

        kam_b = _as_kam(client)
        org_b = run(client, _make_org, shared_name)
        _create_b2b_deal(client, workflow_id, org_b)
        assert kam_b.id != kam_a.id

        sign_in(client, kam_a)
        response = client.get("/api/deals", params={"q": shared_name})
        ids = {item["id"] for item in response.json()["items"]}
        assert ids == {deal_a["id"]}
