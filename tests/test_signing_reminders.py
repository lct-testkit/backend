"""B7-1: напоминания подписантам о приближающемся дедлайне (`sweep_signature_reminders`).

Пороги по умолчанию — `SIGNATURE_REMIND_BEFORE_HOURS=48,4`, минимальный зазор
`SIGNATURE_REMIND_MIN_GAP_HOURS=6`."""

from __future__ import annotations

import datetime as dt
import functools
import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.signing_helpers import _build

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


async def _set_deadline(document_id: uuid.UUID, deadline_at: dt.datetime) -> None:
    from app.core.db import session_scope
    from app.modules.signing.models import SignatureDocument

    async with session_scope() as session:
        document = await session.get(SignatureDocument, document_id)
        document.deadline_at = deadline_at


async def _request_reminder_state(request_id: uuid.UUID) -> dict[str, object]:
    from app.core.db import session_scope
    from app.modules.signing.models import SignatureRequest

    async with session_scope() as session:
        request = await session.get(SignatureRequest, request_id)
        return {
            "reminders_sent": request.reminders_sent,
            "last_reminded_at": request.last_reminded_at,
            "access_token_hash": request.access_token_hash,
        }


async def _entity_of_notification(recipient_id: uuid.UUID, template_code: str):
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.notification.models import Notification

    async with session_scope() as session:
        row = (
            await session.execute(
                select(Notification.entity_type, Notification.entity_id).where(
                    Notification.recipient_id == recipient_id,
                    Notification.template_code == template_code,
                )
            )
        ).first()
        return None if row is None else (row[0], row[1])


async def _notifications_for(recipient_id: uuid.UUID, template_code: str) -> int:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.notification.models import Notification

    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(Notification).where(
                        Notification.recipient_id == recipient_id,
                        Notification.template_code == template_code,
                    )
                )
            )
            .scalars()
            .all()
        )
        return len(rows)


def _sweep(client) -> dict[str, int]:
    from app.modules.signing.tasks import sweep_signature_reminders

    return run(client, sweep_signature_reminders, {})


class TestInternalSigner:
    def test_a_deadline_within_the_first_threshold_sends_one_reminder(
        self, client, kam_user
    ) -> None:
        built = _build(client, requests=[{"status": "sent", "user": kam_user}])
        run(
            client,
            functools.partial(
                _set_deadline,
                built.document_id,
                dt.datetime.now(dt.UTC) + dt.timedelta(hours=20),
            ),
        )

        result = _sweep(client)

        assert result["reminded"] == 1
        state = run(client, _request_reminder_state, built.request_ids[0])
        assert state["reminders_sent"] == 1
        assert state["last_reminded_at"] is not None
        from app.modules.signing.service import TPL_SIGNATURE_DEADLINE_REMINDER

        assert (
            run(
                client,
                functools.partial(_notifications_for, kam_user.id, TPL_SIGNATURE_DEADLINE_REMINDER),
            )
            == 1
        )

    def test_a_second_sweep_within_the_gap_sends_nothing(self, client, kam_user) -> None:
        built = _build(client, requests=[{"status": "sent", "user": kam_user}])
        run(
            client,
            functools.partial(
                _set_deadline,
                built.document_id,
                dt.datetime.now(dt.UTC) + dt.timedelta(hours=20),
            ),
        )

        first = _sweep(client)
        second = _sweep(client)

        assert first["reminded"] == 1
        assert second["reminded"] == 0
        state = run(client, _request_reminder_state, built.request_ids[0])
        assert state["reminders_sent"] == 1

    def test_a_deadline_beyond_every_threshold_sends_nothing(self, client, kam_user) -> None:
        built = _build(client, requests=[{"status": "sent", "user": kam_user}])
        run(
            client,
            functools.partial(
                _set_deadline,
                built.document_id,
                dt.datetime.now(dt.UTC) + dt.timedelta(hours=100),
            ),
        )

        result = _sweep(client)

        assert result["reminded"] == 0

    @pytest.mark.parametrize("status", ["signed", "rejected", "void", "pending"])
    def test_a_request_outside_sent_or_viewed_is_not_reminded(
        self, client, kam_user, status: str
    ) -> None:
        built = _build(client, requests=[{"status": status, "user": kam_user}])
        run(
            client,
            functools.partial(
                _set_deadline,
                built.document_id,
                dt.datetime.now(dt.UTC) + dt.timedelta(hours=20),
            ),
        )

        result = _sweep(client)

        assert result["reminded"] == 0


class TestExternalSigner:
    def test_without_smtp_the_initiator_gets_a_manual_reminder(self, client, kam_user) -> None:
        built = _build(
            client,
            created_by=kam_user.id,
            requests=[{"status": "sent", "token": "ext-reminder-token"}],
        )
        run(
            client,
            functools.partial(
                _set_deadline,
                built.document_id,
                dt.datetime.now(dt.UTC) + dt.timedelta(hours=20),
            ),
        )
        before = run(client, _request_reminder_state, built.request_ids[0])

        result = _sweep(client)

        assert result["reminded"] == 1
        after = run(client, _request_reminder_state, built.request_ids[0])
        # Без SMTP новый токен внешнему не выпускается — старый хэш не меняется.
        assert after["access_token_hash"] == before["access_token_hash"]
        assert after["reminders_sent"] == 1

        from app.modules.signing.service import TPL_SIGNATURE_REMINDER_MANUAL

        assert (
            run(
                client,
                functools.partial(_notifications_for, kam_user.id, TPL_SIGNATURE_REMINDER_MANUAL),
            )
            == 1
        )
        # Уведомление ведёт к документу: запрос подписанта инициатору недоступен (404).
        entity = run(
            client,
            functools.partial(_entity_of_notification, kam_user.id, TPL_SIGNATURE_REMINDER_MANUAL),
        )
        assert entity == ("signature_document", built.document_id)


@pytest.fixture
def kam_user(client):
    from tests.conftest import _make_user

    return run(client, _make_user, "KAM")
