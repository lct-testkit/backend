"""B2-8: события FILE_ADDED/FILE_REMOVED, `attachments_count` в истории статусов и ручка
`GET /api/deals/{id}/history/{history_id}/attachments`.

Требует `TEST_DATABASE_URL` — настоящая Postgres (см. докстринг `tests/conftest.py`).
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import by_code, create_deal, create_published_workflow, login, transition_deal

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _graph_body() -> dict:
    return {
        "statuses": [
            {"code": "step4", "name": "Шаг 4", "type": "initial", "sort_order": 10},
            {"code": "step6", "name": "Шаг 6", "type": "won", "sort_order": 20},
        ],
        "transitions": [{"from_status": "step4", "to_status": "step6", "name": "К подписанию"}],
        "sla_rules": [],
    }


def _scene(client):
    owner = login(client, "ADMIN")
    graph = create_published_workflow(client, _graph_body(), deal_type="b2b")
    statuses = by_code(graph)
    deal = create_deal(client, graph["workflow"]["id"])
    return owner, statuses, deal


async def _ready_file() -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.files.models import File

    async with session_scope() as session:
        file = File(
            storage_key=f"{uuid.uuid4()}/dogovor.pdf",
            bucket="files",
            original_filename="dogovor.pdf",
            mime_type="application/pdf",
            size_bytes=1024,
            sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            status="ready",
        )
        session.add(file)
        await session.flush()
        return file.id


def _attach(client, deal_id: str, *, category: str = "contract", idempotency_key: str | None = None):
    file_id = run(client, _ready_file)
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    response = client.post(
        "/api/attachments",
        json={
            "file_id": str(file_id),
            "entity_type": "deal",
            "entity_id": deal_id,
            "category": category,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _history_events(client, deal_id: str) -> dict:
    response = client.get(f"/api/deals/{deal_id}/history")
    assert response.status_code == 200, response.text
    return response.json()


class TestFileAddedEvent:
    def test_attaching_a_file_to_a_deal_creates_exactly_one_event(self, client) -> None:
        _, _, deal = _scene(client)

        attachment = _attach(client, deal["id"])

        history = _history_events(client, deal["id"])
        file_added = [e for e in history["events"] if e["event_type"] == "FILE_ADDED"]
        assert len(file_added) == 1
        payload = file_added[0]["payload"]
        assert payload["attachment_id"] == attachment["id"]
        assert payload["category"] == "contract"
        assert payload["filename"] == "dogovor.pdf"

    def test_idempotency_key_replay_does_not_duplicate_the_event(self, client) -> None:
        _, _, deal = _scene(client)
        key = str(uuid.uuid4())
        file_id = run(client, _ready_file)
        body = {
            "file_id": str(file_id),
            "entity_type": "deal",
            "entity_id": deal["id"],
            "category": "contract",
        }
        first = client.post("/api/attachments", json=body, headers={"Idempotency-Key": key})
        assert first.status_code == 201, first.text

        # Повтор с ТЕМ ЖЕ телом и ключом — настоящий реплей, отдаёт закэшированный ответ.
        response = client.post("/api/attachments", json=body, headers={"Idempotency-Key": key})
        assert response.status_code == 201, response.text
        assert response.json()["id"] == first.json()["id"]

        history = _history_events(client, deal["id"])
        file_added = [e for e in history["events"] if e["event_type"] == "FILE_ADDED"]
        assert len(file_added) == 1

    def test_attaching_to_an_organization_does_not_create_a_deal_event(self, client) -> None:
        login(client, "ADMIN")

        async def _org() -> uuid.UUID:
            from app.core.db import session_scope
            from app.modules.catalog.models import Organization

            async with session_scope() as session:
                org = Organization(name=f"Вуз {uuid.uuid4().hex[:6]}", org_type="university")
                session.add(org)
                await session.flush()
                return org.id

        org_id = run(client, _org)
        file_id = run(client, _ready_file)
        response = client.post(
            "/api/attachments",
            json={
                "file_id": str(file_id),
                "entity_type": "organization",
                "entity_id": str(org_id),
                "category": "contract",
            },
        )
        assert response.status_code == 201, response.text
        attachment_id = response.json()["id"]

        # БД общая на сессию тестов: считать все DealEvent ненадёжно (другие тесты пишут
        # свои). Проверяем точечно — нет события, несущего id ЭТОГО вложения.
        async def _events_referencing_this_attachment() -> int:
            from sqlalchemy import func, select

            from app.core.db import session_scope
            from app.modules.crm.models import DealEvent

            async with session_scope() as session:
                return (
                    await session.scalar(
                        select(func.count())
                        .select_from(DealEvent)
                        .where(DealEvent.payload["attachment_id"].astext == attachment_id)
                    )
                    or 0
                )

        assert run(client, _events_referencing_this_attachment) == 0

    def test_deleting_an_attachment_creates_file_removed(self, client) -> None:
        _, _, deal = _scene(client)
        attachment = _attach(client, deal["id"])

        response = client.delete(f"/api/attachments/{attachment['id']}")
        assert response.status_code == 200, response.text

        history = _history_events(client, deal["id"])
        removed = [e for e in history["events"] if e["event_type"] == "FILE_REMOVED"]
        assert len(removed) == 1
        assert removed[0]["payload"]["attachment_id"] == attachment["id"]


class TestAttachmentsCountAndHistoryEndpoint:
    def test_transition_with_attachments_shows_count_and_the_endpoint_lists_them(
        self, client
    ) -> None:
        owner, statuses, deal = _scene(client)
        attachment = _attach(client, deal["id"])

        moved = transition_deal(
            client, deal, statuses["step6"]["id"], attachments=[attachment["id"]]
        )
        assert moved.status_code == 200, moved.text

        history = _history_events(client, deal["id"])
        (transition_row,) = [s for s in history["statuses"] if s["to_status_id"] == statuses["step6"]["id"]]
        assert transition_row["attachments_count"] == 1

        files_response = client.get(
            f"/api/deals/{deal['id']}/history/{transition_row['id']}/attachments"
        )
        assert files_response.status_code == 200, files_response.text
        (item,) = files_response.json()["items"]
        assert item["id"] == attachment["id"]
        assert item["file"]["original_filename"] == "dogovor.pdf"

    def test_history_entries_without_attachments_default_to_zero(self, client) -> None:
        owner, statuses, deal = _scene(client)
        moved = transition_deal(client, deal, statuses["step6"]["id"])
        assert moved.status_code == 200, moved.text

        history = _history_events(client, deal["id"])
        (row,) = [s for s in history["statuses"] if s["to_status_id"] == statuses["step6"]["id"]]
        assert row["attachments_count"] == 0

    def test_foreign_deal_history_id_is_not_found(self, client) -> None:
        owner, statuses, deal = _scene(client)
        other_deal = create_deal(client, deal["workflow_id"])

        response = client.get(f"/api/deals/{other_deal['id']}/history/{uuid.uuid4()}/attachments")
        assert response.status_code == 404, response.text

    def test_auditor_cannot_read_history_attachments(self, client) -> None:
        from tests.conftest import _make_user, authenticate

        owner, statuses, deal = _scene(client)
        auditor = run(client, _make_user, "AUDITOR")
        client.headers["X-CSRF-Token"] = authenticate(client, auditor)

        response = client.get(
            f"/api/deals/{deal['id']}/history/{uuid.uuid4()}/attachments"
        )
        assert response.status_code == 403, response.text
