"""Фоновые задачи ПЭП (spec.txt §15: `signature.expire_deadlines`,
`signature.clean_otp`). Тем же тиком, что и сроки подписания, соглашения об
ЭДО с истёкшим `valid_to` получают статус `expired`.

Частичный индекс `ix_signature_documents_status_deadline` (только
`pending`/`partially_signed`) держит выборку быстрой независимо от общего
числа документов — тот же приём, что `ix_deals_sla_due_open` в
`app/modules/crm/tasks.py`.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import session_scope
from app.core.metrics import track_task
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.crm.models import Deal
from app.modules.crm.models import SignatureStatus as DealSignatureStatus
from app.modules.notification.service import NotificationPriority, get_notification_service
from app.modules.signing.models import (
    OPEN_REQUEST_STATUSES,
    EdmAgreement,
    EdmAgreementStatus,
    SignatureDocument,
    SignatureOtpCode,
    SignatureRequest,
)
from app.modules.signing.models import SignatureDocumentStatus as DocStatus
from app.modules.signing.models import SignatureRequestStatus as ReqStatus
from app.modules.signing.service import (
    TPL_SIGNATURE_EXPIRED,
    _apply_deal_signature_outcome,
    _sync_deal_signature_status,
    suspend_requests_of_agreement,
)

logger = structlog.get_logger(__name__)

#: `signature_otp_codes` хранит ретеншен 30 дней (dop.md §10.9) — доказательство
#: лежит в `signatures.evidence`, сами коды нужны только на срок жизни OTP.
OTP_RETENTION_DAYS = 30


@track_task
async def sweep_signature_deadlines(ctx: dict[str, Any]) -> dict[str, int]:
    now = dt.datetime.now(dt.UTC)
    expired = 0

    async with session_scope() as session:
        stmt = select(SignatureDocument).where(
            SignatureDocument.status.in_(
                [DocStatus.PENDING.value, DocStatus.PARTIALLY_SIGNED.value]
            ),
            SignatureDocument.deadline_at.is_not(None),
            SignatureDocument.deadline_at < now,
        )
        # Строки под замком: документ, который в этот момент подписывают, пропускается до
        # следующего тика (не истекает под руками подписанта), а два воркера не берут один и
        # тот же документ.
        documents = list(
            (
                await session.execute(
                    stmt.order_by(SignatureDocument.id).with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )

        for document in documents:
            document.status = DocStatus.EXPIRED.value
            requests_stmt = select(SignatureRequest).where(
                SignatureRequest.document_id == document.id,
                SignatureRequest.status.in_([s.value for s in OPEN_REQUEST_STATUSES]),
            )
            for request in (await session.execute(requests_stmt.with_for_update())).scalars().all():
                request.status = ReqStatus.EXPIRED.value
            await session.flush()

            await AuditService(session).record(
                AuditAction.SIGNATURE_VOID,
                entity_type="signature_document",
                entity_id=document.id,
                changes={
                    "status": {"old": None, "new": "expired"},
                    "reason": {"old": None, "new": "deadline"},
                },
            )
            if document.created_by:
                await get_notification_service().notify_user(
                    session,
                    recipient_id=document.created_by,
                    template_code=TPL_SIGNATURE_EXPIRED,
                    priority=NotificationPriority.HIGH,
                    entity_type="signature_document",
                    entity_id=document.id,
                )
            if document.entity_type == "deal":
                deal = await session.get(Deal, document.entity_id)
                # Только за активный документ сделки: истёкший старый документ не должен ни
                # менять статус подписи новой, ни откатывать сделку по правилу `on_expired`.
                if deal is not None and deal.active_signature_document_id == document.id:
                    _sync_deal_signature_status(deal, document, DealSignatureStatus.EXPIRED.value)
                    await _apply_deal_signature_outcome(
                        session,
                        deal,
                        rule=document.on_expired,
                        note=f"Истёк срок подписания документа «{document.title}»",
                    )
            expired += 1

        agreements_expired = await _expire_edm_agreements(session, today=dt.date.today())

    if expired or agreements_expired:
        logger.info(
            "signature_deadlines_swept", expired=expired, agreements_expired=agreements_expired
        )
    return {"expired": expired, "agreements_expired": agreements_expired}


async def _expire_edm_agreements(session: AsyncSession, *, today: dt.date) -> int:
    """`valid_to` включительно (`EdmAgreementService.find_active_for_contact`):
    просрочено то, что закончилось вчера и раньше. Отозванные не трогаем."""
    stmt = select(EdmAgreement).where(
        EdmAgreement.status == EdmAgreementStatus.ACTIVE.value,
        EdmAgreement.valid_to.is_not(None),
        EdmAgreement.valid_to < today,
    )
    agreements = list((await session.execute(stmt)).scalars().all())
    for agreement in agreements:
        agreement.status = EdmAgreementStatus.EXPIRED.value
        await session.flush()
        # Запросы, опиравшиеся на соглашение, больше не подписываются (см. `suspend_...`).
        await suspend_requests_of_agreement(session, agreement, reason="agreement_expired")
        await AuditService(session).record(
            AuditAction.EDM_AGREEMENT_EXPIRED,
            entity_type="edm_agreement",
            entity_id=agreement.id,
            changes={
                "status": {"old": EdmAgreementStatus.ACTIVE.value, "new": agreement.status},
                "reason": {"old": None, "new": "valid_to_passed"},
            },
        )
    return len(agreements)


@track_task
async def sweep_signature_otp_cleanup(ctx: dict[str, Any]) -> dict[str, int]:
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=OTP_RETENTION_DAYS)
    async with session_scope() as session:
        result = await session.execute(
            delete(SignatureOtpCode).where(SignatureOtpCode.created_at < cutoff)
        )
    removed = result.rowcount or 0
    logger.info("signature_otp_codes_purged", removed=removed)
    return {"removed": removed}
