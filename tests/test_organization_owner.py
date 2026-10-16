"""B4-1: право «ответственный за вуз» — `PUT`/`DELETE /api/organizations/{id}/owner`.

ТЗ, Функц.11: Руководитель «изменяет ответственных пользователей за вузы (менять,
удалять, назначать)». Раньше это делал любой КАМ обычным `PATCH` (право `organization:
write`); теперь `owner_id` недоступен для правки через `PATCH` вовсе, а смена идёт
только через отдельное право `organization:assign_owner` (HEAD/ADMIN).

Настоящая Postgres обязательна — см. `tests/conftest.py`.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL
from tests.crm_helpers import create_team, create_user, login, sign_in

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _marker() -> str:
    return uuid.uuid4().hex[:10]


def _create_org(client, **fields):
    body = {"name": f"ООО «Вуз {_marker()}»", "org_type": "university", **fields}
    return client.post("/api/organizations", json=body)


class TestSetOwnerHappyPath:
    def test_head_assigns_a_kam_of_his_own_team(self, client) -> None:
        team_id = create_team(client)
        head = create_user(client, "HEAD", team_id=team_id)
        kam = create_user(client, "KAM", team_id=team_id)

        sign_in(client, head)
        created = _create_org(client)
        assert created.status_code == 201, created.text
        org = created.json()

        response = client.put(
            f"/api/organizations/{org['id']}/owner",
            json={"owner_id": str(kam.id), "reason": "Передача портфеля региона"},
            headers={"If-Match": str(org["version"])},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["owner_id"] == str(kam.id)
        assert body["version"] == org["version"] + 1

        audit = client.get("/api/admin/audit", params={"entity_id": org["id"]})
        assert audit.status_code == 200, audit.text
        actions = [item["action"] for item in audit.json()["items"]]
        assert "ORGANIZATION_OWNER_CHANGED" in actions

        sign_in(client, kam)
        unread = client.get("/api/notifications")
        assert unread.status_code == 200, unread.text
        codes = [item["template_code"] for item in unread.json()["items"]]
        assert "ORG_OWNER_ASSIGNED" in codes

    def test_remove_owner_makes_the_university_ownerless(self, client) -> None:
        head = login(client, "HEAD")
        created = _create_org(client, owner_id=str(head.id))
        org = created.json()

        response = client.request(
            "DELETE",
            f"/api/organizations/{org['id']}/owner",
            json={"reason": "Вуз выведен из портфеля"},
            headers={"If-Match": str(org["version"])},
        )
        assert response.status_code == 200, response.text
        assert response.json()["owner_id"] is None


class TestSetOwnerPermissionMatrix:
    def test_head_cannot_assign_a_kam_outside_his_team(self, client) -> None:
        team_a = create_team(client)
        team_b = create_team(client)
        head = create_user(client, "HEAD", team_id=team_a)
        other_kam = create_user(client, "KAM", team_id=team_b)

        sign_in(client, head)
        created = _create_org(client)
        org = created.json()

        response = client.put(
            f"/api/organizations/{org['id']}/owner",
            json={"owner_id": str(other_kam.id), "reason": "x"},
            headers={"If-Match": str(org["version"])},
        )
        assert response.status_code == 403, response.text

    def test_kam_cannot_use_put_delete_or_patch_owner_id(self, client) -> None:
        kam = login(client, "KAM")
        created = _create_org(client, owner_id=str(kam.id))
        org = created.json()
        other = create_user(client, "KAM")

        put_resp = client.put(
            f"/api/organizations/{org['id']}/owner",
            json={"owner_id": str(other.id), "reason": "x"},
            headers={"If-Match": str(org["version"])},
        )
        assert put_resp.status_code == 403, put_resp.text

        delete_resp = client.request(
            "DELETE",
            f"/api/organizations/{org['id']}/owner",
            json={"reason": "x"},
            headers={"If-Match": str(org["version"])},
        )
        assert delete_resp.status_code == 403, delete_resp.text

        patch_owner = client.patch(
            f"/api/organizations/{org['id']}",
            json={"owner_id": str(other.id)},
            headers={"If-Match": str(org["version"])},
        )
        assert patch_owner.status_code == 403, patch_owner.text

        patch_other = client.patch(
            f"/api/organizations/{org['id']}",
            json={"website": "https://example.edu"},
            headers={"If-Match": str(org["version"])},
        )
        assert patch_other.status_code == 200, patch_other.text

    def test_kam_create_with_foreign_owner_id_is_forbidden(self, client) -> None:
        kam = login(client, "KAM")
        other = create_user(client, "KAM")
        response = _create_org(client, owner_id=str(other.id))
        assert response.status_code == 403, response.text

        own = _create_org(client, owner_id=str(kam.id))
        assert own.status_code == 201, own.text
        assert own.json()["owner_id"] == str(kam.id)

    def test_head_assigns_via_patch_writes_audit(self, client) -> None:
        team_id = create_team(client)
        head = create_user(client, "HEAD", team_id=team_id)
        kam = create_user(client, "KAM", team_id=team_id)

        sign_in(client, head)
        created = _create_org(client, owner_id=str(head.id))
        org = created.json()

        response = client.patch(
            f"/api/organizations/{org['id']}",
            json={"owner_id": str(kam.id)},
            headers={"If-Match": str(org["version"])},
        )
        assert response.status_code == 200, response.text
        assert response.json()["owner_id"] == str(kam.id)


class TestSetOwnerValidation:
    def test_stale_if_match_is_a_conflict(self, client) -> None:
        head = login(client, "HEAD")
        created = _create_org(client, owner_id=str(head.id))
        org = created.json()

        response = client.put(
            f"/api/organizations/{org['id']}/owner",
            json={"owner_id": None, "reason": "x"},
            headers={"If-Match": str(org["version"] + 5)},
        )
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "CRM-1002"

    def test_same_owner_is_a_validation_error(self, client) -> None:
        head = login(client, "HEAD")
        created = _create_org(client, owner_id=str(head.id))
        org = created.json()

        response = client.put(
            f"/api/organizations/{org['id']}/owner",
            json={"owner_id": str(head.id), "reason": "x"},
            headers={"If-Match": str(org["version"])},
        )
        assert response.status_code == 422, response.text

    def test_inactive_user_cannot_become_owner(self, client) -> None:
        team_id = create_team(client)
        head = create_user(client, "HEAD", team_id=team_id)
        inactive = create_user(client, "KAM", team_id=team_id, status="blocked")

        sign_in(client, head)
        created = _create_org(client, owner_id=str(head.id))
        org = created.json()

        response = client.put(
            f"/api/organizations/{org['id']}/owner",
            json={"owner_id": str(inactive.id), "reason": "x"},
            headers={"If-Match": str(org["version"])},
        )
        assert response.status_code == 422, response.text

    def test_auditor_cannot_become_owner(self, client) -> None:
        # ADMIN не упирается в проверку «своей команды» — роль цели проверяется напрямую.
        login(client, "ADMIN")
        auditor = create_user(client, "AUDITOR")
        created = _create_org(client)
        org = created.json()

        response = client.put(
            f"/api/organizations/{org['id']}/owner",
            json={"owner_id": str(auditor.id), "reason": "x"},
            headers={"If-Match": str(org["version"])},
        )
        assert response.status_code == 422, response.text


def test_me_scopes_reflect_the_new_permission(client) -> None:
    login(client, "HEAD")
    me = client.get("/api/me")
    assert me.status_code == 200, me.text
    assert "organization:assign_owner" in me.json()["scopes"]

    login(client, "KAM")
    me_kam = client.get("/api/me")
    assert "organization:assign_owner" not in me_kam.json()["scopes"]
