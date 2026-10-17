"""B4-9: каналы связи, согласие и источник контакта (A-20).

`backend-sprints.md` строки 3112-3191. Контакт заводится напрямую в БД
(`tests/crm_helpers.create_contact`, как в других тестах каталога) — дедупликация
и ИНН тут не нужны, только PATCH.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _login(client, user) -> None:
    client.headers["X-CSRF-Token"] = authenticate(client, user)


async def _make_contact(owner_id: uuid.UUID) -> str:
    """Контакт с `created_by=owner_id` — иначе `contact_scope_clause` не пускает KAM к
    только что созданному контакту (`tests.crm_helpers.create_contact` не заполняет
    `created_by`, он рассчитан на вызов от ADMIN, у которого скоуп не ограничен)."""
    from app.core.db import session_scope
    from app.modules.catalog.models import Contact

    async with session_scope() as session:
        contact = Contact(
            first_name="Пётр", last_name="Петров", middle_name="Петрович", created_by=owner_id
        )
        session.add(contact)
        await session.flush()
        return str(contact.id)


def _patch(client, contact_id: str, version: int, **body):
    return client.patch(
        f"/api/contacts/{contact_id}", json=body, headers={"If-Match": str(version)}
    )


async def _channel_rows(contact_id: str) -> list[dict]:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.catalog.models import ContactChannel

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(ContactChannel).where(ContactChannel.contact_id == uuid.UUID(contact_id))
            )
        ).scalars()
        return [
            {
                "id": str(r.id),
                "type": r.type,
                "value": r.value,
                "is_primary": r.is_primary,
                "is_verified": r.is_verified,
            }
            for r in rows
        ]


async def _make_consent(subject_id: str, accepted: bool = True, revoked: bool = False) -> str:
    import datetime as dt

    from app.core.db import session_scope
    from app.modules.identity.models import Consent

    async with session_scope() as session:
        consent = Consent(
            subject_type="contact",
            subject_id=uuid.UUID(subject_id),
            policy_version="1.0",
            policy_text_hash="x" * 64,
            accepted_at=dt.datetime.now(dt.UTC) if accepted else None,
            revoked_at=dt.datetime.now(dt.UTC) if revoked else None,
        )
        session.add(consent)
        await session.flush()
        return str(consent.id)


async def _audit_changes_raw_text(entity_id: str) -> str:
    """Весь `changes` журнала аудита по этому контакту — как одна строка, чтобы искать
    по подстроке и убедиться, что сырые значения каналов туда не попали."""
    import json

    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.audit.models import AuditLog

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(AuditLog.changes).where(AuditLog.entity_id == uuid.UUID(entity_id))
            )
        ).scalars()
        return json.dumps(list(rows), ensure_ascii=False, default=str)


class TestChannelsReplace:
    def test_patch_replaces_channels_and_normalizes_values(self, client) -> None:
        kam = run(client, _make_user, "KAM")
        _login(client, kam)
        contact_id = run(client, _make_contact, kam.id)

        response = _patch(
            client,
            contact_id,
            1,
            channels=[
                {"type": "telegram", "value": "https://t.me/ivanov_mgtu", "is_primary": True},
                {"type": "phone_extra", "value": "8 (495) 123-45-67"},
            ],
        )
        assert response.status_code == 200, response.text
        assert response.json()["version"] == 2

        rows = run(client, _channel_rows, contact_id)
        by_type = {r["type"]: r for r in rows}
        assert by_type["telegram"]["value"] == "@ivanov_mgtu"
        assert by_type["phone_extra"]["value"] == "+74951234567"

        masked = client.get(f"/api/contacts/{contact_id}/channels")
        assert masked.status_code == 200, masked.text
        masked_values = {i["type"]: i["value"] for i in masked.json()["items"]}
        assert masked_values["telegram"] != "@ivanov_mgtu"
        assert "ivanov_mgtu" not in masked_values["telegram"]

        revealed = client.post(f"/api/contacts/{contact_id}/reveal")
        assert revealed.status_code == 200, revealed.text
        revealed_values = {c["type"]: c["value"] for c in revealed.json()["channels"]}
        assert revealed_values["telegram"] == "@ivanov_mgtu"

    def test_repeating_the_same_patch_does_not_change_version_or_ids(self, client) -> None:
        kam = run(client, _make_user, "KAM")
        _login(client, kam)
        contact_id = run(client, _make_contact, kam.id)
        channels = [{"type": "telegram", "value": "@ivanov_mgtu"}]

        first = _patch(client, contact_id, 1, channels=channels)
        assert first.status_code == 200, first.text
        version = first.json()["version"]
        before_rows = run(client, _channel_rows, contact_id)

        second = _patch(client, contact_id, version, channels=channels)
        assert second.status_code == 200, second.text
        assert second.json()["version"] == version
        after_rows = run(client, _channel_rows, contact_id)
        assert before_rows == after_rows

    def test_empty_list_removes_all_channels(self, client) -> None:
        kam = run(client, _make_user, "KAM")
        _login(client, kam)
        contact_id = run(client, _make_contact, kam.id)
        first = _patch(
            client, contact_id, 1, channels=[{"type": "phone_extra", "value": "+79990001122"}]
        )
        assert first.status_code == 200, first.text

        second = _patch(client, contact_id, first.json()["version"], channels=[])
        assert second.status_code == 200, second.text
        assert run(client, _channel_rows, contact_id) == []

    def test_more_than_ten_channels_is_rejected(self, client) -> None:
        kam = run(client, _make_user, "KAM")
        _login(client, kam)
        contact_id = run(client, _make_contact, kam.id)
        channels = [{"type": "phone_extra", "value": f"+7999000{i:04d}"} for i in range(11)]
        response = _patch(client, contact_id, 1, channels=channels)
        assert response.status_code == 422, response.text

    def test_two_primary_channels_of_the_same_type_is_rejected(self, client) -> None:
        kam = run(client, _make_user, "KAM")
        _login(client, kam)
        contact_id = run(client, _make_contact, kam.id)
        response = _patch(
            client,
            contact_id,
            1,
            channels=[
                {"type": "phone_extra", "value": "+79990001122", "is_primary": True},
                {"type": "phone_extra", "value": "+79990001133", "is_primary": True},
            ],
        )
        assert response.status_code == 422, response.text

    def test_invalid_email_channel_is_rejected_with_field(self, client) -> None:
        kam = run(client, _make_user, "KAM")
        _login(client, kam)
        contact_id = run(client, _make_contact, kam.id)
        response = _patch(
            client, contact_id, 1, channels=[{"type": "email_extra", "value": "not-an-email"}]
        )
        assert response.status_code == 422, response.text

    def test_audit_does_not_contain_raw_channel_values(self, client) -> None:
        kam = run(client, _make_user, "KAM")
        _login(client, kam)
        contact_id = run(client, _make_contact, kam.id)
        response = _patch(
            client, contact_id, 1, channels=[{"type": "telegram", "value": "@ivanov_mgtu"}]
        )
        assert response.status_code == 200, response.text
        raw = run(client, _audit_changes_raw_text, contact_id)
        assert "ivanov_mgtu" not in raw


class TestSource:
    def test_empty_source_is_recorded_by_kam(self, client) -> None:
        kam = run(client, _make_user, "KAM")
        _login(client, kam)
        contact_id = run(client, _make_contact, kam.id)
        response = _patch(client, contact_id, 1, source="conference")
        assert response.status_code == 200, response.text

    def test_filled_source_can_only_be_changed_by_admin(self, client) -> None:
        kam = run(client, _make_user, "KAM")
        _login(client, kam)
        contact_id = run(client, _make_contact, kam.id)
        first = _patch(client, contact_id, 1, source="conference")
        assert first.status_code == 200, first.text

        again_by_kam = _patch(client, contact_id, first.json()["version"], source="website")
        assert again_by_kam.status_code == 403, again_by_kam.text

        _login(client, run(client, _make_user, "ADMIN"))
        by_admin = _patch(client, contact_id, first.json()["version"], source="website")
        assert by_admin.status_code == 200, by_admin.text


class TestConsent:
    def test_foreign_or_revoked_consent_is_rejected(self, client) -> None:
        kam = run(client, _make_user, "KAM")
        _login(client, kam)
        contact_id = run(client, _make_contact, kam.id)
        other_contact_id = run(client, _make_contact, kam.id)
        foreign_consent = run(client, _make_consent, other_contact_id, True, False)
        revoked_consent = run(client, _make_consent, contact_id, True, True)

        for consent_id in (foreign_consent, revoked_consent):
            response = _patch(client, contact_id, 1, consent_id=consent_id)
            assert response.status_code == 422, response.text

    def test_valid_consent_is_accepted(self, client) -> None:
        kam = run(client, _make_user, "KAM")
        _login(client, kam)
        contact_id = run(client, _make_contact, kam.id)
        consent_id = run(client, _make_consent, contact_id, True, False)

        response = _patch(client, contact_id, 1, consent_id=consent_id)
        assert response.status_code == 200, response.text

    def test_consent_id_on_create_must_be_null(self, client) -> None:
        _login(client, run(client, _make_user, "KAM"))
        response = client.post(
            "/api/contacts",
            json={
                "first_name": "Пётр",
                "last_name": "Тестов",
                "consent_id": str(uuid.uuid4()),
            },
        )
        assert response.status_code == 422, response.text
