"""B6-2: `GET /admin/security-events` — чтение журнала безопасности.

До этой задачи `record_security_event` писал события, но прочитать их можно было только
прямым SQL-запросом. БД общая на сессию тестов — изолируем результаты фильтром по
`user_id` конкретного теста, а не общим числом строк в таблице.

Требует `TEST_DATABASE_URL` (см. докстринг `tests/conftest.py`).
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _login(client, user) -> None:
    client.headers["X-CSRF-Token"] = authenticate(client, user)


async def _insert_events(user_id: uuid.UUID, specs: list[dict]) -> None:
    from app.core.db import session_scope
    from app.modules.identity.models import SecurityEvent

    async with session_scope() as session:
        for spec in specs:
            session.add(
                SecurityEvent(
                    user_id=user_id,
                    event_type=spec.get("event_type", "LOGIN_FAILED"),
                    severity=spec.get("severity", "info"),
                    ip=spec.get("ip"),
                    details=spec.get("details"),
                    created_at=spec["created_at"],
                )
            )


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class TestPagination:
    def test_five_events_paginate_without_duplicates(self, client) -> None:
        admin = run(client, _make_user, "ADMIN")
        subject_id = uuid.uuid4()
        now = _now()
        run(
            client,
            _insert_events,
            subject_id,
            [{"created_at": now - dt.timedelta(minutes=i)} for i in range(5)],
        )
        _login(client, admin)

        seen: list[str] = []
        cursor = None
        pages = 0
        while True:
            params = {"user_id": str(subject_id), "limit": 2}
            if cursor:
                params["cursor"] = cursor
            response = client.get("/api/admin/security-events", params=params)
            assert response.status_code == 200, response.text
            body = response.json()
            seen.extend(item["id"] for item in body["items"])
            pages += 1
            cursor = body["next_cursor"]
            if not cursor:
                break

        assert pages == 3
        assert len(seen) == len(set(seen)) == 5


class TestPermissionMatrix:
    def test_admin_and_auditor_can_read_head_and_kam_cannot(self, client) -> None:
        for role, expected in (("ADMIN", 200), ("AUDITOR", 200), ("HEAD", 403), ("KAM", 403)):
            user = run(client, _make_user, role)
            _login(client, user)
            response = client.get("/api/admin/security-events")
            assert response.status_code == expected, (role, response.text)


class TestFilters:
    def test_event_type_filter_accepts_several_values(self, client) -> None:
        admin = run(client, _make_user, "ADMIN")
        subject_id = uuid.uuid4()
        now = _now()
        run(
            client,
            _insert_events,
            subject_id,
            [
                {"event_type": "LOGIN_FAILED", "created_at": now},
                {"event_type": "BRUTE_FORCE_LOCK", "created_at": now},
                {"event_type": "PASSWORD_CHANGED", "created_at": now},
            ],
        )
        _login(client, admin)

        response = client.get(
            "/api/admin/security-events",
            params={
                "user_id": str(subject_id),
                "event_type": ["LOGIN_FAILED", "BRUTE_FORCE_LOCK"],
            },
        )
        assert response.status_code == 200, response.text
        types = {item["event_type"] for item in response.json()["items"]}
        assert types == {"LOGIN_FAILED", "BRUTE_FORCE_LOCK"}

    def test_min_severity_excludes_info(self, client) -> None:
        admin = run(client, _make_user, "ADMIN")
        subject_id = uuid.uuid4()
        now = _now()
        run(
            client,
            _insert_events,
            subject_id,
            [
                {"severity": "info", "created_at": now},
                {"severity": "warning", "created_at": now},
                {"severity": "critical", "created_at": now},
            ],
        )
        _login(client, admin)

        response = client.get(
            "/api/admin/security-events",
            params={"user_id": str(subject_id), "min_severity": "warning"},
        )
        assert response.status_code == 200, response.text
        severities = {item["severity"] for item in response.json()["items"]}
        assert severities == {"warning", "critical"}

    def test_ip_network_filter(self, client) -> None:
        admin = run(client, _make_user, "ADMIN")
        subject_id = uuid.uuid4()
        now = _now()
        run(
            client,
            _insert_events,
            subject_id,
            [
                {"ip": "203.0.113.17", "created_at": now},
                {"ip": "198.51.100.1", "created_at": now},
            ],
        )
        _login(client, admin)

        response = client.get(
            "/api/admin/security-events",
            params={"user_id": str(subject_id), "ip": "203.0.113.0/24"},
        )
        assert response.status_code == 200, response.text
        ips = {item["ip"] for item in response.json()["items"]}
        assert ips == {"203.0.113.17"}


class TestDetailsAreMasked:
    def test_secret_in_details_is_masked_on_write(self, client) -> None:
        from app.modules.identity.models import SecurityEventType, Severity
        from app.modules.identity.service import IdentityService

        admin = run(client, _make_user, "ADMIN")
        subject_id = uuid.uuid4()

        async def _record() -> None:
            from app.core.db import session_scope

            async with session_scope() as session:
                await IdentityService(session).record_security_event(
                    SecurityEventType.LOGIN_FAILED,
                    user_id=subject_id,
                    severity=Severity.WARNING,
                    details={"email": "ivan@rt.ru", "password": "x", "reason": "bad_creds"},
                )

        run(client, _record)
        _login(client, admin)

        response = client.get("/api/admin/security-events", params={"user_id": str(subject_id)})
        assert response.status_code == 200, response.text
        (item,) = response.json()["items"]
        assert item["details"]["password"] == "***"
        assert item["details"]["email"] != "ivan@rt.ru"
        assert item["details"]["reason"] == "bad_creds"


class TestExport:
    def test_export_writes_mass_export_and_audit_entry(self, client) -> None:
        from sqlalchemy import select

        admin = run(client, _make_user, "ADMIN")
        subject_id = uuid.uuid4()
        run(client, _insert_events, subject_id, [{"created_at": _now()}])
        _login(client, admin)

        response = client.get(
            "/api/admin/security-events/export", params={"user_id": str(subject_id)}
        )
        assert response.status_code == 200, response.text

        async def _audit_count() -> int:
            from app.core.db import session_scope
            from app.modules.audit.models import AuditLog

            async with session_scope() as session:
                rows = await session.execute(
                    select(AuditLog).where(
                        AuditLog.action == "AUDIT_EXPORTED",
                        AuditLog.entity_type == "security_event",
                        AuditLog.actor_id == admin.id,
                    )
                )
                return len(list(rows.scalars()))

        assert run(client, _audit_count) >= 1


class TestSummary:
    def test_summary_counts_recent_events(self, client) -> None:
        admin = run(client, _make_user, "ADMIN")
        _login(client, admin)

        response = client.get("/api/admin/security-events/summary", params={"hours": 1})
        assert response.status_code == 200, response.text
        body = response.json()
        assert "by_type" in body
        assert "critical_count" in body
