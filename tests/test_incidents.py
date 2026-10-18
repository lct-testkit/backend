"""B6-12: реестр инцидентов безопасности — кандидат из события, подтверждение, сроки РКН,
генерация уведомлений, права, сканер дедлайнов.
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


def _login(client, user) -> None:
    client.headers["X-CSRF-Token"] = authenticate(client, user)


@pytest.fixture(autouse=True)
def _stub_storage(monkeypatch):
    """Настоящего S3 в тестах нет (тот же приём, что `test_erasure_act_signature.py`) —
    PDF-генерация уведомлений РКН должна проверяться без сетевого вызова."""
    from app.modules.identity import incident_service as svc

    store: dict[tuple[str, str], bytes] = {}

    async def upload(*, bucket: str, key: str, body: bytes, content_type: str) -> None:
        store[(bucket, key)] = body

    async def ensure(bucket: str) -> None:
        return None

    async def delete(*, bucket: str, key: str) -> None:
        store.pop((bucket, key), None)

    monkeypatch.setattr(svc, "upload_object_bytes", upload)
    monkeypatch.setattr(svc, "ensure_bucket", ensure)
    monkeypatch.setattr(svc, "delete_object", delete)
    return store


async def _record_malware_event(user_id: uuid.UUID) -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.identity.models import SecurityEventType, Severity
    from app.modules.identity.service import IdentityService

    async with session_scope() as session:
        event = await IdentityService(session).record_security_event(
            SecurityEventType.MALWARE_DETECTED,
            user_id=user_id,
            severity=Severity.CRITICAL,
            details={"file_id": "test"},
        )
        return event.id


async def _incidents_for_event(event_id: uuid.UUID) -> list:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.identity.models import SecurityIncident

    async with session_scope() as session:
        rows = await session.execute(
            select(SecurityIncident).where(SecurityIncident.event_ids.contains([str(event_id)]))
        )
        found = list(rows.scalars().all())
        for row in found:
            session.expunge(row)
        return found


async def _notifications_for(recipient_id: uuid.UUID, template_code: str) -> list:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.notification.models import Notification

    async with session_scope() as session:
        rows = await session.execute(
            select(Notification).where(
                Notification.recipient_id == recipient_id,
                Notification.template_code == template_code,
            )
        )
        return list(rows.scalars().all())


class TestCandidateFromEvent:
    def test_malware_event_creates_a_candidate_and_notifies_admins(self, client) -> None:
        admin = run(client, _make_user, "ADMIN")
        target = run(client, _make_user, "KAM")

        event_id = run(client, functools.partial(_record_malware_event, target.id))

        incidents = run(client, functools.partial(_incidents_for_event, event_id))
        assert len(incidents) == 1
        incident = incidents[0]
        assert incident.status == "candidate"
        assert incident.rkn_24_due_at is None  # срок не идёт до подтверждения
        assert incident.number.startswith("INC-")

        notes = run(
            client,
            functools.partial(_notifications_for, admin.id, "SECURITY_INCIDENT_CANDIDATE"),
        )
        assert len(notes) == 1

    def test_a_candidate_failure_does_not_roll_back_the_security_event(
        self, client, monkeypatch
    ) -> None:
        """SAVEPOINT: сбой создания кандидата не должен стереть само событие безопасности."""
        from app.modules.identity import incident_service as svc

        async def boom(*_args, **_kwargs):
            raise RuntimeError("шаблон/рендер сломан")

        monkeypatch.setattr(svc.IncidentService, "suggest", boom)
        target = run(client, _make_user, "KAM")

        event_id = run(client, functools.partial(_record_malware_event, target.id))

        async def _event_exists() -> bool:
            from app.core.db import session_scope
            from app.modules.identity.models import SecurityEvent

            async with session_scope() as session:
                return await session.get(SecurityEvent, event_id) is not None

        assert run(client, _event_exists) is True


class TestConfirmSetsDeadlines:
    def test_confirm_sets_24_and_72_hour_deadlines(self, client) -> None:
        admin = run(client, _make_user, "ADMIN")
        target = run(client, _make_user, "KAM")
        event_id = run(client, functools.partial(_record_malware_event, target.id))
        incident = run(client, functools.partial(_incidents_for_event, event_id))[0]
        _login(client, admin)
        detected_at = dt.datetime.now(dt.UTC).replace(microsecond=0)

        response = client.post(
            f"/api/admin/incidents/{incident.id}/confirm",
            headers={"If-Match": str(incident.version)},
            json={
                "detected_at": detected_at.isoformat(),
                "data_categories": ["СНИЛС"],
                "subjects_estimate": 3,
                "causes": "тест",
                "harm": "тест",
                "measures": "тест",
            },
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "open"
        due_24 = dt.datetime.fromisoformat(body["rkn_24_due_at"].replace("Z", "+00:00"))
        due_72 = dt.datetime.fromisoformat(body["rkn_72_due_at"].replace("Z", "+00:00"))
        assert (due_24 - detected_at) == dt.timedelta(hours=24)
        assert (due_72 - detected_at) == dt.timedelta(hours=72)

    def test_the_deadlines_follow_the_settings(self, client, monkeypatch) -> None:
        """`INCIDENT_RKN_24_HOURS`/`INCIDENT_RKN_72_HOURS` раньше существовали только в Settings:
        `confirm` считал 24 и 72 часа константами, и смена настройки ничего не меняла."""
        import types

        from app.modules.identity import incident_service as svc

        monkeypatch.setattr(
            svc,
            "get_settings",
            lambda: types.SimpleNamespace(incident_rkn_24_hours=12, incident_rkn_72_hours=36),
        )
        admin = run(client, _make_user, "ADMIN")
        target = run(client, _make_user, "KAM")
        event_id = run(client, functools.partial(_record_malware_event, target.id))
        incident = run(client, functools.partial(_incidents_for_event, event_id))[0]
        _login(client, admin)
        detected_at = dt.datetime.now(dt.UTC).replace(microsecond=0)

        response = client.post(
            f"/api/admin/incidents/{incident.id}/confirm",
            headers={"If-Match": str(incident.version)},
            json={"detected_at": detected_at.isoformat()},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        due_24 = dt.datetime.fromisoformat(body["rkn_24_due_at"].replace("Z", "+00:00"))
        due_72 = dt.datetime.fromisoformat(body["rkn_72_due_at"].replace("Z", "+00:00"))
        assert (due_24 - detected_at) == dt.timedelta(hours=12)
        assert (due_72 - detected_at) == dt.timedelta(hours=36)

    def test_confirming_twice_is_rejected(self, client) -> None:
        admin = run(client, _make_user, "ADMIN")
        target = run(client, _make_user, "KAM")
        event_id = run(client, functools.partial(_record_malware_event, target.id))
        incident = run(client, functools.partial(_incidents_for_event, event_id))[0]
        _login(client, admin)
        body = {"detected_at": dt.datetime.now(dt.UTC).isoformat()}
        first = client.post(
            f"/api/admin/incidents/{incident.id}/confirm",
            headers={"If-Match": str(incident.version)},
            json=body,
        )
        assert first.status_code == 200, first.text
        new_version = first.json()["version"]

        second = client.post(
            f"/api/admin/incidents/{incident.id}/confirm",
            headers={"If-Match": str(new_version)},
            json=body,
        )

        assert second.status_code == 422, second.text


class TestRknNoticeFlow:
    def _confirmed(self, client):
        admin = run(client, _make_user, "ADMIN")
        target = run(client, _make_user, "KAM")
        event_id = run(client, functools.partial(_record_malware_event, target.id))
        incident = run(client, functools.partial(_incidents_for_event, event_id))[0]
        _login(client, admin)
        confirmed = client.post(
            f"/api/admin/incidents/{incident.id}/confirm",
            headers={"If-Match": str(incident.version)},
            json={"detected_at": dt.datetime.now(dt.UTC).isoformat()},
        ).json()
        return confirmed

    def test_mark_sent_without_generating_first_is_422(self, client) -> None:
        confirmed = self._confirmed(client)

        response = client.post(
            f"/api/admin/incidents/{confirmed['id']}/rkn-notice/24h/sent",
            headers={"If-Match": str(confirmed["version"])},
            json={"reference": "RKN-001"},
        )

        assert response.status_code == 422, response.text

    def test_generate_then_mark_sent_then_repeat_is_409(self, client) -> None:
        confirmed = self._confirmed(client)
        incident_id = confirmed["id"]

        notice = client.post(f"/api/admin/incidents/{incident_id}/rkn-notice/24h")
        assert notice.status_code == 200, notice.text
        assert notice.json()["stage"] == "24h"
        assert notice.json()["file_id"]
        assert "incident_summary" in notice.json()["fields"]

        sent = client.post(
            f"/api/admin/incidents/{incident_id}/rkn-notice/24h/sent",
            headers={"If-Match": str(confirmed["version"])},
            json={"reference": "RKN-001"},
        )
        assert sent.status_code == 200, sent.text
        assert sent.json()["status"] == "rkn_24_sent"
        new_version = sent.json()["version"]

        repeat = client.post(
            f"/api/admin/incidents/{incident_id}/rkn-notice/24h/sent",
            headers={"If-Match": str(new_version)},
            json={"reference": "RKN-002"},
        )
        assert repeat.status_code == 409, repeat.text

    def test_72h_notice_requires_24h_sent_first(self, client) -> None:
        confirmed = self._confirmed(client)

        response = client.post(f"/api/admin/incidents/{confirmed['id']}/rkn-notice/72h")

        assert response.status_code == 422, response.text


class TestSubjectsNotice:
    def test_a_candidate_cannot_have_a_subjects_notice(self, client) -> None:
        admin = run(client, _make_user, "ADMIN")
        target = run(client, _make_user, "KAM")
        event_id = run(client, functools.partial(_record_malware_event, target.id))
        incident = run(client, functools.partial(_incidents_for_event, event_id))[0]
        _login(client, admin)

        response = client.post(f"/api/admin/incidents/{incident.id}/subjects-notice")

        assert response.status_code == 422, response.text

    def test_a_confirmed_incident_gets_one_notice(self, client) -> None:
        confirmed = TestRknNoticeFlow()._confirmed(client)

        first = client.post(f"/api/admin/incidents/{confirmed['id']}/subjects-notice")
        assert first.status_code == 200, first.text
        assert first.json()["file_id"]

        second = client.post(f"/api/admin/incidents/{confirmed['id']}/subjects-notice")
        assert second.status_code == 409, second.text


class TestPermissions:
    def test_admin_and_auditor_can_read_head_and_kam_cannot(self, client) -> None:
        for role, expected in (("ADMIN", 200), ("AUDITOR", 200), ("HEAD", 403), ("KAM", 403)):
            _login(client, run(client, _make_user, role))
            response = client.get("/api/admin/incidents")
            assert response.status_code == expected, (role, response.text)

    def test_only_admin_can_create(self, client) -> None:
        _login(client, run(client, _make_user, "AUDITOR"))

        response = client.post(
            "/api/admin/incidents",
            json={"title": "Тест", "severity": "warning"},
        )

        assert response.status_code == 403, response.text

    def test_if_match_is_required_for_patch(self, client) -> None:
        admin = run(client, _make_user, "ADMIN")
        target = run(client, _make_user, "KAM")
        event_id = run(client, functools.partial(_record_malware_event, target.id))
        incident = run(client, functools.partial(_incidents_for_event, event_id))[0]
        _login(client, admin)

        response = client.patch(
            f"/api/admin/incidents/{incident.id}", json={"title": "Новое название"}
        )

        assert response.status_code == 422, response.text


class TestPatchRefusesToClearRequiredFields:
    """`title` и `severity` — NOT NULL: `null` в PATCH раньше доходил до БД и давал 500."""

    def _candidate(self, client):
        admin = run(client, _make_user, "ADMIN")
        target = run(client, _make_user, "KAM")
        event_id = run(client, functools.partial(_record_malware_event, target.id))
        incident = run(client, functools.partial(_incidents_for_event, event_id))[0]
        _login(client, admin)
        return incident

    @pytest.mark.parametrize("body", [{"title": None}, {"severity": None}, {"title": "   "}])
    def test_null_or_blank_required_field_is_422_not_500(self, client, body) -> None:
        incident = self._candidate(client)

        response = client.patch(
            f"/api/admin/incidents/{incident.id}",
            headers={"If-Match": str(incident.version)},
            json=body,
        )

        assert response.status_code == 422, response.text

    def test_nullable_fields_can_still_be_cleared(self, client) -> None:
        incident = self._candidate(client)

        response = client.patch(
            f"/api/admin/incidents/{incident.id}",
            headers={"If-Match": str(incident.version)},
            json={"description": None, "title": "Новое название"},
        )

        assert response.status_code == 200, response.text
        assert response.json()["description"] is None
        assert response.json()["title"] == "Новое название"


class TestDeadlineSweeper:
    async def _overdue_incident(self, admin_id: uuid.UUID) -> uuid.UUID:
        from app.core.db import session_scope
        from app.modules.identity.incident_service import IncidentService

        async with session_scope() as session:
            incident = await IncidentService(session).create_manual(
                title="Тест просрочки",
                severity="critical",
                description=None,
                data_categories=[],
                subjects_estimate=None,
                created_by=admin_id,
            )
            incident.status = "open"
            incident.detected_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=30)
            incident.rkn_24_due_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=6)
            incident.rkn_72_due_at = dt.datetime.now(dt.UTC) + dt.timedelta(hours=42)
            await session.flush()
            return incident.id

    @staticmethod
    async def _sweep() -> dict:
        from app.modules.identity.tasks import sweep_incident_deadlines

        return await sweep_incident_deadlines({})

    @staticmethod
    async def _audit_count(incident_id: uuid.UUID) -> int:
        from sqlalchemy import func, select

        from app.core.db import session_scope
        from app.modules.audit.models import AuditLog

        async with session_scope() as session:
            return await session.scalar(
                select(func.count()).where(
                    AuditLog.entity_id == incident_id,
                    AuditLog.action == "INCIDENT_DEADLINE_OVERDUE",
                )
            )

    def test_overdue_sends_one_notification_and_one_audit_record(self, client) -> None:
        admin = run(client, _make_user, "ADMIN")
        incident_id = run(client, functools.partial(self._overdue_incident, admin.id))

        run(client, self._sweep)

        notes = run(
            client, functools.partial(_notifications_for, admin.id, "SECURITY_INCIDENT_DEADLINE")
        )
        assert len(notes) == 1
        assert run(client, functools.partial(self._audit_count, incident_id)) == 1

        run(client, self._sweep)  # второй проход не дублирует

        notes_after = run(
            client, functools.partial(_notifications_for, admin.id, "SECURITY_INCIDENT_DEADLINE")
        )
        assert len(notes_after) == 1
        assert run(client, functools.partial(self._audit_count, incident_id)) == 1
