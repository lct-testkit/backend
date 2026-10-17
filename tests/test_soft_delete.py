"""B4-3 (A-16): мягкое удаление сделки и организации — матрица прав, блокеры, 409.

ТЗ, Функц.11 называет «удаление» среди прав Руководителя, но матрица прав `new_spec`
отдаёт мягкое удаление сделки и организации только ADMIN; это и проверяется ниже.
Настоящая Postgres обязательна — см. `tests/conftest.py`.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL
from tests.crm_helpers import create_contact, create_deal, create_published_workflow, login

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


class TestDeleteDealHappyPath:
    def test_admin_deletes_a_deal_and_can_restore_it(self, client) -> None:
        login(client, "ADMIN")
        workflow = create_published_workflow(client, deal_type="b2b")
        deal = create_deal(client, workflow["workflow"]["id"])

        response = client.request(
            "DELETE",
            f"/api/deals/{deal['id']}",
            json={"reason": "Дубль сделки, создан по ошибке"},
            headers={"If-Match": str(deal["version"])},
        )
        assert response.status_code == 200, response.text

        # Мягко удалённая — не видна обычному чтению и списку.
        assert client.get(f"/api/deals/{deal['id']}").status_code == 404
        listing = client.get("/api/deals")
        assert deal["id"] not in [item["id"] for item in listing.json()["items"]]

        # Но видна в списке удалённых (право есть у ADMIN).
        deleted_list = client.get("/api/deals", params={"deleted": "true"})
        assert deleted_list.status_code == 200, deleted_list.text
        assert deal["id"] in [item["id"] for item in deleted_list.json()["items"]]

        audit = client.get("/api/admin/audit", params={"entity_id": deal["id"]})
        actions = [item["action"] for item in audit.json()["items"]]
        assert "DEAL_DELETED" in actions

        restored = client.post(
            f"/api/deals/{deal['id']}/restore", headers={"If-Match": str(deal["version"] + 1)}
        )
        assert restored.status_code == 200, restored.text
        assert restored.json()["id"] == deal["id"]

        again = client.get(f"/api/deals/{deal['id']}")
        assert again.status_code == 200, again.text

        # Повторное восстановление уже не удалённой сделки — конфликт состояния.
        second_restore = client.post(
            f"/api/deals/{deal['id']}/restore", headers={"If-Match": str(deal["version"] + 2)}
        )
        assert second_restore.status_code == 409, second_restore.text
        assert second_restore.json()["code"] == "CRM-1305"


class TestDeleteDealPermissionMatrix:
    def test_head_and_kam_cannot_delete_a_deal(self, client) -> None:
        login(client, "ADMIN")
        workflow = create_published_workflow(client, deal_type="b2b")
        deal = create_deal(client, workflow["workflow"]["id"])

        login(client, "HEAD")
        head_resp = client.request(
            "DELETE",
            f"/api/deals/{deal['id']}",
            json={"reason": "x"},
            headers={"If-Match": str(deal["version"])},
        )
        assert head_resp.status_code == 403, head_resp.text

        login(client, "KAM")
        kam_resp = client.request(
            "DELETE",
            f"/api/deals/{deal['id']}",
            json={"reason": "x"},
            headers={"If-Match": str(deal["version"])},
        )
        assert kam_resp.status_code == 403, kam_resp.text

    def test_deleted_list_requires_delete_permission(self, client) -> None:
        login(client, "KAM")
        response = client.get("/api/deals", params={"deleted": "true"})
        assert response.status_code == 403, response.text


class TestDeletedDealHidesTasks:
    def test_task_of_a_deleted_deal_is_not_listed(self, client) -> None:
        admin = login(client, "ADMIN")
        workflow = create_published_workflow(client, deal_type="b2b")
        deal = create_deal(client, workflow["workflow"]["id"])
        task = client.post(
            "/api/tasks",
            json={
                "deal_id": deal["id"],
                "title": "Позвонить",
                "assignee_id": str(admin.id),
            },
        )
        assert task.status_code == 201, task.text

        before = client.get("/api/tasks")
        assert task.json()["id"] in [item["id"] for item in before.json()["items"]]

        client.request(
            "DELETE",
            f"/api/deals/{deal['id']}",
            json={"reason": "x"},
            headers={"If-Match": str(deal["version"])},
        )

        after = client.get("/api/tasks")
        assert task.json()["id"] not in [item["id"] for item in after.json()["items"]]


class TestOrganizationDeleteBlockers:
    def test_organization_with_a_live_deal_cannot_be_deleted(self, client) -> None:
        login(client, "ADMIN")
        org = _create_org(client)
        workflow = create_published_workflow(client, deal_type="b2b")
        deal = create_deal(client, workflow["workflow"]["id"], organization_id=org["id"])

        response = client.request(
            "DELETE",
            f"/api/organizations/{org['id']}",
            json={"reason": "x"},
            headers={"If-Match": str(org["version"])},
        )
        assert response.status_code == 409, response.text
        body = response.json()
        assert body["code"] == "CRM-1303"
        codes = {b["code"] for b in body["blockers"]}
        assert "live_deals" in codes

        # Удаляем сделку — блокер снят.
        client.request(
            "DELETE",
            f"/api/deals/{deal['id']}",
            json={"reason": "x"},
            headers={"If-Match": str(deal["version"])},
        )
        second = client.request(
            "DELETE",
            f"/api/organizations/{org['id']}",
            json={"reason": "x"},
            headers={"If-Match": str(org["version"])},
        )
        assert second.status_code == 200, second.text

    def test_organization_with_a_live_contact_cannot_be_deleted(self, client) -> None:
        login(client, "ADMIN")
        org = _create_org(client)
        contact_id = create_contact(client)
        client.patch(
            f"/api/contacts/{contact_id}",
            json={"organization_id": org["id"]},
            headers={"If-Match": "1"},
        )

        response = client.request(
            "DELETE",
            f"/api/organizations/{org['id']}",
            json={"reason": "x"},
            headers={"If-Match": str(org["version"])},
        )
        assert response.status_code == 409, response.text
        codes = {b["code"] for b in response.json()["blockers"]}
        assert "live_contacts" in codes

    def test_head_cannot_delete_an_organization(self, client) -> None:
        login(client, "ADMIN")
        org = _create_org(client)

        login(client, "HEAD")
        response = client.request(
            "DELETE",
            f"/api/organizations/{org['id']}",
            json={"reason": "x"},
            headers={"If-Match": str(org["version"])},
        )
        assert response.status_code == 403, response.text

    def test_deleted_organizations_list_requires_restore_permission(self, client) -> None:
        login(client, "KAM")
        response = client.get("/api/organizations", params={"deleted": "true"})
        assert response.status_code == 403, response.text


class TestDeleteStaleIfMatch:
    def test_stale_if_match_on_delete_is_a_conflict(self, client) -> None:
        login(client, "ADMIN")
        workflow = create_published_workflow(client, deal_type="b2b")
        deal = create_deal(client, workflow["workflow"]["id"])

        response = client.request(
            "DELETE",
            f"/api/deals/{deal['id']}",
            json={"reason": "x"},
            headers={"If-Match": str(deal["version"] + 5)},
        )
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "CRM-1002"
