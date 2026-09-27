"""Idempotency-Key на создании комментария, задачи и вложения (A-15).

До этой правки ключ принимали только `POST /api/deals`, `/api/organizations` и
`/api/contacts` — повтор запроса (таймаут у клиента, ретрай после разрыва
соединения) на комментарий, задачу или привязку файла создавал вторую запись.
Тесты мирорят `tests/test_idempotency_release.py` и
`tests/test_contacts_dedupe.py::test_repeat_with_the_same_idempotency_key_is_replayed_not_a_duplicate`,
но для новых ручек.
"""

from __future__ import annotations

import hashlib
import uuid

import pytest

from app.core.storage import _MAGIC_BYTES_LEN, ObjectInspection
from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import create_deal, create_published_workflow, login

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _scene(client) -> tuple[dict, uuid.UUID]:
    """Админ, опубликованная воронка и сделка — общий фон для всех трёх ручек."""
    admin = login(client, "ADMIN")
    graph = create_published_workflow(client)
    deal = create_deal(client, graph["workflow"]["id"])
    return deal, admin.id


class TestCommentIdempotency:
    def test_success_is_replayed_from_the_stored_response(self, client) -> None:
        deal, _ = _scene(client)
        key = f"idem-{uuid.uuid4().hex}"
        body = {"body": "Первый комментарий"}

        first = client.post(
            f"/api/deals/{deal['id']}/comments", json=body, headers={"Idempotency-Key": key}
        )
        assert first.status_code == 201, first.text
        replay = client.post(
            f"/api/deals/{deal['id']}/comments", json=body, headers={"Idempotency-Key": key}
        )
        assert replay.status_code == 201, replay.text
        assert replay.json()["id"] == first.json()["id"]  # тот же ответ, а не второй комментарий

        listing = client.get(f"/api/deals/{deal['id']}/comments")
        assert len(listing.json()["items"]) == 1

    def test_same_key_with_another_body_is_a_conflict(self, client) -> None:
        deal, _ = _scene(client)
        key = f"idem-{uuid.uuid4().hex}"
        first = client.post(
            f"/api/deals/{deal['id']}/comments",
            json={"body": "А"},
            headers={"Idempotency-Key": key},
        )
        assert first.status_code == 201, first.text
        other = client.post(
            f"/api/deals/{deal['id']}/comments",
            json={"body": "Б"},
            headers={"Idempotency-Key": key},
        )
        assert other.status_code == 409 and other.json()["code"] == "CRM-1003", other.text

    def test_retry_after_a_failed_request_is_executed_again(self, client) -> None:
        deal, _ = _scene(client)
        key = f"idem-{uuid.uuid4().hex}"
        bad_deal_id = uuid.uuid4()

        # Чужая/не найденная сделка — 404, ключ не должен отравиться навсегда.
        failed = client.post(
            f"/api/deals/{bad_deal_id}/comments",
            json={"body": "Тест"},
            headers={"Idempotency-Key": key},
        )
        assert failed.status_code == 404, failed.text

        retry = client.post(
            f"/api/deals/{deal['id']}/comments",
            json={"body": "Тест"},
            headers={"Idempotency-Key": key},
        )
        assert retry.status_code == 201, retry.text


class TestTaskIdempotency:
    def test_success_is_replayed_from_the_stored_response(self, client) -> None:
        deal, admin_id = _scene(client)
        key = f"idem-{uuid.uuid4().hex}"
        body = {"deal_id": deal["id"], "title": "Позвонить", "assignee_id": str(admin_id)}

        first = client.post("/api/tasks", json=body, headers={"Idempotency-Key": key})
        assert first.status_code == 201, first.text
        replay = client.post("/api/tasks", json=body, headers={"Idempotency-Key": key})
        assert replay.status_code == 201, replay.text
        assert replay.json()["id"] == first.json()["id"]  # тот же ответ, а не вторая задача

        listing = client.get("/api/tasks", params={"deal_id": deal["id"]})
        assert len(listing.json()["items"]) == 1

    def test_same_key_with_another_body_is_a_conflict(self, client) -> None:
        deal, admin_id = _scene(client)
        key = f"idem-{uuid.uuid4().hex}"
        first = client.post(
            "/api/tasks",
            json={"deal_id": deal["id"], "title": "Задача А", "assignee_id": str(admin_id)},
            headers={"Idempotency-Key": key},
        )
        assert first.status_code == 201, first.text
        other = client.post(
            "/api/tasks",
            json={"deal_id": deal["id"], "title": "Задача Б", "assignee_id": str(admin_id)},
            headers={"Idempotency-Key": key},
        )
        assert other.status_code == 409 and other.json()["code"] == "CRM-1003", other.text

    def test_retry_after_a_failed_request_is_executed_again(self, client) -> None:
        deal, admin_id = _scene(client)
        key = f"idem-{uuid.uuid4().hex}"
        bad_deal_id = str(uuid.uuid4())

        failed = client.post(
            "/api/tasks",
            json={"deal_id": bad_deal_id, "title": "Задача", "assignee_id": str(admin_id)},
            headers={"Idempotency-Key": key},
        )
        assert failed.status_code == 404, failed.text

        retry = client.post(
            "/api/tasks",
            json={"deal_id": deal["id"], "title": "Задача", "assignee_id": str(admin_id)},
            headers={"Idempotency-Key": key},
        )
        assert retry.status_code == 201, retry.text


class TestAttachmentIdempotency:
    """Привязка файла к сделке — единственная POST-ручка вложений, которая создаёт новую
    строку (`commit` только переводит уже существующий файл в `ready`)."""

    @pytest.fixture(autouse=True)
    def _storage(self, client, monkeypatch: pytest.MonkeyPatch):
        from app.modules.files import service as files_service

        self.stored: dict[str, bytes] = {}

        async def fake_ensure_bucket(bucket: str) -> None:
            return None

        async def fake_presigned_put(**_kwargs: object) -> str:
            return "https://storage.invalid/put"

        async def fake_delete_object(**_kwargs: object) -> None:
            return None

        async def fake_inspect(*, bucket: str, key: str) -> ObjectInspection:
            content = self.stored[key]
            return ObjectInspection(
                exists=True,
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                magic_bytes=content[:_MAGIC_BYTES_LEN],
            )

        monkeypatch.setattr(files_service, "ensure_bucket", fake_ensure_bucket)
        monkeypatch.setattr(files_service, "generate_presigned_put", fake_presigned_put)
        monkeypatch.setattr(files_service, "delete_object", fake_delete_object)
        monkeypatch.setattr(files_service, "inspect_object", fake_inspect)
        self.client = client

    async def _storage_key(self, file_id: str) -> str:
        from app.core.db import session_scope
        from app.modules.files.models import File

        async with session_scope() as session:
            file = await session.get(File, uuid.UUID(file_id))
            assert file is not None
            return file.storage_key

    def _ready_file(self, content: bytes = b"%PDF-1.4 test") -> str:
        intent = self.client.post(
            "/api/files/upload-intent",
            json={
                "filename": "doc.pdf",
                "size_bytes": len(content),
                "mime_type": "application/pdf",
            },
        )
        assert intent.status_code == 201, intent.text
        file_id = intent.json()["file_id"]
        self.stored[run(self.client, self._storage_key, file_id)] = content
        commit = self.client.post(f"/api/files/{file_id}/commit", json={})
        assert commit.status_code == 200, commit.text
        return file_id

    def test_success_is_replayed_from_the_stored_response(self, client) -> None:
        deal, _ = _scene(client)
        file_id = self._ready_file()
        key = f"idem-{uuid.uuid4().hex}"
        body = {
            "file_id": file_id,
            "entity_type": "deal",
            "entity_id": deal["id"],
            "category": "other",
        }

        first = client.post("/api/attachments", json=body, headers={"Idempotency-Key": key})
        assert first.status_code == 201, first.text
        replay = client.post("/api/attachments", json=body, headers={"Idempotency-Key": key})
        assert replay.status_code == 201, replay.text
        assert replay.json()["id"] == first.json()["id"]  # тот же ответ, а не второе вложение

        listing = client.get(
            "/api/attachments", params={"entity_type": "deal", "entity_id": deal["id"]}
        )
        assert len(listing.json()["items"]) == 1

    def test_same_key_with_another_body_is_a_conflict(self, client) -> None:
        deal, _ = _scene(client)
        file_a = self._ready_file(b"%PDF-1.4 file-a")
        file_b = self._ready_file(b"%PDF-1.4 file-b")
        key = f"idem-{uuid.uuid4().hex}"
        first = client.post(
            "/api/attachments",
            json={
                "file_id": file_a,
                "entity_type": "deal",
                "entity_id": deal["id"],
                "category": "other",
            },
            headers={"Idempotency-Key": key},
        )
        assert first.status_code == 201, first.text
        other = client.post(
            "/api/attachments",
            json={
                "file_id": file_b,
                "entity_type": "deal",
                "entity_id": deal["id"],
                "category": "other",
            },
            headers={"Idempotency-Key": key},
        )
        assert other.status_code == 409 and other.json()["code"] == "CRM-1003", other.text

    def test_retry_after_a_failed_request_is_executed_again(self, client) -> None:
        deal, _ = _scene(client)
        key = f"idem-{uuid.uuid4().hex}"
        bad_file_id = str(uuid.uuid4())

        failed = client.post(
            "/api/attachments",
            json={
                "file_id": bad_file_id,
                "entity_type": "deal",
                "entity_id": deal["id"],
                "category": "other",
            },
            headers={"Idempotency-Key": key},
        )
        assert failed.status_code == 404, failed.text

        file_id = self._ready_file()
        retry = client.post(
            "/api/attachments",
            json={
                "file_id": file_id,
                "entity_type": "deal",
                "entity_id": deal["id"],
                "category": "other",
            },
            headers={"Idempotency-Key": key},
        )
        assert retry.status_code == 201, retry.text
