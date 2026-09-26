"""Файлы: имена, общий объект хранилища, отказ при `commit`, защита файлов подписи.

Чистые проверки (`sanitize_filename`) идут без БД; сквозные — через API на PostgreSQL
(`TEST_DATABASE_URL`) с подменённым хранилищем: SeaweedFS в тестах нет.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import uuid
from types import SimpleNamespace

import pytest

from app.core.storage import _MAGIC_BYTES_LEN, ObjectInspection
from app.modules.files.service import sanitize_filename
from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

needs_db = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


class TestSanitizeFilename:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Договор.pdf", "Договор.pdf"),
            ("../../etc/passwd.pdf", "passwd.pdf"),
            ("..\\..\\windows\\evil.pdf", "evil.pdf"),
            ("C:\\Users\\me\\scan.pdf", "scan.pdf"),
            ('bad"name.pdf', "badname.pdf"),
            ("line\r\nbreak.pdf", "line break.pdf"),
            ("nul\x00byte.pdf", "nulbyte.pdf"),
            ("tab\tand   spaces.pdf", "tab and spaces.pdf"),
            ("  .hidden.pdf.  ", "hidden.pdf"),
            ("evil\u202efdp.exe", "evilfdp.exe"),
            ("a<b>c|d?e*f:g.pdf", "abcdefg.pdf"),
            ("", "file"),
            ("...", "file"),
            ("///", "file"),
        ],
    )
    def test_dangerous_parts_are_removed(self, raw: str, expected: str) -> None:
        assert sanitize_filename(raw) == expected

    def test_a_hidden_extension_stays_visible_after_cleaning(self) -> None:
        # Нулевой байт раньше «прятал» настоящее расширение от проверок на стороне клиента.
        assert sanitize_filename("photo.png\x00.exe").endswith(".exe")

    def test_a_long_name_is_cut_but_keeps_its_extension(self) -> None:
        cleaned = sanitize_filename("я" * 400 + ".pdf")

        assert len(cleaned) == 255
        assert cleaned.endswith(".pdf")

    def test_a_long_name_without_an_extension_is_cut(self) -> None:
        assert len(sanitize_filename("я" * 400)) == 255


@needs_db
class TestUploadOverHttp:
    @pytest.fixture(autouse=True)
    def _storage(self, client, monkeypatch: pytest.MonkeyPatch):
        from app.modules.files import service as files_service

        self.stored: dict[str, bytes] = {}
        self.deleted: list[tuple[str, str]] = []
        self.monkeypatch = monkeypatch

        async def fake_ensure_bucket(bucket: str) -> None:
            return None

        async def fake_presigned_put(**_kwargs: object) -> str:
            return "https://storage.invalid/put"

        async def fake_delete_object(*, bucket: str, key: str) -> None:
            self.deleted.append((bucket, key))

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
        self.user = run(client, _make_user, "KAM")
        client.headers["X-CSRF-Token"] = authenticate(client, self.user)

    def _intent(self, filename: str, *, size: int = 100, mime: str = "application/pdf"):
        return self.client.post(
            "/api/files/upload-intent",
            json={"filename": filename, "size_bytes": size, "mime_type": mime},
        )

    async def _row(self, file_id: str):
        from app.core.db import session_scope
        from app.modules.files.models import File

        async with session_scope() as session:
            file = await session.get(File, uuid.UUID(file_id))
            session.expunge(file)
            return file

    async def _audit_actions(self, file_id: str) -> list[str]:
        from sqlalchemy import select

        from app.core.db import session_scope
        from app.modules.audit.models import AuditLog

        async with session_scope() as session:
            rows = await session.execute(
                select(AuditLog.action).where(AuditLog.entity_id == uuid.UUID(file_id))
            )
            return list(rows.scalars())

    def _upload(self, filename: str, content: bytes, *, declared: int | None = None, **kwargs):
        response = self._intent(filename, size=declared or len(content), **kwargs)
        assert response.status_code == 201, response.text
        file_id = response.json()["file_id"]
        self.stored[run(self.client, self._row, file_id).storage_key] = content
        return file_id

    def _commit(self, file_id: str):
        return self.client.post(f"/api/files/{file_id}/commit", json={})

    # --- имена ------------------------------------------------------------------------

    def test_a_long_cyrillic_name_no_longer_breaks_the_storage_key(self) -> None:
        response = self._intent("я" * 200 + ".pdf")

        assert response.status_code == 201, response.text
        row = run(self.client, self._row, response.json()["file_id"])
        assert len(row.storage_key) < 100
        assert row.storage_key.endswith("/upload.pdf")
        assert row.original_filename == "я" * 200 + ".pdf"

    def test_the_stored_name_is_sanitised_and_the_key_carries_no_name(self) -> None:
        response = self._intent('../../секрет\r\n"x".pdf')

        assert response.status_code == 201, response.text
        row = run(self.client, self._row, response.json()["file_id"])
        assert row.original_filename == "секрет x.pdf"
        assert ".." not in row.storage_key
        assert "секрет" not in row.storage_key

    def test_the_extension_is_taken_from_the_cleaned_name(self) -> None:
        assert self._intent("photo.png\x00.exe").status_code == 415

    # --- отказ при commit сохраняется ---------------------------------------------------

    def test_a_mismatched_file_is_recorded_as_infected(self) -> None:
        file_id = self._upload("scan.pdf", b"\x89PNG\r\n\x1a\n not a pdf")

        response = self._commit(file_id)

        assert response.status_code == 422, response.text
        assert response.json()["code"] == "CRM-1403"
        row = run(self.client, self._row, file_id)
        assert (row.status, row.scan_result) == ("infected", "signature_mismatch")
        assert "FILE_INFECTED" in run(self.client, self._audit_actions, file_id)

    def test_a_second_commit_of_an_infected_file_is_refused_too(self) -> None:
        file_id = self._upload("scan.pdf", b"\x89PNG\r\n\x1a\n not a pdf")
        assert self._commit(file_id).status_code == 422

        again = self._commit(file_id)

        assert again.status_code == 422, again.text
        assert again.json()["code"] == "CRM-1403"

    def test_a_file_over_the_limit_is_recorded_and_its_object_removed(self) -> None:
        from app.core.config import get_settings

        settings = get_settings()
        self.monkeypatch.setattr(settings, "files_max_size_bytes", 20)
        self.monkeypatch.setattr(settings, "deal_files_max_size_bytes", 20)
        # `size_bytes` в запросе занижен — лимит ловится по факту, уже при commit.
        file_id = self._upload("big.pdf", b"%PDF-1.4 " + b"x" * 100, declared=10)
        response = self._commit(file_id)

        assert response.status_code == 413, response.text
        row = run(self.client, self._row, file_id)
        assert (row.status, row.scan_result) == ("infected", "file_too_large")
        assert (row.bucket, row.storage_key) in self.deleted
        assert "FILE_TOO_LARGE" in run(self.client, self._audit_actions, file_id)

    def test_a_virus_verdict_is_recorded(self) -> None:
        from app.modules.files import service as files_service

        class Dirty:
            async def scan(self, *, bucket: str, key: str):
                return files_service.ScanResult(clean=False, detail="Eicar-Test")

        previous = files_service.get_antivirus_scanner()
        files_service.register_antivirus_scanner(Dirty())
        try:
            file_id = self._upload("scan.pdf", b"%PDF-1.4 ok")
            response = self._commit(file_id)
        finally:
            files_service.register_antivirus_scanner(previous)

        assert response.status_code == 422, response.text
        row = run(self.client, self._row, file_id)
        assert (row.status, row.scan_result) == ("infected", "Eicar-Test")
        assert "FILE_INFECTED" in run(self.client, self._audit_actions, file_id)

    # --- MIME по содержимому ------------------------------------------------------------

    def test_mime_comes_from_the_verified_content_not_from_the_claim(self) -> None:
        file_id = self._upload("scan.pdf", b"%PDF-1.4 mime " + uuid.uuid4().bytes, mime="text/html")

        response = self._commit(file_id)

        assert response.status_code == 200, response.text
        assert run(self.client, self._row, file_id).mime_type == "application/pdf"

    # --- дедупликация и общий объект ------------------------------------------------------

    def test_identical_content_shares_one_object(self) -> None:
        content = b"%PDF-1.4 shared " + uuid.uuid4().bytes
        first = self._upload("a.pdf", content)
        second = self._upload("b.pdf", content)
        assert self._commit(first).status_code == 200

        assert self._commit(second).status_code == 200

        a = run(self.client, self._row, first)
        b = run(self.client, self._row, second)
        assert b.storage_key == a.storage_key
        assert b.bucket == a.bucket

    def test_an_object_of_another_bucket_is_never_shared(self) -> None:
        from app.core.db import session_scope
        from app.modules.files.models import File

        content = b"%PDF-1.4 elsewhere " + uuid.uuid4().bytes

        async def _foreign_copy() -> str:
            async with session_scope() as session:
                file = File(
                    storage_key=f"{uuid.uuid4()}/report.pdf",
                    bucket="reports",
                    original_filename="report.pdf",
                    mime_type="application/pdf",
                    size_bytes=len(content),
                    sha256=hashlib.sha256(content).hexdigest(),
                    status="ready",
                )
                session.add(file)
                await session.flush()
                return file.storage_key

        foreign_key = run(self.client, _foreign_copy)
        file_id = self._upload("mine.pdf", content)

        assert self._commit(file_id).status_code == 200

        row = run(self.client, self._row, file_id)
        assert row.storage_key != foreign_key
        assert row.bucket != "reports"

    def test_a_deleted_copy_is_not_a_dedup_target(self) -> None:
        from app.core.db import session_scope
        from app.modules.files.models import File

        content = b"%PDF-1.4 gone " + uuid.uuid4().bytes
        first = self._upload("a.pdf", content)
        assert self._commit(first).status_code == 200

        async def _soft_delete() -> None:
            async with session_scope() as session:
                file = await session.get(File, uuid.UUID(first))
                file.status = "deleted"
                file.deleted_at = file.created_at

        run(self.client, _soft_delete)
        second = self._upload("b.pdf", content)

        assert self._commit(second).status_code == 200
        assert (
            run(self.client, self._row, second).storage_key
            != (run(self.client, self._row, first)).storage_key
        )


@needs_db
class TestSharedObjectLifetime:
    """Объект удаляется физически, только если на него не ссылается другая живая запись."""

    @staticmethod
    async def _two_files_one_object() -> tuple[uuid.UUID, uuid.UUID, str]:
        from app.core.db import session_scope
        from app.modules.files.models import File

        key = f"{uuid.uuid4()}/shared.pdf"
        async with session_scope() as session:
            files = [
                File(
                    storage_key=key,
                    bucket="files",
                    original_filename="shared.pdf",
                    mime_type="application/pdf",
                    size_bytes=10,
                    sha256="b" * 64,
                    status="ready",
                )
                for _ in range(2)
            ]
            session.add_all(files)
            await session.flush()
            return files[0].id, files[1].id, key

    @staticmethod
    async def _release(file_id: uuid.UUID, bucket: str, key: str) -> bool:
        from app.core.db import session_scope
        from app.modules.files.models import File
        from app.modules.files.service import delete_object_if_unreferenced

        async with session_scope() as session:
            file = await session.get(File, file_id)
            file.status = "deleted"
            file.deleted_at = file.created_at
            await session.flush()
            return await delete_object_if_unreferenced(
                session, bucket=bucket, key=key, excluding_file_id=file_id
            )

    def test_the_object_survives_while_another_record_uses_it(self, client, monkeypatch) -> None:
        from app.modules.files import service as files_service

        deleted: list[str] = []

        async def fake_delete(*, bucket: str, key: str) -> None:
            deleted.append(key)

        monkeypatch.setattr(files_service, "delete_object", fake_delete)
        first, second, key = run(client, self._two_files_one_object)

        assert run(client, self._release, first, "files", key) is False
        assert deleted == []
        assert run(client, self._release, second, "files", key) is True
        assert deleted == [key]

    def test_report_retention_leaves_a_shared_object_alone(self, client, monkeypatch) -> None:
        from app.core.db import session_scope
        from app.modules.files import service as files_service
        from app.modules.files.models import File
        from app.modules.reporting.models import ReportJob
        from app.modules.reporting.tasks import expire_report_files

        deleted: list[str] = []

        async def fake_delete(*, bucket: str, key: str) -> None:
            deleted.append(key)

        monkeypatch.setattr(files_service, "delete_object", fake_delete)
        owner = run(client, _make_user, "KAM")
        report_id, user_copy_id, key = run(client, self._two_files_one_object)

        async def _expire_the_report() -> None:
            import datetime as dt

            async with session_scope() as session:
                session.add(
                    ReportJob(
                        template_code="deal_funnel",
                        format="xlsx",
                        requested_by=owner.id,
                        status="completed",
                        file_id=report_id,
                        expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=1),
                    )
                )
            await expire_report_files({})

        run(client, _expire_the_report)

        async def _state() -> tuple[str, str]:
            async with session_scope() as session:
                report_file = await session.get(File, report_id)
                user_copy = await session.get(File, user_copy_id)
                return report_file.status, user_copy.status

        assert run(client, _state) == ("deleted", "ready")
        # Файл, на который ссылалась вторая запись, остался цел: удалять объект было нельзя.
        assert key not in deleted

    def test_two_parallel_attachments_are_both_counted(self, client) -> None:
        from app.core.db import session_scope
        from app.modules.files.models import File
        from app.modules.files.service import AttachmentService

        owner = run(client, _make_user, "KAM")

        async def _scene() -> int:
            async with session_scope() as session:
                file = File(
                    storage_key=f"{uuid.uuid4()}/doc.pdf",
                    bucket="files",
                    original_filename="doc.pdf",
                    mime_type="application/pdf",
                    size_bytes=10,
                    sha256="c" * 64,
                    status="ready",
                    uploaded_by=owner.id,
                )
                session.add(file)
                await session.flush()
                file_id = file.id

            principal = SimpleNamespace(user_id=owner.id, is_admin=True)

            async def attach() -> None:
                async with session_scope() as session:
                    target = await session.get(File, file_id)
                    await AttachmentService(session).create(
                        principal,
                        file=target,
                        entity_type="deal",
                        entity_id=uuid.uuid4(),
                        category="other",
                        description=None,
                    )
                    await asyncio.sleep(0.2)  # держим транзакцию: обе успевают прочитать счётчик

            await asyncio.gather(attach(), attach())
            async with session_scope() as session:
                return (await session.get(File, file_id)).refcount

        assert run(client, _scene) == 2


@needs_db
class TestSigningFilesAreProtected:
    def _admin(self, client):
        admin = run(client, _make_user, "ADMIN")
        client.headers["X-CSRF-Token"] = authenticate(client, admin)
        return admin

    def _file_row(self, client, file_id):
        from app.core.db import session_scope
        from app.modules.files.models import File

        async def _get():
            async with session_scope() as session:
                file = await session.get(File, file_id)
                return file.status, file.deleted_at

        return run(client, _get)

    def test_signed_copy_protocol_and_original_cannot_be_deleted(self, client) -> None:
        from tests.signing_helpers import _build

        built = _build(
            client,
            status="signed",
            requests=[{"user": None, "status": "signed"}],
            with_results=True,
        )

        async def _original() -> uuid.UUID:
            from app.core.db import session_scope
            from app.modules.signing.models import SignatureDocument

            async with session_scope() as session:
                return (await session.get(SignatureDocument, built.document_id)).file_id

        original_id = run(client, _original)
        self._admin(client)

        for file_id in (built.signed_file_id, built.protocol_file_id, original_id):
            response = client.delete(f"/api/files/{file_id}")
            assert response.status_code == 422, (file_id, response.text)
            assert self._file_row(client, file_id) == ("ready", None)

    def test_an_unreferenced_file_is_still_deletable(self, client) -> None:
        from app.core.db import session_scope
        from app.modules.files.models import File

        async def _plain() -> uuid.UUID:
            async with session_scope() as session:
                file = File(
                    storage_key=f"{uuid.uuid4()}/plain.pdf",
                    bucket="files",
                    original_filename="plain.pdf",
                    mime_type="application/pdf",
                    size_bytes=10,
                    status="ready",
                )
                session.add(file)
                await session.flush()
                return file.id

        file_id = run(client, _plain)
        self._admin(client)

        response = client.delete(f"/api/files/{file_id}")

        assert response.status_code == 200, response.text
        assert self._file_row(client, file_id)[0] == "deleted"


@needs_db
class TestDownloadName:
    def test_a_legacy_name_with_a_quote_and_a_line_break_is_cleaned_for_the_header(
        self, client, monkeypatch
    ) -> None:
        from app.core.db import session_scope
        from app.modules.files import service as files_service
        from app.modules.files.models import File
        from app.modules.files.service import FileService

        captured: dict[str, str] = {}

        async def fake_presigned_get(**kwargs) -> str:
            captured.update(kwargs)
            return "https://storage.invalid/get"

        monkeypatch.setattr(files_service, "generate_presigned_get", fake_presigned_get)

        async def _download() -> None:
            async with session_scope() as session:
                file = File(
                    storage_key=f"{uuid.uuid4()}/old.pdf",
                    bucket="files",
                    original_filename='a".pdf\r\nSet-Cookie: x=1',
                    mime_type="application/pdf",
                    size_bytes=10,
                    status="ready",
                )
                session.add(file)
                await session.flush()
                await FileService(session).download_url(file)

        run(client, functools.partial(_download))

        name = captured["filename"]
        assert '"' not in name and "\r" not in name and "\n" not in name
