"""Схемы модуля ПЭП (dop.md §10.10, spec.txt §6, §5.12).

`SignerSpec` — общий формат подписанта что для конструктора workflow (DSL
`request_signature`, `app.modules.workflow.dsl`), что для ручного создания
документа через API: ровно один из `role`/`user_id`/`contact_id`/
`contact_role` должен быть задан, резолвинг — в `SignatureDocumentService`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

SigningOrderLiteral = Literal["sequential", "parallel"]
DocTypeLiteral = Literal["kp", "act", "consent", "erasure_act", "offer", "custom"]
EntityTypeLiteral = Literal["deal", "erasure_request", "report_job"]
PartyTypeLiteral = Literal["organization", "contact", "user"]
ConclusionMethodLiteral = Literal["paper", "ukep", "offer_acceptance", "employment"]

# --- Подписанты --------------------------------------------------------------


class SignerSpec(BaseModel):
    role: str | None = None
    user_id: uuid.UUID | None = None
    contact_id: uuid.UUID | None = None
    contact_role: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> SignerSpec:
        provided = [
            value is not None
            for value in (self.role, self.user_id, self.contact_id, self.contact_role)
        ]
        if sum(provided) != 1:
            raise ValueError(
                "Нужно указать ровно одно из: role, user_id, contact_id, contact_role"
            )
        return self


# --- Соглашения об ЭДО --------------------------------------------------------


class EdmAgreementCreateRequest(BaseModel):
    party_type: PartyTypeLiteral
    party_id: uuid.UUID
    agreement_number: NonEmptyStr | None = Field(default=None, max_length=64)
    agreement_file_id: uuid.UUID | None = None
    conclusion_method: ConclusionMethodLiteral
    signed_at: dt.datetime | None = None
    valid_from: dt.date | None = None
    valid_to: dt.date | None = None


class EdmAgreementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    party_type: str
    party_id: uuid.UUID
    agreement_number: str | None
    agreement_file_id: uuid.UUID | None
    conclusion_method: str
    signed_at: dt.datetime | None
    valid_from: dt.date | None
    valid_to: dt.date | None
    status: str
    revoked_at: dt.datetime | None
    revoke_reason: str | None
    created_by: uuid.UUID | None
    created_at: dt.datetime


class EdmAgreementListResponse(BaseModel):
    items: list[EdmAgreementOut]
    next_cursor: str | None = None


class EdmAgreementRevokeRequest(BaseModel):
    reason: NonEmptyStr


# --- Шаблоны (только чтение через API — управление сиды/БД, см. память спринта) --


class SignatureTemplateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    doc_type: str
    default_deadline_days: int
    required_signer_roles: list[dict]
    is_active: bool
    version: int


class SignatureTemplateListResponse(BaseModel):
    items: list[SignatureTemplateOut]


# --- Документы -----------------------------------------------------------------


class SignatureDocumentCreateRequest(BaseModel):
    doc_type: DocTypeLiteral
    title: NonEmptyStr = Field(max_length=255)
    entity_type: EntityTypeLiteral
    entity_id: uuid.UUID
    template_code: NonEmptyStr | None = None
    file_id: uuid.UUID | None = None
    signers: list[SignerSpec] = Field(min_length=1)
    signing_order: SigningOrderLiteral = "sequential"
    deadline_days: int | None = Field(default=None, gt=0, le=365)

    @model_validator(mode="after")
    def _exactly_one_source(self) -> SignatureDocumentCreateRequest:
        if (self.template_code is None) == (self.file_id is None):
            raise ValueError("Нужно указать ровно одно из: template_code, file_id")
        return self


class SignatureRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_id: uuid.UUID
    signer_type: str
    signer_user_id: uuid.UUID | None
    signer_contact_id: uuid.UUID | None
    signer_role_code: str | None
    signer_name_snapshot: str
    signer_identifier_masked: str | None
    sign_order: int
    status: str
    sent_at: dt.datetime | None
    viewed_at: dt.datetime | None
    decided_at: dt.datetime | None
    reject_reason: str | None
    created_at: dt.datetime
    # Заполняется только в ответе `/send`, который активировал этот запрос
    # (dop.md §10.4 фаза 2 п.6): в БД хранится только sha256 токена, поэтому
    # это единственный момент, когда сырую ссылку вообще можно отдать —
    # ровно тот же приём, что `issue_invite` в identity (раздел 4.1).
    sign_url: str | None = None


class SignatureDocumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    doc_type: str
    template_code: str | None
    title: str
    entity_type: str
    entity_id: uuid.UUID
    content_hash: str
    signing_order: str
    status: str
    deadline_at: dt.datetime | None
    created_by: uuid.UUID | None
    created_at: dt.datetime
    completed_at: dt.datetime | None
    void_reason: str | None
    requests: list[SignatureRequestOut] = Field(default_factory=list)


class SignatureDocumentListResponse(BaseModel):
    items: list[SignatureDocumentOut]
    next_cursor: str | None = None


class VoidRequest(BaseModel):
    reason: NonEmptyStr


class DownloadUrlOut(BaseModel):
    """Презайн-ссылка, а не проксирование байтов через API (раздел 9) —
    та же форма ответа, что `files.schemas.DownloadUrlResponse`."""

    download_url: str
    expires_at: dt.datetime


# --- Страница подписания (внутренняя и публичная используют одну форму) --------


class SigningDocumentPreview(BaseModel):
    id: uuid.UUID
    title: str
    doc_type: str
    content_hash: str
    deadline_at: dt.datetime | None
    status: str
    preview_url: str


class SigningSignerPreview(BaseModel):
    name: str
    sign_order: int
    status: str
    is_me: bool


class SigningPageOut(BaseModel):
    document: SigningDocumentPreview
    signers: list[SigningSignerPreview]
    my_request_id: uuid.UUID
    my_status: str
    agreement_text: str


class ChallengeResponse(BaseModel):
    channel: str
    sent_to_masked: str
    expires_in_seconds: int
    # Только в dev/demo: в закрытом контуре нет реального SMS/telegram-шлюза
    # (см. docstring `SignatureOtpService.challenge`), поэтому в проде это
    # поле всегда `None` — подписание кодом, который некуда доставить,
    # честно недоступно, а не тихо расходится с реальностью.
    debug_code: str | None = None


class SignRequest(BaseModel):
    otp: Annotated[str, StringConstraints(strip_whitespace=True, min_length=4, max_length=8)]


class RejectRequest(BaseModel):
    reason: NonEmptyStr


class SignatureOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    request_id: uuid.UUID
    document_id: uuid.UUID
    content_hash: str
    method: str
    signer_display: str
    algorithm: str
    signed_at: dt.datetime
    is_disputed: bool
    hash: str


# --- Проверка подписи ----------------------------------------------------------

VerifyStatusLiteral = Literal[
    "valid", "disputed", "void", "hash_mismatch", "not_found"
]


class VerifyResult(BaseModel):
    status: VerifyStatusLiteral
    signature_id: uuid.UUID | None = None
    signer_display: str | None = None
    signed_at: dt.datetime | None = None
    document_hash: str | None = None
    method: str | None = None
    is_disputed: bool = False
