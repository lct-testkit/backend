"""Уведомления: настройки получателя, тихие часы, очередь доставки, рендер шаблонов, лента.

Сквозные тесты на настоящей PostgreSQL (`TEST_DATABASE_URL`); внешний шлюз подменён записывающим.
"""

from __future__ import annotations

import datetime as dt
import functools
import uuid
from types import SimpleNamespace

import pytest

from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _login(client, user) -> None:
    client.headers["X-CSRF-Token"] = authenticate(client, user)


async def _add_template(
    code: str,
    channel: str,
    *,
    subject: str | None = None,
    body: str = "Текст",
    active: bool = True,
) -> None:
    from app.core.db import session_scope
    from app.modules.notification.models import NotificationTemplate

    async with session_scope() as session:
        session.add(
            NotificationTemplate(
                code=code,
                channel=channel,
                subject_template=subject,
                body_template=body,
                is_active=active,
            )
        )


async def _set_pref(user_id: uuid.UUID, code: str, **fields) -> None:
    from app.core.db import session_scope
    from app.modules.notification.models import UserNotificationPref

    async with session_scope() as session:
        session.add(UserNotificationPref(user_id=user_id, event_code=code, **fields))


async def _notify(user_id: uuid.UUID, code: str, **kwargs) -> None:
    from app.core.db import session_scope
    from app.modules.notification.service import RealNotificationService

    async with session_scope() as session:
        await RealNotificationService().notify_user(
            session, recipient_id=user_id, template_code=code, **kwargs
        )


async def _feed(user_id: uuid.UUID) -> list[dict]:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.notification.models import Notification

    async with session_scope() as session:
        rows = await session.execute(
            select(Notification).where(Notification.recipient_id == user_id)
        )
        return [
            {"id": n.id, "code": n.template_code, "is_read": n.is_read, "priority": n.priority}
            for n in rows.scalars()
        ]


async def _deliveries(user_id: uuid.UUID) -> list[dict]:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.notification.models import Notification, NotificationDelivery

    async with session_scope() as session:
        rows = await session.execute(
            select(NotificationDelivery, Notification.template_code)
            .join(Notification, Notification.id == NotificationDelivery.notification_id)
            .where(Notification.recipient_id == user_id)
            .order_by(NotificationDelivery.created_at)
        )
        return [
            {
                "id": d.id,
                "code": code,
                "channel": d.channel,
                "status": d.status,
                "attempt": d.attempt,
                "error": d.error,
            }
            for d, code in rows.all()
        ]


async def _close_other_pending() -> None:
    """Чужие ожидающие доставки закрываются: тик берёт всё, что стоит в очереди."""
    from sqlalchemy import update

    from app.core.db import session_scope
    from app.modules.notification.models import NotificationDelivery

    async with session_scope() as session:
        await session.execute(
            update(NotificationDelivery)
            .where(NotificationDelivery.status == "pending")
            .values(status="skipped", error="закрыто тестом")
        )


async def _dispatch() -> dict:
    from app.modules.notification.tasks import dispatch_pending_notifications

    return await dispatch_pending_notifications({})


class RecordingGateway:
    def __init__(self, *, fail_retryable: bool = False) -> None:
        self.sent: list[dict] = []
        self.fail_retryable = fail_retryable

    async def send(self, *, address_masked, subject, body, address=None) -> None:
        from app.modules.notification.service import ChannelDeliveryError

        self.sent.append(
            {"masked": address_masked, "subject": subject, "body": body, "address": address}
        )
        if self.fail_retryable:
            raise ChannelDeliveryError("временный сбой", retryable=True)


@pytest.fixture
def gateway(client):
    from app.modules.notification import service

    previous = dict(service._channel_gateways)
    fake = RecordingGateway()
    service.register_channel_gateway("email", fake)
    service.register_channel_gateway("telegram", fake)
    run(client, _close_other_pending)
    yield fake
    service._channel_gateways.clear()
    service._channel_gateways.update(previous)


def _code() -> str:
    return f"HARD_{uuid.uuid4().hex[:10].upper()}"


class TestRecipientPreferences:
    def test_a_disabled_event_never_reaches_the_feed(self, client) -> None:
        user = run(client, _make_user, "KAM")
        code = _code()
        run(client, functools.partial(_add_template, code, "in_app"))
        run(client, functools.partial(_set_pref, user.id, code, is_enabled=False))

        run(client, functools.partial(_notify, user.id, code))

        assert run(client, _feed, user.id) == []

    def test_an_enabled_event_does_reach_it(self, client) -> None:
        user = run(client, _make_user, "KAM")
        code = _code()
        run(client, functools.partial(_add_template, code, "in_app"))
        run(client, functools.partial(_set_pref, user.id, code, is_enabled=True))

        run(client, functools.partial(_notify, user.id, code))

        assert [n["code"] for n in run(client, _feed, user.id)] == [code]

    def test_a_critical_notification_ignores_the_switch(self, client) -> None:
        user = run(client, _make_user, "KAM")
        code = _code()
        run(client, functools.partial(_set_pref, user.id, code, is_enabled=False))

        run(client, functools.partial(_notify, user.id, code, priority="critical"))

        assert [n["priority"] for n in run(client, _feed, user.id)] == ["critical"]

    def test_channels_without_in_app_keep_the_feed_empty(self, client) -> None:
        user = run(client, _make_user, "KAM")
        code = _code()
        run(client, functools.partial(_set_pref, user.id, code, channels=["email"]))

        run(client, functools.partial(_notify, user.id, code))

        assert run(client, _feed, user.id) == []

    def test_no_preference_means_the_default(self, client) -> None:
        user = run(client, _make_user, "KAM")

        run(client, functools.partial(_notify, user.id, _code()))

        assert len(run(client, _feed, user.id)) == 1


class TestQuietHours:
    ALL_DAY = {"quiet_hours_start": dt.time(0, 0), "quiet_hours_end": dt.time(23, 59, 59)}

    def _scene(self, client, *, quiet: bool):
        user = run(client, _make_user, "KAM")
        code = _code()
        run(
            client,
            functools.partial(
                _add_template, code, "email", subject="Тема {{ deal }}", body="Тело {{ deal }}"
            ),
        )
        if quiet:
            run(client, functools.partial(_set_pref, user.id, code, **self.ALL_DAY))
        run(client, functools.partial(_notify, user.id, code, payload={"deal": "D-1"}))
        return user, code

    def test_a_delivery_in_the_quiet_window_waits_instead_of_being_dropped(
        self, client, gateway
    ) -> None:
        user, _ = self._scene(client, quiet=True)

        (delivery,) = run(client, _deliveries, user.id)
        assert delivery["status"] == "pending"

        result = run(client, _dispatch)

        assert result["deferred"] >= 1
        assert gateway.sent == []
        (after,) = run(client, _deliveries, user.id)
        assert (after["status"], after["attempt"]) == ("pending", 0)

    def test_it_goes_out_once_the_window_is_over(self, client, gateway) -> None:
        from sqlalchemy import delete

        from app.core.db import session_scope
        from app.modules.notification.models import UserNotificationPref

        user, _ = self._scene(client, quiet=True)
        run(client, _dispatch)

        async def _open_the_window() -> None:
            async with session_scope() as session:
                await session.execute(
                    delete(UserNotificationPref).where(UserNotificationPref.user_id == user.id)
                )

        run(client, _open_the_window)
        run(client, _dispatch)

        (sent,) = gateway.sent
        # Текст и тема отрисованы из шаблона, адрес — настоящий, а не маска.
        assert (sent["subject"], sent["body"]) == ("Тема D-1", "Тело D-1")
        assert sent["address"] == user.email
        assert sent["masked"] != user.email
        (after,) = run(client, _deliveries, user.id)
        assert after["status"] == "sent"


class TestDispatcher:
    def test_a_delivery_held_by_another_tick_is_skipped(self, client, gateway) -> None:
        from sqlalchemy import select

        from app.core.db import get_session_factory
        from app.modules.notification.models import NotificationDelivery

        user = run(client, _make_user, "KAM")
        code = _code()
        run(client, functools.partial(_add_template, code, "email"))
        run(client, functools.partial(_notify, user.id, code))
        (delivery,) = run(client, _deliveries, user.id)

        async def _scenario() -> tuple[int, int]:
            async with get_session_factory()() as holder:
                await holder.execute(
                    select(NotificationDelivery)
                    .where(NotificationDelivery.id == delivery["id"])
                    .with_for_update()
                )
                skipped = await _dispatch()
                await holder.rollback()
            taken = await _dispatch()
            return skipped["sent"], taken["sent"]

        assert run(client, _scenario) == (0, 1)
        assert len(gateway.sent) == 1

    def test_important_notifications_go_first(self, client, gateway) -> None:
        user = run(client, _make_user, "KAM")
        code = _code()
        run(client, functools.partial(_add_template, code, "email", body="{{ n }}"))
        for priority in ("normal", "critical", "high"):
            run(
                client,
                functools.partial(
                    _notify, user.id, code, payload={"n": priority}, priority=priority
                ),
            )

        run(client, _dispatch)

        assert [item["body"] for item in gateway.sent] == ["critical", "high", "normal"]

    def test_a_temporary_failure_is_retried_on_a_schedule_not_on_the_next_tick(
        self, client, gateway
    ) -> None:
        from sqlalchemy import update

        from app.core.db import session_scope
        from app.modules.notification.models import NotificationDelivery

        gateway.fail_retryable = True
        user = run(client, _make_user, "KAM")
        code = _code()
        run(client, functools.partial(_add_template, code, "email"))
        run(client, functools.partial(_notify, user.id, code))

        run(client, _dispatch)
        run(client, _dispatch)  # сразу следующий тик: срок повтора ещё не подошёл

        assert len(gateway.sent) == 1
        (delivery,) = run(client, _deliveries, user.id)
        assert (delivery["status"], delivery["attempt"]) == ("pending", 1)

        async def _age() -> None:
            async with session_scope() as session:
                await session.execute(
                    update(NotificationDelivery)
                    .where(NotificationDelivery.id == delivery["id"])
                    .values(created_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=1))
                )

        run(client, _age)
        run(client, _dispatch)

        assert len(gateway.sent) == 2

    def test_a_channel_without_an_address_is_skipped_not_sent(self, client, gateway) -> None:
        # У Telegram нет поля для chat id: отправлять некуда, и это не повод дёргать шлюз.
        user = run(client, _make_user, "KAM")
        code = _code()
        run(client, functools.partial(_add_template, code, "telegram"))
        run(client, functools.partial(_notify, user.id, code))

        run(client, _dispatch)

        (delivery,) = run(client, _deliveries, user.id)
        assert delivery["status"] == "skipped"
        assert "адреса" in delivery["error"]
        assert gateway.sent == []

    def test_a_template_that_fails_to_render_is_skipped_once(self, client, gateway) -> None:
        user = run(client, _make_user, "KAM")
        code = _code()
        run(client, functools.partial(_add_template, code, "email", body="{{ 1 / 0 }}"))
        run(client, functools.partial(_notify, user.id, code))

        run(client, _dispatch)

        (delivery,) = run(client, _deliveries, user.id)
        assert delivery["status"] == "skipped"
        assert "ZeroDivisionError" in delivery["error"]
        assert gateway.sent == []


class TestFeed:
    @pytest.mark.parametrize(
        "body",
        [
            "{{ 1 / 0 }}",
            "{{ 'a' + 1 }}",
            "{{ 10 ** 10 ** 10 }}",
            "{{ 'x' * 10 ** 9 }}",
            "{{ 1 << 10 ** 9 }}",
            "{% for i in range(10 ** 9) %}x{% endfor %}",
            "{{ unclosed",
        ],
    )
    def test_any_template_error_shows_the_entry_without_text(self, body: str) -> None:
        from app.modules.notification.service import NotificationQueryService

        service = NotificationQueryService(None)  # type: ignore[arg-type]
        template = SimpleNamespace(code="X", subject_template=None, body_template=body)
        notification = SimpleNamespace(payload={})

        assert service.render_for_display(notification, template) == (None, None)  # type: ignore[arg-type]

    def test_python_internals_are_not_reachable_from_a_template(self) -> None:
        from app.modules.notification.service import render_template

        assert render_template("[{{ ''.__class__ }}]", {}) == "[]"

    def test_ordinary_arithmetic_and_text_still_render(self) -> None:
        from app.modules.notification.service import render_template

        assert render_template("{{ 2 ** 10 }} {{ '-' * 5 }} {{ 3 * 4 }} {{ n }}", {"n": "ok"}) == (
            "1024 ----- 12 ok"
        )

    def test_a_broken_template_does_not_break_the_list(self, client) -> None:
        user = run(client, _make_user, "KAM")
        code = _code()
        run(client, functools.partial(_add_template, code, "in_app", body="{{ 1 / 0 }}"))
        run(client, functools.partial(_notify, user.id, code))
        _login(client, user)

        response = client.get("/api/notifications")

        assert response.status_code == 200, response.text
        assert [item["template_code"] for item in response.json()["items"]] == [code]

    def test_an_empty_id_list_marks_nothing(self, client) -> None:
        user = run(client, _make_user, "KAM")
        for _ in range(3):
            run(client, functools.partial(_notify, user.id, _code()))
        _login(client, user)

        nothing = client.post("/api/notifications/read", json={"ids": []})
        assert nothing.status_code == 200, nothing.text
        assert nothing.json() == {"updated": 0}
        assert client.get("/api/notifications/unread-count").json() == {"count": 3}

        one = run(client, _feed, user.id)[0]["id"]
        assert client.post("/api/notifications/read", json={"ids": [str(one)]}).json() == {
            "updated": 1
        }
        assert client.post("/api/notifications/read", json={}).json() == {"updated": 2}


class TestTemplatePagination:
    def test_pages_cover_every_template_once(self, client) -> None:
        # Раньше строки сортировались по (код, канал), а курсор резал по (создан, id): при
        # разных порядках создания и кодов страницы теряли и дублировали записи.
        prefix = f"ZZP{uuid.uuid4().hex[:6].upper()}"
        codes = [f"{prefix}_M", f"{prefix}_A", f"{prefix}_Z"]
        for code in codes:
            run(client, functools.partial(_add_template, code, "in_app"))
        _login(client, run(client, _make_user, "ADMIN"))

        seen: list[tuple[str, str]] = []
        cursor = None
        for _ in range(200):
            params = {"limit": 2}
            if cursor:
                params["cursor"] = cursor
            page = client.get("/api/admin/notification-templates", params=params)
            assert page.status_code == 200, page.text
            body = page.json()
            seen += [(item["id"], item["code"]) for item in body["items"]]
            cursor = body["next_cursor"]
            if not cursor:
                break

        mine = [code for _, code in seen if code.startswith(prefix)]
        assert sorted(mine) == sorted(codes)
        assert len(seen) == len({template_id for template_id, _ in seen})
