"""Массовое переназначение сделок: только открытые, только активному преемнику из своей команды,
с системным комментарием в каждой сделке.

Найдено внешним тестированием: массовая передача трогала и закрытые сделки, не оставляла следа в
ленте сделки и не проверяла, что преемник — активный сотрудник (уволенному или чужой команде
сделки уходили молча).
"""

from __future__ import annotations

import pytest

from tests.conftest import TEST_DATABASE_URL
from tests.crm_helpers import (
    by_code,
    create_deal,
    create_published_workflow,
    create_team,
    create_user,
    login,
    sign_in,
)

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _bulk(client, deal_ids: list[str], successor_id, reason: str = "передача дел"):
    return client.post(
        "/api/deals/bulk/reassign",
        json={"deal_ids": deal_ids, "successor_id": str(successor_id), "reason": reason},
    )


def _prepare(client):
    login(client, "ADMIN")
    graph = create_published_workflow(client)
    return graph, by_code(graph)


def _comments(client, deal_id: str) -> list[dict]:
    response = client.get(f"/api/deals/{deal_id}/comments")
    assert response.status_code == 200, response.text
    body = response.json()
    return body["items"] if isinstance(body, dict) else body


class TestBulkReassign:
    def test_open_deals_move_and_leave_a_system_comment(self, client) -> None:
        graph, _codes = _prepare(client)
        successor = create_user(client, "KAM")
        deals = [create_deal(client, graph["workflow"]["id"]) for _ in range(2)]

        response = _bulk(client, [d["id"] for d in deals], successor.id)

        assert response.status_code == 200, response.text
        assert response.json()["reassigned_count"] == 2
        for deal in deals:
            card = client.get(f"/api/deals/{deal['id']}").json()["deal"]
            assert card["owner_id"] == str(successor.id)
            comments = _comments(client, deal["id"])
            assert any(
                c.get("is_system") and "передача дел" in c["body"] for c in comments
            ), comments

    def test_closed_deal_is_left_alone(self, client) -> None:
        import uuid

        from sqlalchemy import update

        from app.core.db import session_scope
        from app.modules.crm.models import Deal
        from tests.conftest import run

        graph, _codes = _prepare(client)
        successor = create_user(client, "KAM")
        open_deal = create_deal(client, graph["workflow"]["id"])
        closed = create_deal(client, graph["workflow"]["id"])

        async def close() -> None:
            async with session_scope() as session:
                await session.execute(
                    update(Deal)
                    .where(Deal.id == uuid.UUID(closed["id"]))
                    .values(closed_at=Deal.created_at)
                )

        # Закрываем в БД: в тестах блокировка перехода (Redis Lua) не снимается сразу, а второй
        # переход по той же сделке подряд отвечает 503.
        run(client, close)

        response = _bulk(client, [open_deal["id"], closed["id"]], successor.id)

        assert response.json()["reassigned_count"] == 1
        owner_after = client.get(f"/api/deals/{closed['id']}").json()["deal"]["owner_id"]
        assert owner_after == closed["owner_id"]

    def test_inactive_successor_is_rejected(self, client) -> None:
        graph, _codes = _prepare(client)
        blocked = create_user(client, "KAM", status="blocked")
        deal = create_deal(client, graph["workflow"]["id"])

        response = _bulk(client, [deal["id"]], blocked.id)

        assert response.status_code == 422, response.text
        assert client.get(f"/api/deals/{deal['id']}").json()["deal"]["owner_id"] != str(blocked.id)

    def test_unknown_successor_is_not_found(self, client) -> None:
        import uuid

        graph, _codes = _prepare(client)
        deal = create_deal(client, graph["workflow"]["id"])
        assert _bulk(client, [deal["id"]], uuid.uuid4()).status_code == 404

    def test_head_hands_over_only_inside_the_own_team(self, client) -> None:
        graph, _codes = _prepare(client)
        deal = create_deal(client, graph["workflow"]["id"])
        own_team, other_team = create_team(client), create_team(client)
        head = create_user(client, "HEAD", team_id=own_team)
        teammate = create_user(client, "KAM", team_id=own_team)
        outsider = create_user(client, "KAM", team_id=other_team)

        sign_in(client, head)
        # Сделка вне скоупа руководителя не переезжает и не «протекает» — счётчик 0.
        response = _bulk(client, [deal["id"]], outsider.id)
        assert response.status_code == 403, response.text
        response = _bulk(client, [deal["id"]], teammate.id)
        assert response.status_code == 200, response.text
        assert response.json()["reassigned_count"] == 0
