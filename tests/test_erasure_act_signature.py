"""B6-9: акт уничтожения ПДн подписывается ответственным лицом (ПЭП), не «системой».

Хранилище подменено (настоящего S3 в тестовом окружении нет) — проверяется выбор подписанта,
создание документа на подпись (`erasure_act`, `parallel`) и запись `act_document_id`/
`act_signature_id` после подписи, не HTTP-клиент S3.

НЕ реализовано в этой итерации (см. erasure_service.py): run_pdf_job/off-loop рендер PDF,
перенос строк в акте, `POST .../act/resign` — не тестируется здесь.
"""

from __future__ import annotations

import datetime as dt
import functools
import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, _make_user, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


@pytest.fixture
def stub_storage(monkeypatch):
    """Подменяет S3 в erasure_service: сохраняет/перезаписывает по ключу в памяти —
    проверяет, что ключ детерминирован, без настоящего хранилища."""
    from app.modules.identity import erasure_service as svc

    store: dict[tuple[str, str], bytes] = {}

    async def upload(*, bucket: str, key: str, body: bytes, content_type: str) -> None:
        store[(bucket, key)] = body

    async def ensure(bucket: str) -> None:
        return None

    async def delete(*, bucket: str, key: str) -> None:
        store.pop((bucket, key), None)

    monkeypatch.setattr(svc, "upload_object_bytes", upload)
    monkeypatch.setattr(svc, "ensure_bucket", ensure)
    monkeypatch.setattr(svc, "delete_object", delete)
    return store


async def _make_erasure_request(
    *, requested_by: uuid.UUID | None = None, subject_id: uuid.UUID | None = None
):
    from app.core.db import session_scope
    from app.modules.identity.models import DataErasureRequest, ErasureStatus, SubjectType

    async with session_scope() as session:
        request = DataErasureRequest(
            subject_type=SubjectType.CONTACT.value,
            subject_id=subject_id or uuid.uuid4(),
            requested_by=requested_by,
            status=ErasureStatus.APPROVED.value,
            executed_at=dt.datetime.now(dt.UTC),
        )
        session.add(request)
        await session.flush()
        session.expunge(request)
        return request


async def _set_responsible_setting(user_id: uuid.UUID) -> None:
    from app.core.db import session_scope
    from app.modules.admin.models import SystemSetting

    async with session_scope() as session:
        session.add(SystemSetting(key="pdn.responsible_user_id", value=str(user_id)))


async def _issue_act_for(request_id: uuid.UUID) -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.identity.erasure_service import ErasureExecutionService
    from app.modules.identity.models import DataErasureRequest

    async with session_scope() as session:
        request = await session.get(DataErasureRequest, request_id)
        service = ErasureExecutionService(session)
        return await service._issue_act(  # noqa: SLF001
            request,
            subject_type="contact",
            subject_display="Тестовый контакт",
            categories_erased=["ФИО", "email"],
            categories_retained=[],
        )


async def _get_request(request_id: uuid.UUID):
    from app.core.db import session_scope
    from app.modules.identity.models import DataErasureRequest

    async with session_scope() as session:
        request = await session.get(DataErasureRequest, request_id)
        session.expunge(request)
        return request


class TestSignerResolution:
    def test_configured_responsible_wins(self, client, stub_storage) -> None:
        responsible = run(client, _make_user, "ADMIN")
        run(client, functools.partial(_set_responsible_setting, responsible.id))
        request = run(client, _make_erasure_request)

        run(client, functools.partial(_issue_act_for, request.id))

        updated = run(client, _get_request, request.id)
        assert updated.act_document_id is not None

        async def _signer_of(document_id):
            from sqlalchemy import select

            from app.core.db import session_scope
            from app.modules.signing.models import SignatureRequest

            async with session_scope() as session:
                rows = (
                    (
                        await session.execute(
                            select(SignatureRequest).where(
                                SignatureRequest.document_id == document_id
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                return [r.signer_user_id for r in rows]

        signers = run(client, functools.partial(_signer_of, updated.act_document_id))
        assert signers == [responsible.id]

    def test_falls_back_to_requested_by_without_configured_responsible(
        self, client, stub_storage
    ) -> None:
        requester = run(client, _make_user, "KAM")
        request = run(client, functools.partial(_make_erasure_request, requested_by=requester.id))

        run(client, functools.partial(_issue_act_for, request.id))

        updated = run(client, _get_request, request.id)
        assert updated.act_document_id is not None

    def test_falls_back_to_any_active_admin(self, client, stub_storage) -> None:
        run(client, _make_user, "ADMIN")  # создаёт хотя бы одного активного админа
        request = run(client, _make_erasure_request)

        act_file_id = run(client, functools.partial(_issue_act_for, request.id))

        assert act_file_id is not None
        updated = run(client, _get_request, request.id)
        assert updated.act_document_id is not None

    def test_subject_cannot_sign_their_own_act(self, client, stub_storage) -> None:
        """Субъект заявки (если это сотрудник) не становится подписантом своего акта —
        проверяется через прямой вызов _resolve_act_signer, а не через создание документа."""
        from app.core.db import session_scope
        from app.modules.identity.erasure_service import ErasureExecutionService
        from app.modules.identity.models import DataErasureRequest, ErasureStatus, SubjectType

        admin = run(client, _make_user, "ADMIN")

        async def _resolve():
            async with session_scope() as session:
                request = DataErasureRequest(
                    subject_type=SubjectType.USER.value,
                    subject_id=admin.id,
                    status=ErasureStatus.APPROVED.value,
                )
                session.add(request)
                await session.flush()
                return await ErasureExecutionService(session)._resolve_act_signer(  # noqa: SLF001
                    request
                )

        resolved = run(client, _resolve)
        assert resolved != admin.id


class TestActDocument:
    def test_act_document_is_parallel_erasure_act(self, client, stub_storage) -> None:
        run(client, _make_user, "ADMIN")
        request = run(client, _make_erasure_request)

        run(client, functools.partial(_issue_act_for, request.id))
        updated = run(client, _get_request, request.id)

        async def _document(document_id):
            from app.core.db import session_scope
            from app.modules.signing.models import SignatureDocument

            async with session_scope() as session:
                document = await session.get(SignatureDocument, document_id)
                return document.doc_type, document.signing_order, document.entity_type

        doc_type, signing_order, entity_type = run(
            client, functools.partial(_document, updated.act_document_id)
        )
        assert doc_type == "erasure_act"
        assert signing_order == "parallel"
        assert entity_type == "erasure_request"

    def test_repeat_issue_reuses_the_same_storage_key(self, client, stub_storage) -> None:
        run(client, _make_user, "ADMIN")
        request = run(client, _make_erasure_request)

        run(client, functools.partial(_issue_act_for, request.id))
        run(client, functools.partial(_issue_act_for, request.id))

        keys = {key for _bucket, key in stub_storage}
        matching = [k for k in keys if k == f"erasure-acts/{request.id}/act.pdf"]
        assert len(matching) == 1
