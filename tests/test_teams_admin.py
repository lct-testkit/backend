"""Команды в админке: карточка, время правки и оптимистичная блокировка
(`identity/router_admin.py`). Сквозные тесты на настоящей PostgreSQL
(`TEST_DATABASE_URL`) — см. докстринг `tests/conftest.py`.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _login(client, user) -> None:
    csrf = authenticate(client, user)
    client.headers["X-CSRF-Token"] = csrf


def _create_team(client, name: str | None = None) -> dict:
    response = client.post(
        "/api/admin/teams", json={"name": name or f"Команда {uuid.uuid4().hex[:6]}"}
    )
    assert response.status_code == 201, response.text
    return response.json()


class TestTeamCard:
    def test_card_returns_the_team_with_version_and_update_time(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        team = _create_team(client, "Команда Юг")

        response = client.get(f"/api/admin/teams/{team['id']}")

        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["id"], body["name"], body["version"]) == (team["id"], "Команда Юг", 1)
        assert body["updated_at"] and body["created_at"]

    def test_creation_and_listing_carry_the_new_fields(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))

        team = _create_team(client)
        listed = client.get("/api/admin/teams", params={"limit": 100}).json()["items"]

        assert team["version"] == 1 and team["updated_at"]
        assert all("version" in item and "updated_at" in item for item in listed)

    def test_unknown_team_is_404(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))

        assert client.get(f"/api/admin/teams/{uuid.uuid4()}").status_code == 404

    def test_only_user_readers_see_the_card(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        team = _create_team(client)
        _login(client, run(client, _make_user, "KAM"))

        assert client.get(f"/api/admin/teams/{team['id']}").status_code == 403


class TestTeamOptimisticLock:
    def _patch(self, client, team: dict, body: dict, **headers):
        return client.patch(f"/api/admin/teams/{team['id']}", json=body, headers=headers)

    def test_matching_version_updates_and_bumps_it(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        team = _create_team(client)

        response = self._patch(client, team, {"name": "Новое имя"}, **{"If-Match": "1"})

        assert response.status_code == 200, response.text
        assert (response.json()["name"], response.json()["version"]) == ("Новое имя", 2)

    def test_stale_version_is_a_conflict_with_the_current_one(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        team = _create_team(client)
        assert self._patch(client, team, {"name": "Первая правка"}, **{"If-Match": "1"}).is_success

        response = self._patch(client, team, {"name": "Вторая правка"}, **{"If-Match": "1"})

        assert response.status_code == 409, response.text
        body = response.json()
        assert (body["code"], body["current_version"]) == ("CRM-1002", 2)
        assert client.get(f"/api/admin/teams/{team['id']}").json()["name"] == "Первая правка"

    def test_without_if_match_the_request_is_rejected(self, client) -> None:
        # C-22: заголовок стал обязательным — той же формой ошибки, что у сделок/организаций.
        _login(client, run(client, _make_user, "ADMIN"))
        team = _create_team(client)

        response = self._patch(client, team, {"name": "Без заголовка"})

        assert response.status_code == 422, response.text
        body = response.json()
        assert body["code"] == "CRM-1001"
        assert body["errors"][0]["field"] == "If-Match"
        # Правка не применилась.
        assert client.get(f"/api/admin/teams/{team['id']}").json()["version"] == 1

    def test_edit_that_changes_nothing_keeps_the_version(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        team = _create_team(client, "Та же команда")

        response = self._patch(client, team, {"name": "Та же команда"}, **{"If-Match": "1"})

        assert response.status_code == 200, response.text
        assert response.json()["version"] == 1

    def test_malformed_if_match_is_a_validation_error(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        team = _create_team(client)

        response = self._patch(client, team, {"name": "X"}, **{"If-Match": "abc"})

        assert response.status_code == 422, response.text


def _add_member(client, team_id: str, *, status: str = "active") -> uuid.UUID:
    async def _create() -> uuid.UUID:
        from app.core.db import session_scope
        from app.modules.identity.models import User

        async with session_scope() as session:
            user = User(
                keycloak_id=str(uuid.uuid4()),
                email=f"{uuid.uuid4().hex[:12]}@rt-it-school.ru",
                full_name="Член команды",
                role="KAM",
                status=status,
                team_id=uuid.UUID(team_id),
                consent_version="1.0",
            )
            session.add(user)
            await session.flush()
            return user.id

    return run(client, _create)


class TestTeamDelete:
    """C-22: `DELETE /api/admin/teams/{id}` — только без активных сотрудников и живых
    дочерних команд."""

    def test_deletes_a_team_without_members(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        team = _create_team(client)

        response = client.delete(f"/api/admin/teams/{team['id']}")

        assert response.status_code == 204, response.text
        assert client.get(f"/api/admin/teams/{team['id']}").status_code == 404
        # Мягкое удаление — не видна и в списке.
        listing = client.get("/api/admin/teams", params={"limit": 100}).json()["items"]
        assert all(item["id"] != team["id"] for item in listing)

    def test_team_with_an_active_member_cannot_be_deleted(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        team = _create_team(client)
        _add_member(client, team["id"])

        response = client.delete(f"/api/admin/teams/{team['id']}")

        assert response.status_code == 409, response.text
        assert response.json()["code"] == "CRM-1303"
        # Не удалилась.
        assert client.get(f"/api/admin/teams/{team['id']}").status_code == 200

    def test_team_with_only_terminated_members_can_be_deleted(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        team = _create_team(client)
        _add_member(client, team["id"], status="terminated")

        response = client.delete(f"/api/admin/teams/{team['id']}")

        assert response.status_code == 204, response.text

    def test_team_with_a_child_team_cannot_be_deleted(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        parent = _create_team(client)
        child = client.post(
            "/api/admin/teams",
            json={"name": f"Дочерняя {uuid.uuid4().hex[:6]}", "parent_id": parent["id"]},
        )
        assert child.status_code == 201, child.text

        response = client.delete(f"/api/admin/teams/{parent['id']}")

        assert response.status_code == 409, response.text
        assert response.json()["code"] == "CRM-1303"

    def test_unknown_team_is_not_found(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))

        response = client.delete(f"/api/admin/teams/{uuid.uuid4()}")

        assert response.status_code == 404, response.text

    def test_kam_cannot_delete_a_team(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        team = _create_team(client)
        _login(client, run(client, _make_user, "KAM"))

        response = client.delete(f"/api/admin/teams/{team['id']}")

        assert response.status_code == 403, response.text
