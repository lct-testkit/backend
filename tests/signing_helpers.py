"""Общие построители для тестов ПЭП поверх БД (`tests/test_signing_api.py`,
`tests/test_signing_file.py`): документы, файлы, запросы и подписи заводятся в БД
напрямую — S3 и NTP в тестах недоступны, `seal()` здесь не запускается.
"""

from __future__ import annotations

import datetime as dt
import functools
import hashlib
import uuid
from dataclasses import dataclass, field

from tests.conftest import authenticate, run

PROTOCOL = b"%PDF-1.4 signing protocol"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _login(client, user) -> None:
    csrf = authenticate(client, user)
    client.headers["X-CSRF-Token"] = csrf


@dataclass
class Built:
    document_id: uuid.UUID
    entity_id: uuid.UUID = field(default_factory=uuid.uuid4)
    # Байты уникальны у каждого документа: БД общая для всех тестов, а проверка
    # по файлу ищет по хэшу.
    original: bytes = b""
    stamped: bytes = b""
    request_ids: list[uuid.UUID] = field(default_factory=list)
    signature_ids: list[uuid.UUID] = field(default_factory=list)
    tokens: dict[uuid.UUID, str] = field(default_factory=dict)
    signed_file_id: uuid.UUID | None = None
    protocol_file_id: uuid.UUID | None = None


async def _document(
    *,
    created_by: uuid.UUID | None = None,
    status: str = "pending",
    requests: list[dict],
    with_results: bool = False,
) -> Built:
    """Документ на подпись. Подписант — `{"user": User | None, "status": …}`;
    без `user` — внешний (контакт с email). Подписанный получает подпись, у
    внешнего в статусе `sent`/`viewed` может быть выданный токен (`"token"`)."""
    from app.core.db import session_scope
    from app.modules.catalog.models import Contact
    from app.modules.files.models import File
    from app.modules.signing.models import (
        EdmAgreement,
        Signature,
        SignatureDocument,
        SignatureRequest,
    )

    now = dt.datetime.now(dt.UTC)
    built = Built(document_id=uuid.uuid4())
    built.original = b"%PDF-1.4 original " + built.document_id.bytes
    built.stamped = built.original + b" with signature stamp"

    def _file(name: str, data: bytes) -> File:
        return File(
            id=uuid.uuid4(),
            storage_key=f"{uuid.uuid4()}/{name}",
            bucket="signatures",
            original_filename=name,
            mime_type="application/pdf",
            size_bytes=len(data),
            sha256=_sha(data),
            status="ready",
        )

    async with session_scope() as session:
        original = _file("doc.pdf", built.original)
        session.add(original)
        signed = protocol = None
        if with_results:
            signed = _file(f"signed-{built.document_id}.pdf", built.stamped)
            protocol = _file(f"protocol-{built.document_id}.pdf", PROTOCOL)
            session.add_all([signed, protocol])
        await session.flush()

        document = SignatureDocument(
            id=built.document_id,
            doc_type="custom",
            title="Договор на подпись",
            entity_type="erasure_request",
            entity_id=built.entity_id,
            file_id=original.id,
            content_hash=_sha(built.original),
            signed_file_id=signed.id if signed else None,
            protocol_file_id=protocol.id if protocol else None,
            signing_order="sequential",
            status=status,
            deadline_at=now + dt.timedelta(days=7),
            created_by=created_by,
        )
        session.add(document)
        await session.flush()
        built.signed_file_id = signed.id if signed else None
        built.protocol_file_id = protocol.id if protocol else None

        for order, spec in enumerate(requests, start=1):
            user = spec.get("user")
            contact = None
            agreement = None
            if user is None:
                contact = Contact(first_name="Пётр", last_name="Сидоров", email="p@example.ru")
                session.add(contact)
                await session.flush()
                # Подпись внешнего подписанта требует действующего соглашения об ЭДО (`send()`
                # его находит и привязывает к запросу) — здесь запросы создаются мимо `send()`.
                # `"agreement_status"` в описании подписанта позволяет тесту отозвать соглашение.
                agreement = EdmAgreement(
                    party_type="contact",
                    party_id=contact.id,
                    conclusion_method="paper",
                    status=spec.get("agreement_status", "active"),
                    revoked_at=now if spec.get("agreement_status") == "revoked" else None,
                )
                session.add(agreement)
                await session.flush()
            token = spec.get("token")
            request = SignatureRequest(
                document_id=document.id,
                signer_type="internal" if user else "external",
                signer_user_id=user.id if user else None,
                signer_contact_id=contact.id if contact else None,
                edm_agreement_id=agreement.id if agreement else None,
                signer_name_snapshot=f"Подписант {order}",
                sign_order=order,
                status=spec["status"],
                access_token_hash=_sha(token.encode()) if token else None,
                token_expires_at=now + dt.timedelta(days=7) if token else None,
                sent_at=now if spec["status"] != "pending" else None,
            )
            session.add(request)
            await session.flush()
            built.request_ids.append(request.id)
            if token:
                built.tokens[request.id] = token
            if spec["status"] == "signed":
                signature = Signature(
                    request_id=request.id,
                    document_id=document.id,
                    content_hash=document.content_hash,
                    method="pep_otp",
                    signer_display=request.signer_name_snapshot,
                    signature_value="value",
                    evidence={},
                    signed_at=now + dt.timedelta(minutes=order),
                    hash=_sha(uuid.uuid4().bytes),
                )
                session.add(signature)
                await session.flush()
                built.signature_ids.append(signature.id)
    return built


def _build(client, **kwargs) -> Built:
    return run(client, functools.partial(_document, **kwargs))


async def _request_state(request_id: uuid.UUID) -> dict[str, object]:
    from app.core.db import session_scope
    from app.modules.signing.models import SignatureRequest

    async with session_scope() as session:
        request = await session.get(SignatureRequest, request_id)
        return {"status": request.status, "token_hash": request.access_token_hash}
