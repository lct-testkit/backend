"""B4-2: массовая смена ответственных за вузы — `POST /api/organizations/bulk/owner`.

Параллельно проверяет исправление `organization_scope_clause` для HEAD: раньше
ветка «ответственный» сравнивала `owner_id` только с самим Руководителем, а не
с его командой, поэтому вуз его КАМа без единой сделки Руководителю не был
виден вовсе (ни прочитать, ни передать). В демо-сиде у всех пользователей
`team_id=null` (см. память проекта), поэтому тесты заводят свои команды, а не
переиспользуют демо-пользователей.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL
from tests.crm_helpers import create_published_workflow, create_team, create_user, login, sign_in

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _marker() -> str:
    return uuid.uuid4().hex[:10]


def _create_org(client, **fields):
    body = {"name": f"ООО «Вуз {_marker()}»", "org_type": "university", **fields}
    response = client.post("/api/organizations", json=body)
    assert response.status_code == 201, response.text
    return response.json()


class TestHeadSeesTeamOwnedOrganizations:
    def test_head_sees_a_kam_s_university_without_any_deal(self, client) -> None:
        team_id = create_team(client)
        head = create_user(client, "HEAD", team_id=team_id)
        kam = create_user(client, "KAM", team_id=team_id)

        sign_in(client, kam)
        org = _create_org(client)

        sign_in(client, head)
        # Раньше это было 404: HEAD видел только свои собственные вузы.
        got = client.get(f"/api/organizations/{org['id']}")
        assert got.status_code == 200, got.text

        listing = client.get("/api/organizations")
        assert org["id"] in [item["id"] for item in listing.json()["items"]]

    def test_head_does_not_see_a_university_of_another_team(self, client) -> None:
        team_a = create_team(client)
        team_b = create_team(client)
        head = create_user(client, "HEAD", team_id=team_a)
        other_kam = create_user(client, "KAM", team_id=team_b)

        sign_in(client, other_kam)
        org = _create_org(client)

        sign_in(client, head)
        got = client.get(f"/api/organizations/{org['id']}")
        assert got.status_code == 404, got.text


class TestBulkTransferByOwner:
    def test_transfer_five_universities_from_owner_to_owner(self, client) -> None:
        team_id = create_team(client)
        head = create_user(client, "HEAD", team_id=team_id)
        kam_a = create_user(client, "KAM", team_id=team_id)
        kam_b = create_user(client, "KAM", team_id=team_id)

        sign_in(client, kam_a)
        orgs = [_create_org(client) for _ in range(5)]

        sign_in(client, head)
        response = client.post(
            "/api/organizations/bulk/owner",
            json={
                "from_owner_id": str(kam_a.id),
                "owner_id": str(kam_b.id),
                "reason": "Перераспределение регионов",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["changed"] == 5
        assert body["remaining"] == 0
        batch_id = body["batch_id"]

        for org in orgs:
            audit = client.get("/api/admin/audit", params={"entity_id": org["id"]})
            records = [
                item
                for item in audit.json()["items"]
                if item["action"] == "ORGANIZATION_OWNER_CHANGED"
            ]
            assert records, audit.text
            assert records[0]["changes"]["batch_id"]["new"] == batch_id

        sign_in(client, kam_b)
        notifications = client.get("/api/notifications")
        payloads = [
            item["payload"]
            for item in notifications.json()["items"]
            if item["template_code"] == "ORG_OWNER_ASSIGNED"
        ]
        assert any(p.get("count") == 5 for p in payloads)

        # Старый владелец без сделок теряет вуз из своего обзора.
        sign_in(client, kam_a)
        for org in orgs:
            assert client.get(f"/api/organizations/{org['id']}").status_code == 404

        sign_in(client, kam_b)
        for org in orgs:
            assert client.get(f"/api/organizations/{org['id']}").status_code == 200

    def test_already_owner_is_skipped_not_double_counted(self, client) -> None:
        team_id = create_team(client)
        head = create_user(client, "HEAD", team_id=team_id)
        kam = create_user(client, "KAM", team_id=team_id)

        sign_in(client, kam)
        org = _create_org(client)

        sign_in(client, head)
        first = client.post(
            "/api/organizations/bulk/owner",
            json={"organization_ids": [org["id"]], "owner_id": str(kam.id), "reason": "noop"},
        )
        assert first.status_code == 200, first.text
        assert first.json()["changed"] == 0
        assert first.json()["skipped"][0]["reason"] == "already_owner"

    def test_null_owner_makes_universities_ownerless(self, client) -> None:
        login(client, "ADMIN")
        workflow = create_published_workflow(client, deal_type="b2b")

        team_id = create_team(client)
        head = create_user(client, "HEAD", team_id=team_id)
        kam = create_user(client, "KAM", team_id=team_id)

        sign_in(client, kam)
        org = _create_org(client)

        sign_in(client, head)
        response = client.post(
            "/api/organizations/bulk/owner",
            json={
                "organization_ids": [org["id"]],
                "owner_id": None,
                "reason": "Вуз выведен из портфеля",
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["changed"] == 1

        # Теперь вуз «ничей» — любой КАМ может завести на него сделку.
        other_kam = create_user(client, "KAM")
        sign_in(client, other_kam)
        deal = client.post(
            "/api/deals",
            json={
                "title": "Новая сделка",
                "deal_type": "b2b",
                "workflow_id": workflow["workflow"]["id"],
                "organization_id": org["id"],
            },
        )
        assert deal.status_code == 201, deal.text

    def test_both_selectors_or_neither_is_a_validation_error(self, client) -> None:
        team_id = create_team(client)
        head = create_user(client, "HEAD", team_id=team_id)
        kam = create_user(client, "KAM", team_id=team_id)
        sign_in(client, head)

        both = client.post(
            "/api/organizations/bulk/owner",
            json={
                "from_owner_id": str(kam.id),
                "organization_ids": [str(uuid.uuid4())],
                "owner_id": str(kam.id),
                "reason": "x",
            },
        )
        assert both.status_code == 422, both.text

        neither = client.post(
            "/api/organizations/bulk/owner",
            json={"owner_id": str(kam.id), "reason": "x"},
        )
        assert neither.status_code == 422, neither.text

    def test_kam_cannot_bulk_transfer(self, client) -> None:
        kam = create_user(client, "KAM")
        sign_in(client, kam)
        response = client.post(
            "/api/organizations/bulk/owner",
            json={"organization_ids": [str(uuid.uuid4())], "owner_id": None, "reason": "x"},
        )
        assert response.status_code == 403, response.text


class TestBulkTransferCarriesOpenDeals:
    def test_carry_open_deals_moves_only_open_deals_of_the_old_owner(self, client) -> None:
        login(client, "ADMIN")
        workflow = create_published_workflow(client, deal_type="b2b")

        team_id = create_team(client)
        head = create_user(client, "HEAD", team_id=team_id)
        kam_a = create_user(client, "KAM", team_id=team_id)
        kam_b = create_user(client, "KAM", team_id=team_id)

        sign_in(client, head)
        org = _create_org(client, owner_id=str(kam_a.id))
        open_deal = client.post(
            "/api/deals",
            json={
                "title": "Открытая сделка",
                "deal_type": "b2b",
                "workflow_id": workflow["workflow"]["id"],
                "organization_id": org["id"],
                "owner_id": str(kam_a.id),
            },
        ).json()

        response = client.post(
            "/api/organizations/bulk/owner",
            json={
                "organization_ids": [org["id"]],
                "owner_id": str(kam_b.id),
                "reason": "Передача портфеля",
                "carry_open_deals": True,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["deals_reassigned"] == 1

        moved = client.get(f"/api/deals/{open_deal['id']}")
        assert moved.status_code == 200, moved.text
        assert moved.json()["deal"]["owner_id"] == str(kam_b.id)
