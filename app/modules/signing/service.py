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

import asyncio
import datetime as dt
import hashlib
import hmac
import secrets
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar, runtime_checkable
from urllib.parse import quote

import bcrypt
import structlog
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.core.config import get_settings
from app.core.db import run_after_commit
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
from app.modules.notification.email_transport import (
    EmailSendError,
    send_email,
    smtp_configured,
)
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
    RenderError,
    apply_signature_stamp,
    render_protocol_pdf,
    render_signature_document,
    validate_pdf,
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

# Подпись и отправка кода не быстрее, чем раз в столько-то секунд на один запрос: внутренний
# `/sign` не имел лимита вовсе, сдерживал перебор только `max_attempts` одного кода.
_SIGN_RATE_LIMIT_PER_MIN = 10

_T = TypeVar("_T")
# xhtml2pdf/reportlab держат глобальное состояние (реестр шрифтов, кэши): параллельные сборки
# PDF из разных потоков не безопасны. Цикл событий они не держат (`to_thread`), но между собой
# идут по одной.
_PDF_LOCK = threading.Lock()


def _pdf_serialized(func: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    with _PDF_LOCK:
        return func(*args, **kwargs)


async def _render_off_loop(func: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
    """PDF (xhtml2pdf, pypdf, reportlab) — CPU на секунды: в потоке, а не в цикле событий."""
    return await asyncio.to_thread(_pdf_serialized, func, *args, **kwargs)


async def _render_template_pdf(body_template: str, context: dict[str, Any]) -> bytes:
    try:
        return await _render_off_loop(render_signature_document, body_template, context)
    except RenderError as exc:
        # Шаблон правится в БД, не через API: ошибка в нём — не 500 «на весь запрос», а понятный
        # отказ создать документ.
        raise ValidationError(
            "Шаблон документа не удалось отрисовать в PDF",
            [FieldError(field="template_code", reason=str(exc)[:200])],
        ) from exc


def _expose_debug_otp() -> bool:
    """Отдавать ли одноразовый код в ответе API (`debug_code`).

    Пока в контуре нет реальной доставки (email не отправляется вовсе, SMS идёт на мок-шлюз),
    во всех профилях, кроме prod, код возвращается в ответе — иначе демо и тесты не смогли бы
    подписать. Явный флаг настроек `signature_expose_debug_otp` (когда он появится в `Settings`)
    имеет приоритет: `false` отключает `debug_code` и в dev/demo, `true` включает его где угодно.
    Без флага действует прежнее правило `not is_prod`."""
    settings = get_settings()
    override = getattr(settings, "signature_expose_debug_otp", None)
    if override is not None:
        return bool(override)
    return not settings.is_prod


def build_sign_url(token: str) -> str:
    """Ссылка внешнего подписанта: страница веб-клиента (SPA `/sign/{token}`), не JSON-ручка."""
    return f"{get_settings().base_url.rstrip('/')}/sign/{token}"


_SIGN_LINK_SUBJECT = "Вам направлен документ на подписание"


def render_sign_link_email(
    *,
    title: str,
    url: str,
    token_expires_at: dt.datetime | None,
    deadline_at: dt.datetime | None,
) -> tuple[str, str]:
    """Тема и текст письма со ссылкой подписания. Только то, что нужно получателю: название
    документа, ссылка и сроки; имён и реквизитов подписанта в письме нет."""
    lines = [
        "Здравствуйте!",
        "",
        f"Вам направлен документ «{title}» на подписание простой электронной подписью.",
        "",
        f"Открыть документ и подписать: {url}",
        "",
        "Ссылка личная: не пересылайте её другим людям.",
    ]
    if token_expires_at is not None:
        until = token_expires_at.astimezone(dt.UTC)
        lines.append(f"Ссылка действует до {until:%d.%m.%Y %H:%M} UTC.")
    if deadline_at is not None:
        lines.append(f"Срок подписания: до {deadline_at.astimezone(dt.UTC):%d.%m.%Y %H:%M} UTC.")
    lines += ["", "Если вы не ожидали это письмо, просто проигнорируйте его."]
    return _SIGN_LINK_SUBJECT, "\n".join(lines)


def _agreement_is_effective(agreement: EdmAgreement | None, today: dt.date | None = None) -> bool:
    """Соглашение об ЭДО действует: не отозвано, не истекло и попадает в срок."""
    if agreement is None or agreement.status != EdmAgreementStatus.ACTIVE.value:
        return False
    today = today or dt.date.today()
    if agreement.valid_from is not None and agreement.valid_from > today:
        return False
    return agreement.valid_to is None or agreement.valid_to >= today


def _sync_deal_signature_status(deal: Deal, document: SignatureDocument, status: str) -> bool:
    """Статус подписи сделки меняет только её АКТИВНЫЙ документ.

    Подпись, отказ или истечение срока старого документа (сделка уже ушла на другой этап, где
    `crm` сбросила `active_signature_document_id`) не должны ни выставлять «подписано», ни
    гасить статус нового документа: гард перехода в `lms_transfer` читает это поле. Версия
    сделки растёт вместе со статусом, чтобы открытая карточка не правила устаревшее (If-Match)."""
    if deal.active_signature_document_id != document.id or deal.signature_status == status:
        return False
    deal.signature_status = status
    deal.version += 1
    return True


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
        await suspend_requests_of_agreement(self._session, agreement, reason="agreement_revoked")
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


async def suspend_requests_of_agreement(
    session: AsyncSession, agreement: EdmAgreement, *, reason: str
) -> int:
    """Соглашение об ЭДО отозвано или истекло: запросы, опиравшиеся на него, подписать больше
    нельзя (ст. 6 63-ФЗ — подпись без действующего соглашения недействительна).

    Соглашение проверяется только в `send()`, а подпись приходит позже — без этого шага
    отозванное соглашение не мешало бы довести документ до `signed` и штампа. Открытые запросы
    возвращаются в `pending` (прежняя ссылка перестаёт работать), а документ — в
    `blocked_no_agreement`: тот же статус, что и при отправке без соглашения, из него выход
    прежний — оформить соглашение и повторить `send()`. Возвращает число затронутых запросов.

    Порядок блокировок тот же, что у подписи: сначала документы, потом запросы."""
    open_statuses = [s.value for s in OPEN_REQUEST_STATUSES]
    document_ids = list(
        (
            await session.execute(
                select(SignatureRequest.document_id)
                .where(
                    SignatureRequest.edm_agreement_id == agreement.id,
                    SignatureRequest.status.in_(open_statuses),
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    if not document_ids:
        return 0
    documents = list(
        (
            await session.execute(
                select(SignatureDocument)
                .where(SignatureDocument.id.in_(document_ids))
                .order_by(SignatureDocument.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    requests = list(
        (
            await session.execute(
                select(SignatureRequest)
                .where(
                    SignatureRequest.edm_agreement_id == agreement.id,
                    SignatureRequest.status.in_(open_statuses),
                )
                .order_by(SignatureRequest.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for request in requests:
        request.status = SignatureRequestStatus.PENDING.value
        request.access_token_hash = None
        request.token_expires_at = None
        request.edm_agreement_id = None
    await session.flush()

    open_documents = {s.value for s in OPEN_DOCUMENT_STATUSES}
    audit = AuditService(session)
    for document in documents:
        if document.status not in open_documents:
            continue
        previous = document.status
        document.status = SignatureDocumentStatus.BLOCKED_NO_AGREEMENT.value
        await session.flush()
        await audit.record(
            AuditAction.SIGNATURE_DOCUMENT_SENT,
            entity_type="signature_document",
            entity_id=document.id,
            changes={
                "status": {"old": previous, "new": document.status},
                "reason": {"old": None, "new": reason},
            },
        )
        if document.created_by:
            await get_notification_service().notify_user(
                session,
                recipient_id=document.created_by,
                template_code=TPL_EDM_AGREEMENT_MISSING,
                priority=NotificationPriority.HIGH,
                payload={"document_id": str(document.id), "reason": reason},
            )
    return len(requests)


# =============================================================================
# Шаблоны — только чтение через API, управление сидами/БД (см. память спринта)
# =============================================================================


class SignatureTemplateService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_active(self) -> list[SignatureTemplate]:
        stmt = (
            select(SignatureTemplate)
            .where(SignatureTemplate.is_active.is_(True))
            .order_by(SignatureTemplate.code)
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
                    SignerType.INTERNAL.value,
                    user.id,
                    None,
                    role,
                    user.effective_name,
                    mask_phone(user.phone) or mask_email(user.email),
                    user.phone,
                    user.email,
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
                    SignerType.INTERNAL.value,
                    user.id,
                    None,
                    role,
                    user.effective_name,
                    mask_phone(user.phone) or mask_email(user.email),
                    user.phone,
                    user.email,
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
        SignerType.EXTERNAL.value,
        None,
        contact.id,
        role_code,
        name,
        mask_phone(contact.phone) or mask_email(contact.email),
        contact.phone,
        contact.email,
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

    async def get_many(self, document_ids: list[uuid.UUID]) -> dict[uuid.UUID, SignatureDocument]:
        """Пачка документов одним `SELECT ... IN` (батч-ручка `/batch`, C-5): не
        найденные просто отсутствуют в результате — вызывающая сторона (роутер)
        решает, что с этим делать, объектная проверка доступа сюда не входит."""
        if not document_ids:
            return {}
        rows = await self._session.execute(
            select(SignatureDocument).where(SignatureDocument.id.in_(document_ids))
        )
        return {d.id: d for d in rows.scalars().all()}

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

    async def _ensure_file_usable(
        self, principal: Principal, file: File, entity_type: str, entity_id: uuid.UUID
    ) -> None:
        if principal.is_admin or file.uploaded_by == principal.user_id:
            return
        attached = await self._session.scalar(
            select(Attachment.id)
            .where(
                Attachment.file_id == file.id,
                Attachment.entity_type == entity_type,
                Attachment.entity_id == entity_id,
                Attachment.deleted_at.is_(None),
            )
            .limit(1)
        )
        if attached is None:
            raise NotFoundError("Файл документа", file.id)

    async def _ensure_pdf_readable(self, file: File) -> None:
        """Битый PDF всплывал бы 500-й у ПОСЛЕДНЕГО подписанта (штамп накладывается при
        завершении), и каждая повторная подпись падала бы так же. Проверяем на создании."""
        try:
            data = await download_object_bytes(bucket=file.bucket, key=file.storage_key)
        except Exception as exc:  # noqa: BLE001 — хранилище: клиент тут не виноват
            logger.warning("signature_file_unreadable", file_id=str(file.id), error=str(exc))
            raise AppError(
                ErrorCode.DEPENDENCY_UNAVAILABLE, "Файл документа сейчас не читается из хранилища"
            ) from exc
        try:
            await _render_off_loop(validate_pdf, data)
        except RenderError as exc:
            raise ValidationError(
                "Файл не читается как PDF: загрузите исправный документ",
                [FieldError(field="file_id", reason="повреждённый или пустой PDF")],
            ) from exc

    async def create(self, principal: Principal, payload: Any) -> SignatureDocument:
        deal = await self._check_entity_access(principal, payload.entity_type, payload.entity_id)

        if payload.template_code:
            templates = SignatureTemplateService(self._session)
            template = await templates.get_by_code(payload.template_code)
            context = await _build_entity_context(
                self._session, entity_type=payload.entity_type, entity_id=payload.entity_id
            )
            pdf_bytes = await _render_template_pdf(template.body_template, context)
            deadline_days = payload.deadline_days or template.default_deadline_days
            file_id = await self._store_generated_pdf(
                pdf_bytes, filename=f"{payload.title}.pdf", uploaded_by=principal.user_id
            )
            content_hash = hashlib.sha256(pdf_bytes).hexdigest()
        else:
            file = await self._session.get(File, payload.file_id)
            if file is None or file.deleted_at is not None:
                raise NotFoundError("Файл документа", payload.file_id)
            # Чужой файл уходил бы внешнему подписанту (публичная страница отдаёт PDF по
            # ссылке): годится свой файл, любой — администратору и приложенный к этой же
            # сущности. Иначе 404, а не 403: по ответу нельзя проверять чужие `file_id`.
            await self._ensure_file_usable(principal, file, payload.entity_type, payload.entity_id)
            if file.status != FileStatus.READY.value:
                raise ValidationError("Файл ещё не прошёл проверку и не готов к использованию")
            if file.mime_type != "application/pdf":
                raise ValidationError(
                    "Документ на подпись должен быть PDF",
                    [FieldError(field="file_id", reason=f"mime_type={file.mime_type!r}")],
                )
            if not file.sha256:
                raise ValidationError("У файла не посчитан sha256 — повторите commit")
            await self._ensure_pdf_readable(file)
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
        pdf_bytes = await _render_template_pdf(template.body_template, context)
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
            # в `SignatureRequestService.challenge`). Инициатор получит ссылку
            # через `reissue_link`.
            logger.warning(
                "external_signer_token_undeliverable_from_workflow_action",
                document_id=str(document.id),
                request_ids=[str(k) for k in revealed],
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
        return (
            select(SignatureRequest)
            .where(SignatureRequest.document_id == document_id)
            .order_by(SignatureRequest.sign_order)
        )

    async def list_requests(self, document_id: uuid.UUID) -> list[SignatureRequest]:
        rows = await self._session.execute(self._requests_query(document_id))
        return list(rows.scalars().all())

    async def requests_by_documents(
        self, document_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[SignatureRequest]]:
        """Запросы на подпись пачки документов одним запросом, по порядку
        подписания — тот же приём, что `signatures_by_document` (батч-ручка
        `/batch`, чтобы не повторять `list_requests` в цикле по каждому id)."""
        grouped: dict[uuid.UUID, list[SignatureRequest]] = {}
        if not document_ids:
            return grouped
        rows = await self._session.execute(
            select(SignatureRequest)
            .where(SignatureRequest.document_id.in_(document_ids))
            .order_by(SignatureRequest.sign_order)
        )
        for request in rows.scalars():
            grouped.setdefault(request.document_id, []).append(request)
        return grouped

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
        # Две одновременные отправки не должны обе пройти проверку статуса и выдать по токену.
        await self._session.refresh(document, with_for_update=True)
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
        self._restart_deadline(document)
        revealed = await self._activate_turn(document, requests)
        await self._session.flush()

        if document.entity_type == "deal":
            deal = await self._session.get(Deal, document.entity_id)
            if deal is not None:
                changed = (
                    deal.signature_status != DealSignatureStatus.PENDING.value
                    or deal.active_signature_document_id != document.id
                )
                deal.signature_status = DealSignatureStatus.PENDING.value
                deal.active_signature_document_id = document.id
                if changed:
                    deal.version += 1

        await self._audit.record(
            AuditAction.SIGNATURE_DOCUMENT_SENT,
            entity_type="signature_document",
            entity_id=document.id,
            changes={"status": {"old": previous_status, "new": document.status}},
        )
        return document, revealed

    @staticmethod
    def _restart_deadline(document: SignatureDocument) -> None:
        """Срок считался при создании и не двигался: документ, ждавший соглашения об ЭДО дольше
        срока, истекал сразу после отправки. Длина срока сохраняется (это `deadline_at` минус
        `created_at`), отсчёт идёт от отправки; срок только удлиняется. У свежесозданного
        документа разница нулевая, `created_at` ещё может быть не подгружен — тогда срок
        остаётся как есть."""
        created_at = document.__dict__.get("created_at")
        if document.deadline_at is None or created_at is None:
            return
        now = dt.datetime.now(dt.UTC)
        if now - created_at < dt.timedelta(minutes=1):
            return
        restarted = now + (document.deadline_at - created_at)
        if restarted > document.deadline_at:
            document.deadline_at = restarted

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
        после чужой подписи) — отдать токен в HTTP-ответе уже некому, это
        честно логируется (см. комментарий ниже), а инициатор получает новую
        ссылку через `reissue_link`.
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
                revealed[request.id] = self._issue_token(request, now)
                await self._queue_link_email(document, request, revealed[request.id])
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

    async def _queue_link_email(
        self, document: SignatureDocument, request: SignatureRequest, token: str
    ) -> None:
        """Ссылка внешнему подписанту письмом — если настроен SMTP; иначе ничего не делается, и
        ссылку, как раньше, выдаёт инициатору ответ `/send` или `/reissue-link`.

        Письмо уходит ПОСЛЕ коммита: токен существует, только если транзакция зафиксирована,
        откат оставил бы получателю мёртвую ссылку. Сбой отправки (сервер недоступен, адрес
        отвергнут) запрос не ломает — он в журнале, инициатор по-прежнему может передать ссылку
        сам."""
        if not smtp_configured():
            return
        contact = (
            await self._session.get(Contact, request.signer_contact_id)
            if request.signer_contact_id
            else None
        )
        address = contact.email if contact else None
        if not address:
            logger.warning("signature_link_email_no_address", request_id=str(request.id))
            return
        subject, body = render_sign_link_email(
            title=document.title,
            url=build_sign_url(token),
            token_expires_at=request.token_expires_at,
            deadline_at=document.deadline_at,
        )

        async def _send() -> None:
            try:
                await send_email(to=address, subject=subject, body=body)
            except EmailSendError:
                # Причина и маскированный адрес уже в журнале транспорта.
                logger.warning("signature_link_email_failed", request_id=str(request.id))

        run_after_commit(self._session, _send)

    def _issue_token(self, request: SignatureRequest, now: dt.datetime) -> str:
        """Новый токен внешнему подписанту. В БД остаётся только его sha256,
        поэтому прежняя ссылка перестаёт работать."""
        token = secrets.token_urlsafe(32)
        request.access_token_hash = hashlib.sha256(token.encode()).hexdigest()
        ttl_days = self._settings.signature_token_ttl_days
        request.token_expires_at = now + dt.timedelta(days=ttl_days)
        return token

    async def reissue_link(self, request: SignatureRequest, *, principal: Principal) -> str:
        """Выдаёт инициатору новую ссылку внешнему подписанту, пока запрос ждёт
        подписи. Нужна, когда очередь `sequential` дошла до него из `_seal()`:
        токен там сгенерирован, но отдать его некому."""
        document = await self.get_or_404(request.document_id)
        if not (principal.is_admin or document.created_by == principal.user_id):
            raise ForbiddenError("Ссылку выдаёт инициатор документа или администратор")
        await self._session.refresh(request, with_for_update=True)
        if request.signer_type != SignerType.EXTERNAL.value:
            raise ValidationError(
                "Ссылка нужна только внешнему подписанту: внутренний подписывает в CRM"
            )
        waiting = (SignatureRequestStatus.SENT.value, SignatureRequestStatus.VIEWED.value)
        if request.status not in waiting or document.status not in {
            s.value for s in OPEN_DOCUMENT_STATUSES
        }:
            raise AppError(
                ErrorCode.DOCUMENT_NOT_SIGNABLE,
                "Запрос сейчас не ждёт подписи: ссылку выдать нельзя",
                extra={"status": request.status},
            )
        old_expires_at = request.token_expires_at
        token = self._issue_token(request, dt.datetime.now(dt.UTC))
        await self._queue_link_email(document, request, token)
        await self._session.flush()
        await self._audit.record(
            AuditAction.SIGNATURE_LINK_REISSUED,
            entity_type="signature_request",
            entity_id=request.id,
            changes={
                "token_expires_at": {
                    "old": old_expires_at.isoformat() if old_expires_at else None,
                    "new": request.token_expires_at.isoformat(),
                }
            },
        )
        return token

    async def signatures_by_document(
        self, document_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, list[Signature]]:
        """Подписи документов одним запросом, по порядку подписания."""
        grouped: dict[uuid.UUID, list[Signature]] = {}
        if not document_ids:
            return grouped
        rows = await self._session.execute(
            select(Signature)
            .where(Signature.document_id.in_(document_ids))
            .order_by(Signature.signed_at)
        )
        for signature in rows.scalars():
            grouped.setdefault(signature.document_id, []).append(signature)
        return grouped

    async def void(
        self, document: SignatureDocument, *, principal: Principal, reason: str
    ) -> SignatureDocument:
        # Строка документа под замком: подпись, пришедшая одновременно, дождётся конца
        # аннулирования и увидит `void`, а не запишется поверх него.
        await self._session.refresh(document, with_for_update=True)
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
            if deal is not None:
                _sync_deal_signature_status(deal, document, DealSignatureStatus.VOID.value)

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
            bucket=file.bucket,
            key=file.storage_key,
            expires_seconds=ttl,
            filename=file.original_filename,
        )
        return url, dt.datetime.now(dt.UTC) + dt.timedelta(seconds=ttl)


# =============================================================================
# Действия подписанта: просмотр, OTP, подпись, отказ
# =============================================================================


class SignatureActionError(AppError):
    pass


@dataclass(slots=True)
class _SignatureEntry:
    """Строка штампа и протокола: данные подписи, которую ещё не записали в `signatures`."""

    id: uuid.UUID
    signer_display: str
    method: str
    signed_at: dt.datetime
    ip: str | None
    user_agent: str | None


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
        if file is None:
            raise NotFoundError("Файл документа", document.file_id)
        preview_url = await generate_presigned_get(
            bucket=file.bucket,
            key=file.storage_key,
            expires_seconds=600,
            inline=True,
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

    async def load_document_pdf(self, request: SignatureRequest) -> tuple[bytes, str]:
        """PDF документа запроса и его имя — для просмотра с origin приложения."""
        document = await self._session.get(SignatureDocument, request.document_id)
        if document is None:
            raise NotFoundError("Документ на подпись", request.document_id)
        file = await self._session.get(File, document.file_id)
        if file is None:
            raise NotFoundError("Файл документа", document.file_id)
        data = await download_object_bytes(bucket=file.bucket, key=file.storage_key)
        return data, file.original_filename

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

    async def _lock_for_action(self, request: SignatureRequest) -> SignatureDocument:
        """Документ, затем запрос — под `FOR UPDATE`; статусы проверяются только после этого.

        Роутер отдаёт запрос прочитанным без блокировки: два одновременных `/sign` оба видели
        `sent`, оба сверяли код и оба писали подпись (дубли файлов и событий), а подпись соседнего
        запроса перезаписывала статус только что аннулированного документа. Под замком второй
        ждёт конца первого и перечитывает итоговое состояние (`refresh` и `populate_existing`
        заменяют объекты в сессии). Замок живёт до коммита транзакции запроса — прежний Redis-лок
        снимался раньше коммита и защищал только от гонки внутри самого `_seal`. Порядок
        «документ → запрос» един для подписи, отклонения, аннулирования и отзыва соглашения,
        поэтому взаимных блокировок нет."""
        document = (
            await self._session.execute(
                select(SignatureDocument)
                .where(SignatureDocument.id == request.document_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if document is None:
            raise NotFoundError("Документ на подпись", request.document_id)
        await self._session.refresh(request, with_for_update=True)
        return document

    async def _ensure_actionable(
        self, request: SignatureRequest, document: SignatureDocument, *, unavailable: str
    ) -> None:
        """Запрос ждёт решения подписанта, а документ и соглашение об ЭДО ещё в силе.

        Статус документа проверялся только у запроса: подпись соседнего запроса оживляла
        аннулированный или истёкший документ и ставила ему `signed`. Соглашение проверялось лишь
        в `send()`: отозванное или истёкшее после отправки не мешало подписать."""
        open_statuses = (SignatureRequestStatus.SENT.value, SignatureRequestStatus.VIEWED.value)
        if request.status not in open_statuses:
            raise AppError(
                ErrorCode.DOCUMENT_NOT_SIGNABLE, unavailable, extra={"status": request.status}
            )
        if document.status not in {s.value for s in OPEN_DOCUMENT_STATUSES}:
            raise AppError(
                ErrorCode.DOCUMENT_NOT_SIGNABLE,
                "Документ больше не принимает подписи",
                extra={"status": document.status},
            )
        if request.signer_type == SignerType.EXTERNAL.value:
            agreement = (
                await self._session.get(EdmAgreement, request.edm_agreement_id)
                if request.edm_agreement_id
                else None
            )
            if not _agreement_is_effective(agreement):
                raise AppError(
                    ErrorCode.EDM_AGREEMENT_MISSING,
                    "Соглашение об ЭДО отозвано или истекло: подписать документ нельзя",
                )

    async def _dispatch_otp(self, channel: str, destination: str, code: str) -> bool:
        """Отправляет код; `True` — он ушёл. SMS идёт на шлюз (результат шлюза учитывается), email —
        по SMTP, когда он настроен. У telegram (и у email без SMTP) транспорта в контуре нет:
        раньше `challenge` всё равно отвечал «отправлено», и подписант ждал сообщения, которого
        не будет."""
        if channel == "sms":
            message_id = await send_sms(to=destination, message=f"Код подтверждения: {code}")
            return message_id is not None
        if channel == "email" and smtp_configured():
            minutes = max(self._settings.signature_otp_ttl_seconds // 60, 1)
            try:
                await send_email(
                    to=destination,
                    subject="Код подтверждения подписи",
                    body=(
                        f"Код подтверждения для подписания документа: {code}\n\n"
                        f"Код действует {minutes} мин. Никому его не сообщайте.\n"
                        "Если вы не запрашивали код, проигнорируйте это письмо."
                    ),
                )
            except EmailSendError:
                return False
            return True
        return False

    async def challenge(
        self, request: SignatureRequest, *, ip: str | None, user_agent: str | None
    ) -> tuple[SignatureOtpCode, str, str, str | None]:
        document = await self._lock_for_action(request)
        await self._ensure_actionable(
            request, document, unavailable="Запрос сейчас недоступен для отправки кода"
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
            str(request.id),
            "signature:otp:send",
            limit=1,
            window_seconds=60,
            detail="Код уже отправлен — повторная отправка возможна через минуту",
            fail_closed=True,
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
        # bcrypt считается десятки миллисекунд: в потоке, чтобы не держать цикл событий.
        code_hash = await asyncio.to_thread(bcrypt.hashpw, code.encode(), salt)
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

        # SMS уходит через `sms-gateway-mock` (dop.md §13), email — по SMTP, если он настроен;
        # у telegram транспорта нет.
        # Код не попадает в наш лог ни в одной ветке — только в тело запроса к шлюзу.
        delivered = await self._dispatch_otp(channel, destination, code)
        debug_code = code if _expose_debug_otp() else None
        if not delivered and debug_code is None:
            # Код некому получить, а в ответе его нет: честная ошибка вместо «отправлено».
            # Исключение откатывает и запись кода.
            logger.warning(
                "signature_otp_undeliverable", request_id=str(request.id), channel=channel
            )
            raise AppError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "Не удалось отправить код подтверждения: канал доставки недоступен. "
                "Обратитесь к инициатору документа",
                extra={"channel": channel},
            )
        logger.info(
            "signature_otp_dispatch",
            request_id=str(request.id),
            channel=channel,
            sent_to=masked,
            delivered=delivered,
        )

        await self._audit.record(
            AuditAction.SIGNATURE_CHALLENGED,
            entity_type="signature_request",
            entity_id=request.id,
            changes={
                "channel": {"old": None, "new": channel},
                "delivered": {"old": None, "new": delivered},
            },
        )
        return otp, channel, masked, debug_code

    async def _fail_otp(self, request: SignatureRequest, otp: SignatureOtpCode) -> None:
        # Счётчик растёт в самой БД: `attempts += 1` в Python терял инкременты при параллельных
        # неверных вводах, а это и есть защита кода от перебора.
        attempts = await self._session.scalar(
            update(SignatureOtpCode)
            .where(SignatureOtpCode.id == otp.id)
            .values(attempts=SignatureOtpCode.attempts + 1)
            .returning(SignatureOtpCode.attempts)
            .execution_options(synchronize_session=False)
        )
        set_committed_value(otp, "attempts", attempts)
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
        # Внутренний `/sign` не имел лимита: публичные ручки ограничены по токену и IP, здесь
        # перебор сдерживал только `max_attempts` одного кода.
        await rate_limit_enforce(
            str(request.id),
            "signature:sign",
            limit=_SIGN_RATE_LIMIT_PER_MIN,
            window_seconds=60,
            detail="Слишком частые попытки подписания, повторите через минуту",
            fail_closed=True,
        )
        document = await self._lock_for_action(request)
        await self._ensure_actionable(
            request, document, unavailable="Запрос сейчас недоступен для подписания"
        )
        # Подпись на запрос одна. Под замком строки запроса статус уже перечитан, но повторная
        # подпись не должна получиться и при любом другом пути к этой точке: уникального индекса
        # по `signatures.request_id` нет.
        already_signed = await self._session.scalar(
            select(Signature.id).where(Signature.request_id == request.id).limit(1)
        )
        if already_signed is not None:
            raise AppError(
                ErrorCode.DOCUMENT_NOT_SIGNABLE,
                "Запрос уже подписан",
                extra={"status": request.status},
            )
        stmt = (
            select(SignatureOtpCode)
            .where(
                SignatureOtpCode.request_id == request.id,
                SignatureOtpCode.consumed_at.is_(None),
            )
            .order_by(SignatureOtpCode.created_at.desc())
            .with_for_update()
        )
        otp = (await self._session.execute(stmt)).scalars().first()
        if otp is None:
            raise AppError(
                ErrorCode.SIGNATURE_OTP_INVALID, "Код не запрашивался или уже использован"
            )
        if otp.expires_at < dt.datetime.now(dt.UTC):
            raise AppError(ErrorCode.SIGNATURE_OTP_INVALID, "Срок действия кода истёк")
        if otp.attempts >= otp.max_attempts:
            raise AppError(ErrorCode.SIGNATURE_OTP_INVALID, "Исчерпаны попытки ввода кода")
        code_ok = await asyncio.to_thread(bcrypt.checkpw, otp_code.encode(), otp.code_hash.encode())
        if not code_ok:
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
            raise AppError(
                ErrorCode.SIGNATURE_OTP_INVALID,
                "Код подтверждения неверен",
                extra={"attempts_left": max(otp.max_attempts - otp.attempts, 0)},
            )
        otp.consumed_at = dt.datetime.now(dt.UTC)
        await self._session.flush()
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
            secret=secret,
            content_hash=document.content_hash,
            signer_id=str(signer_id),
            signed_at_iso=now.isoformat(),
            nonce=nonce,
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
                # Nonce — открытый вход HMAC (секретом служит ключ сервера): без него значение
                # подписи не пересчитать, то есть целостность записи не проверить.
                "nonce": nonce,
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

        signature_id = uuid7()
        request.status = SignatureRequestStatus.SIGNED.value
        request.decided_at = now
        await self._session.flush()

        all_requests = await SignatureDocumentService(self._session).list_requests(document.id)
        terminal = (SignatureRequestStatus.SIGNED.value, SignatureRequestStatus.VOID.value)
        remaining = [r for r in all_requests if r.status not in terminal]
        if not remaining:
            # Тяжёлая часть (скачать оригинал, штамп, два PDF, загрузка в S3) идёт ДО захвата
            # глобального замка цепочки подписей: замок транзакционный и держится до коммита,
            # то есть на всё время работы с хранилищем он блокировал бы подписи ВСЕХ документов.
            await self._complete_document(
                document,
                all_requests,
                _SignatureEntry(
                    id=signature_id,
                    signer_display=request.signer_name_snapshot,
                    method=SignatureMethod.PEP_OTP.value,
                    signed_at=now,
                    ip=ip,
                    user_agent=user_agent,
                ),
            )

        prev_hash = await self._chain_head()
        # `created_at` берётся под замком цепочки и с `clock_timestamp()`: серверный `now()` — это
        # начало транзакции, и при параллельных подписях порядок по `created_at` расходился с
        # порядком захвата замка (голова цепочки выбиралась не той, цепочка ветвилась).
        created_at = await self._session.scalar(text("SELECT clock_timestamp()"))
        signature = Signature(
            id=signature_id,
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
            created_at=created_at,
        )
        signature.hash = compute_chain_hash(
            prev_hash=prev_hash,
            signature_value=signature_value,
            content_hash=document.content_hash,
            request_id=str(request.id),
            signed_at_iso=now.isoformat(),
        )
        self._session.add(signature)
        await self._session.flush()

        await self._audit.record(
            AuditAction.SIGNATURE_SIGNED,
            entity_type="signature_request",
            entity_id=request.id,
            changes={"signature_id": {"old": None, "new": str(signature.id)}},
        )

        if remaining:
            document.status = SignatureDocumentStatus.PARTIALLY_SIGNED.value
            if document.signing_order == "sequential":
                doc_service = SignatureDocumentService(self._session)
                revealed = await doc_service._activate_turn(document, all_requests)  # noqa: SLF001
                if revealed:
                    # Следующий подписант в цепочке активировался не из
                    # `send()`, отдать ему ссылку через HTTP-ответ некому —
                    # инициатор выдаёт её сам: `POST /signature-requests/{id}/
                    # reissue-link` (`reissue_link`).
                    logger.warning(
                        "external_signer_token_undeliverable_mid_chain",
                        document_id=str(document.id),
                        request_ids=[str(k) for k in revealed],
                    )
            await self._session.flush()

        if document.entity_type == "deal":
            deal = await self._session.get(Deal, document.entity_id)
            if deal is not None:
                _sync_deal_signature_status(
                    deal,
                    document,
                    (
                        DealSignatureStatus.SIGNED.value
                        if not remaining
                        else DealSignatureStatus.PARTIALLY_SIGNED.value
                    ),
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
        self,
        document: SignatureDocument,
        requests: list[SignatureRequest],
        last: _SignatureEntry,
    ) -> None:
        """Штамп, протокол и вложение — на последней подписи. Сама подпись `last` ещё не
        записана (см. `_seal`): её данные приходят отдельно и входят в штамп и протокол."""
        now = dt.datetime.now(dt.UTC)
        document.status = SignatureDocumentStatus.SIGNED.value
        document.completed_at = now

        signatures_stmt = (
            select(Signature)
            .where(Signature.document_id == document.id)
            .order_by(Signature.signed_at)
        )
        signatures = [
            _SignatureEntry(
                id=sig.id,
                signer_display=sig.signer_display,
                method=sig.method,
                signed_at=sig.signed_at,
                ip=sig.ip,
                user_agent=sig.user_agent,
            )
            for sig in (await self._session.execute(signatures_stmt)).scalars().all()
        ]
        signatures.append(last)
        signatures.sort(key=lambda entry: entry.signed_at)

        original = await self._session.get(File, document.file_id)
        original_bytes = await download_object_bytes(
            bucket=original.bucket, key=original.storage_key
        )
        settings = get_settings()
        # QR и ссылка в штампе ведут на страницу проверки веб-клиента (`/verify/{id}`),
        # а не на JSON-ручку.
        verify_url = f"{settings.base_url.rstrip('/')}/verify/{{sig_id}}"
        final_sig_id = str(signatures[-1].id)
        stamp_lines = (
            [
                "Документ подписан простой электронной подписью",
            ]
            + [
                f"{sig.signer_display} / {sig.signed_at.strftime('%d.%m.%Y %H:%M')} UTC"
                for sig in signatures
            ]
            + [f"Хэш: {document.content_hash[:16]}…"]
        )
        try:
            stamped_bytes = await _render_off_loop(
                apply_signature_stamp,
                original_bytes,
                lines=stamp_lines,
                verify_url=verify_url.format(sig_id=final_sig_id),
            )
        except RenderError as exc:
            # Битый PDF раньше давал 500 и откат, и так на каждой повторной подписи. Теперь —
            # понятный отказ (OTP при откате не расходуется): документ надо пересоздать.
            logger.warning(
                "signature_stamp_failed", document_id=str(document.id), error=str(exc)[:200]
            )
            raise AppError(
                ErrorCode.DOCUMENT_NOT_SIGNABLE,
                "Не удалось наложить штамп подписи: PDF документа повреждён. "
                "Пересоздайте документ на подпись",
            ) from exc
        doc_service = SignatureDocumentService(self._session)
        signed_file_id = await doc_service._store_generated_pdf(  # noqa: SLF001
            stamped_bytes, filename=f"signed-{document.id}.pdf", uploaded_by=document.created_by
        )
        document.signed_file_id = signed_file_id

        protocol_bytes = await _render_off_loop(
            render_protocol_pdf,
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

        if document.entity_type == "deal":
            self._session.add(
                Attachment(
                    file_id=signed_file_id,
                    entity_type=document.entity_type,
                    entity_id=document.entity_id,
                    category=AttachmentCategory.SIGNATURE_CONTAINER.value,
                    description=document.title,
                    uploaded_by=document.created_by,
                )
            )
            # Счётчик ссылок, как у остальных вложений (`AttachmentService.create`): подписанный
            # файл не должен удаляться через `DELETE /files/{id}`.
            await self._session.execute(
                update(File)
                .where(File.id == signed_file_id)
                .values(refcount=File.refcount + 1)
                .execution_options(synchronize_session=False)
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
        document = await self._lock_for_action(request)
        if request.status not in OPEN_REQUEST_STATUSES:
            raise AppError(
                ErrorCode.DOCUMENT_NOT_SIGNABLE,
                "Запрос уже завершён и не может быть отклонён",
                extra={"status": request.status},
            )
        if document.status not in {s.value for s in OPEN_DOCUMENT_STATUSES}:
            # Отклонение через соседний запрос «оживляло» аннулированный документ: ему ставился
            # `rejected`, а сделке откатывался статус.
            raise AppError(
                ErrorCode.DOCUMENT_NOT_SIGNABLE,
                "Документ больше не принимает решений",
                extra={"status": document.status},
            )
        now = dt.datetime.now(dt.UTC)
        request.status = SignatureRequestStatus.REJECTED.value
        request.decided_at = now
        request.reject_reason = reason

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
                self._session.add(
                    DealEvent(
                        deal_id=deal.id,
                        event_type=DealEventType.SIGNATURE_REJECTED.value,
                        actor_id=request.signer_user_id,
                        payload={"document_id": str(document.id), "reason": reason},
                    )
                )
                # Откат статуса сделки — только за её активный документ: отказ по старому
                # документу не должен возвращать сделку, уже ушедшую на другой этап.
                if deal.active_signature_document_id == document.id:
                    _sync_deal_signature_status(deal, document, DealSignatureStatus.REJECTED.value)
                    await _apply_deal_signature_outcome(
                        self._session,
                        deal,
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


def pdf_inline_headers(filename: str) -> dict[str, str]:
    """Заголовки ответа с PDF «для просмотра»: `inline`, имя — по RFC 5987 (кириллица),
    без кэширования (документ подписывают, чужой прокси хранить его незачем)."""
    disposition = f"inline; filename=\"document.pdf\"; filename*=UTF-8''{quote(filename, safe='')}"
    return {
        "Content-Disposition": disposition,
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }


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


# Итоги проверки доказательства подписи (`check_signature_evidence`).
INTEGRITY_VERIFIED = "verified"
INTEGRITY_UNAVAILABLE = "unavailable"
INTEGRITY_BROKEN = "broken"


@dataclass(slots=True)
class IntegrityReport:
    """Итог проверки одной подписи: `state` и человекочитаемые причины (для лога, не для клиента:
    публичная проверка отдаёт только состояние)."""

    state: str
    problems: list[str]


def check_signature_evidence(
    signature: Signature, *, secret: str, key_version: int
) -> IntegrityReport:
    """Пересчитывает метку целостности (HMAC) и хэш звена цепочки из того, что лежит в БД.

    Проверка доступна только подписям с сохранённым `nonce` (`evidence.auth.nonce`): он —
    открытый вход HMAC. Подпись без него поставлена до того, как nonce стали сохранять, и
    пересчитать её нечем — это «проверка недоступна», а не подделка. Так же недоступна подпись,
    поставленная ключом другой версии: серверный ключ в настройках один, прежних он не хранит.

    Что покрывает проверка: `content_hash`, идентификатор подписанта, метка времени и nonce —
    через HMAC; `prev_hash`, значение подписи, номер запроса — через хэш звена; сами поля
    доказательства (подписант, документ, время) — через сверку с записью. Связь с предыдущим
    звеном проверяет `VerifyService`, ему нужна БД."""
    evidence = signature.evidence if isinstance(signature.evidence, dict) else {}
    auth = evidence.get("auth") if isinstance(evidence.get("auth"), dict) else {}
    nonce = auth.get("nonce")
    if not isinstance(nonce, str) or not nonce:
        return IntegrityReport(
            INTEGRITY_UNAVAILABLE, ["в доказательстве нет nonce (подпись до его сохранения)"]
        )
    if signature.key_version != key_version:
        return IntegrityReport(
            INTEGRITY_UNAVAILABLE,
            [f"подпись поставлена ключом версии {signature.key_version}: его в настройках нет"],
        )

    signer = evidence.get("signer") if isinstance(evidence.get("signer"), dict) else {}
    time_block = evidence.get("time") if isinstance(evidence.get("time"), dict) else {}
    document = evidence.get("document") if isinstance(evidence.get("document"), dict) else {}
    signer_id = signer.get("id")
    signed_at_iso = time_block.get("signed_at")
    if not isinstance(signer_id, str) or not isinstance(signed_at_iso, str):
        return IntegrityReport(
            INTEGRITY_BROKEN, ["в доказательстве нет подписанта или времени подписи"]
        )

    problems: list[str] = []
    expected_value = compute_signature_value(
        secret=secret,
        content_hash=signature.content_hash,
        signer_id=signer_id,
        signed_at_iso=signed_at_iso,
        nonce=nonce,
    )
    if not hmac.compare_digest(expected_value, signature.signature_value):
        problems.append("значение подписи не совпадает с пересчитанным HMAC")

    try:
        evidence_time = dt.datetime.fromisoformat(signed_at_iso)
    except ValueError:
        problems.append("время подписи в доказательстве не разобрать")
    else:
        if evidence_time.tzinfo is None:
            evidence_time = evidence_time.replace(tzinfo=dt.UTC)
        if evidence_time != signature.signed_at:
            problems.append("время подписи в доказательстве расходится с записью")
    if document.get("content_hash") not in (None, signature.content_hash):
        problems.append("хэш документа в доказательстве расходится с записью")

    expected_hash = compute_chain_hash(
        prev_hash=signature.prev_hash,
        signature_value=signature.signature_value,
        content_hash=signature.content_hash,
        request_id=str(signature.request_id),
        signed_at_iso=signed_at_iso,
    )
    if expected_hash != signature.hash:
        problems.append("хэш звена цепочки не совпадает с пересчитанным")
    return IntegrityReport(INTEGRITY_BROKEN if problems else INTEGRITY_VERIFIED, problems)


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

    async def check_integrity(self, signature: Signature) -> IntegrityReport:
        """Доказательство подписи и её место в цепочке: см. `check_signature_evidence`.

        К пересчёту добавляется связь с предыдущим звеном: `prev_hash` обязан указывать на
        существующую подпись (или быть началом цепочки). Порядок звеньев по времени здесь не
        сверяется: до исправления гонки (`created_at` брался из начала транзакции) порядок мог
        расходиться с цепочкой у подписей, поставленных параллельно, и это была бы ложная тревога.
        Подписи без nonce не проверяются вовсе — как и в `check_signature_evidence`."""
        settings = get_settings()
        report = check_signature_evidence(
            signature,
            secret=settings.signature_server_secret.get_secret_value(),
            key_version=settings.signature_key_version,
        )
        if report.state == INTEGRITY_UNAVAILABLE:
            return report
        prev_hash = signature.prev_hash
        if prev_hash and prev_hash != GENESIS_HASH:
            predecessor = await self._session.scalar(
                select(Signature.id).where(Signature.hash == prev_hash).limit(1)
            )
            if predecessor is None:
                report.problems.append("звено цепочки ссылается на несуществующую подпись")
        if report.problems:
            report.state = INTEGRITY_BROKEN
        return report

    async def verify_by_id(self, signature_id: uuid.UUID) -> dict[str, Any]:
        signature = await self._session.get(Signature, signature_id)
        if signature is None:
            return {"status": "not_found"}
        document = await self._session.get(SignatureDocument, signature.document_id)
        integrity = await self.check_integrity(signature)
        if integrity.state == INTEGRITY_BROKEN:
            # Причины — только в лог: клиенту публичной проверки устройство защиты не нужно.
            logger.error(
                "signature_integrity_broken",
                signature_id=str(signature.id),
                problems=integrity.problems,
            )
        status = "valid"
        if integrity.state == INTEGRITY_BROKEN:
            # Сильнее прочих статусов: подделанная запись не «оспорена» и не «аннулирована».
            status = "tampered"
        elif signature.is_disputed:
            status = "disputed"
        elif document is not None and document.status == SignatureDocumentStatus.VOID.value:
            status = "void"
        return {
            "status": status,
            "integrity": integrity.state,
            "signature_id": signature.id,
            "signer_display": signature.signer_display,
            "signed_at": signature.signed_at,
            "document_hash": signature.content_hash,
            "method": signature.method,
            "is_disputed": signature.is_disputed,
        }

    async def verify_by_file(self, file_bytes: bytes) -> dict[str, Any]:
        digest = hashlib.sha256(file_bytes).hexdigest()
        # Проверяют не только исходный PDF (`content_hash` подписи), но и копию со
        # штампом `signed-{id}.pdf` — файл документа в `signed_file_id`: у него
        # другой хэш, а подписи те же.
        stamped_documents = (
            select(SignatureDocument.id)
            .join(File, File.id == SignatureDocument.signed_file_id)
            .where(File.sha256 == digest)
        )
        stmt = (
            select(Signature)
            .where(
                or_(Signature.content_hash == digest, Signature.document_id.in_(stamped_documents))
            )
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
        open_statuses = [s.value for s in OPEN_REQUEST_STATUSES]
        # Порядок блокировок тот же, что у подписи (документы, затем запросы): иначе аннулирование
        # при увольнении и подпись этого же документа ждали бы друг друга.
        document_ids = list(
            (
                await session.execute(
                    select(SignatureRequest.document_id)
                    .where(
                        SignatureRequest.signer_user_id == user_id,
                        SignatureRequest.status.in_(open_statuses),
                    )
                    .distinct()
                )
            )
            .scalars()
            .all()
        )
        if not document_ids:
            return 0
        documents = list(
            (
                await session.execute(
                    select(SignatureDocument)
                    .where(SignatureDocument.id.in_(document_ids))
                    .order_by(SignatureDocument.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        requests = list(
            (
                await session.execute(
                    select(SignatureRequest)
                    .where(
                        SignatureRequest.signer_user_id == user_id,
                        SignatureRequest.status.in_(open_statuses),
                    )
                    .order_by(SignatureRequest.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        for request in requests:
            request.status = SignatureRequestStatus.VOID.value

        open_doc_statuses = {s.value for s in OPEN_DOCUMENT_STATUSES} | {"draft"}
        for document in documents:
            if document.status not in open_doc_statuses:
                continue
            document.void_reason = reason
            document.status = SignatureDocumentStatus.VOID.value
            document.voided_by = None
            # Соседние запросы аннулированного документа тоже гасятся: раньше они оставались
            # `sent`, и подпись коллеги «оживляла» документ, ставя ему `signed`.
            siblings = (
                (
                    await session.execute(
                        select(SignatureRequest)
                        .where(
                            SignatureRequest.document_id == document.id,
                            SignatureRequest.status.in_(open_statuses),
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            for sibling in siblings:
                sibling.status = SignatureRequestStatus.VOID.value
            if document.entity_type == "deal":
                deal = await session.get(Deal, document.entity_id)
                if deal is not None:
                    _sync_deal_signature_status(deal, document, DealSignatureStatus.VOID.value)
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
