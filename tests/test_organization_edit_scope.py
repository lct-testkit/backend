"""Q-3 (QA 20.10): реквизиты вуза правит ответственный за него, руководитель его команды, ADMIN
или тот, кому выдан одобренный действующий доступ. Вуз, который КАМ видит лишь через свою сделку,
открывается для чтения, а правка даёт 403 (раньше скоуп записи совпадал со скоупом чтения).

Сквозные проверки на настоящей PostgreSQL (`TEST_DATABASE_URL`, см. `tests/conftest.py`)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import (
    create_deal,
    create_organization,
    create_published_workflow,
    create_team,
    create_user,
    login,
    sign_in,
)

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

FORBIDDEN_TEXT = (
    "Менять реквизиты вуза может ответственный за него или руководитель. Запросите доступ"
)


async def _set_owner(org_id: str, owner_id: uuid.UUID | None) -> None:
    from app.core.db import session_scope
    from app.modules.catalog.models import Organization

    async with session_scope() as session:
        org = await session.get(Organization, uuid.UUID(org_id))
        assert org is not None
        org.owner_id = owner_id
        await session.flush()


async def _set_team_head(team_id: uuid.UUID, head_id: uuid.UUID) -> None:
    from app.core.db import session_scope
    from app.modules.identity.models import Team

    async with session_scope() as session:
        team = await session.get(Team, team_id)
        assert team is not None
        team.head_id = head_id
        await session.flush()


async def _expire_access(request_id: str) -> None:
    import datetime as dt

    from app.core.db import session_scope
    from app.modules.catalog.models import AccessRequest

    async with session_scope() as session:
        req = await session.get(AccessRequest, uuid.UUID(request_id))
        assert req is not None
        req.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        await session.flush()


def _get(client, org_id: str) -> dict[str, Any]:
    response = client.get(f"/api/organizations/{org_id}")
    assert response.status_code == 200, response.text
    return response.json()


def _patch(client, org_id: str, **fields: Any):
    version = _get(client, org_id)["version"]
    return client.patch(
        f"/api/organizations/{org_id}", json=fields, headers={"If-Match": str(version)}
    )


def _deal_for(client, workflow_id: str, org_id: str, owner) -> None:
    """Вуз виден `owner` лишь потому, что у него есть сделка с этим вузом."""
    create_deal(client, workflow_id, organization_id=org_id, owner_id=str(owner.id))


def _setup_foreign_org_with_deal(client):
    """Вуз ведёт `owner`; у `kam` с ним лишь собственная сделка. Возвращает (org_id, owner, kam)."""
    owner = create_user(client, "KAM")
    kam = create_user(client, "KAM")
    org_id = create_organization(client)
    run(client, _set_owner, org_id, owner.id)
    login(client, "ADMIN")
    workflow = create_published_workflow(client)["workflow"]["id"]
    _deal_for(client, workflow, org_id, kam)
    return org_id, owner, kam


class TestKam:
    def test_owner_edits_and_sees_can_edit(self, client) -> None:
        owner = create_user(client, "KAM")
        org_id = create_organization(client)
        run(client, _set_owner, org_id, owner.id)
        sign_in(client, owner)

        assert _get(client, org_id)["can_edit"] is True
        response = _patch(client, org_id, short_name="МИФИ")
        assert response.status_code == 200, response.text
        assert response.json()["short_name"] == "МИФИ"
        assert response.json()["can_edit"] is True

    def test_deal_only_visibility_reads_but_cannot_edit(self, client) -> None:
        org_id, _owner, kam = _setup_foreign_org_with_deal(client)
        sign_in(client, kam)

        card = _get(client, org_id)
        assert card["can_edit"] is False
        response = _patch(client, org_id, short_name="Чужое название")
        assert response.status_code == 403, response.text
        body = response.json()
        assert body["code"] == "CRM-1102"
        assert body["detail"] == FORBIDDEN_TEXT
        # Данные не тронуты.
        after = _get(client, org_id)
        assert after["short_name"] == card["short_name"]
        assert after["version"] == card["version"]

    @pytest.mark.parametrize("action", ["apply-drift", "dismiss-drift"])
    def test_drift_actions_are_also_forbidden(self, client, action: str) -> None:
        org_id, _owner, kam = _setup_foreign_org_with_deal(client)
        sign_in(client, kam)

        version = _get(client, org_id)["version"]
        response = client.post(
            f"/api/organizations/{org_id}/{action}",
            json={"fields": []},
            headers={"If-Match": str(version)},
        )
        assert response.status_code == 403, response.text
        assert response.json()["detail"] == FORBIDDEN_TEXT

    def test_approved_access_allows_editing_until_revoked(self, client) -> None:
        org_id, owner, kam = _setup_foreign_org_with_deal(client)
        sign_in(client, kam)
        request_id = client.post(
            "/api/access-requests", json={"entity_id": org_id, "reason": "Нужно поправить адрес"}
        ).json()["id"]
        # Пока заявка не одобрена — только чтение.
        assert _get(client, org_id)["can_edit"] is False

        sign_in(client, owner)
        assert (
            client.post(
                f"/api/access-requests/{request_id}/decision", json={"decision": "approve"}
            ).status_code
            == 200
        )

        sign_in(client, kam)
        assert _get(client, org_id)["can_edit"] is True
        response = _patch(client, org_id, short_name="Правка по доступу")
        assert response.status_code == 200, response.text

        sign_in(client, owner)
        assert client.post(f"/api/access-requests/{request_id}/revoke").status_code == 200
        sign_in(client, kam)
        assert _get(client, org_id)["can_edit"] is False
        assert _patch(client, org_id, short_name="Ещё правка").status_code == 403

    def test_expired_access_no_longer_allows_editing(self, client) -> None:
        org_id, owner, kam = _setup_foreign_org_with_deal(client)
        sign_in(client, kam)
        request_id = client.post(
            "/api/access-requests", json={"entity_id": org_id, "reason": "Нужно поправить адрес"}
        ).json()["id"]
        sign_in(client, owner)
        client.post(f"/api/access-requests/{request_id}/decision", json={"decision": "approve"})
        run(client, _expire_access, request_id)

        sign_in(client, kam)
        assert _get(client, org_id)["can_edit"] is False
        assert _patch(client, org_id, short_name="После срока").status_code == 403

    def test_organization_without_an_owner_is_editable_by_whoever_sees_it(self, client) -> None:
        kam = create_user(client, "KAM")
        org_id = create_organization(client)
        login(client, "ADMIN")
        workflow = create_published_workflow(client)["workflow"]["id"]
        _deal_for(client, workflow, org_id, kam)

        sign_in(client, kam)
        assert _get(client, org_id)["can_edit"] is True
        assert _patch(client, org_id, short_name="Ничей вуз").status_code == 200

    def test_list_marks_each_organization(self, client) -> None:
        org_id, _owner, kam = _setup_foreign_org_with_deal(client)
        own_id = create_organization(client)
        run(client, _set_owner, own_id, kam.id)

        sign_in(client, kam)
        items = client.get("/api/organizations").json()["items"]
        flags = {item["id"]: item["can_edit"] for item in items}
        assert flags == {org_id: False, own_id: True}


class TestHead:
    def test_head_edits_organizations_of_own_team_only(self, client) -> None:
        team_id = create_team(client)
        head = create_user(client, "HEAD", team_id=team_id)
        member = create_user(client, "KAM", team_id=team_id)
        run(client, _set_team_head, team_id, head.id)
        other = create_user(client, "KAM")

        team_org = create_organization(client)
        run(client, _set_owner, team_org, member.id)
        foreign_org = create_organization(client)
        run(client, _set_owner, foreign_org, other.id)
        # Вуз чужой команды Руководитель видит лишь через сделку своего менеджера.
        login(client, "ADMIN")
        workflow = create_published_workflow(client)["workflow"]["id"]
        _deal_for(client, workflow, foreign_org, member)

        sign_in(client, head)
        assert _get(client, team_org)["can_edit"] is True
        assert _patch(client, team_org, short_name="Вуз команды").status_code == 200
        assert _get(client, foreign_org)["can_edit"] is False
        response = _patch(client, foreign_org, short_name="Чужая команда")
        assert response.status_code == 403, response.text
        assert response.json()["detail"] == FORBIDDEN_TEXT


class TestAdmin:
    def test_admin_edits_any_organization(self, client) -> None:
        owner = create_user(client, "KAM")
        org_id = create_organization(client)
        run(client, _set_owner, org_id, owner.id)

        login(client, "ADMIN")
        assert _get(client, org_id)["can_edit"] is True
        response = _patch(client, org_id, short_name="Правка администратора")
        assert response.status_code == 200, response.text
