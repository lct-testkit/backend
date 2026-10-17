"""B7-6: детерминированные ключи PDF подписи/протокола и уборка сирот в S3.

Хранилище подменено словарём в памяти (настоящего SeaweedFS/S3 в тестовом окружении нет) —
проверяется логика (детерминированный ключ, удаление при откате, уборщик по возрасту), а не
HTTP-клиент S3."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.core.storage import ObjectSummary
from tests.conftest import TEST_DATABASE_URL, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

BUCKET = "signatures"


@pytest.fixture
def fake_store(monkeypatch):
    """`{(bucket, key): bytes}` в памяти + подмена `upload_object_bytes`/`delete_object` в
    `signing.service` и `delete_object`/`list_objects` в `signing.tasks` — оба модуля должны
    видеть одно и то же хранилище."""
    from app.modules.signing import service as svc
    from app.modules.signing import tasks as tasks_mod

    store: dict[tuple[str, str], bytes] = {}
    deleted: list[tuple[str, str]] = []

    async def upload(*, bucket: str, key: str, body: bytes, content_type: str) -> None:
        store[(bucket, key)] = body

    async def delete(*, bucket: str, key: str) -> None:
        store.pop((bucket, key), None)
        deleted.append((bucket, key))

    async def ensure(bucket: str) -> None:
        return None

    monkeypatch.setattr(svc, "upload_object_bytes", upload)
    monkeypatch.setattr(svc, "ensure_bucket", ensure)
    monkeypatch.setattr(svc, "delete_object", delete)
    monkeypatch.setattr(tasks_mod, "delete_object", delete)

    return {"store": store, "deleted": deleted}


class TestDeterministicKey:
    def test_signed_and_protocol_use_the_document_id_key(self, client, fake_store) -> None:
        document_id = uuid.uuid4()

        async def _store():
            from app.core.db import session_scope
            from app.modules.signing.service import SignatureDocumentService

            async with session_scope() as session:
                svc = SignatureDocumentService(session)
                signed_id = await svc._store_generated_pdf(  # noqa: SLF001
                    b"%PDF signed",
                    filename="signed.pdf",
                    uploaded_by=None,
                    key=f"signed/{document_id}/signed.pdf",
                )
                protocol_id = await svc._store_generated_pdf(  # noqa: SLF001
                    b"%PDF protocol",
                    filename="protocol.pdf",
                    uploaded_by=None,
                    key=f"signed/{document_id}/protocol.pdf",
                )
            return signed_id, protocol_id

        run(client, _store)
        assert (BUCKET, f"signed/{document_id}/signed.pdf") in fake_store["store"]
        assert (BUCKET, f"signed/{document_id}/protocol.pdf") in fake_store["store"]

    def test_a_repeat_overwrites_the_same_key_not_a_new_one(self, client, fake_store) -> None:
        document_id = uuid.uuid4()
        key = f"signed/{document_id}/signed.pdf"

        async def _store(payload: bytes):
            from app.core.db import session_scope
            from app.modules.signing.service import SignatureDocumentService

            async with session_scope() as session:
                return await SignatureDocumentService(session)._store_generated_pdf(  # noqa: SLF001
                    payload, filename="signed.pdf", uploaded_by=None, key=key
                )

        run(client, _store, b"%PDF v1")
        run(client, _store, b"%PDF v2")

        assert fake_store["store"][(BUCKET, key)] == b"%PDF v2"
        assert len([k for k in fake_store["store"] if k[1] == key]) == 1


class TestRollbackCleansUpTheObject:
    def test_object_uploaded_then_rolled_back_is_deleted(self, client, fake_store) -> None:
        key = f"signed/{uuid.uuid4()}/signed.pdf"

        async def _upload_then_blow_up():
            from app.core.db import session_scope
            from app.modules.signing.service import SignatureDocumentService

            async with session_scope() as session:
                await SignatureDocumentService(session)._store_generated_pdf(  # noqa: SLF001
                    b"%PDF", filename="signed.pdf", uploaded_by=None, key=key
                )
                raise RuntimeError("симуляция сбоя после загрузки, до коммита")

        async def _store_and_fail():
            with pytest.raises(RuntimeError):
                await _upload_then_blow_up()

        run(client, _store_and_fail)

        assert (BUCKET, key) not in fake_store["store"]
        assert (BUCKET, key) in fake_store["deleted"]

    def test_successful_commit_does_not_delete_the_object(self, client, fake_store) -> None:
        key = f"signed/{uuid.uuid4()}/signed.pdf"

        async def _store():
            from app.core.db import session_scope
            from app.modules.signing.service import SignatureDocumentService

            async with session_scope() as session:
                await SignatureDocumentService(session)._store_generated_pdf(  # noqa: SLF001
                    b"%PDF", filename="signed.pdf", uploaded_by=None, key=key
                )

        run(client, _store)

        assert (BUCKET, key) in fake_store["store"]
        assert (BUCKET, key) not in fake_store["deleted"]


class TestOrphanSweep:
    def test_old_orphan_without_a_files_row_is_removed(
        self, client, fake_store, monkeypatch
    ) -> None:
        from app.modules.signing import tasks as tasks_mod

        key = f"signed/{uuid.uuid4()}/signed.pdf"
        fake_store["store"][(BUCKET, key)] = b"%PDF"
        old = dt.datetime.now(dt.UTC) - dt.timedelta(hours=48)

        async def list_objects(*, bucket: str, prefix: str = ""):
            return [ObjectSummary(key=key, last_modified=old, size=4)]

        monkeypatch.setattr(tasks_mod, "list_objects", list_objects)
        result = run(client, tasks_mod.sweep_signature_orphans, {})

        assert result["removed"] == 1
        assert (BUCKET, key) not in fake_store["store"]

    def test_fresh_orphan_is_not_removed(self, client, fake_store, monkeypatch) -> None:
        from app.modules.signing import tasks as tasks_mod

        key = f"signed/{uuid.uuid4()}/signed.pdf"
        fake_store["store"][(BUCKET, key)] = b"%PDF"
        recent = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)

        async def list_objects(*, bucket: str, prefix: str = ""):
            return [ObjectSummary(key=key, last_modified=recent, size=4)]

        monkeypatch.setattr(tasks_mod, "list_objects", list_objects)
        result = run(client, tasks_mod.sweep_signature_orphans, {})

        assert result["removed"] == 0
        assert (BUCKET, key) in fake_store["store"]

    def test_old_object_with_a_files_row_is_kept(self, client, fake_store, monkeypatch) -> None:
        from app.modules.signing import tasks as tasks_mod

        document_id = uuid.uuid4()
        key = f"signed/{document_id}/signed.pdf"
        old = dt.datetime.now(dt.UTC) - dt.timedelta(hours=48)

        async def _store():
            from app.core.db import session_scope
            from app.modules.signing.service import SignatureDocumentService

            async with session_scope() as session:
                await SignatureDocumentService(session)._store_generated_pdf(  # noqa: SLF001
                    b"%PDF", filename="signed.pdf", uploaded_by=None, key=key
                )

        run(client, _store)

        async def list_objects(*, bucket: str, prefix: str = ""):
            return [ObjectSummary(key=key, last_modified=old, size=4)]

        monkeypatch.setattr(tasks_mod, "list_objects", list_objects)
        result = run(client, tasks_mod.sweep_signature_orphans, {})

        assert result["removed"] == 0
        assert (BUCKET, key) in fake_store["store"]
