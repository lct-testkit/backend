"""Модели ПЭП (простая электронная подпись, dop.md §10.9, spec.txt §5.12).

Правовая рамка — 63-ФЗ, ст. 5/9 (dop.md §10.1): ПЭП значима только вместе с
доказательствами (`evidence`) и, для внешних контрагентов, действующим
соглашением об ЭДО (`edm_agreements`). Ключевые инварианты, ради которых так
устроена схема:

* **`signature_documents.content_hash` — якорь.** Подпись привязывается к
  хэшу файла, а не к его имени или ссылке: подмена документа между отправкой
  и подписанием обнаруживается пересчётом хэша при `seal()` (dop.md §10.4 п.14).
* **`signatures` — append-only.** Запись переживает обезличивание подписанта
  (`signer_display` — снимок, не FK на живые ПДн) и не может быть тихо
  вырезана: тот же приём hash-chain (`prev_hash`/`hash`), что и `audit_log`
  (см. `app.modules.audit.models`), плюс отдельный BEFORE UPDATE/DELETE
  триггер в миграции — единственное разрешённое изменение поля `is_disputed`
  проверяется на уровне БД, а не только в сервисном слое.
* **`key_version`** на `signatures` — осознанное расширение сверх терсе-списка
  полей из dop.md §10.9: без него ротация `SIGNATURE_SERVER_SECRET`
  (dop.md §10.11) сделала бы старые подписи непроверяемыми. Тот же приём,
  что `Workflow.published_graph` в `app.modules.workflow.models`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, IpAddressType, TimestampMixin, UuidPkMixin


class EdmPartyType(StrEnum):
    ORGANIZATION = "organization"
    CONTACT = "contact"
    USER = "user"


class ConclusionMethod(StrEnum):
    PAPER = "paper"
    UKEP = "ukep"
    OFFER_ACCEPTANCE = "offer_acceptance"
    EMPLOYMENT = "employment"


class EdmAgreementStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"


class SignatureDocType(StrEnum):
    KP = "kp"
    ACT = "act"
    CONSENT = "consent"
    ERASURE_ACT = "erasure_act"
    OFFER = "offer"
    CUSTOM = "custom"


class SigningOrder(StrEnum):
    SEQUENTIAL = "sequential"
    PARALLEL = "parallel"


class SignatureDocumentStatus(StrEnum):
    DRAFT = "draft"
    PENDING = "pending"
    PARTIALLY_SIGNED = "partially_signed"
    SIGNED = "signed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    VOID = "void"
    BLOCKED_NO_AGREEMENT = "blocked_no_agreement"


#: Статусы, которые ещё «в работе» — на них бьёт индекс для sweep дедлайнов
#: (dop.md §10.9: `(status, deadline_at) WHERE status IN (...)`).
OPEN_DOCUMENT_STATUSES = frozenset(
    {SignatureDocumentStatus.PENDING, SignatureDocumentStatus.PARTIALLY_SIGNED}
)


class SignerType(StrEnum):
    INTERNAL = "internal"
    EXTERNAL = "external"


class SignatureRequestStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    VIEWED = "viewed"
    SIGNED = "signed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    LOCKED = "locked"
    VOID = "void"


#: Статусы запроса, которые ещё можно подписать/отклонить.
OPEN_REQUEST_STATUSES = frozenset(
    {
        SignatureRequestStatus.PENDING,
        SignatureRequestStatus.SENT,
        SignatureRequestStatus.VIEWED,
    }
)

OTP_CHANNELS = frozenset({"sms", "email", "telegram"})


class SignatureMethod(StrEnum):
    PEP_OTP = "pep_otp"
    PEP_SESSION = "pep_session"
    UNEP = "unep"
    UKEP = "ukep"


class EdmAgreement(UuidPkMixin, TimestampMixin, Base):
    """Соглашение об ЭДО (dop.md §10.1, §10.9). Без действующей записи внешняя
    подпись блокируется статусом `blocked_no_agreement` — это не деталь
    реализации, а прямое требование ст. 9 63-ФЗ, поэтому проверка идёт на
    уровне сервиса при отправке документа, а не только здесь."""

    __tablename__ = "edm_agreements"
    __table_args__ = (
        Index("ix_edm_agreements_party", "party_type", "party_id", "status"),
        CheckConstraint(
            "party_type IN ('organization','contact','user')",
            name="edm_agreements_party_type_valid",
        ),
        CheckConstraint(
            "conclusion_method IN ('paper','ukep','offer_acceptance','employment')",
            name="edm_agreements_conclusion_method_valid",
        ),
        CheckConstraint(
            "status IN ('active','expired','revoked')", name="edm_agreements_status_valid"
        ),
        CheckConstraint(
            "status <> 'revoked' OR revoked_at IS NOT NULL",
            name="edm_agreements_revoked_at_present",
        ),
    )

    party_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # Без FK: сторона может быть организацией, контактом или пользователем —
    # тот же полиморфный приём, что `attachments.entity_id` (раздел 9).
    party_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    agreement_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    agreement_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("files.id", ondelete="RESTRICT"), nullable=True
    )
    conclusion_method: Mapped[str] = mapped_column(String(24), nullable=False)
    signed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_from: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )


class SignatureTemplate(UuidPkMixin, TimestampMixin, Base):
    """Шаблон документа (dop.md §10.9). `body_template` — Jinja2 → HTML,
    рендерится в `app.modules.signing.rendering`."""

    __tablename__ = "signature_templates"
    __table_args__ = (
        CheckConstraint(
            "doc_type IN ('kp','act','consent','erasure_act','offer','custom')",
            name="signature_templates_doc_type_valid",
        ),
        # Единственный формат, который реально умеет рендерить сервис
        # (`app.modules.signing.rendering`, xhtml2pdf) — столбец существует
        # для соответствия модели данных dop.md §10.9, но разрешать значения
        # без обработчика значило бы врать пользователю в схеме.
        CheckConstraint("output_format IN ('pdf')", name="signature_templates_output_format_valid"),
    )

    code: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    doc_type: Mapped[str] = mapped_column(String(16), nullable=False)
    body_template: Mapped[str] = mapped_column(Text, nullable=False)
    # Список вида [{"role": "HEAD"}, {"contact_role": "decision_maker"}] —
    # тот же формат, что `signers` в DSL-действии `request_signature`
    # (`app.modules.workflow.dsl`), используется как значение по умолчанию,
    # когда документ создаётся из шаблона без явного списка подписантов.
    required_signer_roles: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    default_deadline_days: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("7")
    )
    output_format: Mapped[str] = mapped_column(String(8), nullable=False, server_default="pdf")
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))


class SignatureDocument(UuidPkMixin, Base):
    __tablename__ = "signature_documents"
    __table_args__ = (
        Index("ix_signature_documents_entity", "entity_type", "entity_id"),
        Index(
            "ix_signature_documents_status_deadline",
            "status",
            "deadline_at",
            postgresql_where=text("status IN ('pending','partially_signed')"),
        ),
        CheckConstraint(
            "doc_type IN ('kp','act','consent','erasure_act','offer','custom')",
            name="signature_documents_doc_type_valid",
        ),
        CheckConstraint(
            "signing_order IN ('sequential','parallel')",
            name="signature_documents_signing_order_valid",
        ),
        CheckConstraint(
            "status IN ('draft','pending','partially_signed','signed','rejected',"
            "'expired','void','blocked_no_agreement')",
            name="signature_documents_status_valid",
        ),
        CheckConstraint(
            "status <> 'void' OR void_reason IS NOT NULL", name="signature_documents_void_reason"
        ),
        CheckConstraint(
            "on_expired IS NULL OR on_expired IN ('notify_initiator','void','previous_status')",
            name="signature_documents_on_expired_valid",
        ),
    )

    doc_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # Код шаблона, а не FK: шаблон версионируется отдельно и мог измениться
    # или быть деактивирован после того, как документ уже создан — тот же
    # принцип, что `workflow_transitions.actions[].template` (раздел 8 DSL).
    template_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # Полиморфная привязка (deal, erasure_request, report_job, ...) — без FK,
    # тот же приём, что `attachments.entity_id`.
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("files.id", ondelete="RESTRICT"), nullable=False
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    signed_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("files.id", ondelete="RESTRICT"), nullable=True
    )
    protocol_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("files.id", ondelete="RESTRICT"), nullable=True
    )
    manifest: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    signing_order: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="sequential"
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="draft")
    deadline_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False, index=True
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    void_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    voided_by: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    # Осознанное расширение сверх терсе-таблицы dop.md §10.9: DSL-действие
    # `request_signature` (`app.modules.workflow.dsl`) валидирует `on_rejected`
    # и `on_expired` на переходе, но их некуда положить, кроме как на сам
    # документ — иначе к моменту решения подписанта (часы или дни спустя)
    # правило было бы утеряно. `NULL` для документов, созданных не из
    # перехода воронки (`POST /api/signature-documents` напрямую): решение
    # подписанта тогда меняет только `deals.signature_status`, без
    # автоматического отката статуса — безопасный вариант по умолчанию.
    on_rejected: Mapped[str | None] = mapped_column(String(64), nullable=True)
    on_expired: Mapped[str | None] = mapped_column(String(32), nullable=True)


class SignatureRequest(UuidPkMixin, Base):
    """Один подписант = одна запись (dop.md §10.9)."""

    __tablename__ = "signature_requests"
    __table_args__ = (
        Index("ix_signature_requests_document", "document_id", "sign_order"),
        Index("ix_signature_requests_user", "signer_user_id", "status"),
        Index("ix_signature_requests_contact", "signer_contact_id", "status"),
        Index(
            "uq_signature_requests_token_hash",
            "access_token_hash",
            unique=True,
            postgresql_where=text("access_token_hash IS NOT NULL"),
        ),
        CheckConstraint(
            "signer_type IN ('internal','external')", name="signature_requests_signer_type_valid"
        ),
        CheckConstraint(
            "status IN ('pending','sent','viewed','signed','rejected','expired','locked','void')",
            name="signature_requests_status_valid",
        ),
        CheckConstraint(
            "(signer_type = 'internal' AND signer_user_id IS NOT NULL) OR "
            "(signer_type = 'external' AND signer_contact_id IS NOT NULL)",
            name="signature_requests_signer_ref_matches_type",
        ),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("signature_documents.id", ondelete="CASCADE"), nullable=False
    )
    signer_type: Mapped[str] = mapped_column(String(16), nullable=False)
    signer_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    signer_contact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="RESTRICT"), nullable=True
    )
    # Роль/роль-в-сделке, которой был адресован запрос (`{"role": "HEAD"}`) —
    # нужна offboarding'у (identity §4.7 / dop §10.7): «если уходящий был
    # подписантом по роли — запрос переадресуется новому руководителю».
    signer_role_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    signer_name_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    signer_identifier_masked: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sign_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    access_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    token_expires_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    viewed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    edm_agreement_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("edm_agreements.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class SignatureOtpCode(UuidPkMixin, Base):
    """dop.md §10.4 п.11-13. Ретеншен 30 дней (`signature.clean_otp`,
    `app.modules.signing.tasks`) — коды сами по себе не доказательство,
    доказательство лежит в `signatures.evidence` (dop.md §10.9)."""

    __tablename__ = "signature_otp_codes"
    __table_args__ = (
        Index("ix_signature_otp_codes_request", "request_id", "created_at"),
        CheckConstraint(
            "channel IN ('sms','email','telegram')", name="signature_otp_channel_valid"
        ),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("signature_requests.id", ondelete="CASCADE"), nullable=False
    )
    # bcrypt инкапсулирует свою соль в самой строке хэша — `code_hash`
    # самодостаточен для `bcrypt.checkpw`. `salt` хранится отдельно только
    # ради буквального соответствия модели данных dop.md §10.9 и не участвует
    # в проверке — тот же байт-в-байт `bcrypt.gensalt()`, который был передан
    # в `hashpw` при создании `code_hash`.
    code_hash: Mapped[str] = mapped_column(String(72), nullable=False)
    salt: Mapped[str] = mapped_column(String(32), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    sent_to_masked: Mapped[str] = mapped_column(String(64), nullable=False)
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("3")
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class Signature(UuidPkMixin, Base):
    """Append-only (dop.md §10.9). Неизменяемость — не только конвенция:
    миграция добавляет BEFORE UPDATE/DELETE триггер, который разрешает менять
    исключительно `is_disputed` (компрометация ключа, dop.md §10.7) и
    запрещает DELETE безусловно — тот же уровень строгости, что `audit_log`
    (`app.modules.audit.models`), но с триггером `FOR EACH ROW`, а не
    `FOR EACH STATEMENT`: нужно сравнить конкретные старое/новое значения.
    """

    __tablename__ = "signatures"
    __table_args__ = (
        Index("ix_signatures_document", "document_id", "created_at"),
        Index("ix_signatures_request", "request_id"),
        CheckConstraint(
            "method IN ('pep_otp','pep_session','unep','ukep')", name="signatures_method_valid"
        ),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("signature_requests.id", ondelete="RESTRICT"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("signature_documents.id", ondelete="RESTRICT"), nullable=False
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    method: Mapped[str] = mapped_column(String(16), nullable=False)
    # Переживает обезличивание подписанта (dop.md §10.7): ФИО на момент
    # подписания, не FK на живой профиль.
    signer_display: Mapped[str] = mapped_column(String(255), nullable=False)
    signature_value: Mapped[str] = mapped_column(Text, nullable=False)
    algorithm: Mapped[str] = mapped_column(String(32), nullable=False, server_default="HMAC-SHA256")
    # Версия `SIGNATURE_SERVER_SECRET` на момент подписания (dop.md §10.11) —
    # см. docstring модуля.
    key_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    signed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    time_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    time_drift_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ip: Mapped[str | None] = mapped_column(IpAddressType(), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_disputed: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    prev_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False, index=True
    )
