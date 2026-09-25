"""Аутентифицированные ручки ПЭП (dop.md §10.10, spec.txt §6).

Публичные ручки (`/public/sign/{token}`, `/public/verify/{id}`) — в
`app.modules.signing.public_router`: у них другая модель доступа (без
`Depends(require_permission)`, свой rate-limit) и смешивать их с
аутентифицированными в одном файле было бы легко перепутать местами.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Path, Query, Response, UploadFile, status
from sqlalchemy import select

from app.core.config import get_settings
from app.core.context import get_client
from app.core.deps import ConsentedUser, DbSession, require_permission
from app.core.errors import ValidationError
from app.core.permissions import Permission
from app.core.security import Principal
from app.modules.signing.models import SignatureDocument
from app.modules.signing.schemas import (
    ChallengeResponse,
    DownloadUrlOut,
    EdmAgreementCreateRequest,
    EdmAgreementListResponse,
    EdmAgreementOut,
    EdmAgreementRevokeRequest,
    RejectRequest,
    SignatureBrief,
    SignatureDocumentCreateRequest,
    SignatureDocumentListResponse,
    SignatureDocumentOut,
    SignatureOut,
    SignatureRequestOut,
    SignatureTemplateListResponse,
    SignatureTemplateOut,
    SigningPageOut,
    SignRequest,
    VerifyResult,
    VoidRequest,
)
from app.modules.signing.service import (
    EdmAgreementService,
    SignatureDocumentService,
    SignatureRequestService,
    SignatureTemplateService,
    VerifyService,
    pdf_inline_headers,
)

signature_documents_router = APIRouter(prefix="/signature-documents", tags=["signing"])
signature_requests_router = APIRouter(tags=["signing"])
signature_templates_router = APIRouter(prefix="/signature-templates", tags=["signing"])
edm_agreements_router = APIRouter(prefix="/admin/edm-agreements", tags=["signing"])
signatures_router = APIRouter(prefix="/signatures", tags=["signing"])

_MAX_VERIFY_UPLOAD_BYTES = 52_428_800  # 50 МБ — тот же порядок, что files_max_size_bytes


@signature_documents_router.post(
    "",
    summary="Создать документ на подпись",
    description=(
        "Создаёт документ из шаблона (`template_code`, данные сущности подставляются "
        "автоматически) либо из уже загруженного PDF (`file_id`). Статус — `draft`, "
        "рассылка подписантам происходит отдельным вызовом `/send`. Роль: KAM (свои "
        "сделки), HEAD, ADMIN."
    ),
    response_model=SignatureDocumentOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_document(
    payload: SignatureDocumentCreateRequest,
    session: DbSession,
    principal: Annotated[Principal, Depends(require_permission(Permission.SIGNATURE_CREATE))],
) -> SignatureDocumentOut:
    service = SignatureDocumentService(session)
    document = await service.create(principal, payload)
    return await _card(service, document)


@signature_documents_router.get(
    "",
    summary="Документы на подпись по сущности",
    description=(
        "История документов сделки: активные, подписанные, аннулированные, просроченные. "
        "Права — как на саму сделку. Роль: KAM (свои сделки), HEAD, ADMIN."
    ),
    response_model=SignatureDocumentListResponse,
)
async def list_documents(
    session: DbSession,
    principal: Annotated[Principal, Depends(require_permission(Permission.SIGNATURE_CREATE))],
    entity_type: Annotated[str, Query(max_length=32)],
    entity_id: Annotated[uuid.UUID, Query()],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> SignatureDocumentListResponse:
    service = SignatureDocumentService(session)
    documents = await service.list_for_entity(principal, entity_type, entity_id, limit)
    signatures = await service.signatures_by_document([d.id for d in documents])
    items = [
        _document_out(d, await service.list_requests(d.id), signatures=signatures.get(d.id))
        for d in documents
    ]
    return SignatureDocumentListResponse(items=items)


@signature_documents_router.get(
    "/{document_id}",
    summary="Карточка документа на подпись",
    response_model=SignatureDocumentOut,
)
async def get_document(
    session: DbSession,
    principal: Annotated[Principal, Depends(require_permission(Permission.SIGNATURE_CREATE))],
    document_id: Annotated[uuid.UUID, Path()],
) -> SignatureDocumentOut:
    service = SignatureDocumentService(session)
    document = await service.get_or_404(document_id)
    await service.ensure_read_access(principal, document)
    return await _card(service, document)


@signature_documents_router.post(
    "/{document_id}/send",
    summary="Запустить сбор подписей",
    description=(
        "Проверяет соглашения об ЭДО для внешних подписантов (иначе `blocked_no_agreement`), "
        "переводит первого подписанта (или всех — при параллельном порядке) в `sent`."
    ),
    response_model=SignatureDocumentOut,
)
async def send_document(
    session: DbSession,
    principal: Annotated[Principal, Depends(require_permission(Permission.SIGNATURE_CREATE))],
    document_id: Annotated[uuid.UUID, Path()],
) -> SignatureDocumentOut:
    service = SignatureDocumentService(session)
    document = await service.get_or_404(document_id)
    await service.ensure_access(principal, document)
    document, revealed_tokens = await service.send(document)
    return await _card(service, document, revealed_tokens=revealed_tokens)


@signature_documents_router.post(
    "/{document_id}/void",
    summary="Аннулировать документ",
    description="Не удаляет: помечает `void` с причиной (dop.md §10.8). Роль: HEAD, ADMIN.",
    response_model=SignatureDocumentOut,
)
async def void_document(
    payload: VoidRequest,
    session: DbSession,
    principal: Annotated[Principal, Depends(require_permission(Permission.SIGNATURE_VOID))],
    document_id: Annotated[uuid.UUID, Path()],
) -> SignatureDocumentOut:
    service = SignatureDocumentService(session)
    document = await service.get_or_404(document_id)
    await service.ensure_access(principal, document)
    document = await service.void(document, principal=principal, reason=payload.reason)
    return await _card(service, document)


@signature_documents_router.get(
    "/{document_id}/protocol",
    summary="Ссылка на протокол подписания",
    response_model=DownloadUrlOut,
)
async def get_protocol(
    session: DbSession,
    principal: Annotated[Principal, Depends(require_permission(Permission.SIGNATURE_CREATE))],
    document_id: Annotated[uuid.UUID, Path()],
) -> DownloadUrlOut:
    service = SignatureDocumentService(session)
    document = await service.get_or_404(document_id)
    await service.ensure_read_access(principal, document)
    url, expires_at = await service.protocol_download(document)
    return DownloadUrlOut(download_url=url, expires_at=expires_at)


@signature_templates_router.get(
    "",
    summary="Активные шаблоны документов",
    response_model=SignatureTemplateListResponse,
)
async def list_templates(
    session: DbSession,
    _: Annotated[Principal, Depends(require_permission(Permission.SIGNATURE_CREATE))],
) -> SignatureTemplateListResponse:
    items = await SignatureTemplateService(session).list_active()
    return SignatureTemplateListResponse(
        items=[SignatureTemplateOut.model_validate(t) for t in items]
    )


# --- Мои задачи на подпись и действия подписанта ----------------------------


@signature_requests_router.get(
    "/me/signature-requests",
    summary="Мои задачи на подпись",
    response_model=list[SignatureRequestOut],
)
async def my_signature_requests(
    session: DbSession,
    principal: Annotated[Principal, Depends(require_permission(Permission.SIGNATURE_SIGN))],
) -> list[SignatureRequestOut]:
    requests = await SignatureRequestService(session).my_requests(principal)
    documents: dict[uuid.UUID, SignatureDocument] = {}
    if requests:
        found = await session.execute(
            select(SignatureDocument).where(
                SignatureDocument.id.in_({r.document_id for r in requests})
            )
        )
        documents = {d.id: d for d in found.scalars().all()}
    items: list[SignatureRequestOut] = []
    for request in requests:
        item = SignatureRequestOut.model_validate(request)
        document = documents.get(request.document_id)
        if document is not None:
            item.document_title = document.title
            item.deadline_at = document.deadline_at
            item.entity_type = document.entity_type
            item.entity_id = document.entity_id
        items.append(item)
    return items


@signature_requests_router.post(
    "/signature-requests/{request_id}/reissue-link",
    summary="Переиздать ссылку внешнему подписанту",
    description=(
        "Выдаёт инициатору документа новую ссылку `sign_url`, пока запрос внешнего "
        "подписанта ждёт подписи (`sent`/`viewed`); прежняя ссылка перестаёт "
        "работать. Нужна, когда очередь дошла до подписанта после подписи "
        "предыдущего и ссылка не была выдана. Внутреннему подписанту — 422, запросу "
        "не в ожидании подписи — 409. Роль: инициатор документа, ADMIN."
    ),
    response_model=SignatureRequestOut,
)
async def reissue_link(
    session: DbSession,
    principal: Annotated[Principal, Depends(require_permission(Permission.SIGNATURE_CREATE))],
    request_id: Annotated[uuid.UUID, Path()],
) -> SignatureRequestOut:
    request = await SignatureRequestService(session).get_or_404(request_id)
    token = await SignatureDocumentService(session).reissue_link(request, principal=principal)
    out = SignatureRequestOut.model_validate(request)
    out.sign_url = _sign_url(token)
    return out


@signature_requests_router.post(
    "/signature-requests/{request_id}/view",
    summary="Отметить ознакомление (внутренний подписант)",
    response_model=SigningPageOut,
)
async def view_request(
    session: DbSession,
    principal: Annotated[Principal, Depends(require_permission(Permission.SIGNATURE_SIGN))],
    request_id: Annotated[uuid.UUID, Path()],
) -> SigningPageOut:
    service = SignatureRequestService(session)
    request = await service.get_for_internal_signer(request_id, principal)
    client = get_client()
    ip = client.ip if client else None
    user_agent = client.user_agent if client else None
    page = await service.build_signing_page(request, mark_viewed=True, ip=ip, user_agent=user_agent)
    page.document.file_url = _internal_file_url(request_id)
    return page


@signature_requests_router.get(
    "/signature-requests/{request_id}/file",
    summary="PDF документа для просмотра (внутренний подписант)",
    description=(
        "PDF документа запроса с origin приложения: `inline`, без CORS и без ссылки на "
        "хранилище. Только свой запрос. Роль: подписант."
    ),
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
)
async def get_request_file(
    session: DbSession,
    principal: Annotated[Principal, Depends(require_permission(Permission.SIGNATURE_SIGN))],
    request_id: Annotated[uuid.UUID, Path()],
) -> Response:
    service = SignatureRequestService(session)
    request = await service.get_for_internal_signer(request_id, principal)
    data, filename = await service.load_document_pdf(request)
    return Response(
        content=data, media_type="application/pdf", headers=pdf_inline_headers(filename)
    )


@signature_requests_router.post(
    "/signature-requests/{request_id}/challenge",
    summary="Запросить одноразовый код",
    response_model=ChallengeResponse,
)
async def challenge_request(
    session: DbSession,
    principal: Annotated[Principal, Depends(require_permission(Permission.SIGNATURE_SIGN))],
    request_id: Annotated[uuid.UUID, Path()],
) -> ChallengeResponse:
    service = SignatureRequestService(session)
    request = await service.get_for_internal_signer(request_id, principal)
    client = get_client()
    otp, channel, masked, debug_code = await service.challenge(
        request, ip=client.ip if client else None, user_agent=client.user_agent if client else None
    )
    return ChallengeResponse(
        channel=channel,
        sent_to_masked=masked,
        expires_in_seconds=get_settings().signature_otp_ttl_seconds,
        debug_code=debug_code,
        max_attempts=otp.max_attempts,
    )


@signature_requests_router.post(
    "/signature-requests/{request_id}/sign",
    summary="Подписать кодом подтверждения",
    response_model=SignatureOut,
)
async def sign_request(
    payload: SignRequest,
    session: DbSession,
    principal: Annotated[Principal, Depends(require_permission(Permission.SIGNATURE_SIGN))],
    request_id: Annotated[uuid.UUID, Path()],
) -> SignatureOut:
    service = SignatureRequestService(session)
    request = await service.get_for_internal_signer(request_id, principal)
    client = get_client()
    signature = await service.sign(
        request,
        otp_code=payload.otp,
        ip=client.ip if client else None,
        user_agent=client.user_agent if client else None,
    )
    return SignatureOut.model_validate(signature)


@signature_requests_router.post(
    "/signature-requests/{request_id}/reject",
    summary="Отклонить документ",
    response_model=SignatureRequestOut,
)
async def reject_request(
    payload: RejectRequest,
    session: DbSession,
    principal: Annotated[Principal, Depends(require_permission(Permission.SIGNATURE_SIGN))],
    request_id: Annotated[uuid.UUID, Path()],
) -> SignatureRequestOut:
    service = SignatureRequestService(session)
    request = await service.get_for_internal_signer(request_id, principal)
    client = get_client()
    request = await service.reject(
        request,
        reason=payload.reason,
        ip=client.ip if client else None,
        user_agent=client.user_agent if client else None,
    )
    return SignatureRequestOut.model_validate(request)


# --- Проверка подписи (аутентифицированная, программная) -------------------


@signatures_router.post(
    "/verify",
    summary="Проверить документ по хэшу",
    description="multipart-файл сверяется по sha256 с сохранёнными подписями.",
    response_model=VerifyResult,
)
async def verify_signature_file(
    session: DbSession,
    _: ConsentedUser,
    file: Annotated[UploadFile, File()],
) -> VerifyResult:
    body = bytearray()
    while chunk := await file.read(1024 * 1024):
        body.extend(chunk)
        if len(body) > _MAX_VERIFY_UPLOAD_BYTES:
            raise ValidationError("Файл слишком большой для проверки")
    result = await VerifyService(session).verify_by_file(bytes(body))
    return VerifyResult(**result)


# --- Соглашения об ЭДО -------------------------------------------------------


@edm_agreements_router.get(
    "",
    summary="Список соглашений об ЭДО",
    response_model=EdmAgreementListResponse,
)
async def list_edm_agreements(
    session: DbSession,
    _: Annotated[Principal, Depends(require_permission(Permission.EDM_READ))],
    party_type: str | None = None,
    party_id: uuid.UUID | None = None,
) -> EdmAgreementListResponse:
    stmt = EdmAgreementService(session).list_query(party_type=party_type, party_id=party_id)
    rows = (await session.execute(stmt.limit(100))).scalars().all()
    return EdmAgreementListResponse(items=[EdmAgreementOut.model_validate(r) for r in rows])


@edm_agreements_router.post(
    "",
    summary="Оформить соглашение об ЭДО",
    description="Без действующей записи внешняя подпись блокируется (dop.md §10.1). Роль: ADMIN.",
    response_model=EdmAgreementOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_edm_agreement(
    payload: EdmAgreementCreateRequest,
    session: DbSession,
    principal: Annotated[Principal, Depends(require_permission(Permission.EDM_ADMIN))],
) -> EdmAgreementOut:
    agreement = await EdmAgreementService(session).create(principal, payload)
    return EdmAgreementOut.model_validate(agreement)


@edm_agreements_router.post(
    "/{agreement_id}/revoke",
    summary="Отозвать соглашение",
    response_model=EdmAgreementOut,
)
async def revoke_edm_agreement(
    payload: EdmAgreementRevokeRequest,
    session: DbSession,
    _: Annotated[Principal, Depends(require_permission(Permission.EDM_ADMIN))],
    agreement_id: Annotated[uuid.UUID, Path()],
) -> EdmAgreementOut:
    service = EdmAgreementService(session)
    agreement = await service.get_or_404(agreement_id)
    agreement = await service.revoke(agreement, reason=payload.reason)
    return EdmAgreementOut.model_validate(agreement)


async def _card(
    service: SignatureDocumentService,
    document,
    *,
    revealed_tokens: dict[uuid.UUID, str] | None = None,
) -> SignatureDocumentOut:
    requests = await service.list_requests(document.id)
    signatures = (await service.signatures_by_document([document.id])).get(document.id)
    return _document_out(document, requests, revealed_tokens=revealed_tokens, signatures=signatures)


def _internal_file_url(request_id: uuid.UUID) -> str:
    return f"{get_settings().api_prefix}/signature-requests/{request_id}/file"


def _sign_url(token: str) -> str:
    # Ссылка ведёт на страницу веб-клиента (SPA `/sign/{token}`), а не на JSON-ручку API.
    return f"{get_settings().base_url.rstrip('/')}/sign/{token}"


def _document_out(
    document,
    requests,
    *,
    revealed_tokens: dict[uuid.UUID, str] | None = None,
    signatures=None,
) -> SignatureDocumentOut:
    out = SignatureDocumentOut.model_validate(document)
    revealed_tokens = revealed_tokens or {}
    outs = []
    for request in requests:
        request_out = SignatureRequestOut.model_validate(request)
        token = revealed_tokens.get(request.id)
        if token:
            request_out.sign_url = _sign_url(token)
        outs.append(request_out)
    out.requests = outs
    out.signatures = [SignatureBrief.model_validate(sig) for sig in signatures or []]
    return out
