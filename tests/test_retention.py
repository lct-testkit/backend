"""B6-6: срок хранения и очистка (`RetentionService`, `app.modules.admin.retention`).

Область этой итерации сужена (см. докстринг retention.py): security_events, OTP-коды подписи,
уведомления, незавершённые загрузки, файлы после `soft_delete`. Архивация audit_log в S3 и
остальные категории полного разбора задачи не реализованы — не тестируются здесь."""

from __future__ import annotations

import datetime as dt
import functools
import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import login

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


async def _make_security_event(created_at: dt.datetime) -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.identity.models import SecurityEvent

    async with session_scope() as session:
        event = SecurityEvent(event_type="LOGIN_FAILED", severity="warning", created_at=created_at)
        session.add(event)
        await session.flush()
        return event.id


async def _make_otp_code(request_id: uuid.UUID, created_at: dt.datetime) -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.signing.models import SignatureOtpCode

    async with session_scope() as session:
        code = SignatureOtpCode(
            request_id=request_id,
            code_hash="x" * 60,
            salt="y" * 20,
            channel="email",
            sent_to_masked="t***@example.ru",
            expires_at=created_at + dt.timedelta(minutes=5),
            created_at=created_at,
        )
        session.add(code)
        await session.flush()
        return code.id


async def _make_notification(
    recipient_id: uuid.UUID, created_at: dt.datetime, *, is_read: bool
) -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.notification.models import Notification

    async with session_scope() as session:
        notification = Notification(
            recipient_id=recipient_id,
            template_code="TEST_CODE",
            created_at=created_at,
            is_read=is_read,
            read_at=created_at if is_read else None,
        )
        session.add(notification)
        await session.flush()
        return notification.id


async def _exists(model, object_id) -> bool:
    from app.core.db import session_scope

    async with session_scope() as session:
        return await session.get(model, object_id) is not None


async def _run_category(category: str, *, dry_run: bool) -> dict[str, object]:
    from app.core.db import session_scope
    from app.modules.admin.retention import RetentionService

    async with session_scope() as session:
        result = await RetentionService(session).run_category(category, dry_run=dry_run)
        return {"deleted": result.deleted, "status": result.status}


class TestSecurityEvents:
    def test_old_event_is_removed_fresh_stays(self, client) -> None:
        from app.modules.identity.models import SecurityEvent

        old = run(
            client,
            functools.partial(
                _make_security_event, dt.datetime.now(dt.UTC) - dt.timedelta(days=37 * 30)
            ),
        )
        fresh = run(client, functools.partial(_make_security_event, dt.datetime.now(dt.UTC)))

        result = run(client, functools.partial(_run_category, "security_events", dry_run=False))

        assert result["deleted"] >= 1
        assert run(client, functools.partial(_exists, SecurityEvent, old)) is False
        assert run(client, functools.partial(_exists, SecurityEvent, fresh)) is True

    def test_dry_run_counts_but_does_not_delete(self, client) -> None:
        from app.modules.identity.models import SecurityEvent

        old = run(
            client,
            functools.partial(
                _make_security_event, dt.datetime.now(dt.UTC) - dt.timedelta(days=37 * 30)
            ),
        )

        result = run(client, functools.partial(_run_category, "security_events", dry_run=True))

        assert result["deleted"] >= 1
        assert run(client, functools.partial(_exists, SecurityEvent, old)) is True


class TestOtpCodes:
    def test_old_code_is_removed_fresh_stays(self, client) -> None:
        from app.modules.signing.models import SignatureOtpCode
        from tests.signing_helpers import _build

        built = _build(client, requests=[{"status": "sent"}])
        request_id = built.request_ids[0]
        old = run(
            client,
            functools.partial(
                _make_otp_code, request_id, dt.datetime.now(dt.UTC) - dt.timedelta(days=31)
            ),
        )
        fresh = run(client, functools.partial(_make_otp_code, request_id, dt.datetime.now(dt.UTC)))

        result = run(client, functools.partial(_run_category, "otp_codes", dry_run=False))

        assert result["deleted"] >= 1
        assert run(client, functools.partial(_exists, SignatureOtpCode, old)) is False
        assert run(client, functools.partial(_exists, SignatureOtpCode, fresh)) is True


class TestNotifications:
    def test_old_read_notification_is_removed(self, client) -> None:
        from app.modules.notification.models import Notification

        user = login(client, "ADMIN")
        old_read = run(
            client,
            functools.partial(
                _make_notification,
                user.id,
                dt.datetime.now(dt.UTC) - dt.timedelta(days=181),
                is_read=True,
            ),
        )
        old_unread = run(
            client,
            functools.partial(
                _make_notification,
                user.id,
                dt.datetime.now(dt.UTC) - dt.timedelta(days=181),
                is_read=False,
            ),
        )

        result = run(client, functools.partial(_run_category, "notifications", dry_run=False))

        assert result["deleted"] >= 1
        assert run(client, functools.partial(_exists, Notification, old_read)) is False
        # Непрочитанное старое уведомление не должно исчезнуть — пользователь его не видел.
        assert run(client, functools.partial(_exists, Notification, old_unread)) is True


class TestRunAllIsIdempotentAndLogged:
    def test_a_second_run_finds_nothing_new_and_both_runs_are_logged(self, client) -> None:
        from sqlalchemy import select

        from app.core.db import session_scope
        from app.modules.admin.models import RetentionRun

        run(
            client,
            functools.partial(
                _make_security_event, dt.datetime.now(dt.UTC) - dt.timedelta(days=37 * 30)
            ),
        )

        first = run(client, functools.partial(_run_category, "security_events", dry_run=False))
        second = run(client, functools.partial(_run_category, "security_events", dry_run=False))

        assert first["deleted"] >= 1
        assert second["deleted"] == 0

        async def _runs_logged() -> int:
            async with session_scope() as session:
                rows = (
                    (
                        await session.execute(
                            select(RetentionRun).where(RetentionRun.category == "security_events")
                        )
                    )
                    .scalars()
                    .all()
                )
                return len(rows)

        assert run(client, _runs_logged) >= 2


class TestApi:
    def test_admin_can_trigger_a_dry_run(self, client) -> None:
        login(client, "ADMIN")

        response = client.post("/api/admin/retention/run", json={"dry_run": True})

        assert response.status_code == 200, response.text
        body = response.json()
        categories = {r["category"] for r in body["results"]}
        assert "security_events" in categories
        assert all(r["dry_run"] for r in body["results"])

    def test_head_cannot_trigger_a_run(self, client) -> None:
        login(client, "HEAD")

        response = client.post("/api/admin/retention/run", json={"dry_run": True})

        assert response.status_code == 403

    def test_admin_and_auditor_can_read_the_log(self, client) -> None:
        login(client, "ADMIN")
        client.post("/api/admin/retention/run", json={"dry_run": True})

        response = client.get("/api/admin/retention/runs")

        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) >= 1

    def test_unknown_category_is_rejected(self, client) -> None:
        login(client, "ADMIN")

        response = client.post(
            "/api/admin/retention/run", json={"dry_run": True, "category": "does-not-exist"}
        )

        assert response.status_code == 422
