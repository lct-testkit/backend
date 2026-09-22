"""Сервисный слой ПЭП (dop.md §10, spec.txt §5.12/§6/§13).

Три вещи стоит понимать перед тем, как трогать этот файл:

* **`SigningService` — тот же контракт, что вызывает identity** (смена/сброс
  пароля, увольнение, блокер обезличивания, dop §10.7). До этого спринта был
  зарегистрирован `NullSigningService`; здесь он заменяется на
  `RealSigningService` без изменения сигнатур — `app/modules/identity/
  admin_service.py` и `router_me.py` не требуют правок. Тот же класс несёт и
  новый метод `request_signature_for_deal`, которым пользуется
  `app/modules/crm/service.py._run_actions` для действия `request_signature`
  (раздел 8 DSL) — единая точка входа, а не два разных сервиса.
* **`crm.models` импортируется напрямую, `crm.service` — никогда.** Документ
  на подпись обязан уметь откатывать статус сделки при отклонении/истечении
  срока (dop §10.6 `on_rejected`/`on_expired`) — то есть писать
  `DealStatusHistory`/`DealEvent`/`DealComment` по тому же протоколу, что
  `DealStatusService.migrate_batch` в `crm/service.py`. Заново вызывать
  `DealService.transition()` нельзя: это ручка для человека с проверкой прав
  и guard-условий, а откат при отклонении подписи — системное действие без
  актора. Импорт `crm.service` создал бы цикл (crm вызывает signing для
  создания запроса, signing вызывал бы crm для отката) — вместо этого
  signing пишет в модели `crm` напрямую, ровно как сам `crm/service.py`
  напрямую пишет в модели `catalog`.
* **`signatures` неизменяема на уровне БД, а не только по конвенции.**
  Миграция 0007 добавляет `BEFORE UPDATE OR DELETE` триггер, который
  разрешает менять только `is_disputed` — `mark_disputed_since` ниже делает
  обычный `UPDATE`, и это единственное легитимное отклонение, которое
  триггер пропустит.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import bcrypt
import structlog
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import (
    AppError,
    ErrorCode,
    FieldError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from app.core.ids import uuid7
from app.core.masking import mask_email, mask_phone
from app.core.rate_limit import enforce as rate_limit_enforce
from app.core.redis_client import distributed_lock
from app.core.security import Principal
from app.core.storage import (
    download_object_bytes,
    ensure_bucket,
    generate_presigned_get,
    inspect_object,
    upload_object_bytes,
)
from app.modules.audit.actions import AuditAction
from app.modules.audit.models import AuditResult
from app.modules.audit.service import AuditService
from app.modules.catalog.models import Contact, Organization
from app.modules.crm.models import (
    Deal,
    DealComment,
    DealEvent,
    DealEventType,
    DealStatusHistory,
    HistoryReason,
)
from app.modules.crm.models import SignatureStatus as DealSignatureStatus
from app.modules.files.models import Attachment, AttachmentCategory, File, FileStatus
from app.modules.identity.models import User
from app.modules.integration.service import get_outbox_service
from app.modules.notification.service import NotificationPriority, get_notification_service
from app.modules.signing.models import (
    OPEN_DOCUMENT_STATUSES,
    OPEN_REQUEST_STATUSES,
    EdmAgreement,
    EdmAgreementStatus,
    Signature,
    SignatureDocument,
    SignatureDocumentStatus,
    SignatureMethod,
    SignatureOtpCode,
    SignatureRequest,
    SignatureRequestStatus,
    SignatureTemplate,
    SignerType,
)
from app.modules.signing.rendering import (
    apply_signature_stamp,
    render_protocol_pdf,
    render_signature_document,
)
from app.modules.signing.schemas import (
    SigningDocumentPreview,
    SigningPageOut,
    SigningSignerPreview,
)
from app.modules.signing.sms_gateway import send_sms
from app.modules.signing.trusted_time import get_trusted_time
from app.modules.workflow.models import WorkflowStatus

logger = structlog.get_logger(__name__)

# --- Уведомления (шаблоны для LoggingNotificationService/будущего диспетчера) --
TPL_SIGNATURE_REQUESTED = "SIGNATURE_REQUESTED"
TPL_SIGNATURE_SIGNED = "SIGNATURE_DOCUMENT_SIGNED"
TPL_SIGNATURE_REJECTED = "SIGNATURE_DOCUMENT_REJECTED"
TPL_SIGNATURE_EXPIRED = "SIGNATURE_DOCUMENT_EXPIRED"
TPL_SIGNATURE_OTP_LOCKED = "SIGNATURE_OTP_LOCKED"
TPL_EDM_AGREEMENT_MISSING = "EDM_AGREEMENT_MISSING"

# Причины аннулирования из dop §10.7 — используются identity.
VOID_REASON_CREDENTIALS_CHANGED = "credentials_changed"
VOID_REASON_KEY_COMPROMISED = "key_compromised"
VOID_REASON_OFFBOARDED = "signer_offboarded"

_SIG_CHAIN_LOCK_ID = 0x5349_474E  # "SIGN"
GENESIS_HASH = "0" * 64

# Легальный текст согласия — dop.md §10.4 фаза 3 п.8 (63-ФЗ, ст.5/9).
AGREEMENT_TEXT = (
    "Нажимая «Подписать», вы применяете простую электронную подпись в "
    "соответствии со ст. 5, 9 Федерального закона от 06.04.2011 № 63-ФЗ "
    "«Об электронной подписи». Ввод одноразового кода, направленного на "
    "указанный канал связи, подтверждает, что подпись поставлена именно "
    "вами. Отклонить документ можно с указанием причины."
)


# =============================================================================
# Соглашения об ЭДО (dop.md §10.1, §10.9)
# =============================================================================


class EdmAgreementService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    async def _ensure_party_exists(self, party_type: str, party_id: uuid.UUID) -> None:
        model = {"organization": Organization, "contact": Contact, "user": User}[party_type]
        found = await self._session.get(model, party_id)
        if found is None or getattr(found, "deleted_at", None) is not None:
            raise NotFoundError("Сторона соглашения", party_id)

    async def create(self, principal: Principal, payload: Any) -> EdmAgreement:
        await self._ensure_party_exists(payload.party_type, payload.party_id)
        agreement = EdmAgreement(
            party_type=payload.party_type,
            party_id=payload.party_id,
            agreement_number=payload.agreement_number,
            agreement_file_id=payload.agreement_file_id,
            conclusion_method=payload.conclusion_method,
            signed_at=payload.signed_at,
            valid_from=payload.valid_from,
            valid_to=payload.valid_to,
            status=EdmAgreementStatus.ACTIVE.value,
            created_by=principal.user_id,
        )
        self._session.add(agreement)
        await self._session.flush()
        await self._audit.record(
            AuditAction.EDM_AGREEMENT_CREATED,
            entity_type="edm_agreement",
            entity_id=agreement.id,
            changes={
                "party_type": {"old": None, "new": agreement.party_type},
                "party_id": {"old": None, "new": str(agreement.party_id)},
                "conclusion_method": {"old": None, "new": agreement.conclusion_method},
            },
        )
        return agreement

    async def get_or_404(self, agreement_id: uuid.UUID) -> EdmAgreement:
        agreement = await self._session.get(EdmAgreement, agreement_id)
        if agreement is None:
            raise NotFoundError("Соглашение об ЭДО", agreement_id)
        return agreement

    async def revoke(self, agreement: EdmAgreement, *, reason: str) -> EdmAgreement:
        if agreement.status == EdmAgreementStatus.REVOKED.value:
            raise AppError(ErrorCode.VALIDATION, "Соглашение уже отозвано")
        agreement.status = EdmAgreementStatus.REVOKED.value
        agreement.revoked_at = dt.datetime.now(dt.UTC)
        agreement.revoke_reason = reason
        await self._session.flush()
        await self._audit.record(
            AuditAction.EDM_AGREEMENT_REVOKED,
            entity_type="edm_agreement",
            entity_id=agreement.id,
            changes={
                "status": {"old": "active", "new": "revoked"},
                "reason": {"old": None, "new": reason},
            },
        )
        return agreement

    def list_query(self, *, party_type: str | None, party_id: uuid.UUID | None):
        stmt = select(EdmAgreement).order_by(EdmAgreement.created_at.desc(), EdmAgreement.id.desc())
        if party_type:
            stmt = stmt.where(EdmAgreement.party_type == party_type)
        if party_id:
            stmt = stmt.where(EdmAgreement.party_id == party_id)
        return stmt

    async def find_active_for_contact(self, contact: Contact) -> EdmAgreement | None:
        """Соглашение может быть оформлено на самого контакта (B2C-физлицо)
        или на его организацию (B2B — представитель подписывает под
        соглашением вуза), dop.md §10.9 допускает оба `party_type`."""
        today = dt.date.today()
        candidates: list[tuple[str, uuid.UUID]] = [("contact", contact.id)]
        if contact.organization_id:
            candidates.append(("organization", contact.organization_id))
        for party_type, party_id in candidates:
            stmt = select(EdmAgreement).where(
                EdmAgreement.party_type == party_type,
                EdmAgreement.party_id == party_id,
                EdmAgreement.status == EdmAgreementStatus.ACTIVE.value,
                (EdmAgreement.valid_from.is_(None)) | (EdmAgreement.valid_from <= today),
                (EdmAgreement.valid_to.is_(None)) | (EdmAgreement.valid_to >= today),
            )
            found = (await self._session.execute(stmt)).scalars().first()
            if found is not None:
                return found
        return None


# =============================================================================
# Шаблоны — только чтение через API, управление сидами/БД (см. память спринта)
# =============================================================================


class SignatureTemplateService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_active(self) -> list[SignatureTemplate]:
        stmt = select(SignatureTemplate).where(SignatureTemplate.is_active.is_(True)).order_by(
            SignatureTemplate.code
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_by_code(self, code: str) -> SignatureTemplate:
        stmt = select(SignatureTemplate).where(
            SignatureTemplate.code == code, SignatureTemplate.is_active.is_(True)
        )
        template = (await self._session.execute(stmt)).scalars().first()
        if template is None:
            raise NotFoundError("Шаблон документа", code)
        return template


# =============================================================================
# Контекст рендеринга шаблона по сущности
# =============================================================================


async def _build_entity_context(
    session: AsyncSession, *, entity_type: str, entity_id: uuid.UUID
) -> dict[str, Any]:
    """Данные сущности для Jinja2 (dop.md §10.4 фаза 1 п.1: «в шаблон
    подставляются данные сделки»). Полностью реализовано для `deal` — на
    сегодня единственная сущность, реально порождающая документы (seed
    `kp_approval`, `app/modules/workflow/seed.py`). Для `erasure_request`/
    `report_job` конвейеры, которые создавали бы для них документы, ещё не
    построены (обезличивание и генерация отчётов — отдельные, не сделанные в
    этом спринте задачи), поэтому контекст для них — минимальный, но не
    падающий: когда эти конвейеры появятся, `POST /api/signature-documents`
    для них уже будет работать, просто с бедным контентом шаблона, а не с
    ошибкой 500.
    """
    now = dt.datetime.now(dt.UTC)
    base = {"today": now.date().isoformat(), "now": now.isoformat(), "entity_id": str(entity_id)}
    if entity_type != "deal":
        return base

    deal = await session.get(Deal, entity_id)
    if deal is None:
        return base
    org = await session.get(Organization, deal.organization_id) if deal.organization_id else None
    contact = await session.get(Contact, deal.contact_id) if deal.contact_id else None
    owner = await session.get(User, deal.owner_id)
    return {
        **base,
        "deal_number": deal.number,
        "deal_title": deal.title,
        "deal_type": deal.deal_type,
        "amount": str(deal.amount) if deal.amount is not None else "",
        "currency": deal.currency,
        "students_planned": deal.students_planned,
        "organization_name": org.name if org else "",
        "organization_inn": org.inn if org else "",
        "organization_address": org.legal_address if org else "",
        "contact_name": (
            f"{contact.last_name} {contact.first_name} {contact.middle_name or ''}".strip()
            if contact
            else ""
        ),
        "owner_name": owner.effective_name if owner else "",
    }


# =============================================================================
# Резолвинг подписантов
# =============================================================================


@dataclass(slots=True)
class ResolvedSigner:
    signer_type: str
    user_id: uuid.UUID | None
    contact_id: uuid.UUID | None
    role_code: str | None
    name_snapshot: str
    identifier_masked: str | None
    phone: str | None
    email: str | None


async def _resolve_role_user(session: AsyncSession, deal: Deal | None, role: str) -> User | None:
    if deal is None:
        return None
    owner = await session.get(User, deal.owner_id)
    if owner is None:
        return None
    if owner.role == role:
        return owner
    if role == "HEAD" and owner.manager_id:
        return await session.get(User, owner.manager_id)
    return None


async def _resolve_contact_by_role(
    session: AsyncSession, deal: Deal | None, contact_role: str
) -> Contact | None:
    if deal is None:
        return None
    if deal.contact_id:
        contact = await session.get(Contact, deal.contact_id)
        if contact and (contact_role != "decision_maker" or contact.is_decision_maker):
            return contact
    if deal.organization_id and contact_role == "decision_maker":
        stmt = select(Contact).where(
            Contact.organization_id == deal.organization_id,
            Contact.is_decision_maker.is_(True),
            Contact.deleted_at.is_(None),
        )
        return (await session.execute(stmt)).scalars().first()
    return None


async def _resolve_signers(
    session: AsyncSession, *, signers_spec: list[Any], deal: Deal | None
) -> list[ResolvedSigner]:
    """`signers_spec` — список объектов/словарей с одним из ключей
    `role`/`user_id`/`contact_id`/`contact_role` (одинаковый формат что для
    ручного создания через API, что для DSL-действия `request_signature`,
    см. `app.modules.workflow.dsl._validate_request_signature`)."""
    resolved: list[ResolvedSigner] = []
    for spec in signers_spec:
        is_dict = isinstance(spec, dict)
        role = getattr(spec, "role", None) or (spec.get("role") if is_dict else None)
        user_id = getattr(spec, "user_id", None) or (spec.get("user_id") if is_dict else None)
        contact_id = getattr(spec, "contact_id", None) or (
            spec.get("contact_id") if is_dict else None
        )
        contact_role = getattr(spec, "contact_role", None) or (
            spec.get("contact_role") if is_dict else None
        )

        if user_id:
            user = await session.get(User, user_id)
            if user is None or user.deleted_at is not None:
                raise NotFoundError("Подписант (пользователь)", user_id)
            resolved.append(
                ResolvedSigner(
                    SignerType.INTERNAL.value, user.id, None, role, user.effective_name,
                    mask_phone(user.phone) or mask_email(user.email), user.phone, user.email,
                )
            )
            continue
        if contact_id:
            contact = await session.get(Contact, contact_id)
            if contact is None or contact.deleted_at is not None:
                raise NotFoundError("Подписант (контакт)", contact_id)
            resolved.append(_contact_to_signer(contact, None))
            continue
        if role:
            user = await _resolve_role_user(session, deal, role)
            if user is None:
                raise ValidationError(
                    f"Не удалось определить подписанта для роли {role!r}",
                    [FieldError(field="signers", reason=f"роль {role} не резолвится в сделке")],
                )
            resolved.append(
                ResolvedSigner(
                    SignerType.INTERNAL.value, user.id, None, role, user.effective_name,
                    mask_phone(user.phone) or mask_email(user.email), user.phone, user.email,
                )
            )
            continue
        if contact_role:
            contact = await _resolve_contact_by_role(session, deal, contact_role)
            if contact is None:
                raise ValidationError(
                    f"Не удалось определить подписанта для роли контакта {contact_role!r}",
                    [FieldError(field="signers", reason="контакт не найден")],
                )
            resolved.append(_contact_to_signer(contact, contact_role))
            continue
        raise ValidationError("Подписант не указан", [FieldError(field="signers", reason="пусто")])
    return resolved


def _contact_to_signer(contact: Contact, role_code: str | None) -> ResolvedSigner:
    name = f"{contact.last_name} {contact.first_name} {contact.middle_name or ''}".strip()
    return ResolvedSigner(
        SignerType.EXTERNAL.value, None, contact.id, role_code, name,
        mask_phone(contact.phone) or mask_email(contact.email), contact.phone, contact.email,
    )


# =============================================================================
# Документы
# =============================================================================


class SignatureDocumentService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)
        self._settings = get_settings()

    async def get_or_404(self, document_id: uuid.UUID) -> SignatureDocument:
        document = await self._session.get(SignatureDocument, document_id)
        if document is None:
            raise NotFoundError("Документ на подпись", document_id)
        return document

    async def _check_entity_access(
        self, principal: Principal, entity_type: str, entity_id: uuid.UUID
    ) -> Deal | None:
        """Раздел 9/файлы уже решают этот вопрос для вложений тем же
        приёмом — локальный импорт внутри метода, не на уровне модуля, чтобы
        не тянуть `crm.service` в зависимости `signing` (см. docstring)."""
        if entity_type == "deal":
            from app.modules.crm.service import DealService

            return await DealService(self._session).get_or_404(entity_id, principal)
        if not principal.is_admin:
            raise ForbiddenError(f"Создание документов для {entity_type!r} доступно только ADMIN")
        return None

    async def list_for_entity(
        self, principal: Principal, entity_type: str, entity_id: uuid.UUID, limit: int = 50
    ) -> list[SignatureDocument]:
        """История документов сущности (в том числе аннулированные и просроченные)."""
        await self._check_entity_access(principal, entity_type, entity_id)
        stmt = (
            select(SignatureDocument)
            .where(
                SignatureDocument.entity_type == entity_type,
                SignatureDocument.entity_id == entity_id,
            )
            .order_by(SignatureDocument.created_at.desc())
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def ensure_access(self, principal: Principal, document: SignatureDocument) -> None:
        """Объектный уровень для `send`/`void` (раздел 3.2) — управление
        документом остаётся у тех, кто видит сделку целиком, а не у любого
        подписанта: иначе подписант мог бы аннулировать или переотправить
        документ, который ему не принадлежит."""
        await self._check_entity_access(principal, document.entity_type, document.entity_id)

    async def ensure_read_access(self, principal: Principal, document: SignatureDocument) -> None:
        """Объектный уровень для чтения карточки/протокола — шире, чем
        `ensure_access`: подписант имеет право видеть доказательство
        собственной подписи (протокол, dop.md §10.5) даже если сделка вне его
        скоупа по раздела 3.2 (например, HEAD подписывает по роли для
        сделки чужой команды). Живые подписанты определяются через
        `signature_requests`, а не через сам факт наличия подписи."""
        is_signer = await self._session.scalar(
            select(SignatureRequest.id).where(
                SignatureRequest.document_id == document.id,
                SignatureRequest.signer_user_id == principal.user_id,
            )
        )
        if is_signer is not None:
            return
        await self._check_entity_access(principal, document.entity_type, document.entity_id)

    async def create(self, principal: Principal, payload: Any) -> SignatureDocument:
        deal = await self._check_entity_access(principal, payload.entity_type, payload.entity_id)

        if payload.template_code:
            templates = SignatureTemplateService(self._session)
            template = await templates.get_by_code(payload.template_code)
            context = await _build_entity_context(
                self._session, entity_type=payload.entity_type, entity_id=payload.entity_id
            )
            pdf_bytes = render_signature_document(template.body_template, context)
            deadline_days = payload.deadline_days or template.default_deadline_days
            file_id = await self._store_generated_pdf(
                pdf_bytes, filename=f"{payload.title}.pdf", uploaded_by=principal.user_id
            )
            content_hash = hashlib.sha256(pdf_bytes).hexdigest()
        else:
            file = await self._session.get(File, payload.file_id)
            if file is None or file.deleted_at is not None:
                raise NotFoundError("Файл документа", payload.file_id)
            if file.status != FileStatus.READY.value:
                raise ValidationError("Файл ещё не прошёл проверку и не готов к использованию")
            if file.mime_type != "application/pdf":
                raise ValidationError(
                    "Документ на подпись должен быть PDF",
                    [FieldError(field="file_id", reason=f"mime_type={file.mime_type!r}")],
                )
            if not file.sha256:
                raise ValidationError("У файла не посчитан sha256 — повторите commit")
            file_id = file.id
            content_hash = file.sha256
            deadline_days = payload.deadline_days or 7

        now = dt.datetime.now(dt.UTC)
        document = SignatureDocument(
            doc_type=payload.doc_type,
            template_code=payload.template_code,
            title=payload.title,
            entity_type=payload.entity_type,
            entity_id=payload.entity_id,
            file_id=file_id,
            content_hash=content_hash,
            signing_order=payload.signing_order,
            status=SignatureDocumentStatus.DRAFT.value,
            deadline_at=now + dt.timedelta(days=deadline_days),
            created_by=principal.user_id,
        )
        self._session.add(document)
        await self._session.flush()

        resolved_signers = await _resolve_signers(
            self._session, signers_spec=payload.signers, deal=deal
        )
        await self._create_requests(document, resolved_signers)

        await self._audit.record(
            AuditAction.SIGNATURE_DOCUMENT_CREATED,
            entity_type="signature_document",
            entity_id=document.id,
            changes={
                "doc_type": {"old": None, "new": document.doc_type},
                "entity_type": {"old": None, "new": document.entity_type},
                "entity_id": {"old": None, "new": str(document.entity_id)},
                "signers": {"old": None, "new": len(resolved_signers)},
            },
        )
        return document

    async def create_from_workflow_action(
        self,
        *,
        deal: Deal,
        action: dict[str, Any],
        principal_id: uuid.UUID | None,
    ) -> SignatureDocument:
        """Вызывается `RealSigningService.request_signature_for_deal` для
        действия `request_signature` DSL-перехода (раздел 8) — `action` уже
        прошёл валидацию `app.modules.workflow.dsl._validate_request_signature`
        на публикации воронки."""
        template = await SignatureTemplateService(self._session).get_by_code(action["template"])
        context = await _build_entity_context(self._session, entity_type="deal", entity_id=deal.id)
        pdf_bytes = render_signature_document(template.body_template, context)
        content_hash = hashlib.sha256(pdf_bytes).hexdigest()
        file_id = await self._store_generated_pdf(
            pdf_bytes, filename=f"{template.code}-{deal.number}.pdf", uploaded_by=principal_id
        )

        deadline_days = action.get("deadline_days") or template.default_deadline_days
        now = dt.datetime.now(dt.UTC)
        document = SignatureDocument(
            doc_type=template.doc_type,
            template_code=template.code,
            title=f"{template.name}: {deal.title}",
            entity_type="deal",
            entity_id=deal.id,
            file_id=file_id,
            content_hash=content_hash,
            signing_order=action.get("order", "sequential"),
            status=SignatureDocumentStatus.DRAFT.value,
            deadline_at=now + dt.timedelta(days=deadline_days),
            created_by=principal_id,
            on_rejected=action.get("on_rejected"),
            on_expired=action.get("on_expired"),
        )
        self._session.add(document)
        await self._session.flush()

        resolved = await _resolve_signers(self._session, signers_spec=action["signers"], deal=deal)
        await self._create_requests(document, resolved)

        await self._audit.record(
            AuditAction.SIGNATURE_DOCUMENT_CREATED,
            entity_type="signature_document",
            entity_id=document.id,
            changes={
                "template": {"old": None, "new": template.code},
                "deal_id": {"old": None, "new": str(deal.id)},
            },
        )
        self._session.add(
            DealEvent(
                deal_id=deal.id,
                event_type=DealEventType.SIGNATURE_REQUESTED.value,
                actor_id=principal_id,
                payload={"document_id": str(document.id), "template": template.code},
            )
        )
        _, revealed = await self.send(document)
        if revealed:
            # Действие DSL — не HTTP-ответ, отдать ссылку вызывающей стороне
            # некому: внешний подписант из workflow-перехода — теоретический
            # случай (сиды используют только `{"role": "HEAD"}`), но фиксируем
            # честно, а не теряем токен молча (тот же принцип, что OTP-заглушка
            # в `SignatureRequestService.challenge`).
            logger.warning(
                "external_signer_token_undeliverable_from_workflow_action",
                document_id=str(document.id), request_ids=[str(k) for k in revealed],
            )
        return document

    async def _create_requests(
        self, document: SignatureDocument, resolved_signers: list[ResolvedSigner]
    ) -> list[SignatureRequest]:
        requests: list[SignatureRequest] = []
        for order, signer in enumerate(resolved_signers, start=1):
            request = SignatureRequest(
                document_id=document.id,
                signer_type=signer.signer_type,
                signer_user_id=signer.user_id,
                signer_contact_id=signer.contact_id,
                signer_role_code=signer.role_code,
                signer_name_snapshot=signer.name_snapshot,
                signer_identifier_masked=signer.identifier_masked,
                sign_order=order,
                status=SignatureRequestStatus.PENDING.value,
            )
            self._session.add(request)
            requests.append(request)
        await self._session.flush()
        return requests

    async def _store_generated_pdf(
        self, pdf_bytes: bytes, *, filename: str, uploaded_by: uuid.UUID | None
    ) -> uuid.UUID:
        await ensure_bucket(self._settings.s3_bucket_signatures)
        file_id = uuid7()
        storage_key = f"{file_id}/{filename}"
        await upload_object_bytes(
            bucket=self._settings.s3_bucket_signatures,
            key=storage_key,
            body=pdf_bytes,
            content_type="application/pdf",
        )
        file = File(
            id=file_id,
            storage_key=storage_key,
            bucket=self._settings.s3_bucket_signatures,
            original_filename=filename,
            mime_type="application/pdf",
            size_bytes=len(pdf_bytes),
            sha256=hashlib.sha256(pdf_bytes).hexdigest(),
            status=FileStatus.READY.value,
            uploaded_by=uploaded_by,
        )
        self._session.add(file)
        await self._session.flush()
        return file_id

    def _requests_query(self, document_id: uuid.UUID):
        return select(SignatureRequest).where(SignatureRequest.document_id == document_id).order_by(
            SignatureRequest.sign_order
        )

    async def list_requests(self, document_id: uuid.UUID) -> list[SignatureRequest]:
        rows = await self._session.execute(self._requests_query(document_id))
        return list(rows.scalars().all())

    async def send(
        self, document: SignatureDocument
    ) -> tuple[SignatureDocument, dict[uuid.UUID, str]]:
        """Возвращает документ и `{request_id: сырой_токен}` для внешних
        подписантов, активированных этим вызовом (см. docstring
        `_activate_turn`) — вызывающая сторона (роутер) решает, показывать
        ли ссылку в ответе."""
        # `blocked_no_agreement` — тоже валидный старт: dop.md §10.4 фаза 2
        # п.7 ждёт, что инициатор оформит соглашение и повторит отправку, а
        # не пересоздаст документ заново. Без этой ветки `blocked_no_agreement`
        # был бы тупиковым статусом без выхода.
        sendable = (
            SignatureDocumentStatus.DRAFT.value,
            SignatureDocumentStatus.BLOCKED_NO_AGREEMENT.value,
        )
        if document.status not in sendable:
            raise AppError(
                ErrorCode.DOCUMENT_NOT_SIGNABLE,
                "Отправить можно только черновик или заблокированный из-за отсутствия соглашения",
                extra={"status": document.status},
            )
        previous_status = document.status
        requests = await self.list_requests(document.id)

        for request in requests:
            if request.signer_type != SignerType.EXTERNAL.value:
                continue
            contact = await self._session.get(Contact, request.signer_contact_id)
            agreement = (
                await EdmAgreementService(self._session).find_active_for_contact(contact)
                if contact
                else None
            )
            if agreement is None:
                document.status = SignatureDocumentStatus.BLOCKED_NO_AGREEMENT.value
                await self._session.flush()
                await self._audit.record(
                    AuditAction.SIGNATURE_DOCUMENT_SENT,
                    entity_type="signature_document",
                    entity_id=document.id,
                    changes={"status": {"old": previous_status, "new": document.status}},
                )
                if document.created_by:
                    await get_notification_service().notify_user(
                        self._session,
                        recipient_id=document.created_by,
                        template_code=TPL_EDM_AGREEMENT_MISSING,
                        priority=NotificationPriority.HIGH,
                        payload={
                            "document_id": str(document.id),
                            "contact_id": str(contact.id) if contact else None,
                        },
                    )
                return document, {}
            request.edm_agreement_id = agreement.id

        document.status = SignatureDocumentStatus.PENDING.value
        revealed = await self._activate_turn(document, requests)
        await self._session.flush()

        if document.entity_type == "deal":
            deal = await self._session.get(Deal, document.entity_id)
            if deal is not None:
                deal.signature_status = DealSignatureStatus.PENDING.value
                deal.active_signature_document_id = document.id

        await self._audit.record(
            AuditAction.SIGNATURE_DOCUMENT_SENT,
            entity_type="signature_document",
            entity_id=document.id,
            changes={"status": {"old": previous_status, "new": document.status}},
        )
        return document, revealed

    async def _activate_turn(
        self, document: SignatureDocument, requests: list[SignatureRequest]
    ) -> dict[uuid.UUID, str]:
        """Переводит в `sent` тех, чья очередь пришла: все сразу для
        `parallel`, только следующего по `sign_order` для `sequential`
        (dop.md §10.3 «юрист, потом руководитель»).

        Возвращает `{request_id: сырой_токен}` для новых внешних подписантов —
        это единственный момент, когда токен вообще существует в открытом
        виде (в БД остаётся только его sha256): `send()` пробрасывает его в
        тело своего ответа (dop.md §10.4 фаза 2, тот же приём, что
        `IdentityService.issue_invite`). Если внешний подписант активируется
        не из `send()`, а из `_seal()` (следующая очередь `sequential`-цепочки
        после чужой подписи) — раздать токен через API уже некому, и это
        честно логируется как ограничение (см. комментарий ниже), а не молча
        теряется.
        """
        now = dt.datetime.now(dt.UTC)
        revealed: dict[uuid.UUID, str] = {}
        candidates = requests
        if document.signing_order == "sequential":
            pending = [r for r in requests if r.status == SignatureRequestStatus.PENDING.value]
            candidates = pending[:1]
        for request in candidates:
            if request.status != SignatureRequestStatus.PENDING.value:
                continue
            request.status = SignatureRequestStatus.SENT.value
            request.sent_at = now
            if request.signer_type == SignerType.EXTERNAL.value:
                token = secrets.token_urlsafe(32)
                request.access_token_hash = hashlib.sha256(token.encode()).hexdigest()
                ttl_days = self._settings.signature_token_ttl_days
                request.token_expires_at = now + dt.timedelta(days=ttl_days)
                revealed[request.id] = token
            else:
                await get_notification_service().notify_user(
                    self._session,
                    recipient_id=request.signer_user_id,
                    template_code=TPL_SIGNATURE_REQUESTED,
                    priority=NotificationPriority.HIGH,
                    entity_type="signature_request",
                    entity_id=request.id,
                )
        return revealed

    async def void(
        self, document: SignatureDocument, *, principal: Principal, reason: str
    ) -> SignatureDocument:
        if document.status == SignatureDocumentStatus.VOID.value:
            raise AppError(ErrorCode.VALIDATION, "Документ уже аннулирован")
        previous = document.status
        document.status = SignatureDocumentStatus.VOID.value
        document.void_reason = reason
        document.voided_by = principal.user_id
        requests = await self.list_requests(document.id)
        for request in requests:
            if request.status in OPEN_REQUEST_STATUSES:
                request.status = SignatureRequestStatus.VOID.value
        await self._session.flush()

        if document.entity_type == "deal":
            deal = await self._session.get(Deal, document.entity_id)
            if deal is not None and deal.active_signature_document_id == document.id:
                deal.signature_status = DealSignatureStatus.VOID.value

        await self._audit.record(
            AuditAction.SIGNATURE_VOID,
            entity_type="signature_document",
            entity_id=document.id,
            changes={
                "status": {"old": previous, "new": document.status},
                "reason": {"old": None, "new": reason},
            },
        )
        return document

    async def protocol_download(self, document: SignatureDocument) -> tuple[str, dt.datetime]:
        if not document.protocol_file_id:
            raise AppError(
                ErrorCode.DOCUMENT_NOT_SIGNABLE,
                "Протокол ещё не сформирован — документ не подписан",
            )
        file = await self._session.get(File, document.protocol_file_id)
        if file is None:
            raise NotFoundError("Файл протокола", document.protocol_file_id)
        ttl = self._settings.reports_link_ttl_minutes * 60
        url = await generate_presigned_get(
            bucket=file.bucket, key=file.storage_key, expires_seconds=ttl,
            filename=file.original_filename,
        )
        return url, dt.datetime.now(dt.UTC) + dt.timedelta(seconds=ttl)


# =============================================================================
# Действия подписанта: просмотр, OTP, подпись, отказ
# =============================================================================


class SignatureActionError(AppError):
    pass


class SignatureRequestService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)
        self._settings = get_settings()

    async def get_or_404(self, request_id: uuid.UUID) -> SignatureRequest:
        request = await self._session.get(SignatureRequest, request_id)
        if request is None:
            raise NotFoundError("Запрос на подпись", request_id)
        return request

    async def get_for_internal_signer(
        self, request_id: uuid.UUID, principal: Principal
    ) -> SignatureRequest:
        request = await self.get_or_404(request_id)
        is_owner = request.signer_user_id == principal.user_id
        if request.signer_type != SignerType.INTERNAL.value or not is_owner:
            raise NotFoundError("Запрос на подпись", request_id)
        return request

    async def get_by_token(self, token: str) -> SignatureRequest:
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        stmt = select(SignatureRequest).where(SignatureRequest.access_token_hash == token_hash)
        request = (await self._session.execute(stmt)).scalars().first()
        if request is None:
            raise AppError(ErrorCode.SIGNATURE_TOKEN_INVALID, "Ссылка недействительна")
        if request.token_expires_at and request.token_expires_at < dt.datetime.now(dt.UTC):
            raise AppError(ErrorCode.SIGNATURE_TOKEN_INVALID, "Срок действия ссылки истёк")
        return request

    async def my_requests(self, principal: Principal) -> list[SignatureRequest]:
        stmt = (
            select(SignatureRequest)
            .where(SignatureRequest.signer_user_id == principal.user_id)
            .order_by(SignatureRequest.created_at.desc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def build_signing_page(
        self,
        request: SignatureRequest,
        *,
        mark_viewed: bool,
        ip: str | None,
        user_agent: str | None,
    ) -> SigningPageOut:
        document = await self._session.get(SignatureDocument, request.document_id)
        if document is None:
            raise NotFoundError("Документ на подпись", request.document_id)
        just_sent = request.status == SignatureRequestStatus.SENT.value
        if mark_viewed and request.viewed_at is None and just_sent:
            request.viewed_at = dt.datetime.now(dt.UTC)
            request.status = SignatureRequestStatus.VIEWED.value
            await self._session.flush()
            await self._audit.record(
                AuditAction.SIGNATURE_VIEWED,
                entity_type="signature_request",
                entity_id=request.id,
                changes={"viewed_at": {"old": None, "new": request.viewed_at.isoformat()}},
            )

        file = await self._session.get(File, document.file_id)
        preview_url = await generate_presigned_get(
            bucket=file.bucket, key=file.storage_key, expires_seconds=600,
            filename=file.original_filename,
        )
        siblings = await SignatureDocumentService(self._session).list_requests(document.id)
        return SigningPageOut(
            document=SigningDocumentPreview(
                id=document.id,
                title=document.title,
                doc_type=document.doc_type,
                content_hash=document.content_hash,
                deadline_at=document.deadline_at,
                status=document.status,
                preview_url=preview_url,
            ),
            signers=[
                SigningSignerPreview(
                    name=sib.signer_name_snapshot,
                    sign_order=sib.sign_order,
                    status=sib.status,
                    is_me=sib.id == request.id,
                )
                for sib in siblings
            ],
            my_request_id=request.id,
            my_status=request.status,
            agreement_text=AGREEMENT_TEXT,
        )

    # --- OTP ---------------------------------------------------------------

    def _channel_and_destination(
        self, request: SignatureRequest, contact: Contact | None, user: User | None
    ) -> tuple[str, str, str]:
        phone = contact.phone if contact else (user.phone if user else None)
        email = contact.email if contact else (user.email if user else None)
        if phone:
            return "sms", phone, mask_phone(phone) or "***"
        if email:
            return "email", email, mask_email(email) or "***"
        raise ValidationError(
            "У подписанта нет верифицированного канала связи (телефон/email)",
            [FieldError(field="signer", reason="phone и email пусты")],
        )

    async def challenge(
        self, request: SignatureRequest, *, ip: str | None, user_agent: str | None
    ) -> tuple[SignatureOtpCode, str, str, str | None]:
        open_statuses = (SignatureRequestStatus.SENT.value, SignatureRequestStatus.VIEWED.value)
        if request.status not in open_statuses:
            raise AppError(
                ErrorCode.DOCUMENT_NOT_SIGNABLE,
                "Запрос сейчас недоступен для отправки кода",
                extra={"status": request.status},
            )
        sent_count = await self._session.scalar(
            select(func.count())
            .select_from(SignatureOtpCode)
            .where(SignatureOtpCode.request_id == request.id)
        )
        if (sent_count or 0) >= 5:
            raise AppError(
                ErrorCode.SIGNATURE_OTP_INVALID, "Превышен лимит отправок кода на этот запрос"
            )
        await rate_limit_enforce(
            str(request.id), "signature:otp:send", limit=1, window_seconds=60,
            detail="Код уже отправлен — повторная отправка возможна через минуту",
        )

        contact = (
            await self._session.get(Contact, request.signer_contact_id)
            if request.signer_contact_id
            else None
        )
        user = (
            await self._session.get(User, request.signer_user_id)
            if request.signer_user_id
            else None
        )
        channel, destination, masked = self._channel_and_destination(request, contact, user)

        code = f"{secrets.randbelow(1_000_000):06d}"
        salt = bcrypt.gensalt()
        code_hash = bcrypt.hashpw(code.encode(), salt)
        otp = SignatureOtpCode(
            request_id=request.id,
            code_hash=code_hash.decode(),
            salt=salt.decode(),
            channel=channel,
            sent_to_masked=masked,
            max_attempts=self._settings.signature_otp_max_attempts,
            expires_at=(
                dt.datetime.now(dt.UTC)
                + dt.timedelta(seconds=self._settings.signature_otp_ttl_seconds)
            ),
        )
        self._session.add(otp)
        await self._session.flush()

        # SMS уходит через `sms-gateway-mock` (dop.md §13) — реальная
        # доставка внутри закрытого контура, не только запись в лог. Email
        # остаётся честной заглушкой: dop.md §13 называет только
        # sms-gateway-mock инфраструктурной задачей, mock-провайдера для
        # почты спецификация не просит, а тянуть его без запроса — за
        # рамки этой правки. Ни в одной ветке код не попадает в наш лог —
        # только в тело запроса к шлюзу (не наш audit/notification контур).
        if channel == "sms":
            await send_sms(to=destination, message=f"Код подтверждения: {code}")
        logger.info(
            "signature_otp_dispatch", request_id=str(request.id), channel=channel, sent_to=masked,
        )

        await self._audit.record(
            AuditAction.SIGNATURE_CHALLENGED,
            entity_type="signature_request",
            entity_id=request.id,
            changes={"channel": {"old": None, "new": channel}},
        )
        debug_code = None if self._settings.is_prod else code
        return otp, channel, masked, debug_code

    async def _fail_otp(self, request: SignatureRequest, otp: SignatureOtpCode) -> None:
        otp.attempts += 1
        await self._session.flush()
        if otp.attempts < otp.max_attempts:
            return
        request.status = SignatureRequestStatus.LOCKED.value
        await self._session.flush()
        await self._audit.record(
            AuditAction.SIGNATURE_OTP_FAILED,
            entity_type="signature_request",
            entity_id=request.id,
            changes={
                "attempts": {"old": None, "new": otp.attempts},
                "status": {"old": None, "new": "locked"},
            },
        )
        document = await self._session.get(SignatureDocument, request.document_id)
        if document and document.created_by:
            await get_notification_service().notify_user(
                self._session,
                recipient_id=document.created_by,
                template_code=TPL_SIGNATURE_OTP_LOCKED,
                priority=NotificationPriority.HIGH,
                entity_type="signature_request",
                entity_id=request.id,
            )

    async def sign(
        self,
        request: SignatureRequest,
        *,
        otp_code: str,
        ip: str | None,
        user_agent: str | None,
    ) -> Signature:
        open_statuses = (SignatureRequestStatus.SENT.value, SignatureRequestStatus.VIEWED.value)
        if request.status not in open_statuses:
            raise AppError(
                ErrorCode.DOCUMENT_NOT_SIGNABLE,
                "Запрос сейчас недоступен для подписания",
                extra={"status": request.status},
            )
        stmt = (
            select(SignatureOtpCode)
            .where(
                SignatureOtpCode.request_id == request.id,
                SignatureOtpCode.consumed_at.is_(None),
            )
            .order_by(SignatureOtpCode.created_at.desc())
        )
        otp = (await self._session.execute(stmt)).scalars().first()
        if otp is None:
            raise AppError(
                ErrorCode.SIGNATURE_OTP_INVALID, "Код не запрашивался или уже использован"
            )
        if otp.expires_at < dt.datetime.now(dt.UTC):
            raise AppError(ErrorCode.SIGNATURE_OTP_INVALID, "Срок действия кода истёк")
        if not bcrypt.checkpw(otp_code.encode(), otp.code_hash.encode()):
            await self._fail_otp(request, otp)
            # `core.db.get_db_session` откатывает ВСЮ транзакцию на любом
            # исключении (раздел 1) — правильно для сбоев, но `raise` ниже
            # штатный исход (неверный код), а не сбой. Без явного commit
            # здесь `otp.attempts`/`request.status=locked`/аудит
            # откатывались бы вместе с ответом: счётчик попыток на каждый
            # неверный код возвращался бы к последнему закоммиченному
            # значению (0), и лимит в `max_attempts` (dop.md §10.4 п.12)
            # был бы физически недостижим — неограниченный перебор кода, а
            # не просто потеря записи аудита. Тот же приём, что уже закрыл
            # этот пробел в `integration.cms`/`public_router`
            # (sprint9-integration-implementation.md, где он же был впервые
            # замечен здесь и сознательно оставлен не исправленным).
            await self._session.commit()
            raise AppError(ErrorCode.SIGNATURE_OTP_INVALID, "Код подтверждения неверен")
        otp.consumed_at = dt.datetime.now(dt.UTC)
        await self._session.flush()

        document = await self._session.get(SignatureDocument, request.document_id)
        if document is None:
            raise NotFoundError("Документ на подпись", request.document_id)

        async with distributed_lock(f"sigdoc:{document.id}:seal", ttl=30) as acquired:
            if not acquired:
                raise AppError(
                    ErrorCode.DEPENDENCY_UNAVAILABLE,
                    "Документ сейчас обрабатывается, повторите попытку",
                )
            return await self._seal(document, request, otp, ip=ip, user_agent=user_agent)

    async def _seal(
        self,
        document: SignatureDocument,
        request: SignatureRequest,
        otp: SignatureOtpCode,
        *,
        ip: str | None,
        user_agent: str | None,
    ) -> Signature:
        file = await self._session.get(File, document.file_id)
        if file is None:
            raise NotFoundError("Файл документа", document.file_id)
        inspection = await inspect_object(bucket=file.bucket, key=file.storage_key)
        if not inspection.exists or inspection.sha256 != document.content_hash:
            document.status = SignatureDocumentStatus.VOID.value
            document.void_reason = "hash_mismatch_detected"
            doc_requests = await SignatureDocumentService(self._session).list_requests(document.id)
            for sibling in doc_requests:
                if sibling.status in OPEN_REQUEST_STATUSES:
                    sibling.status = SignatureRequestStatus.VOID.value
            await self._session.flush()
            await self._audit.record(
                AuditAction.SIGNATURE_VOID,
                entity_type="signature_document",
                entity_id=document.id,
                result=AuditResult.ERROR,
                changes={"reason": {"old": None, "new": "hash_mismatch_detected"}},
            )
            # `core.db.get_db_session` откатывает ВСЮ транзакцию на любом
            # исключении — без явного commit здесь `VOID`/аудит откатывались
            # бы вместе с ответом 409: подмена документа между отправкой и
            # подписанием (dop.md §10.4 п.14, «инцидент безопасности») не
            # оставляла бы следа, а следующая попытка `sign()` на том же
            # запросе видела бы его всё ещё в `SENT`/`VIEWED` вместо `void` —
            # тот же приём, что уже закрыл этот пробел в
            # `integration.cms`/`public_router`
            # (sprint9-integration-implementation.md).
            await self._session.commit()
            raise AppError(
                ErrorCode.DOCUMENT_HASH_MISMATCH,
                "Документ изменился после отправки на подпись — подпись не поставлена",
            )

        # Реальный NTP-запрос (dop.md §10.8/§13) — до этой точки в `_seal`
        # ничего не мутировано в текущей транзакции (`otp.consumed_at` в
        # `sign()` — только `flush`, не `commit`), поэтому если рассинхрон
        # превышает порог и `get_trusted_time` бросает `AppError`, откат
        # всей транзакции (включая потребление OTP) — желаемое поведение:
        # подписант не должен терять попытку кода из-за временной проблемы
        # с доверенным временем, а не из-за собственной ошибки.
        now, time_source, drift_ms = await get_trusted_time()
        signer_id = request.signer_user_id or request.signer_contact_id
        nonce = secrets.token_hex(16)
        secret = self._settings.signature_server_secret.get_secret_value()
        signature_value = compute_signature_value(
            secret=secret, content_hash=document.content_hash, signer_id=str(signer_id),
            signed_at_iso=now.isoformat(), nonce=nonce,
        )

        dwell_seconds = (
            (now - request.viewed_at).total_seconds() if request.viewed_at is not None else None
        )
        evidence = {
            "signer": {
                "type": request.signer_type,
                "id": str(signer_id),
                "name_snapshot": request.signer_name_snapshot,
                "identifier_masked": request.signer_identifier_masked,
            },
            "document": {
                "id": str(document.id),
                "content_hash": document.content_hash,
                "filename": file.original_filename,
                "size": file.size_bytes,
            },
            "auth": {
                "method": SignatureMethod.PEP_OTP.value,
                "otp_sent_to": otp.sent_to_masked,
                "otp_channel": otp.channel,
                "otp_sent_at": otp.created_at.isoformat(),
                "otp_verified_at": now.isoformat(),
                "attempts": otp.attempts + 1,
            },
            "context": {
                "ip": ip,
                "user_agent": user_agent,
                "device_fp": None,
                "viewed_at": request.viewed_at.isoformat() if request.viewed_at else None,
                "dwell_seconds": dwell_seconds,
            },
            "agreement": {
                "edm_agreement_id": (
                    str(request.edm_agreement_id) if request.edm_agreement_id else None
                ),
                "signed_at": now.isoformat(),
            },
            "time": {"signed_at": now.isoformat(), "source": time_source, "drift_ms": drift_ms},
        }

        prev_hash = await self._chain_head()
        signature = Signature(
            request_id=request.id,
            document_id=document.id,
            content_hash=document.content_hash,
            method=SignatureMethod.PEP_OTP.value,
            signer_display=request.signer_name_snapshot,
            signature_value=signature_value,
            key_version=self._settings.signature_key_version,
            evidence=evidence,
            signed_at=now,
            time_source=time_source,
            time_drift_ms=drift_ms,
            ip=ip,
            user_agent=user_agent,
            prev_hash=prev_hash,
        )
        signature.hash = compute_chain_hash(
            prev_hash=prev_hash, signature_value=signature_value,
            content_hash=document.content_hash,
            request_id=str(request.id), signed_at_iso=now.isoformat(),
        )
        self._session.add(signature)

        request.status = SignatureRequestStatus.SIGNED.value
        request.decided_at = now
        await self._session.flush()

        await self._audit.record(
            AuditAction.SIGNATURE_SIGNED,
            entity_type="signature_request",
            entity_id=request.id,
            changes={"signature_id": {"old": None, "new": str(signature.id)}},
        )

        all_requests = await SignatureDocumentService(self._session).list_requests(document.id)
        terminal = (SignatureRequestStatus.SIGNED.value, SignatureRequestStatus.VOID.value)
        remaining = [r for r in all_requests if r.status not in terminal]
        if remaining:
            document.status = SignatureDocumentStatus.PARTIALLY_SIGNED.value
            if document.signing_order == "sequential":
                doc_service = SignatureDocumentService(self._session)
                revealed = await doc_service._activate_turn(document, all_requests)  # noqa: SLF001
                if revealed:
                    # Следующий подписант в цепочке активировался не из
                    # `send()`, отдать ему ссылку через HTTP-ответ некому —
                    # тот же честно залогированный пробел, что в
                    # `create_from_workflow_action` (см. комментарий там).
                    logger.warning(
                        "external_signer_token_undeliverable_mid_chain",
                        document_id=str(document.id), request_ids=[str(k) for k in revealed],
                    )
            await self._session.flush()
        else:
            await self._complete_document(document, all_requests)

        if document.entity_type == "deal":
            deal = await self._session.get(Deal, document.entity_id)
            if deal is not None:
                deal.signature_status = (
                    DealSignatureStatus.SIGNED.value
                    if not remaining
                    else DealSignatureStatus.PARTIALLY_SIGNED.value
                )
                self._session.add(
                    DealEvent(
                        deal_id=deal.id,
                        event_type=DealEventType.SIGNATURE_SIGNED.value,
                        actor_id=request.signer_user_id,
                        payload={
                            "document_id": str(document.id),
                            "signature_id": str(signature.id),
                        },
                    )
                )
        return signature

    async def _chain_head(self) -> str | None:
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"), {"lock_id": _SIG_CHAIN_LOCK_ID}
        )
        result = await self._session.execute(
            select(Signature.hash)
            .order_by(Signature.created_at.desc(), Signature.id.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _complete_document(
        self, document: SignatureDocument, requests: list[SignatureRequest]
    ) -> None:
        now = dt.datetime.now(dt.UTC)
        document.status = SignatureDocumentStatus.SIGNED.value
        document.completed_at = now

        signatures_stmt = (
            select(Signature)
            .where(Signature.document_id == document.id)
            .order_by(Signature.signed_at)
        )
        signatures = list((await self._session.execute(signatures_stmt)).scalars().all())

        original = await self._session.get(File, document.file_id)
        original_bytes = await download_object_bytes(
            bucket=original.bucket, key=original.storage_key
        )
        settings = get_settings()
        # QR и ссылка в штампе ведут на страницу проверки веб-клиента (`/verify/{id}`),
        # а не на JSON-ручку.
        verify_url = f"{settings.base_url.rstrip('/')}/verify/{{sig_id}}"
        last_signature = signatures[-1] if signatures else None
        final_sig_id = str(last_signature.id) if last_signature else document.id
        stamp_lines = [
            "Документ подписан простой электронной подписью",
        ] + [
            f"{sig.signer_display} / {sig.signed_at.strftime('%d.%m.%Y %H:%M')} UTC"
            for sig in signatures
        ] + [f"Хэш: {document.content_hash[:16]}…"]
        stamped_bytes = apply_signature_stamp(
            original_bytes,
            lines=stamp_lines,
            verify_url=verify_url.format(sig_id=final_sig_id),
        )
        doc_service = SignatureDocumentService(self._session)
        signed_file_id = await doc_service._store_generated_pdf(  # noqa: SLF001
            stamped_bytes, filename=f"signed-{document.id}.pdf", uploaded_by=document.created_by
        )
        document.signed_file_id = signed_file_id

        protocol_bytes = render_protocol_pdf(
            document_title=document.title,
            content_hash=document.content_hash,
            entries=[
                {
                    "signer_display": sig.signer_display,
                    "method": sig.method,
                    "signed_at": sig.signed_at.isoformat(),
                    "ip": sig.ip,
                    "user_agent": sig.user_agent,
                    "signature_id": str(sig.id),
                }
                for sig in signatures
            ],
            verify_url=verify_url.format(sig_id=final_sig_id),
        )
        protocol_file_id = await doc_service._store_generated_pdf(  # noqa: SLF001
            protocol_bytes, filename=f"protocol-{document.id}.pdf", uploaded_by=document.created_by
        )
        document.protocol_file_id = protocol_file_id
        document.manifest = {
            "original_file_id": str(document.file_id),
            "signed_file_id": str(signed_file_id),
            "protocol_file_id": str(protocol_file_id),
            "signatures": [str(sig.id) for sig in signatures],
        }
        await self._session.flush()

        for attachment_entity_type, attachment_entity_id in (
            [(document.entity_type, document.entity_id)] if document.entity_type == "deal" else []
        ):
            self._session.add(
                Attachment(
                    file_id=signed_file_id,
                    entity_type=attachment_entity_type,
                    entity_id=attachment_entity_id,
                    category=AttachmentCategory.SIGNATURE_CONTAINER.value,
                    description=document.title,
                    uploaded_by=document.created_by,
                )
            )
            await self._session.flush()

        await get_outbox_service().publish(
            self._session,
            aggregate_type="signature_document",
            aggregate_id=document.id,
            event_type="DOCUMENT_SIGNED",
            payload={"entity_type": document.entity_type, "entity_id": str(document.entity_id)},
        )

        for request in requests:
            recipient = request.signer_user_id
            if recipient:
                await get_notification_service().notify_user(
                    self._session,
                    recipient_id=recipient,
                    template_code=TPL_SIGNATURE_SIGNED,
                    priority=NotificationPriority.NORMAL,
                    entity_type="signature_document",
                    entity_id=document.id,
                )
        if document.created_by:
            await get_notification_service().notify_user(
                self._session,
                recipient_id=document.created_by,
                template_code=TPL_SIGNATURE_SIGNED,
                priority=NotificationPriority.NORMAL,
                entity_type="signature_document",
                entity_id=document.id,
            )

    async def reject(
        self, request: SignatureRequest, *, reason: str, ip: str | None, user_agent: str | None
    ) -> SignatureRequest:
        if request.status not in OPEN_REQUEST_STATUSES:
            raise AppError(
                ErrorCode.DOCUMENT_NOT_SIGNABLE,
                "Запрос уже завершён и не может быть отклонён",
                extra={"status": request.status},
            )
        now = dt.datetime.now(dt.UTC)
        request.status = SignatureRequestStatus.REJECTED.value
        request.decided_at = now
        request.reject_reason = reason

        document = await self._session.get(SignatureDocument, request.document_id)
        document.status = SignatureDocumentStatus.REJECTED.value
        doc_requests = await SignatureDocumentService(self._session).list_requests(document.id)
        for sibling in doc_requests:
            if sibling.id != request.id and sibling.status in OPEN_REQUEST_STATUSES:
                sibling.status = SignatureRequestStatus.VOID.value
        await self._session.flush()

        await self._audit.record(
            AuditAction.SIGNATURE_REJECTED,
            entity_type="signature_request",
            entity_id=request.id,
            changes={"reason": {"old": None, "new": reason}},
        )

        if document.entity_type == "deal":
            deal = await self._session.get(Deal, document.entity_id)
            if deal is not None:
                deal.signature_status = DealSignatureStatus.REJECTED.value
                self._session.add(
                    DealEvent(
                        deal_id=deal.id,
                        event_type=DealEventType.SIGNATURE_REJECTED.value,
                        actor_id=request.signer_user_id,
                        payload={"document_id": str(document.id), "reason": reason},
                    )
                )
                await _apply_deal_signature_outcome(
                    self._session, deal,
                    rule=document.on_rejected,
                    note=f"Подписант отклонил документ «{document.title}»: {reason}",
                )
        if document.created_by:
            await get_notification_service().notify_user(
                self._session,
                recipient_id=document.created_by,
                template_code=TPL_SIGNATURE_REJECTED,
                priority=NotificationPriority.HIGH,
                entity_type="signature_document",
                entity_id=document.id,
                payload={"reason": reason},
            )
        return request


def compute_signature_value(
    *, secret: str, content_hash: str, signer_id: str, signed_at_iso: str, nonce: str
) -> str:
    """dop.md §10.4 п.16: `HMAC-SHA256(server_secret, content_hash || signer_id ||
    signed_at || nonce)` — метка целостности записи, не УКЭП (см. docstring модуля)."""
    message = "||".join([content_hash, signer_id, signed_at_iso, nonce])
    return hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()


def compute_chain_hash(
    *,
    prev_hash: str | None,
    signature_value: str,
    content_hash: str,
    request_id: str,
    signed_at_iso: str,
) -> str:
    canonical = "|".join(
        [prev_hash or GENESIS_HASH, signature_value, content_hash, request_id, signed_at_iso]
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _apply_deal_signature_outcome(
    session: AsyncSession, deal: Deal, *, rule: str | None, note: str
) -> None:
    """Откат статуса сделки при отклонении/истечении срока подписи (dop.md
    §10.6): `previous_status` — назад по `deal_status_history`, код статуса —
    в конкретный статус текущей воронки, `notify_initiator`/`void`/`None` —
    без изменения статуса. Пишет `DealStatusHistory`/`DealEvent`/`DealComment`
    тем же протоколом, что `DealStatusService.migrate_batch`
    (`app/modules/crm/service.py`) — см. docstring модуля."""
    if not rule or rule in ("notify_initiator", "void"):
        return

    target_status_id: uuid.UUID | None = None
    if rule == "previous_status":
        target_status_id = await session.scalar(
            select(DealStatusHistory.from_status_id)
            .where(
                DealStatusHistory.deal_id == deal.id,
                DealStatusHistory.from_status_id.is_not(None),
            )
            .order_by(DealStatusHistory.changed_at.desc())
            .limit(1)
        )
    else:
        target_status_id = await session.scalar(
            select(WorkflowStatus.id).where(
                WorkflowStatus.workflow_id == deal.workflow_id, WorkflowStatus.code == rule
            )
        )
    if target_status_id is None or target_status_id == deal.status_id:
        return

    now = dt.datetime.now(dt.UTC)
    previous_status_id = deal.status_id
    deal.status_id = target_status_id
    deal.status_changed_at = now
    deal.version += 1
    session.add(
        DealStatusHistory(
            deal_id=deal.id,
            from_status_id=previous_status_id,
            to_status_id=target_status_id,
            changed_by=None,
            reason=HistoryReason.SIGNATURE_REJECTED.value,
            comment=note,
            changed_at=now,
        )
    )
    session.add(DealComment(deal_id=deal.id, author_id=None, body=note, is_system=True))


# =============================================================================
# Проверка подписи (dop.md §10.5, публичная и программная)
# =============================================================================


class VerifyService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def verify_by_id(self, signature_id: uuid.UUID) -> dict[str, Any]:
        signature = await self._session.get(Signature, signature_id)
        if signature is None:
            return {"status": "not_found"}
        document = await self._session.get(SignatureDocument, signature.document_id)
        status = "valid"
        if signature.is_disputed:
            status = "disputed"
        elif document is not None and document.status == SignatureDocumentStatus.VOID.value:
            status = "void"
        return {
            "status": status,
            "signature_id": signature.id,
            "signer_display": signature.signer_display,
            "signed_at": signature.signed_at,
            "document_hash": signature.content_hash,
            "method": signature.method,
            "is_disputed": signature.is_disputed,
        }

    async def verify_by_file(self, file_bytes: bytes) -> dict[str, Any]:
        digest = hashlib.sha256(file_bytes).hexdigest()
        stmt = (
            select(Signature)
            .where(Signature.content_hash == digest)
            .order_by(Signature.signed_at.desc())
        )
        signature = (await self._session.execute(stmt)).scalars().first()
        if signature is None:
            return {"status": "hash_mismatch"}
        return await self.verify_by_id(signature.id)


# =============================================================================
# Контракт identity + приёмник для DSL-действия request_signature (crm)
# =============================================================================


@runtime_checkable
class SigningService(Protocol):
    """Контракт, который использует identity (dop §10.7) и crm (раздел 8 DSL)."""

    async def void_pending_for_user(
        self, session: AsyncSession, user_id: uuid.UUID, *, reason: str
    ) -> int: ...

    async def mark_disputed_since(
        self, session: AsyncSession, user_id: uuid.UUID, *, since: dt.datetime
    ) -> int: ...

    async def count_signatures(self, session: AsyncSession, user_id: uuid.UUID) -> int: ...

    async def count_signatures_for_contact(
        self, session: AsyncSession, contact_id: uuid.UUID
    ) -> int:
        """Тот же блокер dop §10.7 (`has_signatures`), но для внешнего

        подписанта контура B (`signer_contact_id`) — представителя вуза или
        B2C-физлица. Без него запрос на удаление контакта не видел бы
        подписи вообще: `count_signatures` смотрит только `signer_user_id`.
        """
        ...

    async def count_pending_requests(self, session: AsyncSession, user_id: uuid.UUID) -> int: ...

    async def reassign_pending(
        self, session: AsyncSession, user_id: uuid.UUID, *, successor_id: uuid.UUID | None
    ) -> int: ...

    async def request_signature_for_deal(
        self,
        session: AsyncSession,
        *,
        deal: Deal,
        action: dict[str, Any],
        principal: Principal,
        now: dt.datetime,
    ) -> None:
        """Действие `request_signature` DSL-перехода (`app/modules/crm/
        service.py._run_actions`)."""


class RealSigningService:
    async def void_pending_for_user(
        self, session: AsyncSession, user_id: uuid.UUID, *, reason: str
    ) -> int:
        stmt = (
            select(SignatureRequest)
            .where(
                SignatureRequest.signer_user_id == user_id,
                SignatureRequest.status.in_([s.value for s in OPEN_REQUEST_STATUSES]),
            )
        )
        requests = list((await session.execute(stmt)).scalars().all())
        for request in requests:
            request.status = SignatureRequestStatus.VOID.value
            document = await session.get(SignatureDocument, request.document_id)
            open_doc_statuses = {s.value for s in OPEN_DOCUMENT_STATUSES} | {"draft"}
            if document and document.status in open_doc_statuses:
                document.void_reason = reason
                document.status = SignatureDocumentStatus.VOID.value
                document.voided_by = None
        if requests:
            await session.flush()
        return len(requests)

    async def mark_disputed_since(
        self, session: AsyncSession, user_id: uuid.UUID, *, since: dt.datetime
    ) -> int:
        # `signatures.signer_display` не FK — подписи ищем через живой
        # `signature_requests.signer_user_id`, который ещё указывает на
        # пользователя на момент компрометации (обезличивание блокируется
        # именно наличием подписей, см. dop §10.7 — до него дело не доходит).
        subq = select(SignatureRequest.id).where(SignatureRequest.signer_user_id == user_id)
        result = await session.execute(
            update(Signature)
            .where(Signature.request_id.in_(subq), Signature.signed_at >= since)
            .values(is_disputed=True)
        )
        return result.rowcount or 0

    async def count_signatures(self, session: AsyncSession, user_id: uuid.UUID) -> int:
        subq = select(SignatureRequest.id).where(SignatureRequest.signer_user_id == user_id)
        count = await session.scalar(
            select(func.count()).select_from(Signature).where(Signature.request_id.in_(subq))
        )
        return int(count or 0)

    async def count_signatures_for_contact(
        self, session: AsyncSession, contact_id: uuid.UUID
    ) -> int:
        subq = select(SignatureRequest.id).where(SignatureRequest.signer_contact_id == contact_id)
        count = await session.scalar(
            select(func.count()).select_from(Signature).where(Signature.request_id.in_(subq))
        )
        return int(count or 0)

    async def count_pending_requests(self, session: AsyncSession, user_id: uuid.UUID) -> int:
        count = await session.scalar(
            select(func.count())
            .select_from(SignatureRequest)
            .where(
                SignatureRequest.signer_user_id == user_id,
                SignatureRequest.status.in_([s.value for s in OPEN_REQUEST_STATUSES]),
            )
        )
        return int(count or 0)

    async def reassign_pending(
        self, session: AsyncSession, user_id: uuid.UUID, *, successor_id: uuid.UUID | None
    ) -> int:
        stmt = select(SignatureRequest).where(
            SignatureRequest.signer_user_id == user_id,
            SignatureRequest.status.in_([s.value for s in OPEN_REQUEST_STATUSES]),
        )
        requests = list((await session.execute(stmt)).scalars().all())
        moved = 0
        for request in requests:
            if successor_id is None:
                continue
            # Персональный подписант (не по роли) — тихая подстановка
            # запрещена (dop §10.7): переоформление остаётся задачей
            # инициатора, здесь только запросы, адресованные роли.
            if not request.signer_role_code:
                continue
            successor = await session.get(User, successor_id)
            if successor is None:
                continue
            request.signer_user_id = successor_id
            request.signer_name_snapshot = successor.effective_name
            masked = mask_phone(successor.phone) or mask_email(successor.email)
            request.signer_identifier_masked = masked
            moved += 1
        if moved:
            await session.flush()
        return moved

    async def request_signature_for_deal(
        self,
        session: AsyncSession,
        *,
        deal: Deal,
        action: dict[str, Any],
        principal: Principal,
        now: dt.datetime,
    ) -> None:
        await SignatureDocumentService(session).create_from_workflow_action(
            deal=deal, action=action, principal_id=principal.user_id
        )


_service: SigningService = RealSigningService()


def register_signing_service(service: SigningService) -> None:
    """Оставлено для тестов: подмена на заглушку без реальных таблиц."""
    global _service
    _service = service


def get_signing_service() -> SigningService:
    return _service
