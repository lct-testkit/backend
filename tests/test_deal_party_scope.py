"""Сделка привязывает организацию и контакт только из скоупа менеджера.

Найдено внешним тестированием: KAM получал доступ к чужой организации, создав сделку с её
`organization_id` — проверялось только существование. Скоуп организаций строится через сделки,
так что такая сделка делала чужую организацию «своей»: открывались её карточка и реквизиты.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import (
    create_organization,
    create_published_workflow,
    create_user,
    login,
    sign_in,
)

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _own_organization(client, owner) -> str:
    async def _create() -> str:
        from app.core.db import session_scope
        from app.modules.catalog.models import Organization

        async with session_scope() as session:
            org = Organization(
                name=f"Вуз {uuid.uuid4().hex[:6]}", org_type="university", owner_id=owner.id
            )
            session.add(org)
            await session.flush()
            return str(org.id)

    return run(client, _create)


def _foreign_organization(client) -> str:
    """Организация с чужим ответственным: в скоупе другого менеджера, не нашего."""
    return _own_organization(client, create_user(client, "KAM"))


def _foreign_contact(client) -> str:
    """Контакт, заведённый другим пользователем: в чужом скоупе, ничьей «ничейности» у него нет."""

    async def _create(creator_id: uuid.UUID) -> str:
        from app.core.db import session_scope
        from app.modules.catalog.models import Contact

        async with session_scope() as session:
            contact = Contact(
                first_name="Чужой",
                last_name=f"Контакт-{uuid.uuid4().hex[:6]}",
                created_by=creator_id,
            )
            session.add(contact)
            await session.flush()
            return str(contact.id)

    return run(client, _create, create_user(client, "KAM").id)


def _payload(workflow_id: str, **extra) -> dict:
    return {
        "title": "Сделка",
        "deal_type": "b2b",
        "workflow_id": workflow_id,
        "priority": "normal",
        **extra,
    }


def _setup(client):
    login(client, "ADMIN")
    graph = create_published_workflow(client)
    return graph["workflow"]["id"]


class TestCreateDeal:
    def test_kam_cannot_take_a_foreign_organization_through_a_deal(self, client) -> None:
        workflow_id = _setup(client)
        foreign_org = _foreign_organization(client)
        kam = create_user(client, "KAM")
        sign_in(client, kam)

        response = client.post(
            "/api/deals", json=_payload(workflow_id, organization_id=foreign_org)
        )

        assert response.status_code == 404, response.text
        # И организация не стала видимой: сделки не появилось, скоуп не расширился.
        assert client.get(f"/api/organizations/{foreign_org}").status_code == 404

    def test_kam_can_start_with_an_unassigned_organization(self, client) -> None:
        # Организация из общего реестра без ответственного: чужого портфеля в ней нет.
        workflow_id = _setup(client)
        unassigned = create_organization(client)
        sign_in(client, create_user(client, "KAM"))

        response = client.post("/api/deals", json=_payload(workflow_id, organization_id=unassigned))

        assert response.status_code == 201, response.text

    def test_kam_can_use_own_organization(self, client) -> None:
        workflow_id = _setup(client)
        kam = create_user(client, "KAM")
        own_org = _own_organization(client, kam)
        sign_in(client, kam)

        response = client.post("/api/deals", json=_payload(workflow_id, organization_id=own_org))

        assert response.status_code == 201, response.text

    def test_kam_cannot_attach_a_foreign_contact(self, client) -> None:
        workflow_id = _setup(client)
        kam = create_user(client, "KAM")
        own_org = _own_organization(client, kam)
        foreign_contact = _foreign_contact(client)
        sign_in(client, kam)

        response = client.post(
            "/api/deals",
            json=_payload(workflow_id, organization_id=own_org, contact_id=foreign_contact),
        )

        assert response.status_code == 404, response.text
        assert client.get(f"/api/contacts/{foreign_contact}").status_code == 404

    def test_admin_is_not_restricted(self, client) -> None:
        workflow_id = _setup(client)
        foreign_org = _foreign_organization(client)
        response = client.post(
            "/api/deals", json=_payload(workflow_id, organization_id=foreign_org)
        )
        assert response.status_code == 201, response.text


class TestUpdateDeal:
    def test_patch_cannot_rebind_a_deal_to_a_foreign_organization(self, client) -> None:
        workflow_id = _setup(client)
        kam = create_user(client, "KAM")
        own_org = _own_organization(client, kam)
        foreign_org = _foreign_organization(client)
        sign_in(client, kam)
        deal = client.post("/api/deals", json=_payload(workflow_id, organization_id=own_org)).json()

        response = client.patch(
            f"/api/deals/{deal['id']}",
            json={"organization_id": foreign_org},
            headers={"If-Match": str(deal["version"])},
        )

        assert response.status_code == 404, response.text
        assert client.get(f"/api/organizations/{foreign_org}").status_code == 404

    def test_patch_to_the_same_organization_is_fine(self, client) -> None:
        workflow_id = _setup(client)
        kam = create_user(client, "KAM")
        own_org = _own_organization(client, kam)
        sign_in(client, kam)
        deal = client.post("/api/deals", json=_payload(workflow_id, organization_id=own_org)).json()

        response = client.patch(
            f"/api/deals/{deal['id']}",
            json={"organization_id": own_org, "title": "Новое название"},
            headers={"If-Match": str(deal["version"])},
        )

        assert response.status_code == 200, response.text
