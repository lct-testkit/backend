"""B6-4: решения `FileService.commit` по результату антивирусной проверки (таблица задачи) —
подмена сканера через `register_antivirus_scanner`, S3 подменён (`tests/test_files_hardening.
py::TestUploadOverHttp._storage` — тот же приём, но здесь свой маленький набор фикстур, чтобы
управлять `CLAMAV_ENABLED`/`clamav_fail_mode`/`clamav_sync_max_bytes` на уровне теста).
"""

from __future__ import annotations

import hashlib
import uuid

import pytest

from app.core.storage import _MAGIC_BYTES_LEN, ObjectInspection
from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


class _FixedScanner:
    def __init__(self, result) -> None:
        self._result = result

    async def scan(self, *, bucket: str, key: str):
        return self._result


class TestCommitDecisions:
    @pytest.fixture(autouse=True)
    def _storage(self, client, monkeypatch: pytest.MonkeyPatch):
        from app.modules.files import service as files_service

        self.stored: dict[str, bytes] = {}
        self.deleted: list[tuple[str, str]] = []
        self.copied: list[tuple[str, str, str, str]] = []
        self.monkeypatch = monkeypatch

        async def fake_ensure_bucket(bucket: str) -> None:
            return None

        async def fake_presigned_put(**_kwargs: object) -> str:
            return "https://storage.invalid/put"

        async def fake_delete_object(*, bucket: str, key: str) -> None:
            self.deleted.append((bucket, key))

        async def fake_copy_object(
            *, src_bucket: str, src_key: str, dst_bucket: str, dst_key: str
        ) -> None:
            self.copied.append((src_bucket, src_key, dst_bucket, dst_key))

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
        monkeypatch.setattr(files_service, "copy_object", fake_copy_object)
        monkeypatch.setattr(files_service, "inspect_object", fake_inspect)

        self.client = client
        self.user = run(client, _make_user, "KAM")
        client.headers["X-CSRF-Token"] = authenticate(client, self.user)

    def _upload(self, content: bytes, filename: str = "scan.pdf"):
        response = self.client.post(
            "/api/files/upload-intent",
            json={"filename": filename, "size_bytes": len(content), "mime_type": "application/pdf"},
        )
        assert response.status_code == 201, response.text
        file_id = response.json()["file_id"]

        async def _row(fid: str):
            from app.core.db import session_scope
            from app.modules.files.models import File

            async with session_scope() as session:
                row = await session.get(File, uuid.UUID(fid))
                session.expunge(row)
                return row

        self.stored[run(self.client, _row, file_id).storage_key] = content
        return file_id

    def _commit(self, file_id: str):
        return self.client.post(f"/api/files/{file_id}/commit", json={})

    async def _row(self, file_id: str):
        from app.core.db import session_scope
        from app.modules.files.models import File

        async with session_scope() as session:
            row = await session.get(File, uuid.UUID(file_id))
            session.expunge(row)
            return row

    def _set_scanner(self, result) -> None:
        from app.modules.files import service as files_service

        self._previous_scanner = files_service.get_antivirus_scanner()
        files_service.register_antivirus_scanner(_FixedScanner(result))

    def _restore_scanner(self) -> None:
        from app.modules.files import service as files_service

        files_service.register_antivirus_scanner(self._previous_scanner)

    def test_clean_gives_ready(self) -> None:
        from app.modules.files.service import ScanResult

        self._set_scanner(ScanResult(clean=True))
        try:
            file_id = self._upload(b"%PDF-1.4 ok " + uuid.uuid4().bytes)
            response = self._commit(file_id)
        finally:
            self._restore_scanner()

        assert response.status_code == 200, response.text
        row = run(self.client, self._row, file_id)
        assert (row.status, row.scan_result) == ("ready", "clean")

    def test_found_gives_infected_and_quarantine(self) -> None:
        from app.modules.files.service import ScanResult

        self._set_scanner(ScanResult(clean=False, signature="Eicar-Test"))
        try:
            file_id = self._upload(b"%PDF-1.4 bad " + uuid.uuid4().bytes)
            response = self._commit(file_id)
        finally:
            self._restore_scanner()

        assert response.status_code == 422, response.text
        row = run(self.client, self._row, file_id)
        assert (row.status, row.scan_result) == ("infected", "Eicar-Test")
        assert len(self.copied) == 1
        assert self.copied[0][3].startswith(f"{file_id}/")

    def test_scan_error_closed_mode_quarantines_without_rejecting(self) -> None:
        from app.core.config import get_settings
        from app.modules.files.service import ScanResult

        settings = get_settings()
        self.monkeypatch.setattr(settings, "clamav_fail_mode", "closed")
        self._set_scanner(ScanResult(clean=False, error=True, detail="clamd_unavailable"))
        try:
            file_id = self._upload(b"%PDF-1.4 err " + uuid.uuid4().bytes)
            response = self._commit(file_id)
        finally:
            self._restore_scanner()

        assert response.status_code == 200, response.text
        row = run(self.client, self._row, file_id)
        assert (row.status, row.scan_result) == ("quarantined", "scan_unavailable")

    def test_scan_error_open_mode_accepts_with_a_note(self) -> None:
        from app.core.config import get_settings
        from app.modules.files.service import ScanResult

        settings = get_settings()
        self.monkeypatch.setattr(settings, "clamav_fail_mode", "open")
        self._set_scanner(ScanResult(clean=False, error=True, detail="clamd_unavailable"))
        try:
            file_id = self._upload(b"%PDF-1.4 err2 " + uuid.uuid4().bytes)
            response = self._commit(file_id)
        finally:
            self._restore_scanner()

        assert response.status_code == 200, response.text
        row = run(self.client, self._row, file_id)
        assert row.status == "ready"
        assert row.scan_result.startswith("skipped:")

    def test_oversized_file_is_quarantined_without_a_sync_scan(self) -> None:
        from app.core.config import get_settings
        from app.modules.files.service import ScanResult

        settings = get_settings()
        self.monkeypatch.setattr(settings, "clamav_enabled", True)
        self.monkeypatch.setattr(settings, "clamav_sync_max_bytes", 10)
        # Сканер не должен даже вызваться — если бы он вызвался и отдал "found", тест бы не
        # отличил "сработала защита по размеру" от "сканер случайно сработал".
        self._set_scanner(ScanResult(clean=False, signature="should-not-be-called"))
        try:
            file_id = self._upload(b"%PDF-1.4 " + b"x" * 100)
            response = self._commit(file_id)
        finally:
            self._restore_scanner()

        assert response.status_code == 200, response.text
        row = run(self.client, self._row, file_id)
        assert (row.status, row.scan_result) == ("quarantined", "pending_scan")

    def test_long_signature_name_is_stored_without_crashing(self) -> None:
        from app.modules.files.service import ScanResult

        long_signature = "A" * 100
        self._set_scanner(ScanResult(clean=False, signature=long_signature))
        try:
            file_id = self._upload(b"%PDF-1.4 long " + uuid.uuid4().bytes)
            response = self._commit(file_id)
        finally:
            self._restore_scanner()

        assert response.status_code == 422, response.text
        row = run(self.client, self._row, file_id)
        assert row.scan_result == long_signature
