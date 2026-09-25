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

    def test_without_if_match_the_edit_still_goes_through(self, client) -> None:
        # Заголовок необязателен: клиенты, которые его не шлют, продолжают работать.
        _login(client, run(client, _make_user, "ADMIN"))
        team = _create_team(client)

        response = self._patch(client, team, {"name": "Без заголовка"})

        assert response.status_code == 200, response.text
        assert response.json()["version"] == 2

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
