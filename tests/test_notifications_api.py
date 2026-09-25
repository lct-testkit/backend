"""Ручки уведомлений поверх БД: счётчик непрочитанных, каталог кодов событий,
предпросмотр шаблона и последние открытые объекты (`/me/recent`).

Сквозные тесты на настоящей PostgreSQL (`TEST_DATABASE_URL`) — см. докстринг
`tests/conftest.py`.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _login(client, user) -> None:
    csrf = authenticate(client, user)
    client.headers["X-CSRF-Token"] = csrf


async def _notify(recipient_id: uuid.UUID, *, read: bool = False, code: str = "DEAL_EVENT") -> None:
    from app.core.db import session_scope
    from app.modules.notification.models import Notification

    async with session_scope() as session:
        session.add(
            Notification(
                recipient_id=recipient_id,
                template_code=code,
                payload={},
                is_read=read,
                read_at=dt.datetime.now(dt.UTC) if read else None,
            )
        )


def _notify_many(client, recipient, *, unread: int = 0, read: int = 0) -> None:
    for _ in range(unread):
        run(client, _notify, recipient.id)
    for _ in range(read):
        run(client, functools.partial(_notify, recipient.id, read=True))


class TestUnreadCount:
    def test_counts_only_my_unread_notifications(self, client) -> None:
        me = run(client, _make_user, "KAM")
        someone = run(client, _make_user, "KAM")
        _notify_many(client, me, unread=3, read=2)
        _notify_many(client, someone, unread=4)
        _login(client, me)

        response = client.get("/api/notifications/unread-count")

        assert response.status_code == 200, response.text
        assert response.json() == {"count": 3}

    def test_reading_lowers_the_counter(self, client) -> None:
        me = run(client, _make_user, "KAM")
        _notify_many(client, me, unread=2)
        _login(client, me)
        assert client.get("/api/notifications/unread-count").json() == {"count": 2}

        assert client.post("/api/notifications/read", json={}).status_code == 200

        assert client.get("/api/notifications/unread-count").json() == {"count": 0}

    def test_the_counter_is_not_capped_like_a_page(self, client) -> None:
        me = run(client, _make_user, "KAM")
        _notify_many(client, me, unread=105)
        _login(client, me)

        assert client.get("/api/notifications/unread-count").json() == {"count": 105}

    def test_login_is_required(self, client) -> None:
        assert client.get("/api/notifications/unread-count").status_code == 401


async def _template(code: str, channel: str, *, active: bool = True) -> None:
    from app.core.db import session_scope
    from app.modules.notification.models import NotificationTemplate

    async with session_scope() as session:
        session.add(
            NotificationTemplate(
                code=code, channel=channel, body_template="Текст", is_active=active
            )
        )


def _add_template(client, code: str, channel: str, *, active: bool = True) -> None:
    run(client, functools.partial(_template, code, channel, active=active))


class TestEventCodes:
    """Коды событий для `PUT /me/notification-prefs` нужны любой роли, а не
    только администратору шаблонов."""

    def test_any_user_sees_the_codes_of_active_templates(self, client) -> None:
        prefix = f"EVT_{uuid.uuid4().hex[:8].upper()}"
        _add_template(client, f"{prefix}_A", "in_app")
        _add_template(client, f"{prefix}_A", "email")
        _add_template(client, f"{prefix}_B", "in_app")
        _add_template(client, f"{prefix}_OFF", "in_app", active=False)
        _login(client, run(client, _make_user, "KAM"))

        response = client.get("/api/notifications/event-codes")

        assert response.status_code == 200, response.text
        mine = {
            item["code"]: item["channels"]
            for item in response.json()["items"]
            if item["code"].startswith(prefix)
        }
        assert mine == {f"{prefix}_A": ["email", "in_app"], f"{prefix}_B": ["in_app"]}

    def test_codes_are_unique_and_sorted(self, client) -> None:
        _login(client, run(client, _make_user, "KAM"))

        response = client.get("/api/notifications/event-codes")
        codes = [item["code"] for item in response.json()["items"]]

        assert codes == sorted(set(codes))

    def test_login_is_required(self, client) -> None:
        assert client.get("/api/notifications/event-codes").status_code == 401


class TestTemplatePreview:
    """`POST /admin/notification-templates/preview`: увидеть результат и поймать
    синтаксическую ошибку до сохранения шаблона."""

    URL = "/api/admin/notification-templates/preview"

    def _admin(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))

    def test_renders_body_and_subject_with_the_payload(self, client) -> None:
        self._admin(client)

        response = client.post(
            self.URL,
            json={
                "subject_template": "Сделка {{ number }}",
                "body_template": "Сделка {{ number }} передана: {{ owner }}",
                "payload": {"number": "D-1", "owner": "Иванов"},
            },
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is True
        assert body["subject"] == "Сделка D-1"
        assert body["body"] == "Сделка D-1 передана: Иванов"
        assert body["error"] is None

    def test_missing_variables_render_empty_like_in_production(self, client) -> None:
        self._admin(client)

        body = client.post(self.URL, json={"body_template": "IP: {{ ip }}."}).json()

        assert body["ok"] is True
        assert body["body"] == "IP: ."

    def test_lists_the_variables_the_template_uses(self, client) -> None:
        self._admin(client)

        body = client.post(
            self.URL,
            json={
                "subject_template": "{{ number }}",
                "body_template": (
                    "{% set total = amount %}{{ total }} {{ owner }}"
                    "{% for item in items %}{{ item }}{% endfor %}"
                ),
            },
        ).json()

        # Переменные, заданные в самом шаблоне (`total`, `item`), не в счёт.
        assert body["variables"] == ["amount", "items", "number", "owner"]

    def test_syntax_error_is_reported_with_the_line(self, client) -> None:
        self._admin(client)

        response = client.post(
            self.URL, json={"body_template": "Первая строка\n{% if deal %}без endif"}
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is False
        assert body["body"] is None
        assert body["error"]["field"] == "body_template"
        assert body["error"]["line"] == 2
        assert body["error"]["message"]

    def test_error_in_the_subject_points_at_the_subject(self, client) -> None:
        self._admin(client)

        body = client.post(
            self.URL, json={"subject_template": "{{ oops", "body_template": "Текст"}
        ).json()

        assert body["ok"] is False
        assert body["error"]["field"] == "subject_template"

    def test_runtime_error_is_reported_not_raised(self, client) -> None:
        self._admin(client)

        response = client.post(self.URL, json={"body_template": "{{ 1 / 0 }}"})

        assert response.status_code == 200, response.text
        assert response.json()["ok"] is False

    def test_template_cannot_reach_python_internals(self, client) -> None:
        self._admin(client)

        body = client.post(
            self.URL, json={"body_template": "{{ ''.__class__.__mro__[1].__subclasses__() }}"}
        ).json()

        assert body["ok"] is False

    def test_only_template_managers_may_preview(self, client) -> None:
        _login(client, run(client, _make_user, "KAM"))

        assert client.post(self.URL, json={"body_template": "x"}).status_code == 403

    def test_empty_template_is_a_validation_error(self, client) -> None:
        self._admin(client)

        assert client.post(self.URL, json={"body_template": ""}).status_code == 422


async def _push_recent(user_id: uuid.UUID, members: list[tuple[str, float]]) -> None:
    from app.core.redis_client import get_redis, key_recent

    await get_redis().zadd(key_recent(user_id), dict(members))


class TestRecentObjects:
    """`GET /me/recent` типизирован: `{items: [{type, id, title, opened_at}]}`."""

    def _entry(self, **overrides) -> str:
        entry = {"type": "deal", "id": str(uuid.uuid4()), "title": "Сделка"}
        entry.update(overrides)
        return json.dumps(entry, ensure_ascii=False)

    def test_returns_typed_items_newest_first(self, client) -> None:
        user = run(client, _make_user, "KAM")
        first, second = self._entry(title="Старая"), self._entry(title="Новая")
        run(client, _push_recent, user.id, [(first, 1_700_000_000.0), (second, 1_700_000_100.0)])
        _login(client, user)

        response = client.get("/api/me/recent")

        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert [item["title"] for item in items] == ["Новая", "Старая"]
        assert set(items[0]) == {"type", "id", "title", "opened_at"}
        assert items[0]["opened_at"].startswith("2023-11-14")

    def test_broken_entries_are_skipped(self, client) -> None:
        user = run(client, _make_user, "KAM")
        good = self._entry()
        run(
            client,
            _push_recent,
            user.id,
            [
                ("это не json", 1.0),
                (json.dumps({"type": "deal"}), 2.0),
                (self._entry(id="не-uuid"), 3.0),
                (good, 4.0),
            ],
        )
        _login(client, user)

        items = client.get("/api/me/recent").json()["items"]

        assert [item["id"] for item in items] == [json.loads(good)["id"]]

    def test_the_schema_describes_the_response(self, client) -> None:
        schema = client.get("/api/openapi.json").json()

        operation = schema["paths"]["/api/me/recent"]["get"]
        content = operation["responses"]["200"]["content"]["application/json"]["schema"]
        assert content == {"$ref": "#/components/schemas/RecentListResponse"}
        item = schema["components"]["schemas"]["RecentItemOut"]
        assert set(item["properties"]) == {"type", "id", "title", "opened_at"}

    def test_empty_history_is_an_empty_list(self, client) -> None:
        _login(client, run(client, _make_user, "KAM"))

        assert client.get("/api/me/recent").json() == {"items": []}
