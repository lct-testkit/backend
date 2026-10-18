"""B4-10 (A-19): филиалы и институты внутри вуза (`organization_branches`).

Сквозные тесты на настоящей PostgreSQL (`TEST_DATABASE_URL`). Скоуп видимости
филиала целиком наследуется от карточки организации — своей проверки у него
нет, поэтому сценарий «чужой вуз» проверяется так же, как для самой организации
(404, не 403 — раздел 3.2). Поэтому каждый вуз в тестах ниже сразу получает
владельца — иначе KAM не видит даже только что созданную организацию.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import create_organization, create_user, login, sign_in

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


async def _set_owner(org_id: str, owner_id: uuid.UUID) -> None:
    from app.core.db import session_scope
    from app.modules.catalog.models import Organization

    async with session_scope() as session:
        org = await session.get(Organization, uuid.UUID(org_id))
        assert org is not None
        org.owner_id = owner_id
        await session.flush()


def _org_owned_by(client, owner) -> str:
    org_id = create_organization(client)
    run(client, lambda: _set_owner(org_id, owner.id))
    return org_id


def _url(org_id: str, *parts: str) -> str:
    return "/".join([f"/api/organizations/{org_id}/branches", *parts])


def _create_branch(client, org_id: str, **overrides: Any):
    body = {"name": "Институт информатики", "address": None, "parent_id": None, **overrides}
    return client.post(_url(org_id), json=body)


async def _count_audit(action: str, entity_id: uuid.UUID) -> int:
    from sqlalchemy import func, select

    from app.core.db import session_scope
    from app.modules.audit.models import AuditLog

    async with session_scope() as session:
        return int(
            (
                await session.execute(
                    select(func.count(AuditLog.id)).where(
                        AuditLog.action == action, AuditLog.entity_id == entity_id
                    )
                )
            ).scalar_one()
        )


class TestCrudAndScope:
    def test_kam_creates_reads_updates_and_deletes_a_branch_of_their_own_org(self, client) -> None:
        kam = login(client, "KAM")
        org_id = _org_owned_by(client, kam)

        created = _create_branch(client, org_id, address="г. Казань, ул. Кремлёвская, 35")
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["organization_id"] == org_id
        assert body["name"] == "Институт информатики"
        assert body["parent_id"] is None

        listed = client.get(_url(org_id))
        assert listed.status_code == 200, listed.text
        assert {b["id"] for b in listed.json()["items"]} == {body["id"]}

        updated = client.patch(_url(org_id, body["id"]), json={"name": "Институт ПО"})
        assert updated.status_code == 200, updated.text
        assert updated.json()["name"] == "Институт ПО"

        deleted = client.delete(_url(org_id, body["id"]))
        assert deleted.status_code == 200, deleted.text

        after = client.get(_url(org_id))
        assert after.json()["items"] == []

        assert run(client, _count_audit, "ORGANIZATION_BRANCH_CREATED", uuid.UUID(body["id"])) == 1
        assert run(client, _count_audit, "ORGANIZATION_BRANCH_UPDATED", uuid.UUID(body["id"])) == 1
        assert run(client, _count_audit, "ORGANIZATION_BRANCH_DELETED", uuid.UUID(body["id"])) == 1

    def test_someone_elses_organization_is_not_found(self, client) -> None:
        owner = create_user(client, "KAM")
        stranger = create_user(client, "KAM")
        org_id = _org_owned_by(client, owner)

        sign_in(client, stranger)
        response = _create_branch(client, org_id)
        assert response.status_code == 404, response.text

    def test_unknown_organization_is_not_found(self, client) -> None:
        login(client, "KAM")
        response = _create_branch(client, str(uuid.uuid4()))
        assert response.status_code == 404, response.text


class TestHierarchy:
    def test_a_three_level_tree(self, client) -> None:
        kam = login(client, "KAM")
        org_id = _org_owned_by(client, kam)

        root = _create_branch(client, org_id, name="Институт").json()
        middle = _create_branch(client, org_id, name="Кафедра ПО", parent_id=root["id"]).json()
        leaf = _create_branch(client, org_id, name="Лаборатория", parent_id=middle["id"]).json()

        listed = client.get(_url(org_id)).json()["items"]
        by_id = {b["id"]: b for b in listed}
        assert by_id[middle["id"]]["parent_id"] == root["id"]
        assert by_id[leaf["id"]]["parent_id"] == middle["id"]

    def test_assigning_a_descendant_as_parent_is_422(self, client) -> None:
        kam = login(client, "KAM")
        org_id = _org_owned_by(client, kam)

        root = _create_branch(client, org_id, name="Институт").json()
        child = _create_branch(client, org_id, name="Кафедра", parent_id=root["id"]).json()

        response = client.patch(_url(org_id, root["id"]), json={"parent_id": child["id"]})
        assert response.status_code == 422, response.text

    def test_a_branch_cannot_be_its_own_parent(self, client) -> None:
        kam = login(client, "KAM")
        org_id = _org_owned_by(client, kam)
        branch = _create_branch(client, org_id).json()

        response = client.patch(_url(org_id, branch["id"]), json={"parent_id": branch["id"]})
        assert response.status_code == 422, response.text

    def test_parent_from_another_organization_is_422(self, client) -> None:
        kam = login(client, "KAM")
        org_a = _org_owned_by(client, kam)
        org_b = _org_owned_by(client, kam)
        branch_a = _create_branch(client, org_a).json()

        response = _create_branch(client, org_b, parent_id=branch_a["id"])
        assert response.status_code == 422, response.text


class TestDeletion:
    def test_deleting_a_parent_with_a_live_child_is_409(self, client) -> None:
        kam = login(client, "KAM")
        org_id = _org_owned_by(client, kam)
        root = _create_branch(client, org_id, name="Институт").json()
        _create_branch(client, org_id, name="Кафедра", parent_id=root["id"])

        response = client.delete(_url(org_id, root["id"]))
        assert response.status_code == 409, response.text

    def test_a_deleted_branch_is_not_in_the_list_and_not_found_directly(self, client) -> None:
        kam = login(client, "KAM")
        org_id = _org_owned_by(client, kam)
        branch = _create_branch(client, org_id).json()
        client.delete(_url(org_id, branch["id"]))

        listed = client.get(_url(org_id))
        assert branch["id"] not in {b["id"] for b in listed.json()["items"]}

        updated = client.patch(_url(org_id, branch["id"]), json={"name": "x"})
        assert updated.status_code == 404, updated.text


class TestUpdateEdgeCases:
    def test_explicit_null_name_is_422_not_500(self, client) -> None:
        kam = login(client, "KAM")
        org_id = _org_owned_by(client, kam)
        branch = _create_branch(client, org_id).json()

        response = client.patch(_url(org_id, branch["id"]), json={"name": None})
        assert response.status_code == 422, response.text

        unchanged = client.get(_url(org_id)).json()["items"][0]
        assert unchanged["name"] == branch["name"]

    def test_address_only_patch_persists_without_an_audit_entry(self, client) -> None:
        # По дизайну B4-10 адрес филиала не входит в аудит (не персональные
        # данные вуза, но и не то, что нужно в истории изменений) — само
        # изменение при этом сохраняется.
        kam = login(client, "KAM")
        org_id = _org_owned_by(client, kam)
        branch = _create_branch(client, org_id, address="Старый адрес").json()

        response = client.patch(_url(org_id, branch["id"]), json={"address": "Новый адрес"})
        assert response.status_code == 200, response.text
        assert response.json()["address"] == "Новый адрес"

        listed = client.get(_url(org_id)).json()["items"][0]
        assert listed["address"] == "Новый адрес"
        audit_count = run(
            client, _count_audit, "ORGANIZATION_BRANCH_UPDATED", uuid.UUID(branch["id"])
        )
        assert audit_count == 0


class TestRolePermissions:
    def test_auditor_cannot_create_a_branch(self, client) -> None:
        kam = login(client, "KAM")
        org_id = _org_owned_by(client, kam)

        login(client, "AUDITOR")
        response = _create_branch(client, org_id)
        assert response.status_code == 403, response.text

    def test_integration_cannot_create_a_branch(self, client) -> None:
        kam = login(client, "KAM")
        org_id = _org_owned_by(client, kam)

        login(client, "INTEGRATION")
        response = _create_branch(client, org_id)
        assert response.status_code == 403, response.text
