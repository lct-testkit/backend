"""Комментарии сделки: упомянутый получает уведомление, список ограничен по умолчанию.

Найдено внешним тестированием: `mentions` только сохранялись (упоминание ничего не значило), а
без `limit` отдавались все комментарии сделки разом.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import create_deal, create_published_workflow, create_user, login

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _mention_notifications(client, user_id, deal_id: str) -> list[dict]:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.notification.models import Notification

    async def _load() -> list[dict]:
        async with session_scope() as session:
            rows = await session.scalars(
                select(Notification).where(
                    Notification.recipient_id == user_id,
                    Notification.entity_id == uuid.UUID(deal_id),
                    Notification.template_code == "DEAL_MENTION",
                )
            )
            return [dict(row.payload) for row in rows]

    return run(client, _load)


class TestMentionNotifications:
    def test_mentioned_colleague_is_notified_and_the_author_is_not(self, client) -> None:
        colleague = create_user(client, "KAM")
        admin = login(client, "ADMIN")
        graph = create_published_workflow(client)
        deal = create_deal(client, graph["workflow"]["id"])

        response = client.post(
            f"/api/deals/{deal['id']}/comments",
            json={"body": "Посмотри", "mentions": [str(colleague.id), str(admin.id)]},
        )

        assert response.status_code == 201, response.text
        got = _mention_notifications(client, colleague.id, deal["id"])
        assert len(got) == 1
        assert got[0]["comment_id"] == response.json()["id"]
        assert got[0]["author_id"] == str(admin.id)
        # В уведомлении нет названия сделки: упомянутый мог не иметь к ней доступа.
        assert deal["title"] not in str(got[0])
        # Автор себя не уведомляет.
        assert _mention_notifications(client, admin.id, deal["id"]) == []

    def test_a_comment_without_mentions_notifies_nobody(self, client) -> None:
        colleague = create_user(client, "KAM")
        login(client, "ADMIN")
        graph = create_published_workflow(client)
        deal = create_deal(client, graph["workflow"]["id"])

        assert (
            client.post(f"/api/deals/{deal['id']}/comments", json={"body": "Так"}).status_code
            == 201
        )
        assert _mention_notifications(client, colleague.id, deal["id"]) == []

    def test_a_rejected_comment_notifies_nobody(self, client) -> None:
        colleague = create_user(client, "KAM")
        blocked = create_user(client, "KAM", status="blocked")
        login(client, "ADMIN")
        graph = create_published_workflow(client)
        deal = create_deal(client, graph["workflow"]["id"])

        response = client.post(
            f"/api/deals/{deal['id']}/comments",
            json={"body": "Так", "mentions": [str(colleague.id), str(blocked.id)]},
        )

        assert response.status_code == 422
        assert _mention_notifications(client, colleague.id, deal["id"]) == []


class TestDefaultCommentLimit:
    def test_without_a_limit_the_first_hundred_come_and_the_rest_by_cursor(self, client) -> None:
        from app.core.db import session_scope
        from app.modules.crm.models import DealComment

        login(client, "ADMIN")
        graph = create_published_workflow(client)
        deal = create_deal(client, graph["workflow"]["id"])

        async def _bulk() -> None:
            async with session_scope() as session:
                for index in range(105):
                    session.add(
                        DealComment(deal_id=uuid.UUID(deal["id"]), body=f"К{index}", is_system=True)
                    )

        run(client, _bulk)

        first = client.get(f"/api/deals/{deal['id']}/comments")
        assert first.status_code == 200, first.text
        assert len(first.json()["items"]) == 100
        assert first.json()["next_cursor"]

        rest = client.get(
            f"/api/deals/{deal['id']}/comments", params={"cursor": first.json()["next_cursor"]}
        )
        assert len(rest.json()["items"]) == 5
        assert rest.json()["next_cursor"] is None
        seen = {c["id"] for c in first.json()["items"]} | {c["id"] for c in rest.json()["items"]}
        assert len(seen) == 105
