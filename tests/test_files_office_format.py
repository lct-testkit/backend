"""B2-9: проверка doc/xls, docx/xlsx по содержимому, а не только по расширению и magic bytes
(«расширение врёт» — раздел 9), и проверенный MIME для rar.

Чистые проверки `office_kind` — без БД, на фикстурах `tests/fixtures/files/` и на архивах,
собранных прямо в тесте (`zipfile`). Сквозные — через `commit`, с подменённым хранилищем
(тот же приём, что `tests/test_files_hardening.py::TestUploadOverHttp`), но здесь `fake_inspect`
отдаёт и `content`, как настоящий `inspect_object` при `keep_up_to`.
"""

from __future__ import annotations

import hashlib
import io
import uuid
import zipfile
from pathlib import Path

import pytest

from app.core.storage import _MAGIC_BYTES_LEN, ObjectInspection
from app.modules.files.service import office_kind
from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

needs_db = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

_FIXTURES = Path(__file__).parent / "fixtures" / "files"


def _zip_with(*names: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in names:
            archive.writestr(name, b"<xml/>")
    return buffer.getvalue()


class TestOfficeKindPureFunction:
    """`office_kind` сам по себе: никакого I/O, только разбор контейнера."""

    def test_the_xls_fixture_is_recognised_as_xls(self) -> None:
        content = (_FIXTURES / "sample.xls").read_bytes()
        assert office_kind(content) == "xls"

    def test_the_doc_fixture_is_recognised_as_doc(self) -> None:
        content = (_FIXTURES / "sample.doc").read_bytes()
        assert office_kind(content) == "doc"

    def test_a_docx_zip_is_recognised_by_its_document_part(self) -> None:
        assert office_kind(_zip_with("word/document.xml", "[Content_Types].xml")) == "docx"

    def test_an_xlsx_zip_is_recognised_by_its_workbook_part(self) -> None:
        assert office_kind(_zip_with("xl/workbook.xml", "[Content_Types].xml")) == "xlsx"

    def test_a_plain_zip_without_office_parts_does_not_match_any_kind(self) -> None:
        assert office_kind(_zip_with("readme.txt")) is None

    def test_a_truncated_ole_file_does_not_crash_and_returns_none(self) -> None:
        content = (_FIXTURES / "sample.doc").read_bytes()[:40]  # обрезан посреди заголовка
        assert office_kind(content) is None

    def test_an_empty_file_does_not_match_any_kind(self) -> None:
        assert office_kind(b"") is None

    def test_garbage_bytes_do_not_match_any_kind(self) -> None:
        assert office_kind(b"not a container at all, just text") is None


@needs_db
class TestDeepCheckOnCommit:
    @pytest.fixture(autouse=True)
    def _storage(self, client, monkeypatch: pytest.MonkeyPatch):
        from app.modules.files import service as files_service

        self.stored: dict[str, bytes] = {}
        self.monkeypatch = monkeypatch

        async def fake_ensure_bucket(bucket: str) -> None:
            return None

        async def fake_presigned_put(**_kwargs: object) -> str:
            return "https://storage.invalid/put"

        async def fake_delete_object(*, bucket: str, key: str) -> None:
            return None

        async def fake_inspect(*, bucket: str, key: str, keep_up_to: int = 0) -> ObjectInspection:
            content = self.stored[key]
            return ObjectInspection(
                exists=True,
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                magic_bytes=content[:_MAGIC_BYTES_LEN],
                content=content if keep_up_to and len(content) <= keep_up_to else None,
            )

        monkeypatch.setattr(files_service, "ensure_bucket", fake_ensure_bucket)
        monkeypatch.setattr(files_service, "generate_presigned_put", fake_presigned_put)
        monkeypatch.setattr(files_service, "delete_object", fake_delete_object)
        monkeypatch.setattr(files_service, "inspect_object", fake_inspect)
        self.client = client
        user = run(client, _make_user, "KAM")
        client.headers["X-CSRF-Token"] = authenticate(client, user)

    def _upload_and_commit(
        self, filename: str, content: bytes, *, mime: str = "application/octet-stream"
    ):
        intent = self.client.post(
            "/api/files/upload-intent",
            json={"filename": filename, "size_bytes": len(content), "mime_type": mime},
        )
        assert intent.status_code == 201, intent.text
        file_id = intent.json()["file_id"]

        async def _storage_key() -> str:
            from app.core.db import session_scope
            from app.modules.files.models import File

            async with session_scope() as session:
                return (await session.get(File, uuid.UUID(file_id))).storage_key

        self.stored[run(self.client, _storage_key)] = content
        return file_id, self.client.post(f"/api/files/{file_id}/commit", json={})

    async def _row(self, file_id: str):
        from app.core.db import session_scope
        from app.modules.files.models import File

        async with session_scope() as session:
            file = await session.get(File, uuid.UUID(file_id))
            session.expunge(file)
            return file

    def test_xls_under_a_doc_name_is_rejected_as_a_mismatch(self) -> None:
        content = (_FIXTURES / "sample.xls").read_bytes()
        file_id, response = self._upload_and_commit("act.doc", content)

        assert response.status_code == 422, response.text
        assert response.json()["code"] == "CRM-1403"
        row = run(self.client, self._row, file_id)
        assert row.status == "infected"
        assert row.scan_result == "format_mismatch:xls"

    def test_doc_under_an_xls_name_is_rejected_as_a_mismatch(self) -> None:
        content = (_FIXTURES / "sample.doc").read_bytes()
        file_id, response = self._upload_and_commit("act.xls", content)

        assert response.status_code == 422, response.text
        row = run(self.client, self._row, file_id)
        assert row.scan_result == "format_mismatch:doc"

    def test_a_plain_zip_under_a_docx_name_is_rejected(self) -> None:
        content = _zip_with("readme.txt")
        file_id, response = self._upload_and_commit("report.docx", content)

        assert response.status_code == 422, response.text
        row = run(self.client, self._row, file_id)
        assert row.scan_result == "format_mismatch:unknown"

    def test_a_real_docx_under_the_correct_name_is_accepted(self) -> None:
        content = _zip_with("word/document.xml", "[Content_Types].xml")
        file_id, response = self._upload_and_commit("report.docx", content)

        assert response.status_code == 200, response.text
        row = run(self.client, self._row, file_id)
        assert row.status == "ready"
        assert (
            row.mime_type
            == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        )

    def test_a_real_xls_under_the_correct_name_is_accepted(self) -> None:
        content = (_FIXTURES / "sample.xls").read_bytes()
        file_id, response = self._upload_and_commit("act.xls", content)

        assert response.status_code == 200, response.text
        row = run(self.client, self._row, file_id)
        assert row.status == "ready"
        assert row.mime_type == "application/vnd.ms-excel"

    def test_a_file_over_the_deep_check_limit_is_accepted_by_signature_only(self) -> None:
        from app.core.config import get_settings

        settings = get_settings()
        self.monkeypatch.setattr(settings, "files_deep_check_max_bytes", 16)
        # Подделка под xls, которая превышает лимит глубокой проверки: сигнатура верна
        # (тот же префикс, что настоящий OLE2), глубокая проверка пропущена.
        content = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"x" * 64
        file_id, response = self._upload_and_commit("act.xls", content)

        assert response.status_code == 200, response.text
        row = run(self.client, self._row, file_id)
        assert row.status == "ready"

    def test_the_deep_check_can_be_disabled_by_zero(self) -> None:
        from app.core.config import get_settings

        settings = get_settings()
        self.monkeypatch.setattr(settings, "files_deep_check_max_bytes", 0)
        content = (_FIXTURES / "sample.xls").read_bytes()  # настоящий xls под чужим именем
        file_id, response = self._upload_and_commit("act.doc", content)

        assert response.status_code == 200, response.text  # глубокая проверка выключена

    def test_rar_gets_a_verified_mime(self) -> None:
        content = b"Rar!\x1a\x07\x00" + b"x" * 32  # RAR4
        file_id, response = self._upload_and_commit("archive.rar", content)

        assert response.status_code == 200, response.text
        row = run(self.client, self._row, file_id)
        assert row.mime_type == "application/vnd.rar"

    def test_rar5_signature_also_gets_a_verified_mime(self) -> None:
        content = b"Rar!\x1a\x07\x01\x00" + b"x" * 32  # RAR5
        file_id, response = self._upload_and_commit("archive.rar", content)

        assert response.status_code == 200, response.text
        row = run(self.client, self._row, file_id)
        assert row.mime_type == "application/vnd.rar"

    # --- регрессия: формат вне office-набора не проходит глубокую проверку -------------

    def test_a_regular_pdf_is_unaffected_by_the_deep_check(self) -> None:
        content = b"%PDF-1.4 " + b"x" * 64
        file_id, response = self._upload_and_commit("contract.pdf", content)

        assert response.status_code == 200, response.text


@needs_db
class TestDealScopeWiring:
    """B2-9 шаг 6: `purpose=deal_attachment` включает больший лимит размера."""

    @pytest.fixture(autouse=True)
    def _auth(self, client, monkeypatch: pytest.MonkeyPatch):
        from app.modules.files import service as files_service

        async def fake_ensure_bucket(bucket: str) -> None:
            return None

        async def fake_presigned_put(**_kwargs: object) -> str:
            return "https://storage.invalid/put"

        monkeypatch.setattr(files_service, "ensure_bucket", fake_ensure_bucket)
        monkeypatch.setattr(files_service, "generate_presigned_put", fake_presigned_put)
        self.client = client
        user = run(client, _make_user, "KAM")
        client.headers["X-CSRF-Token"] = authenticate(client, user)

    def test_deal_attachment_purpose_uses_the_larger_limit(self) -> None:
        from app.core.config import get_settings

        settings = get_settings()
        over_general_limit = settings.files_max_size_bytes + 1024

        general = self.client.post(
            "/api/files/upload-intent",
            json={
                "filename": "big.pdf",
                "size_bytes": over_general_limit,
                "mime_type": "application/pdf",
            },
        )
        assert general.status_code == 413, general.text

        deal_scoped = self.client.post(
            "/api/files/upload-intent",
            json={
                "filename": "big.pdf",
                "size_bytes": over_general_limit,
                "mime_type": "application/pdf",
                "purpose": "deal_attachment",
            },
        )
        assert deal_scoped.status_code == 201, deal_scoped.text
