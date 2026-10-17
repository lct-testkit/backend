"""B6-3: `KeycloakEventSync` — опрос Admin API Keycloak без живого Keycloak.

`admin_request` подменяется напрямую (тот же приём, что `tests/test_identity_hardening.py::
TestPasswordPolicyIsNot500`): возвращает заранее заданный httpx.Response по пути запроса.
Требует `TEST_DATABASE_URL` — события пишутся в security_events, пользователь ищется по
`users.keycloak_id` (настоящая Postgres), курсор и дедупликация — в Redis (`fakeredis`, через
фикстуру `client`, см. докстринг `tests/conftest.py`).
"""

from __future__ import annotations

import datetime as dt
import uuid

import httpx
import pytest

from tests.conftest import TEST_DATABASE_URL, _make_user, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


class _FakeKeycloak:
    """`events_responses` — очередь ответов на последовательные вызовы `/events` (чтобы
    проверить «второй проход по старой выборке не дублирует»); `brute_force` — отображение
    `keycloak_id -> ответ attack-detection`."""

    def __init__(self, events_responses: list[list[dict]], brute_force: dict | None = None):
        from app.modules.identity.keycloak import KeycloakClient

        self.client = KeycloakClient()
        self._events_responses = list(events_responses)
        self._brute_force = brute_force or {}
        self.events_calls = 0
        self.brute_force_calls: list[str] = []
        self.client.admin_request = self._admin_request  # type: ignore[method-assign]

    def _response(self, status: int, path: str, *, json_body) -> httpx.Response:
        # `raise_for_status()` требует, чтобы на ответе был выставлен `request` — иначе
        # httpx бросает RuntimeError независимо от кода статуса (это не проверка статуса).
        request = httpx.Request("GET", f"http://keycloak.invalid{path}")
        return httpx.Response(status, json=json_body, request=request)

    async def _admin_request(self, method: str, path: str, *, json_body=None, params=None):
        if path == "/events":
            self.events_calls += 1
            body = self._events_responses.pop(0) if self._events_responses else []
            return self._response(200, path, json_body=body)
        if path.startswith("/attack-detection/brute-force/users/"):
            keycloak_id = path.rsplit("/", 1)[-1]
            self.brute_force_calls.append(keycloak_id)
            body = self._brute_force.get(keycloak_id, {"disabled": False})
            return self._response(200, path, json_body=body)
        raise AssertionError(f"unexpected path {path!r}")


def _login_error(*, keycloak_id: str | None, username: str, ip: str, time_ms: int) -> dict:
    return {
        "time": time_ms,
        "type": "LOGIN_ERROR",
        "error": "invalid_user_credentials",
        "userId": keycloak_id,
        "ipAddress": ip,
        "details": {"username": username},
    }


def _now_ms() -> int:
    return int(dt.datetime.now(dt.UTC).timestamp() * 1000)


async def _enable_polling_and_run(fake: _FakeKeycloak):
    from app.core.config import get_settings
    from app.core.db import session_scope
    from app.modules.identity.keycloak_events import KeycloakEventSync

    settings = get_settings()
    settings.keycloak_events_poll_enabled = True
    try:
        async with session_scope() as session:
            return await KeycloakEventSync(session, fake.client).run()
    finally:
        settings.keycloak_events_poll_enabled = False


async def _security_events_for(user_id: uuid.UUID | None, username_hash: str | None = None):
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.identity.models import SecurityEvent

    async with session_scope() as session:
        stmt = select(SecurityEvent)
        if user_id is not None:
            stmt = stmt.where(SecurityEvent.user_id == user_id)
        rows = (await session.execute(stmt)).scalars().all()
        if username_hash is not None:
            rows = [r for r in rows if (r.details or {}).get("username_hash") == username_hash]
        return rows


class TestKnownUserLoginFailures:
    def test_three_failures_same_user_and_ip_become_one_batched_event(self, client) -> None:
        user = run(client, _make_user, "KAM")
        now = _now_ms()
        fake = _FakeKeycloak(
            [
                [
                    _login_error(
                        keycloak_id=user.keycloak_id, username="ivan", ip="203.0.113.5", time_ms=now
                    ),
                    _login_error(
                        keycloak_id=user.keycloak_id,
                        username="ivan",
                        ip="203.0.113.5",
                        time_ms=now + 1000,
                    ),
                    _login_error(
                        keycloak_id=user.keycloak_id,
                        username="ivan",
                        ip="203.0.113.5",
                        time_ms=now + 2000,
                    ),
                ]
            ]
        )

        result = run(client, _enable_polling_and_run, fake)

        assert result.fetched == 3
        assert result.batches == 1
        events = run(client, _security_events_for, user.id)
        (event,) = [e for e in events if e.event_type == "LOGIN_FAILED"]
        assert event.details["count"] == 3
        assert event.user_id == user.id


class TestUnknownUser:
    def test_unknown_login_has_no_user_id_and_no_raw_username(self, client) -> None:
        now = _now_ms()
        fake = _FakeKeycloak(
            [[_login_error(keycloak_id=None, username="phantom", ip="198.51.100.9", time_ms=now)]]
        )

        result = run(client, _enable_polling_and_run, fake)

        assert result.fetched == 1
        assert result.batches == 1
        events = run(client, _security_events_for, None)
        matching = [e for e in events if e.event_type == "LOGIN_FAILED" and e.user_id is None]
        assert matching
        for event in matching:
            details_text = str(event.details)
            assert "phantom" not in details_text


class TestBruteForceLock:
    def test_disabled_user_gives_one_lock_event(self, client) -> None:
        user = run(client, _make_user, "KAM")
        now = _now_ms()
        fake = _FakeKeycloak(
            [
                [
                    _login_error(
                        keycloak_id=user.keycloak_id, username="ivan", ip="203.0.113.5", time_ms=now
                    )
                ]
            ],
            brute_force={user.keycloak_id: {"disabled": True, "numFailures": 5}},
        )

        result = run(client, _enable_polling_and_run, fake)

        assert result.locked == 1
        events = run(client, _security_events_for, user.id)
        locks = [e for e in events if e.event_type == "BRUTE_FORCE_LOCK"]
        assert len(locks) == 1
        assert locks[0].severity == "critical"

    def test_repeated_pass_within_lock_ttl_does_not_duplicate(self, client) -> None:
        user = run(client, _make_user, "KAM")
        now = _now_ms()

        fake1 = _FakeKeycloak(
            [
                [
                    _login_error(
                        keycloak_id=user.keycloak_id, username="ivan", ip="203.0.113.5", time_ms=now
                    )
                ]
            ],
            brute_force={user.keycloak_id: {"disabled": True, "numFailures": 5}},
        )
        first = run(client, _enable_polling_and_run, fake1)
        assert first.locked == 1

        fake2 = _FakeKeycloak(
            [
                [
                    _login_error(
                        keycloak_id=user.keycloak_id,
                        username="ivan",
                        ip="203.0.113.5",
                        time_ms=now + 500,
                    )
                ]
            ],
            brute_force={user.keycloak_id: {"disabled": True, "numFailures": 6}},
        )
        second = run(client, _enable_polling_and_run, fake2)

        assert second.locked == 0
        events = run(client, _security_events_for, user.id)
        locks = [e for e in events if e.event_type == "BRUTE_FORCE_LOCK"]
        assert len(locks) == 1


class TestOverlapDeduplication:
    def test_two_passes_over_the_same_window_do_not_double_count(self, client) -> None:
        user = run(client, _make_user, "KAM")
        now = _now_ms()
        event = _login_error(
            keycloak_id=user.keycloak_id, username="ivan", ip="203.0.113.5", time_ms=now
        )

        fake1 = _FakeKeycloak([[event]])
        run(client, _enable_polling_and_run, fake1)

        # Второй проход видит ТО ЖЕ событие (накладка окон) — дедупликация по его полям.
        fake2 = _FakeKeycloak([[event]])
        result2 = run(client, _enable_polling_and_run, fake2)

        assert result2.batches == 0
        events = run(client, _security_events_for, user.id)
        login_failed = [e for e in events if e.event_type == "LOGIN_FAILED"]
        assert len(login_failed) == 1
        assert login_failed[0].details["count"] == 1


class TestKeycloakFailureAndDisabledPolling:
    def test_keycloak_500_does_not_crash_the_task(self, client) -> None:
        from app.modules.identity.keycloak import KeycloakClient

        fake_client = KeycloakClient()

        async def _failing(method, path, *, json_body=None, params=None):
            request = httpx.Request(method, f"http://keycloak.invalid{path}")
            return httpx.Response(500, json={"error": "internal"}, request=request)

        fake_client.admin_request = _failing  # type: ignore[method-assign]

        async def _run():
            from app.core.config import get_settings
            from app.core.db import session_scope
            from app.modules.identity.keycloak_events import KeycloakEventSync

            settings = get_settings()
            settings.keycloak_events_poll_enabled = True
            try:
                async with session_scope() as session:
                    return await KeycloakEventSync(session, fake_client).run()
            finally:
                settings.keycloak_events_poll_enabled = False

        result = run(client, _run)
        assert result.errored is True

    def test_polling_disabled_makes_no_requests(self, client) -> None:
        fake = _FakeKeycloak([[]])

        async def _run():
            from app.core.db import session_scope
            from app.modules.identity.keycloak_events import KeycloakEventSync

            async with session_scope() as session:
                return await KeycloakEventSync(session, fake.client).run()

        result = run(client, _run)

        assert result.skipped is True
        assert fake.events_calls == 0
