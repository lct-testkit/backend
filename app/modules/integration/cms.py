"""Приём заявок с сайта (Laravel CMS), new_spec §4.14.

`POST /api/v1/integrations/cms/leads`: HMAC-подписанный вебхук без сессии
(источник — не пользователь браузера, у CMS нет учётки Keycloak). Тело
сохраняется в `inbound_messages` до разбора (раздел 3.6: «входящие —
inbox»), затем — найти-или-создать `Contact` и создать `Deal` в
`b2c_individual_v1` тем же `DealService.create()`, что и ручной ввод КАМа, с
`principal.role == INTEGRATION` (раздел 6.6, уже проверяется в
`crm.service.DealService.create` — этот модуль просто собирает payload и
берёт синтетический Principal, свою ветку создания сделки не пишет).
"""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode
from app.modules.catalog.models import Contact
from app.modules.catalog.schemas import ContactCreateRequest
from app.modules.catalog.service import ContactService
from app.modules.crm.models import Deal
from app.modules.crm.schemas import DealCreateRequest
from app.modules.crm.service import DealService
from app.modules.integration.models import InboundMessage, InboundStatus, IntegrationSourceCode
from app.modules.integration.security import record_signature_failure, verify_signature
from app.modules.integration.service import get_integration_principal, pick_least_loaded_owner

logger = structlog.get_logger(__name__)


class CmsLeadService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def ingest(
        self,
        *,
        raw_body: bytes,
        secret: str | None,
        signature_header: str | None,
        external_id: str,
        payload: dict[str, Any],
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> InboundMessage:
        existing = (
            await self._session.execute(
                select(InboundMessage).where(
                    InboundMessage.source_code == IntegrationSourceCode.CMS.value,
                    InboundMessage.external_id == external_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            # Повтор доставки того же вебхука (раздел 4.14: `Idempotency-Key`) —
            # не обрабатываем второй раз, отдаём то, чем закончился первый.
            return existing

        signature_valid = verify_signature(secret, raw_body, signature_header)
        message = InboundMessage(
            source_code=IntegrationSourceCode.CMS.value,
            external_id=external_id,
            message_type="lead",
            raw_payload=payload,
            signature_valid=signature_valid,
            status=InboundStatus.RECEIVED.value,
        )
        self._session.add(message)
        await self._session.flush()

        if not signature_valid:
            message.status = InboundStatus.FAILED.value
            message.error = "invalid_signature"
            await record_signature_failure(
                self._session,
                source_code=IntegrationSourceCode.CMS.value,
                external_id=external_id,
                ip=ip,
                user_agent=user_agent,
            )
            # `core.db.get_db_session` документированно откатывает ВСЮ
            # транзакцию на любом исключении, включая запись аудита (раздел
            # 1: «требование атомарности») — правильно для обычных ошибок,
            # но здесь `raise` ниже как раз и есть штатный исход (запрос
            # корректно отклонён), а не сбой. Без явного commit здесь этот
            # `InboundMessage`/`SecurityEvent` откатывались бы вместе с
            # ответом 401 — попытка подбора подписи не оставляла бы следов,
            # прямое нарушение раздела 3.3 («Отказы в доступе логируются
            # тоже»). Тот же (пока не исправленный) пробел уже есть в
            # `identity.service`'s «нет роли CRM» и `signing.service._fail_otp`
            # — см. sprint9-integration-implementation.md.
            await self._session.commit()
            raise AppError(
                ErrorCode.INTEGRATION_BAD_SIGNATURE, "Подпись вебхука недействительна", status=401
            )

        try:
            deal, is_duplicate = await self._create_or_find_lead(payload, external_id)
        except Exception as exc:  # noqa: BLE001 — тело фиксируем в любом случае
            message.status = InboundStatus.FAILED.value
            message.error = str(exc)[:2000]
            await self._session.flush()
            raise

        message.status = (
            InboundStatus.DUPLICATE.value if is_duplicate else InboundStatus.PROCESSED.value
        )
        message.resulting_entity_type = "deal"
        message.resulting_entity_id = deal.id
        message.processed_at = message.received_at
        await self._session.flush()
        return message

    async def _create_or_find_lead(
        self, payload: dict[str, Any], external_id: str
    ) -> tuple[Deal, bool]:
        principal = await get_integration_principal(self._session)
        phone = (payload.get("phone") or "").strip() or None
        email = (payload.get("email") or "").strip() or None

        contact = None
        if phone:
            contact = (
                (
                    await self._session.execute(
                        select(Contact).where(Contact.phone == phone, Contact.deleted_at.is_(None))
                    )
                )
                .scalars()
                .first()
            )
        if contact is None and email:
            contact = (
                (
                    await self._session.execute(
                        select(Contact).where(Contact.email == email, Contact.deleted_at.is_(None))
                    )
                )
                .scalars()
                .first()
            )

        if contact is not None:
            # dop.md-стиль дедупликации (раздел 11.6, применённый здесь к
            # контактам, а не только к организациям): тот же человек уже
            # писал нам — не плодим вторую сделку, если по нему уже есть
            # незакрытая B2C-сделка того же типа продукта.
            open_deal = (
                (
                    await self._session.execute(
                        select(Deal).where(
                            Deal.contact_id == contact.id,
                            Deal.deal_type == "b2c",
                            Deal.closed_at.is_(None),
                            Deal.deleted_at.is_(None),
                        )
                    )
                )
                .scalars()
                .first()
            )
            if open_deal is not None:
                return open_deal, True
        else:
            contact_payload = ContactCreateRequest(
                first_name=payload.get("first_name") or "Без имени",
                last_name=payload.get("last_name") or "—",
                middle_name=payload.get("middle_name"),
                email=email,
                phone=phone,
                source=IntegrationSourceCode.CMS.value,
                external_ids={"cms_lead_id": external_id},
            )
            contact = await ContactService(self._session).create(principal, contact_payload)

        owner_id = await pick_least_loaded_owner(self._session)
        deal_payload = DealCreateRequest(
            title=payload.get("product_name") or f"Заявка с сайта {external_id}",
            deal_type="b2c",
            contact_id=contact.id,
            owner_id=owner_id,
            source=IntegrationSourceCode.CMS.value,
            external_ids={"cms_lead_id": external_id},
        )
        deal = await DealService(self._session).create(principal, deal_payload)
        return deal, False
