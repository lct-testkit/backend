"""Защита подписи от гонок и «оживления» документов (отчёт внешнего тестирования, блок ПЭП).

`seal()` в остальных тестах ПЭП не запускается: S3 и NTP недоступны. Здесь хранилище, время и
сборка PDF подменены заглушками, а сама подпись (`SignatureRequestService.sign`) идёт по-настоящему:
замки строк, счётчик кода, цепочка хэшей, статус сделки — на настоящей PostgreSQL.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import functools
import uuid
from types import SimpleNamespace

import bcrypt
import pytest

from tests.conftest import TEST_DATABASE_URL, _make_user, run
from tests.signing_helpers import _build, _login

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

CODE = "123456"
IP = "198.51.100.9"


@pytest.fixture
def stubbed_storage(monkeypatch):
    """Хранилище, NTP и PDF заменены: проверяется логика подписи, а не инфраструктура."""
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.core.storage import ObjectInspection
    from app.modules.files.models import File
    from app.modules.signing import service as svc

    async def inspect(*, bucket: str, key: str) -> ObjectInspection:
        async with session_scope() as session:
            sha = await session.scalar(
                select(File.sha256).where(File.bucket == bucket, File.storage_key == key)
            )
        return ObjectInspection(exists=True, size_bytes=1, sha256=sha or "", magic_bytes=b"%PDF")

    async def trusted_time():
        return dt.datetime.now(dt.UTC), "test_clock", None

    async def download(*, bucket: str, key: str) -> bytes:
        return b"%PDF-1.4 stub"

    async def nothing(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(svc, "inspect_object", inspect)
    monkeypatch.setattr(svc, "get_trusted_time", trusted_time)
    monkeypatch.setattr(svc, "download_object_bytes", download)
    monkeypatch.setattr(svc, "ensure_bucket", nothing)
    monkeypatch.setattr(svc, "upload_object_bytes", nothing)
    monkeypatch.setattr(svc, "apply_signature_stamp", lambda pdf, **kw: pdf + b" stamped")
    monkeypatch.setattr(svc, "render_protocol_pdf", lambda **kw: b"%PDF-1.4 protocol")


async def _issue_otp(request_id: uuid.UUID, code: str = CODE) -> None:
    from app.core.db import session_scope
    from app.modules.signing.models import SignatureOtpCode

    salt = bcrypt.gensalt()
    async with session_scope() as session:
        session.add(
            SignatureOtpCode(
                request_id=request_id,
                code_hash=bcrypt.hashpw(code.encode(), salt).decode(),
                salt=salt.decode(),
                channel="email",
                sent_to_masked="p***@example.ru",
                max_attempts=3,
                expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5),
            )
        )


async def _try_sign(request_id: uuid.UUID, code: str = CODE):
    """`Signature.id` при успехе, `AppError` — при отказе (транзакция откатывается как в API)."""
    from app.core.db import session_scope
    from app.core.errors import AppError
    from app.modules.signing.models import SignatureRequest
    from app.modules.signing.service import SignatureRequestService

    try:
        async with session_scope() as session:
            request = await session.get(SignatureRequest, request_id)
            signature = await SignatureRequestService(session).sign(
                request, otp_code=code, ip=IP, user_agent="pytest"
            )
            return signature.id
    except AppError as exc:
        return exc


async def _gather_signs(*calls):
    return await asyncio.gather(*(_try_sign(*call) for call in calls))


async def _signatures_of(document_id: uuid.UUID) -> list:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.signing.models import Signature

    async with session_scope() as session:
        rows = await session.execute(select(Signature).where(Signature.document_id == document_id))
        found = list(rows.scalars())
        for row in found:
            session.expunge(row)
        return found


async def _get(model, object_id):
    from app.core.db import session_scope

    async with session_scope() as session:
        obj = await session.get(model, object_id)
        session.expunge(obj)
        return obj


def _error_code(result) -> str:
    return result.code.value


class TestConcurrentSign:
    def test_two_simultaneous_signs_make_one_signature(self, client, stubbed_storage) -> None:
        from app.modules.signing.models import SignatureDocument

        signer = run(client, _make_user, "KAM")
        built = _build(client, requests=[{"user": signer, "status": "sent"}])
        request_id = built.request_ids[0]
        run(client, _issue_otp, request_id)

        results = run(client, _gather_signs, (request_id,), (request_id,))

        won = [r for r in results if isinstance(r, uuid.UUID)]
        lost = [r for r in results if not isinstance(r, uuid.UUID)]
        assert len(won) == 1 and len(lost) == 1, results
        assert _error_code(lost[0]) == "CRM-1505"
        assert len(run(client, _signatures_of, built.document_id)) == 1
        document = run(client, _get, SignatureDocument, built.document_id)
        assert document.status == "signed"

    def test_signature_carries_the_nonce_and_the_hmac_recomputes(
        self, client, stubbed_storage
    ) -> None:
        from app.core.config import get_settings
        from app.modules.signing.service import compute_chain_hash, compute_signature_value

        signer = run(client, _make_user, "KAM")
        built = _build(client, requests=[{"user": signer, "status": "sent"}])
        run(client, _issue_otp, built.request_ids[0])
        assert isinstance(run(client, _try_sign, built.request_ids[0]), uuid.UUID)

        (signature,) = run(client, _signatures_of, built.document_id)

        nonce = signature.evidence["auth"]["nonce"]
        secret = get_settings().signature_server_secret.get_secret_value()
        assert signature.signature_value == compute_signature_value(
            secret=secret,
            content_hash=signature.content_hash,
            signer_id=signature.evidence["signer"]["id"],
            signed_at_iso=signature.signed_at.isoformat(),
            nonce=nonce,
        )
        # Хэш звена пересчитывается из того, что лежит в БД (в том числе метка времени).
        assert signature.hash == compute_chain_hash(
            prev_hash=signature.prev_hash,
            signature_value=signature.signature_value,
            content_hash=signature.content_hash,
            request_id=str(signature.request_id),
            signed_at_iso=signature.signed_at.isoformat(),
        )

    def test_parallel_signatures_of_different_documents_keep_a_linear_chain(
        self, client, stubbed_storage
    ) -> None:
        signers = [run(client, _make_user, "KAM") for _ in range(2)]
        built = [_build(client, requests=[{"user": s, "status": "sent"}]) for s in signers]
        for item in built:
            run(client, _issue_otp, item.request_ids[0])

        results = run(client, _gather_signs, *[(item.request_ids[0],) for item in built])

        assert all(isinstance(r, uuid.UUID) for r in results), results
        signatures = [run(client, _signatures_of, item.document_id)[0] for item in built]
        first, second = sorted(signatures, key=lambda sig: sig.created_at)
        assert first.created_at < second.created_at
        assert second.prev_hash == first.hash

    def test_parallel_wrong_codes_all_count(self, client, stubbed_storage) -> None:
        from app.modules.signing.models import SignatureOtpCode, SignatureRequest

        signer = run(client, _make_user, "KAM")
        built = _build(client, requests=[{"user": signer, "status": "sent"}])
        request_id = built.request_ids[0]
        run(client, _issue_otp, request_id)

        results = run(client, _gather_signs, *[(request_id, "000001")] * 5)

        assert all(not isinstance(r, uuid.UUID) for r in results)
        assert sorted(_error_code(r) for r in results).count("CRM-1503") == 3

        async def _state():
            from sqlalchemy import select

            from app.core.db import session_scope

            async with session_scope() as session:
                otp = (
                    await session.execute(
                        select(SignatureOtpCode).where(SignatureOtpCode.request_id == request_id)
                    )
                ).scalar_one()
                request = await session.get(SignatureRequest, request_id)
                return otp.attempts, request.status

        # Ни один неверный ввод не потерян: все три попытки учтены, запрос заблокирован, а
        # оставшиеся два запроса получили отказ «не подписывается», а не ещё две попытки.
        assert run(client, _state) == (3, "locked")

    def test_a_broken_pdf_is_a_clear_refusal_and_keeps_the_code(
        self, client, stubbed_storage, monkeypatch
    ) -> None:
        from app.modules.signing import service as svc
        from app.modules.signing.rendering import RenderError

        def broken(pdf, **kwargs):
            raise RenderError("нет страниц")

        monkeypatch.setattr(svc, "apply_signature_stamp", broken)
        signer = run(client, _make_user, "KAM")
        built = _build(client, requests=[{"user": signer, "status": "sent"}])
        run(client, _issue_otp, built.request_ids[0])

        result = run(client, _try_sign, built.request_ids[0])

        assert _error_code(result) == "CRM-1505"
        assert run(client, _signatures_of, built.document_id) == []
        # Транзакция откатилась целиком: код не израсходован, документ не «подписан».
        monkeypatch.setattr(svc, "apply_signature_stamp", lambda pdf, **kw: pdf)
        assert isinstance(run(client, _try_sign, built.request_ids[0]), uuid.UUID)


class TestVoidedDocumentStaysVoid:
    def test_a_sibling_request_of_a_voided_document_cannot_sign(
        self, client, stubbed_storage
    ) -> None:
        from app.core.db import session_scope
        from app.modules.signing.models import SignatureDocument

        signers = [run(client, _make_user, "KAM") for _ in range(2)]
        built = _build(
            client,
            requests=[{"user": s, "status": "sent"} for s in signers],
        )
        run(client, _issue_otp, built.request_ids[1])

        async def _void_only_the_document() -> None:
            # Состояние, которое раньше создавал `void_pending_for_user`: документ аннулирован,
            # а запрос соседа остался `sent`.
            async with session_scope() as session:
                document = await session.get(SignatureDocument, built.document_id)
                document.status = "void"
                document.void_reason = "test"

        run(client, _void_only_the_document)

        result = run(client, _try_sign, built.request_ids[1])

        assert _error_code(result) == "CRM-1505"
        assert run(client, _signatures_of, built.document_id) == []
        assert run(client, _get, SignatureDocument, built.document_id).status == "void"

    def test_voiding_for_a_user_voids_the_siblings_too(self, client) -> None:
        from app.core.db import session_scope
        from app.modules.signing.models import SignatureDocument, SignatureRequest
        from app.modules.signing.service import RealSigningService

        signers = [run(client, _make_user, "KAM") for _ in range(2)]
        built = _build(client, requests=[{"user": s, "status": "sent"} for s in signers])

        async def _offboard() -> int:
            async with session_scope() as session:
                return await RealSigningService().void_pending_for_user(
                    session, signers[0].id, reason="signer_offboarded"
                )

        assert run(client, _offboard) == 1

        assert run(client, _get, SignatureDocument, built.document_id).status == "void"
        assert run(client, _get, SignatureRequest, built.request_ids[1]).status == "void"

    def test_challenge_for_a_voided_document_is_refused(self, client) -> None:
        from app.core.db import session_scope
        from app.modules.signing.models import SignatureDocument

        signer = run(client, _make_user, "KAM")
        built = _build(client, requests=[{"user": signer, "status": "sent"}])

        async def _void() -> None:
            async with session_scope() as session:
                document = await session.get(SignatureDocument, built.document_id)
                document.status = "void"
                document.void_reason = "test"

        run(client, _void)
        _login(client, signer)

        response = client.post(f"/api/signature-requests/{built.request_ids[0]}/challenge")

        assert response.status_code == 409, response.text
        assert response.json()["code"] == "CRM-1505"


class TestEdmAgreement:
    def test_revoked_agreement_blocks_challenge_and_sign(self, client, stubbed_storage) -> None:
        token = f"tok-{uuid.uuid4().hex}"
        built = _build(
            client,
            requests=[
                {"user": None, "status": "sent", "token": token, "agreement_status": "revoked"}
            ],
        )
        run(client, _issue_otp, built.request_ids[0])

        challenge = client.post(f"/public/sign/{token}/challenge")
        signed = run(client, _try_sign, built.request_ids[0])

        assert challenge.status_code == 409, challenge.text
        assert challenge.json()["code"] == "CRM-1501"
        assert _error_code(signed) == "CRM-1501"
        assert run(client, _signatures_of, built.document_id) == []

    def test_revoking_blocks_the_open_documents_and_kills_the_link(self, client) -> None:
        from app.core.db import session_scope
        from app.modules.signing.models import (
            EdmAgreement,
            SignatureDocument,
            SignatureRequest,
        )
        from app.modules.signing.service import EdmAgreementService

        token = f"tok-{uuid.uuid4().hex}"
        built = _build(client, requests=[{"user": None, "status": "sent", "token": token}])
        request = run(client, _get, SignatureRequest, built.request_ids[0])
        assert request.edm_agreement_id is not None

        async def _revoke() -> None:
            async with session_scope() as session:
                service = EdmAgreementService(session)
                agreement = await session.get(EdmAgreement, request.edm_agreement_id)
                await service.revoke(agreement, reason="test")

        run(client, _revoke)

        document = run(client, _get, SignatureDocument, built.document_id)
        assert document.status == "blocked_no_agreement"
        after = run(client, _get, SignatureRequest, built.request_ids[0])
        assert after.status == "pending"
        assert after.access_token_hash is None
        assert client.get(f"/public/sign/{token}").json()["code"] == "CRM-1504"

    def test_an_expired_agreement_is_no_better_than_a_revoked_one(self, client) -> None:
        from app.core.db import session_scope
        from app.modules.signing.models import EdmAgreement, SignatureRequest
        from app.modules.signing.service import _agreement_is_effective

        today = dt.date.today()
        built = _build(client, requests=[{"user": None, "status": "sent"}])
        request = run(client, _get, SignatureRequest, built.request_ids[0])

        async def _load() -> EdmAgreement:
            async with session_scope() as session:
                agreement = await session.get(EdmAgreement, request.edm_agreement_id)
                session.expunge(agreement)
                return agreement

        agreement = run(client, _load)

        assert _agreement_is_effective(agreement, today)
        agreement.valid_to = today - dt.timedelta(days=1)
        assert not _agreement_is_effective(agreement, today)
        agreement.valid_to = None
        agreement.valid_from = today + dt.timedelta(days=1)
        assert not _agreement_is_effective(agreement, today)
        agreement.valid_from = None
        agreement.status = "revoked"
        assert not _agreement_is_effective(agreement, today)
        assert not _agreement_is_effective(None, today)


class TestDealSignatureStatus:
    @staticmethod
    async def _deal() -> uuid.UUID:
        from app.core.db import session_scope
        from app.modules.catalog.models import Organization
        from app.modules.crm.models import Deal
        from app.modules.identity.models import User
        from app.modules.workflow.models import Workflow, WorkflowStatus

        async with session_scope() as session:
            workflow = Workflow(
                code=f"wf-{uuid.uuid4().hex[:10]}", name="Воронка", deal_type="b2b", state="draft"
            )
            session.add(workflow)
            await session.flush()
            status = WorkflowStatus(workflow_id=workflow.id, code="new", name="Новая")
            org = Organization(name=f"Вуз {uuid.uuid4().hex[:6]}", org_type="university")
            owner = User(
                keycloak_id=str(uuid.uuid4()),
                email=f"{uuid.uuid4().hex[:8]}@rt-it-school.ru",
                full_name="Петров П.П.",
                role="KAM",
                status="active",
                consent_version="1.0",
            )
            session.add_all([status, org, owner])
            await session.flush()
            deal = Deal(
                number=f"D-{uuid.uuid4().hex[:10]}",
                title="Сделка с подписью",
                deal_type="b2b",
                workflow_id=workflow.id,
                status_id=status.id,
                organization_id=org.id,
                owner_id=owner.id,
                signature_status="pending",
            )
            session.add(deal)
            await session.flush()
            return deal.id

    @staticmethod
    async def _attach(deal_id: uuid.UUID, document_id: uuid.UUID, *, active: bool) -> None:
        from app.core.db import session_scope
        from app.modules.crm.models import Deal
        from app.modules.signing.models import SignatureDocument

        async with session_scope() as session:
            document = await session.get(SignatureDocument, document_id)
            document.entity_type = "deal"
            document.entity_id = deal_id
            await session.flush()
            if active:
                deal = await session.get(Deal, deal_id)
                deal.active_signature_document_id = document_id

    def _scene(self, client, *, active: bool):
        signer = run(client, _make_user, "KAM")
        built = _build(client, requests=[{"user": signer, "status": "sent"}])
        deal_id = run(client, self._deal)
        run(client, functools.partial(self._attach, deal_id, built.document_id, active=active))
        run(client, _issue_otp, built.request_ids[0])
        return built, deal_id

    def test_the_active_document_sets_the_status_and_bumps_the_version(
        self, client, stubbed_storage
    ) -> None:
        from app.modules.crm.models import Deal

        built, deal_id = self._scene(client, active=True)
        before = run(client, _get, Deal, deal_id)

        assert isinstance(run(client, _try_sign, built.request_ids[0]), uuid.UUID)

        after = run(client, _get, Deal, deal_id)
        assert after.signature_status == "signed"
        assert after.version == before.version + 1

    def test_an_old_document_does_not_touch_the_deal(self, client, stubbed_storage) -> None:
        from app.modules.crm.models import Deal

        built, deal_id = self._scene(client, active=False)
        before = run(client, _get, Deal, deal_id)

        assert isinstance(run(client, _try_sign, built.request_ids[0]), uuid.UUID)

        after = run(client, _get, Deal, deal_id)
        assert after.signature_status == before.signature_status == "pending"
        assert after.version == before.version

    def test_expiry_of_an_old_document_leaves_the_deal_alone(self, client) -> None:
        from app.core.db import session_scope
        from app.modules.crm.models import Deal
        from app.modules.signing.models import SignatureDocument
        from app.modules.signing.tasks import sweep_signature_deadlines

        signer = run(client, _make_user, "KAM")
        built = _build(client, requests=[{"user": signer, "status": "sent"}])
        deal_id = run(client, self._deal)
        run(client, functools.partial(self._attach, deal_id, built.document_id, active=False))

        async def _make_overdue() -> None:
            async with session_scope() as session:
                document = await session.get(SignatureDocument, built.document_id)
                document.deadline_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)

        run(client, _make_overdue)
        before = run(client, _get, Deal, deal_id)

        run(client, sweep_signature_deadlines, {})

        assert run(client, _get, SignatureDocument, built.document_id).status == "expired"
        after = run(client, _get, Deal, deal_id)
        assert (after.signature_status, after.version) == (before.signature_status, before.version)


class TestFileOwnership:
    @staticmethod
    async def _file(owner_id: uuid.UUID) -> uuid.UUID:
        from app.core.db import session_scope
        from app.modules.files.models import File

        async with session_scope() as session:
            file = File(
                storage_key=f"{uuid.uuid4()}/doc.pdf",
                bucket="files",
                original_filename="doc.pdf",
                mime_type="application/pdf",
                size_bytes=10,
                sha256="a" * 64,
                status="ready",
                uploaded_by=owner_id,
            )
            session.add(file)
            await session.flush()
            return file.id

    @staticmethod
    async def _check(principal, file_id, entity_id, *, attached: bool = False) -> str:
        from app.core.db import session_scope
        from app.core.errors import NotFoundError
        from app.modules.files.models import Attachment, File
        from app.modules.signing.service import SignatureDocumentService

        async with session_scope() as session:
            file = await session.get(File, file_id)
            if attached:
                session.add(
                    Attachment(
                        file_id=file_id,
                        entity_type="deal",
                        entity_id=entity_id,
                        category="other",
                        uploaded_by=file.uploaded_by,
                    )
                )
                await session.flush()
            try:
                await SignatureDocumentService(session)._ensure_file_usable(
                    principal, file, "deal", entity_id
                )
            except NotFoundError:
                return "not_found"
            return "ok"

    def test_only_own_or_attached_files_may_be_sent_for_signing(self, client) -> None:
        author = run(client, _make_user, "KAM")
        stranger = run(client, _make_user, "KAM")
        file_id = run(client, self._file, author.id)
        deal_id = uuid.uuid4()

        def check(principal, **kwargs):
            return run(
                client, functools.partial(self._check, principal, file_id, deal_id, **kwargs)
            )

        mine = SimpleNamespace(user_id=author.id, is_admin=False)
        theirs = SimpleNamespace(user_id=stranger.id, is_admin=False)
        admin = SimpleNamespace(user_id=uuid.uuid4(), is_admin=True)

        assert check(mine) == "ok"
        assert check(admin) == "ok"
        assert check(theirs) == "not_found"
        # Файл, приложенный к этой же сделке, годится любому, кто сделку видит.
        assert check(theirs, attached=True) == "ok"


class TestOtpDelivery:
    @staticmethod
    async def _set_phone(user_id: uuid.UUID, phone: str) -> None:
        from app.core.db import session_scope
        from app.modules.identity.models import User

        async with session_scope() as session:
            (await session.get(User, user_id)).phone = phone

    @staticmethod
    async def _otp_count(request_id: uuid.UUID) -> int:
        from sqlalchemy import func, select

        from app.core.db import session_scope
        from app.modules.signing.models import SignatureOtpCode

        async with session_scope() as session:
            return await session.scalar(
                select(func.count())
                .select_from(SignatureOtpCode)
                .where(SignatureOtpCode.request_id == request_id)
            )

    def _challenge(self, client, monkeypatch, *, debug: bool, gateway: str | None, phone: bool):
        from app.modules.signing import service as svc

        async def fake_send_sms(*, to: str, message: str, base_url: str | None = None):
            return gateway

        monkeypatch.setattr(svc, "send_sms", fake_send_sms)
        monkeypatch.setattr(svc, "_expose_debug_otp", lambda: debug)
        signer = run(client, _make_user, "KAM")
        if phone:
            run(client, functools.partial(self._set_phone, signer.id, "+79991234567"))
        _login(client, signer)
        built = _build(client, requests=[{"user": signer, "status": "sent"}])
        response = client.post(f"/api/signature-requests/{built.request_ids[0]}/challenge")
        return response, built.request_ids[0]

    def test_email_with_no_transport_is_an_error_when_the_code_is_not_shown(
        self, client, monkeypatch
    ) -> None:
        response, request_id = self._challenge(
            client, monkeypatch, debug=False, gateway=None, phone=False
        )

        assert response.status_code == 503, response.text
        assert response.json()["code"] == "CRM-9503"
        # Код не сохранён: подписант не получил бы его, а попытка на него не тратится.
        assert run(client, self._otp_count, request_id) == 0

    def test_a_failed_sms_is_an_error_when_the_code_is_not_shown(self, client, monkeypatch) -> None:
        response, request_id = self._challenge(
            client, monkeypatch, debug=False, gateway=None, phone=True
        )

        assert response.status_code == 503, response.text
        assert run(client, self._otp_count, request_id) == 0

    def test_a_delivered_sms_hides_the_code(self, client, monkeypatch) -> None:
        response, _ = self._challenge(client, monkeypatch, debug=False, gateway="msg-1", phone=True)

        assert response.status_code == 200, response.text
        assert response.json()["channel"] == "sms"
        assert response.json()["debug_code"] is None

    def test_a_demo_stand_keeps_working_without_a_gateway(self, client, monkeypatch) -> None:
        response, _ = self._challenge(client, monkeypatch, debug=True, gateway=None, phone=True)

        assert response.status_code == 200, response.text
        assert len(response.json()["debug_code"]) == 6

    def test_the_setting_overrides_the_profile_rule(self, monkeypatch) -> None:
        from app.modules.signing import service as svc

        def settings(*, is_prod: bool, flag):
            return SimpleNamespace(is_prod=is_prod, signature_expose_debug_otp=flag)

        monkeypatch.setattr(svc, "get_settings", lambda: settings(is_prod=False, flag=None))
        assert svc._expose_debug_otp() is True
        monkeypatch.setattr(svc, "get_settings", lambda: settings(is_prod=True, flag=None))
        assert svc._expose_debug_otp() is False
        monkeypatch.setattr(svc, "get_settings", lambda: settings(is_prod=False, flag=False))
        assert svc._expose_debug_otp() is False
        monkeypatch.setattr(svc, "get_settings", lambda: settings(is_prod=True, flag=True))
        assert svc._expose_debug_otp() is True
        # Поля в настройках может и не быть вовсе: тогда действует правило профиля.
        monkeypatch.setattr(svc, "get_settings", lambda: SimpleNamespace(is_prod=False))
        assert svc._expose_debug_otp() is True


class TestClientIp:
    def test_the_signature_records_the_normalised_client_ip(self, client, stubbed_storage) -> None:
        token = f"tok-{uuid.uuid4().hex}"
        built = _build(client, requests=[{"user": None, "status": "sent", "token": token}])
        headers = {"X-Real-IP": IP, "X-Forwarded-For": "10.66.66.66"}

        challenge = client.post(f"/public/sign/{token}/challenge", headers=headers)
        assert challenge.status_code == 200, challenge.text
        signed = client.post(
            f"/public/sign/{token}/sign",
            json={"otp": challenge.json()["debug_code"]},
            headers=headers,
        )

        assert signed.status_code == 200, signed.text
        (signature,) = run(client, _signatures_of, built.document_id)
        assert str(signature.ip) == IP

    def test_a_forged_forwarded_header_does_not_get_a_fresh_limit(self, client) -> None:
        from app.core.config import get_settings

        ip_limit = get_settings().public_sign_rate_limit_per_min * 10
        # Раньше ключ лимита был сырым адресом соединения, и его обходили сменой X-Forwarded-For.
        # Теперь адрес — из X-Real-IP, который выставляет прокси. Токены разные, чтобы не упереться
        # в лимит на один токен: считается именно общий лимит по адресу.
        real = f"203.0.113.{(uuid.uuid4().int % 200) + 1}"
        statuses = [
            client.get(
                f"/public/sign/nosuch-{uuid.uuid4().hex}/file",
                headers={"X-Real-IP": real, "X-Forwarded-For": f"10.0.0.{i % 250}"},
            ).status_code
            for i in range(ip_limit + 1)
        ]

        assert statuses[-1] == 429
        assert 429 not in statuses[:-1]


class TestDeadline:
    def test_the_deadline_is_counted_from_the_send(self) -> None:
        from app.modules.signing.service import SignatureDocumentService

        now = dt.datetime.now(dt.UTC)
        created = now - dt.timedelta(days=10)
        document = SimpleNamespace(deadline_at=created + dt.timedelta(days=7))
        document.created_at = created

        SignatureDocumentService._restart_deadline(document)

        assert document.deadline_at > now + dt.timedelta(days=6, hours=23)

    def test_a_fresh_document_keeps_its_deadline(self) -> None:
        from app.modules.signing.service import SignatureDocumentService

        now = dt.datetime.now(dt.UTC)
        deadline = now + dt.timedelta(days=7)
        document = SimpleNamespace(deadline_at=deadline, created_at=now)

        SignatureDocumentService._restart_deadline(document)

        assert document.deadline_at == deadline

    def test_an_unloaded_created_at_leaves_the_deadline_alone(self) -> None:
        from app.modules.signing.service import SignatureDocumentService

        deadline = dt.datetime.now(dt.UTC) + dt.timedelta(days=7)
        document = SimpleNamespace(deadline_at=deadline)

        SignatureDocumentService._restart_deadline(document)

        assert document.deadline_at == deadline


class TestRendering:
    def test_a_template_cannot_reach_python_internals(self) -> None:
        from app.modules.signing.rendering import RenderError, render_template_html

        with pytest.raises(RenderError):
            render_template_html("{{ ''.__class__.__mro__ }}", {})

    def test_ordinary_templates_still_render(self) -> None:
        from app.modules.signing.rendering import render_template_html

        html = render_template_html("<p>{{ deal_title }}</p>", {"deal_title": "Сделка <b>"})

        assert "<p>Сделка &lt;b&gt;</p>" in html

    def test_garbage_is_not_a_pdf(self) -> None:
        from app.modules.signing.rendering import RenderError, validate_pdf

        with pytest.raises(RenderError):
            validate_pdf(b"not a pdf at all")
        with pytest.raises(RenderError):
            validate_pdf(b"")

    def test_a_real_pdf_passes(self) -> None:
        import io

        from reportlab.pdfgen import canvas

        from app.modules.signing.rendering import validate_pdf

        buffer = io.BytesIO()
        page = canvas.Canvas(buffer)
        page.drawString(72, 720, "test")
        page.save()

        validate_pdf(buffer.getvalue())


class TestSmsGatewayMock:
    def _post(self, body: dict):
        from fastapi.testclient import TestClient

        from app.mocks.sms_gateway import app

        return TestClient(app).post("/send", json=body)

    def test_an_ordinary_message_is_accepted(self) -> None:
        response = self._post({"to": "+79991234567", "message": "Код подтверждения: 123456"})

        assert response.status_code == 200, response.text
        assert response.json()["status"] == "queued"

    def test_oversized_fields_are_refused(self) -> None:
        assert self._post({"to": "9" * 33, "message": "x"}).status_code == 422
        assert self._post({"to": "+79991234567", "message": "x" * 501}).status_code == 422


async def _sign_and_load(request_id: uuid.UUID, document_id: uuid.UUID):
    """Подпись через настоящий `sign()`; возвращает отсоединённую копию записи."""
    await _issue_otp(request_id)
    signed = await _try_sign(request_id)
    assert isinstance(signed, uuid.UUID), signed
    (signature,) = await _signatures_of(document_id)
    return signature


def _detached_copy(signature, **overrides):
    """Копия записи подписи в памяти (в БД не пишется: таблица неизменяемая) с подменёнными
    полями — так проверяется, что пересчёт замечает правку записи или доказательства."""
    import copy

    from app.modules.signing.models import Signature

    fields = {column.key: getattr(signature, column.key) for column in Signature.__table__.columns}
    fields["evidence"] = copy.deepcopy(signature.evidence)
    fields.update(overrides)
    return Signature(**fields)


async def _verify_by_id(signature_id: uuid.UUID) -> dict:
    from app.core.db import session_scope
    from app.modules.signing.service import VerifyService

    async with session_scope() as session:
        return await VerifyService(session).verify_by_id(signature_id)


async def _integrity_of(signature) -> object:
    from app.core.db import session_scope
    from app.modules.signing.service import VerifyService

    async with session_scope() as session:
        return await VerifyService(session).check_integrity(signature)


class TestSignatureEvidenceCheck:
    """`verify` пересчитывает HMAC метки целостности и хэш звена по тому, что лежит в БД."""

    def _signed(self, client, stubbed_storage):
        signer = run(client, _make_user, "KAM")
        built = _build(client, requests=[{"user": signer, "status": "sent"}])
        signature = run(client, _sign_and_load, built.request_ids[0], built.document_id)
        return built, signature

    def test_a_fresh_signature_is_verified(self, client, stubbed_storage) -> None:
        _, signature = self._signed(client, stubbed_storage)

        result = run(client, _verify_by_id, signature.id)

        assert result["status"] == "valid"
        assert result["integrity"] == "verified"

    def test_the_public_verify_page_carries_the_integrity_state(
        self, client, stubbed_storage
    ) -> None:
        _, signature = self._signed(client, stubbed_storage)

        body = client.get(f"/public/verify/{signature.id}").json()

        assert body["status"] == "valid"
        assert body["integrity"] == "verified"

    def test_a_signature_without_a_nonce_is_unavailable_not_forged(
        self, client, stubbed_storage
    ) -> None:
        # Так выглядят подписи, поставленные до того, как nonce стали сохранять.
        _, signature = self._signed(client, stubbed_storage)
        evidence = signature.evidence
        del evidence["auth"]["nonce"]

        report = run(client, _integrity_of, _detached_copy(signature, evidence=evidence))

        assert report.state == "unavailable"

    def test_a_row_built_by_the_test_helpers_is_unavailable(self, client) -> None:
        signer = run(client, _make_user, "KAM")
        built = _build(client, requests=[{"user": signer, "status": "signed"}])

        result = run(client, _verify_by_id, built.signature_ids[0])

        assert result["status"] == "valid"
        assert result["integrity"] == "unavailable"

    def test_a_signature_made_with_another_key_version_is_unavailable(
        self, client, stubbed_storage
    ) -> None:
        _, signature = self._signed(client, stubbed_storage)

        report = run(client, _integrity_of, _detached_copy(signature, key_version=99))

        assert report.state == "unavailable"

    @pytest.mark.parametrize(
        "override",
        [
            pytest.param({"signature_value": "0" * 64}, id="signature_value"),
            pytest.param({"content_hash": "1" * 64}, id="content_hash"),
            pytest.param({"prev_hash": "2" * 64}, id="prev_hash"),
            pytest.param({"request_id": uuid.uuid4()}, id="request_id"),
            pytest.param({"hash": "3" * 64}, id="chain_hash"),
            pytest.param({"signed_at": dt.datetime(2020, 1, 1, tzinfo=dt.UTC)}, id="signed_at"),
        ],
    )
    def test_a_changed_column_is_detected(self, client, stubbed_storage, override) -> None:
        _, signature = self._signed(client, stubbed_storage)

        report = run(client, _integrity_of, _detached_copy(signature, **override))

        assert report.state == "broken", report.problems

    @pytest.mark.parametrize(
        "path",
        [
            ("signer", "id"),
            ("time", "signed_at"),
            ("auth", "nonce"),
            ("document", "content_hash"),
        ],
    )
    def test_a_changed_evidence_field_is_detected(self, client, stubbed_storage, path) -> None:
        _, signature = self._signed(client, stubbed_storage)
        evidence = signature.evidence
        section, key = path
        evidence[section][key] = "2020-01-01T00:00:00+00:00" if key == "signed_at" else "changed"

        report = run(client, _integrity_of, _detached_copy(signature, evidence=evidence))

        assert report.state == "broken", report.problems

    def test_a_link_to_a_missing_predecessor_is_detected(self, client, stubbed_storage) -> None:
        from app.modules.signing.service import compute_chain_hash

        _, signature = self._signed(client, stubbed_storage)
        # Хэш звена пересчитан под подставной `prev_hash`: собственная запись согласована, но
        # предыдущего звена с таким хэшем в цепочке нет.
        fake_prev = "4" * 64
        relinked = _detached_copy(
            signature,
            prev_hash=fake_prev,
            hash=compute_chain_hash(
                prev_hash=fake_prev,
                signature_value=signature.signature_value,
                content_hash=signature.content_hash,
                request_id=str(signature.request_id),
                signed_at_iso=signature.evidence["time"]["signed_at"],
            ),
        )

        report = run(client, _integrity_of, relinked)

        assert report.state == "broken"
        assert any("несуществующую" in problem for problem in report.problems)

    def test_a_broken_signature_is_reported_as_tampered_in_the_verify_result(
        self, client, stubbed_storage, monkeypatch
    ) -> None:
        from sqlalchemy.ext.asyncio import AsyncSession

        _, signature = self._signed(client, stubbed_storage)
        forged = _detached_copy(signature, signature_value="0" * 64)
        original_get = AsyncSession.get

        async def fake_get(self, model, ident, *args, **kwargs):
            if ident == signature.id:
                return forged
            return await original_get(self, model, ident, *args, **kwargs)

        monkeypatch.setattr(AsyncSession, "get", fake_get)

        result = run(client, _verify_by_id, signature.id)

        assert result["status"] == "tampered"
        assert result["integrity"] == "broken"


class TestOneSignaturePerRequest:
    @staticmethod
    async def _second_signature(request_id: uuid.UUID, document_id: uuid.UUID) -> str:
        from sqlalchemy.exc import IntegrityError

        from app.core.db import get_session_factory
        from app.modules.signing.models import Signature

        async with get_session_factory()() as session:
            session.add(
                Signature(
                    request_id=request_id,
                    document_id=document_id,
                    content_hash="a" * 64,
                    method="pep_otp",
                    signer_display="Дубль",
                    signature_value="v",
                    evidence={},
                    signed_at=dt.datetime.now(dt.UTC),
                    hash=uuid.uuid4().hex + uuid.uuid4().hex,
                )
            )
            try:
                await session.flush()
            except IntegrityError as exc:
                await session.rollback()
                return getattr(getattr(exc.orig, "__cause__", None), "constraint_name", "")
            await session.rollback()
            return ""

    def test_the_database_refuses_a_second_signature_for_a_request(self, client) -> None:
        signer = run(client, _make_user, "KAM")
        built = _build(client, requests=[{"user": signer, "status": "signed"}])

        constraint = run(client, self._second_signature, built.request_ids[0], built.document_id)

        assert constraint == "uq_signatures_request_id"
