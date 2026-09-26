"""Загрузка `.json` (выгрузка «Данные оплат» для импорта оплат).

Расширение `json` добавлено в общий список разрешённых, а при `commit` у такого файла
проверяется начало содержимого: у JSON нет сигнатуры, но корень файла с данными — массив или
объект. `.svg` по-прежнему запрещён независимо от списка.

Чистые проверки (`_check_magic_bytes`, значения по умолчанию, `.env.example`) идут без БД;
сквозные — через настоящее API на PostgreSQL (`TEST_DATABASE_URL`) с подменённым хранилищем:
SeaweedFS в тестах нет, `inspect_object` подменяется так, чтобы снимать ровно тот же префикс, что
и настоящий.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.core.storage import _MAGIC_BYTES_LEN, ObjectInspection
from app.modules.files.service import _check_magic_bytes, _extension
from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

_DEFAULT_EXTENSIONS: str = Settings.model_fields["allowed_file_extensions"].default
_BOM = b"\xef\xbb\xbf"


class TestJsonContentCheck:
    def test_array_and_object_roots_are_accepted(self) -> None:
        assert _check_magic_bytes("json", b'[{"a": 1}]')
        assert _check_magic_bytes("json", b'{"orders": []}')

    def test_bom_and_whitespace_before_the_root_are_allowed(self) -> None:
        assert _check_magic_bytes("json", _BOM + b'[{"a": 1}]')
        assert _check_magic_bytes("json", b'  \r\n\t[{"a": 1}]')
        assert _check_magic_bytes("json", _BOM + b'\r\n  {"a": 1}')

    def test_prefix_of_only_bom_and_whitespace_is_accepted(self) -> None:
        # Снимается лишь начало файла (16 байт): за длинным отступом значащий символ может быть
        # дальше, судить по такому префиксу не по чему.
        assert _check_magic_bytes("json", b" " * _MAGIC_BYTES_LEN)
        assert _check_magic_bytes("json", _BOM)
        assert _check_magic_bytes("json", _BOM + b"\r\n" * 6)

    def test_empty_object_is_not_json(self) -> None:
        assert not _check_magic_bytes("json", b"")

    @pytest.mark.parametrize(
        "content",
        [
            b"\x89PNG\r\n\x1a\n\x00\x00",  # картинка под видом json
            b"%PDF-1.7\n",
            b"PK\x03\x04\x14\x00",  # zip / xlsx
            b"<html><script>alert(1)</script>",
            b"<?xml version='1.0'?><a/>",
            b"MZ\x90\x00\x03\x00\x00\x00",  # исполняемый файл
            b'"just a string"',
            b"12345",
            b"null",
            b"\xff\xfe[\x00{\x00",  # UTF-16: импорт читает только UTF-8
            b"\x00[",
        ],
    )
    def test_anything_else_is_rejected(self, content: bytes) -> None:
        assert not _check_magic_bytes("json", content)

    def test_bom_followed_by_a_non_json_byte_is_rejected(self) -> None:
        assert not _check_magic_bytes("json", _BOM + b"<html>")

    def test_other_formats_are_checked_as_before(self) -> None:
        assert _check_magic_bytes("pdf", b"%PDF-1.7")
        assert not _check_magic_bytes("pdf", b'[{"a": 1}]')
        # csv и xml сигнатуры не имеют — как и раньше, принимаются любым началом.
        assert _check_magic_bytes("csv", b"\x00anything")
        assert _check_magic_bytes("xml", b"anything")

    def test_extension_is_case_insensitive(self) -> None:
        assert _extension("Данные оплат.JSON") == "json"


class TestAllowedExtensionsDefault:
    @staticmethod
    def _allowed(value: str) -> frozenset[str]:
        # Свойство `Settings.allowed_extensions` без построения всех настроек приложения.
        return Settings.allowed_extensions.fget(  # type: ignore[attr-defined,no-any-return]
            SimpleNamespace(allowed_file_extensions=value)
        )

    def test_json_is_allowed_by_default_and_svg_is_not(self) -> None:
        allowed = self._allowed(_DEFAULT_EXTENSIONS)
        assert "json" in allowed
        assert "svg" not in allowed
        # Прежние форматы никуда не делись.
        assert {"xlsx", "xls", "csv", "xml", "pdf", "docx"} <= allowed

    def test_env_example_lists_json_and_not_svg(self) -> None:
        example = Path(__file__).resolve().parents[1] / ".env.example"
        if not example.exists():
            pytest.skip("нет .env.example рядом с тестами")
        line = next(
            row
            for row in example.read_text(encoding="utf-8").splitlines()
            if row.startswith("ALLOWED_FILE_EXTENSIONS=")
        )
        allowed = self._allowed(line.split("=", 1)[1])
        assert "json" in allowed
        assert "svg" not in allowed
        assert allowed == self._allowed(_DEFAULT_EXTENSIONS)


@pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)
class TestJsonUploadOverHttp:
    """`upload-intent` → (клиент грузит объект в хранилище) → `commit`."""

    @pytest.fixture(autouse=True)
    def _storage(self, client, monkeypatch: pytest.MonkeyPatch):
        from app.core.config import get_settings
        from app.modules.files import service as files_service

        # Локальный `.env` может нести устаревший список расширений — проверяем поставляемый.
        monkeypatch.setattr(get_settings(), "allowed_file_extensions", _DEFAULT_EXTENSIONS)
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
                # Как настоящий `inspect_object`: только начало объекта.
                magic_bytes=content[:_MAGIC_BYTES_LEN],
            )

        monkeypatch.setattr(files_service, "ensure_bucket", fake_ensure_bucket)
        monkeypatch.setattr(files_service, "generate_presigned_put", fake_presigned_put)
        monkeypatch.setattr(files_service, "delete_object", fake_delete_object)
        monkeypatch.setattr(files_service, "inspect_object", fake_inspect)
        self.client = client
        client.headers["X-CSRF-Token"] = authenticate(client, run(client, _make_user, "KAM"))

    def _intent(self, filename: str, size: int = 100):
        return self.client.post(
            "/api/files/upload-intent",
            json={"filename": filename, "size_bytes": size, "mime_type": "application/json"},
        )

    async def _storage_key(self, file_id: str) -> str:
        from app.core.db import session_scope
        from app.modules.files.models import File

        async with session_scope() as session:
            file = await session.get(File, uuid.UUID(file_id))
            assert file is not None
            return file.storage_key

    def _upload_and_commit(self, filename: str, content: bytes):
        response = self._intent(filename, len(content))
        assert response.status_code == 201, response.text
        file_id = response.json()["file_id"]
        self.stored[run(self.client, self._storage_key, file_id)] = content
        return file_id, self.client.post(f"/api/files/{file_id}/commit", json={})

    def test_json_upload_intent_is_accepted(self) -> None:
        response = self._intent("Данные оплат.json")

        assert response.status_code == 201, response.text
        assert response.json()["upload_url"] == "https://storage.invalid/put"

    def test_uppercase_extension_is_accepted_too(self) -> None:
        assert self._intent("PAYMENTS.JSON").status_code == 201

    def test_svg_stays_forbidden_even_when_listed_as_allowed(self, monkeypatch) -> None:
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "allowed_file_extensions", f"{_DEFAULT_EXTENSIONS},svg")

        response = self._intent("logo.svg")

        assert response.status_code == 415, response.text
        assert response.json()["code"] == "CRM-1401"

    def test_unknown_extension_is_still_refused(self) -> None:
        assert self._intent("payload.exe").status_code == 415

    def test_json_file_becomes_ready_on_commit(self) -> None:
        content = f'[null, {{"Номер заявки": "ORD-{uuid.uuid4().hex}"}}]'.encode()

        file_id, response = self._upload_and_commit("Данные оплат.json", content)

        assert response.status_code == 200, response.text
        assert response.json()["id"] == file_id
        assert response.json()["status"] == "ready"

    def test_json_with_bom_and_a_long_indent_becomes_ready(self) -> None:
        # Отступ длиннее снимаемого префикса: значащего символа в нём нет вовсе.
        content = _BOM + b" " * 64 + f'{{"id": "{uuid.uuid4().hex}"}}'.encode()

        _file_id, response = self._upload_and_commit("indented.json", content)

        assert response.status_code == 200, response.text
        assert response.json()["status"] == "ready"

    def test_png_renamed_to_json_is_refused_on_commit(self) -> None:
        content = b"\x89PNG\r\n\x1a\n" + uuid.uuid4().bytes

        _file_id, response = self._upload_and_commit("payments.json", content)

        assert response.status_code == 422, response.text
        assert response.json()["code"] == "CRM-1403"

    def test_html_renamed_to_json_is_refused_on_commit(self) -> None:
        content = f"<html><body>{uuid.uuid4().hex}</body></html>".encode()

        _file_id, response = self._upload_and_commit("payments.json", content)

        assert response.status_code == 422, response.text
        assert response.json()["code"] == "CRM-1403"
