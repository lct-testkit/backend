"""Курсорная пагинация списков интеграций (`/admin/integrations/*`).

Тело ответа остаётся массивом (форма не меняется), курсор едет в заголовке
`X-Next-Cursor`. Сквозные тесты на настоящей PostgreSQL (`TEST_DATABASE_URL`) —
см. докстринг `tests/conftest.py`. БД общая для всех тестов, поэтому каждый
тест выбирает свои строки уникальным фильтром (период, тип сущности).
"""

from __future__ import annotations

import datetime as dt
import functools
import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

BASE = "/api/admin/integrations"


def _admin(client) -> None:
    csrf = authenticate(client, run(client, _make_user, "ADMIN"))
    client.headers["X-CSRF-Token"] = csrf


def _window() -> tuple[dt.datetime, dt.datetime]:
    """Свой отрезок времени в далёком будущем (случайный день в пределах ~8 лет):
    чужие и прошлые строки в него не попадают."""
    days = 400 + uuid.uuid4().int % 3000
    start = dt.datetime.now(dt.UTC) + dt.timedelta(days=days, seconds=uuid.uuid4().int % 86400)
    return start, start + dt.timedelta(hours=2)


def _walk(client, path: str, **params) -> list[list[dict]]:
    """Обходит все страницы по заголовку `X-Next-Cursor`, возвращает список страниц."""
    pages: list[list[dict]] = []
    cursor = None
    while True:
        query = {**params, **({"cursor": cursor} if cursor else {})}
        response = client.get(f"{BASE}/{path}", params=query)
        assert response.status_code == 200, response.text
        assert isinstance(response.json(), list)
        pages.append(response.json())
        cursor = response.headers.get("X-Next-Cursor")
        if not cursor:
            return pages
        assert len(pages) < 50, "курсор не заканчивается"


async def _events(start: dt.datetime, count: int, *, status: str = "pending") -> list[uuid.UUID]:
    from app.core.db import session_scope
    from app.modules.integration.models import OutboxEvent

    ids = []
    async with session_scope() as session:
        for index in range(count):
            event = OutboxEvent(
                aggregate_type="deal",
                aggregate_id=uuid.uuid4(),
                event_type="DEAL_CREATED",
                target="lms",
                status=status,
                created_at=start + dt.timedelta(minutes=index),
            )
            session.add(event)
            await session.flush()
            ids.append(event.id)
    return ids


async def _messages(start: dt.datetime, count: int) -> list[uuid.UUID]:
    from app.core.db import session_scope
    from app.modules.integration.models import InboundMessage

    ids = []
    async with session_scope() as session:
        for index in range(count):
            message = InboundMessage(
                source_code="cms",
                external_id=f"lst-{uuid.uuid4()}",
                raw_payload={},
                signature_valid=True,
                received_at=start + dt.timedelta(minutes=index),
            )
            session.add(message)
            await session.flush()
            ids.append(message.id)
    return ids


async def _refs(entity_type: str, count: int, *, unsynced: int = 0) -> list[uuid.UUID]:
    """`count` синхронизированных связей (с разным временем) и `unsynced` без времени."""
    from app.core.db import session_scope
    from app.modules.integration.models import ExternalRef

    ids = []
    now = dt.datetime.now(dt.UTC)
    async with session_scope() as session:
        for index in range(count + unsynced):
            ref = ExternalRef(
                entity_type=entity_type,
                entity_id=uuid.uuid4(),
                source_code="bitrix24",
                external_id=str(uuid.uuid4()),
                synced_version=1,
                last_synced_at=now - dt.timedelta(minutes=index) if index < count else None,
                sync_direction="outbound",
            )
            session.add(ref)
            await session.flush()
            ids.append(ref.id)
    return ids


class TestOutboxEventsPagination:
    def test_pages_cover_every_row_once_newest_first(self, client) -> None:
        _admin(client)
        start, end = _window()
        ids = run(client, functools.partial(_events, start, 5))
        window = {"from": start.isoformat(), "to": end.isoformat()}

        pages = _walk(client, "outbox-events", limit=2, **window)

        assert [len(page) for page in pages] == [2, 2, 1]
        seen = [item["id"] for page in pages for item in page]
        assert seen == [str(i) for i in reversed(ids)]

    def test_last_page_has_no_cursor(self, client) -> None:
        _admin(client)
        start, end = _window()
        run(client, functools.partial(_events, start, 2))

        response = client.get(
            f"{BASE}/outbox-events",
            params={"limit": 2, "from": start.isoformat(), "to": end.isoformat()},
        )

        assert len(response.json()) == 2
        assert "X-Next-Cursor" not in response.headers

    def test_period_bounds_are_from_inclusive_to_exclusive(self, client) -> None:
        _admin(client)
        start, _ = _window()
        ids = run(client, functools.partial(_events, start, 3))  # start, +1 мин, +2 мин

        response = client.get(
            f"{BASE}/outbox-events",
            params={
                "from": start.isoformat(),
                "to": (start + dt.timedelta(minutes=2)).isoformat(),
            },
        )

        assert [item["id"] for item in response.json()] == [str(ids[1]), str(ids[0])]

    def test_status_filter_survives_paging(self, client) -> None:
        _admin(client)
        start, end = _window()
        run(client, functools.partial(_events, start, 3, status="dead"))
        run(client, functools.partial(_events, start + dt.timedelta(minutes=10), 2))

        pages = _walk(
            client,
            "outbox-events",
            limit=2,
            status="dead",
            **{"from": start.isoformat(), "to": end.isoformat()},
        )

        assert sum(len(page) for page in pages) == 3
        assert {item["status"] for page in pages for item in page} == {"dead"}

    def test_default_call_is_unchanged(self, client) -> None:
        _admin(client)

        response = client.get(f"{BASE}/outbox-events")

        assert response.status_code == 200, response.text
        assert isinstance(response.json(), list)
        assert len(response.json()) <= 50

    def test_limit_is_bounded(self, client) -> None:
        _admin(client)

        assert client.get(f"{BASE}/outbox-events", params={"limit": 0}).status_code == 422
        assert client.get(f"{BASE}/outbox-events", params={"limit": 201}).status_code == 422

    def test_broken_cursor_is_a_validation_error(self, client) -> None:
        _admin(client)

        response = client.get(f"{BASE}/outbox-events", params={"cursor": "мусор"})

        assert response.status_code == 422, response.text


class TestInboundMessagesPagination:
    def test_pages_cover_every_row_once(self, client) -> None:
        _admin(client)
        start, end = _window()
        ids = run(client, functools.partial(_messages, start, 5))

        pages = _walk(
            client,
            "inbound-messages",
            limit=2,
            source_code="cms",
            **{"from": start.isoformat(), "to": end.isoformat()},
        )

        assert [len(page) for page in pages] == [2, 2, 1]
        assert [i["id"] for page in pages for i in page] == [str(i) for i in reversed(ids)]


class TestExternalRefsPagination:
    def test_unsynced_refs_come_last_and_paging_does_not_lose_them(self, client) -> None:
        _admin(client)
        entity_type = f"t{uuid.uuid4().hex[:12]}"
        ids = run(client, functools.partial(_refs, entity_type, 3, unsynced=2))

        pages = _walk(client, "external-refs", limit=2, entity_type=entity_type)

        seen = [item["id"] for page in pages for item in page]
        assert len(seen) == 5 and len(set(seen)) == 5
        assert set(seen) == {str(i) for i in ids}
        # Сначала три синхронизированные (свежие первыми), потом две без времени.
        synced = [item["last_synced_at"] for page in pages for item in page]
        assert all(synced[:3]) and synced[3:] == [None, None]
        assert synced[:3] == sorted(synced[:3], reverse=True)

    def test_period_filter_applies_to_the_sync_time(self, client) -> None:
        _admin(client)
        entity_type = f"t{uuid.uuid4().hex[:12]}"
        run(client, functools.partial(_refs, entity_type, 2, unsynced=1))
        long_ago = (dt.datetime.now(dt.UTC) - dt.timedelta(days=1)).isoformat()

        response = client.get(
            f"{BASE}/external-refs", params={"entity_type": entity_type, "from": long_ago}
        )

        assert len(response.json()) == 2  # без времени синхронизации в период не попадает
