"""Тесты модуля ПЭП (спринт 6, dop.md §10, spec.txt §5.12).

Как и в остальных тестах этого репозитория (см. `tests/test_registry.py`),
здесь нет поднятых PostgreSQL/Redis: покрываются чистые функции — рендеринг
PDF (`signing.rendering`, реально запускает Jinja2/xhtml2pdf/pypdf, но без
БД и сети), хэш-цепочка и HMAC-метка (`signing.service`), схемы-валидаторы
(`signing.schemas`) и права (`core.permissions`). Реальный доступ к
`signature_documents`/`signature_requests`/`signatures` (создание документа,
OTP-цикл, seal, триггер неизменяемости) проверен вживую против настоящего
Postgres в этой же сессии (миграция применена, insert/update/delete на
`signatures` подтверждены построчно) — того же типа верификация, что и для
остального DB-слоя репозитория, просто не как pytest-тест.
"""

from __future__ import annotations

import hashlib
import io
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bcrypt
import pytest
from pydantic import ValidationError
from pypdf import PdfReader

from app.core.errors import AppError, ErrorCode
from app.core.permissions import Permission, has_permission
from app.modules.identity.models import Role
from app.modules.signing import trusted_time
from app.modules.signing.rendering import (
    RenderError,
    apply_signature_stamp,
    html_to_pdf,
    render_protocol_pdf,
    render_signature_document,
    render_template_html,
)
from app.modules.signing.schemas import SignatureDocumentCreateRequest, SignerSpec
from app.modules.signing.service import GENESIS_HASH, compute_chain_hash, compute_signature_value
from app.modules.signing.sms_gateway import send_sms


class TestTemplateRendering:
    def test_escapes_untrusted_context_values(self) -> None:
        html = render_template_html(
            "<p>{{ org }}</p>", {"org": "ООО «Ромашка» <script>alert(1)</script>"}
        )
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_html_to_pdf_produces_valid_pdf_with_cyrillic_text(self) -> None:
        html = render_template_html("<h1>{{ text }}</h1>", {"text": "Согласовано"})
        pdf_bytes = html_to_pdf(html)
        assert pdf_bytes.startswith(b"%PDF")
        assert len(PdfReader(io.BytesIO(pdf_bytes)).pages) == 1

    def test_render_signature_document_end_to_end(self) -> None:
        pdf_bytes = render_signature_document(
            "<p>КП {{ deal_number }} на {{ amount }} {{ currency }}</p>",
            {"deal_number": "B2B-000123", "amount": "150000.00", "currency": "RUB"},
        )
        assert pdf_bytes.startswith(b"%PDF")

    def test_external_resources_are_rejected(self) -> None:
        with pytest.raises(RenderError):
            html_to_pdf('<img src="http://evil.example/x.png">')


class TestStampAndProtocol:
    def _base_pdf(self) -> bytes:
        return render_signature_document("<p>{{ t }}</p>", {"t": "Документ"})

    def test_stamp_preserves_page_count_and_changes_content(self) -> None:
        original = self._base_pdf()
        stamped = apply_signature_stamp(
            original,
            lines=["Документ подписан простой электронной подписью", "Иванов И. И."],
            verify_url="https://example.local/public/verify/00000000-0000-0000-0000-000000000000",
        )
        assert len(PdfReader(io.BytesIO(original)).pages) == len(
            PdfReader(io.BytesIO(stamped)).pages
        )
        assert hashlib.sha256(stamped).hexdigest() != hashlib.sha256(original).hexdigest()
        assert stamped.startswith(b"%PDF")

    def test_stamp_rejects_empty_document(self) -> None:
        with pytest.raises(RenderError):
            apply_signature_stamp(
                b"", lines=["x"], verify_url="https://example.local/public/verify/x"
            )

    def test_protocol_pdf_is_valid(self) -> None:
        pdf_bytes = render_protocol_pdf(
            document_title="КП B2B-000123",
            content_hash="a" * 64,
            entries=[
                {
                    "signer_display": "Иванов И. И.",
                    "method": "pep_otp",
                    "signed_at": "2026-09-18T12:00:00+03:00",
                    "ip": "10.0.0.1",
                    "signature_id": "00000000-0000-0000-0000-000000000001",
                }
            ],
            verify_url="https://example.local/public/verify/00000000-0000-0000-0000-000000000001",
        )
        assert pdf_bytes.startswith(b"%PDF")


class TestHashChainAndHmac:
    def test_chain_hash_uses_genesis_when_no_prev(self) -> None:
        h1 = compute_chain_hash(
            prev_hash=None, signature_value="v", content_hash="c", request_id="r", signed_at_iso="t"
        )
        h2 = compute_chain_hash(
            prev_hash=GENESIS_HASH,
            signature_value="v",
            content_hash="c",
            request_id="r",
            signed_at_iso="t",
        )
        assert h1 == h2

    def test_chain_hash_changes_with_prev_hash(self) -> None:
        base = {
            "signature_value": "v",
            "content_hash": "c",
            "request_id": "r",
            "signed_at_iso": "t",
        }
        assert compute_chain_hash(prev_hash="a" * 64, **base) != compute_chain_hash(
            prev_hash="b" * 64, **base
        )

    def test_signature_value_is_deterministic(self) -> None:
        kwargs = {
            "secret": "s3cr3t",
            "content_hash": "c",
            "signer_id": "u1",
            "signed_at_iso": "t",
            "nonce": "n",
        }
        assert compute_signature_value(**kwargs) == compute_signature_value(**kwargs)

    def test_signature_value_changes_with_nonce(self) -> None:
        base = {"secret": "s3cr3t", "content_hash": "c", "signer_id": "u1", "signed_at_iso": "t"}
        value1 = compute_signature_value(**base, nonce="n1")
        value2 = compute_signature_value(**base, nonce="n2")
        assert value1 != value2

    def test_signature_value_requires_correct_secret(self) -> None:
        base = {"content_hash": "c", "signer_id": "u1", "signed_at_iso": "t", "nonce": "n"}
        assert compute_signature_value(secret="right", **base) != compute_signature_value(
            secret="wrong", **base
        )


class TestOtpBcryptRoundtrip:
    """dop.md §10.4 п.11: `bcrypt(code)` + отдельно хранимая соль (см.
    docstring `SignatureOtpCode` в `app/modules/signing/models.py`)."""

    def test_correct_code_verifies(self) -> None:
        code = "123456"
        salt = bcrypt.gensalt()
        code_hash = bcrypt.hashpw(code.encode(), salt)
        assert bcrypt.checkpw(code.encode(), code_hash)

    def test_wrong_code_does_not_verify(self) -> None:
        salt = bcrypt.gensalt()
        code_hash = bcrypt.hashpw(b"123456", salt)
        assert not bcrypt.checkpw(b"654321", code_hash)


class TestSignerSpecValidation:
    def test_exactly_one_field_required(self) -> None:
        with pytest.raises(ValidationError):
            SignerSpec()

    def test_more_than_one_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SignerSpec(role="HEAD", contact_role="decision_maker")

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"role": "HEAD"},
            {"contact_role": "decision_maker"},
            {"user_id": "00000000-0000-0000-0000-000000000001"},
            {"contact_id": "00000000-0000-0000-0000-000000000001"},
        ],
    )
    def test_single_field_accepted(self, kwargs: dict) -> None:
        SignerSpec(**kwargs)


class TestSignatureDocumentCreateValidation:
    _base = {
        "doc_type": "kp",
        "title": "Test",
        "entity_type": "deal",
        "entity_id": "00000000-0000-0000-0000-000000000001",
        "signers": [{"role": "HEAD"}],
    }

    def test_requires_exactly_one_source(self) -> None:
        with pytest.raises(ValidationError):
            SignatureDocumentCreateRequest(**self._base)

    def test_both_sources_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SignatureDocumentCreateRequest(
                **self._base,
                template_code="kp_approval",
                file_id="00000000-0000-0000-0000-000000000002",
            )

    def test_template_code_alone_accepted(self) -> None:
        SignatureDocumentCreateRequest(**self._base, template_code="kp_approval")

    def test_file_id_alone_accepted(self) -> None:
        SignatureDocumentCreateRequest(**self._base, file_id="00000000-0000-0000-0000-000000000002")


class TestPermissionMatrix:
    """dop.md §13: новые строки матрицы прав."""

    @pytest.mark.parametrize("role", [Role.KAM, Role.HEAD, Role.ADMIN])
    def test_signature_create_granted(self, role: Role) -> None:
        assert has_permission(role.value, Permission.SIGNATURE_CREATE)

    def test_signature_create_denied_for_auditor(self) -> None:
        assert not has_permission(Role.AUDITOR.value, Permission.SIGNATURE_CREATE)

    @pytest.mark.parametrize("role", [Role.HEAD, Role.ADMIN])
    def test_signature_void_granted(self, role: Role) -> None:
        assert has_permission(role.value, Permission.SIGNATURE_VOID)

    def test_signature_void_denied_for_kam(self) -> None:
        assert not has_permission(Role.KAM.value, Permission.SIGNATURE_VOID)

    def test_edm_read_granted_to_auditor_only_among_non_admin(self) -> None:
        assert has_permission(Role.AUDITOR.value, Permission.EDM_READ)
        assert not has_permission(Role.KAM.value, Permission.EDM_READ)
        assert not has_permission(Role.HEAD.value, Permission.EDM_READ)

    def test_edm_admin_reserved_for_admin(self) -> None:
        assert has_permission(Role.ADMIN.value, Permission.EDM_ADMIN)
        assert not has_permission(Role.HEAD.value, Permission.EDM_ADMIN)
        assert not has_permission(Role.KAM.value, Permission.EDM_ADMIN)


class TestSmsGatewayClient:
    """dop.md §13: `sms-gateway-mock`. Тот же приём, что
    `TestBitrixSecretRedaction` в `tests/test_integration.py` — настоящий
    локальный HTTP-сервер и порт 9 (discard) для мгновенного отказа
    соединения, а не мок-библиотека (в репозитории такой пока нет)."""

    @staticmethod
    def _serve(status: int, body: bytes) -> ThreadingHTTPServer:
        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — имя метода фиксировано http.server
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                pass  # не шуметь в тестовом выводе

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    async def test_successful_send_returns_gateway_message_id(self) -> None:
        server = self._serve(200, b'{"id": "msg-123", "status": "queued"}')
        try:
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            message_id = await send_sms(to="+79991234567", message="123456", base_url=base_url)
        finally:
            server.shutdown()
            server.server_close()
        assert message_id == "msg-123"

    async def test_connection_failure_degrades_to_none(self) -> None:
        # Порт 9 (discard) в этом окружении — надёжный «мгновенный отказ
        # соединения» (см. tests/test_integration.py, tests/test_api_smoke.py).
        message_id = await send_sms(
            to="+79991234567", message="123456", base_url="http://127.0.0.1:9"
        )
        assert message_id is None

    async def test_gateway_error_response_degrades_to_none(self) -> None:
        server = self._serve(500, b'{"error": "internal"}')
        try:
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            message_id = await send_sms(to="+79991234567", message="123456", base_url=base_url)
        finally:
            server.shutdown()
            server.server_close()
        assert message_id is None


class TestTrustedTime:
    """dop.md §10.8/§13: NTP недоступен → честная деградация до
    `system_clock` (не блокирует подписание); NTP ответил, но измеренный
    рассинхрон превышает порог → блокирует. Мокается только `_query_
    offset_seconds` (граница с `ntplib`/сетью) — решение о деградации/блоке
    вокруг него не мок, тот же принцип, что остальной файл применяет к
    чистым функциям."""

    async def test_ntp_unreachable_degrades_to_system_clock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(host: str, port: int, timeout: float) -> float:
            raise OSError("network unreachable")

        monkeypatch.setattr(trusted_time, "_query_offset_seconds", _boom)
        _, source, drift_ms = await trusted_time.get_trusted_time()
        assert source == trusted_time.FALLBACK_SOURCE
        assert drift_ms is None

    async def test_small_drift_is_reported_and_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(trusted_time, "_query_offset_seconds", lambda *a, **kw: 0.5)
        _, source, drift_ms = await trusted_time.get_trusted_time()
        assert source == "ntp://ntp"
        assert drift_ms == 500

    async def test_excessive_drift_blocks_signing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(trusted_time, "_query_offset_seconds", lambda *a, **kw: 10.0)
        with pytest.raises(AppError) as exc_info:
            await trusted_time.get_trusted_time()
        assert exc_info.value.code == ErrorCode.SIGNATURE_TIME_UNTRUSTED

    async def test_negative_drift_also_blocks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(trusted_time, "_query_offset_seconds", lambda *a, **kw: -8.0)
        with pytest.raises(AppError) as exc_info:
            await trusted_time.get_trusted_time()
        assert exc_info.value.code == ErrorCode.SIGNATURE_TIME_UNTRUSTED
