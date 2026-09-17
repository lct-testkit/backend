"""Тесты файлов и вложений (спринт 4, раздел 9).

Проверяется то, что не требует поднятого SeaweedFS: разбор расширения и
сверка magic bytes (раздел 9: «расширение врёт») — именно здесь легко
случайно ослабить проверку и пропустить подменённый файл.
"""

from __future__ import annotations

from app.modules.files.service import NullAntivirusScanner, _check_magic_bytes, _extension


class TestExtensionParsing:
    def test_simple_extension(self) -> None:
        assert _extension("contract.PDF") == "pdf"

    def test_no_extension(self) -> None:
        assert _extension("no-extension") == ""

    def test_double_extension_takes_last_part(self) -> None:
        assert _extension("archive.tar.gz") == "gz"


class TestMagicBytes:
    def test_pdf_signature_matches(self) -> None:
        assert _check_magic_bytes("pdf", b"%PDF-1.7\n...")

    def test_pdf_signature_rejects_mismatched_bytes(self) -> None:
        # .pdf на диске, но по факту PNG — классическая подмена расширения,
        # которую и должен ловить commit() до перевода файла в ready.
        assert not _check_magic_bytes("pdf", b"\x89PNG\r\n\x1a\n")

    def test_png_signature_matches(self) -> None:
        assert _check_magic_bytes("png", b"\x89PNG\r\n\x1a\n\x00\x00")

    def test_docx_accepts_zip_signature(self) -> None:
        # docx/xlsx — zip-контейнеры, сигнатура совпадает с обычным zip.
        assert _check_magic_bytes("docx", b"PK\x03\x04\x14\x00")

    def test_ole2_signature_matches_doc_and_xls(self) -> None:
        ole2 = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
        assert _check_magic_bytes("doc", ole2)
        assert _check_magic_bytes("xls", ole2)

    def test_unknown_extension_has_no_signature_to_check(self) -> None:
        # Уже отсечено allowlist'ом раньше по пайплайну — здесь не должно падать.
        assert _check_magic_bytes("unknown", b"anything")


class TestNullAntivirusScanner:
    async def test_stub_scanner_reports_clean(self) -> None:
        result = await NullAntivirusScanner().scan(bucket="files", key="some/key")
        assert result.clean is True
