"""Приём заявок с сайта (Laravel CMS), new_spec §4.14.

`POST /api/v1/integrations/cms/leads`: HMAC-подписанный вебхук без сессии
(источник — не пользователь браузера, у CMS нет учётки Keycloak). Тело
сохраняется в `inbound_messages` до разбора (раздел 3.6: «входящие —
inbox»), затем — найти-или-создать `Contact` (`ContactService.find_or_create`)
и создать `Deal` в `b2c_individual_v1` тем же `DealService.create()`, что и ручной
ввод КАМа, с `principal.role == INTEGRATION` (раздел 6.6, уже проверяется в
`crm.service.DealService.create` — этот модуль просто собирает payload и берёт
синтетический Principal, свою ветку создания сделки не пишет). Заявка с номером заказа
(«Номер заявки» из выгрузки оплат) идёт через `OrderIngestService` — тем же путём, что
загрузка файла оплат: оплаченный заказ, идемпотентный по номеру.

Порядок обработки важен — каждый шаг закрывает конкретный дефект:

1. **Подпись — до всего остального.** Раньше повтор по `Idempotency-Key` искался первым, и запрос
   с чужой подписью и угаданным ключом «занимал» ключ: настоящая доставка получала `200 failed`.
   Теперь неверная подпись не оставляет ничего под ключом отправителя — только запись-улику под
   собственным `invalid:<uuid>` и событие безопасности.
2. **Тело — JSON-объект**, иначе 422 (а не 500 на `null`, `[]`, строке или числе).
3. **Повтор по ключу** возвращает итог первой обработки (`processed`/`duplicate`). Прежняя неудача
   (`failed`) итогом не считается: отправитель повторяет доставку именно потому, что она не
   прошла, и получает новую попытку, а не вечное «failed».
4. **Обработка целиком в SAVEPOINT.** При отказе откатывается только она, а «сырое» тело остаётся
   во входящих со статусом `failed` и очищенной причиной (без SQL и трассировок); запись
   фиксируется до ответа с ошибкой — иначе транзакция запроса унесла бы её вместе с откатом.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any, NoReturn

import structlog
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import get_request_id
from app.core.errors import AppError, ErrorCode
from app.core.normalize import company_key
from app.core.security import Principal
from app.modules.catalog.lookup import ProductIndex
from app.modules.catalog.models import Contact, Product
from app.modules.catalog.service import ContactService
from app.modules.crm.models import Deal, DealComment, DealProduct
from app.modules.crm.schemas import DealCreateRequest, DealProductIn
from app.modules.crm.service import DealService
from app.modules.integration.lead_payload import (
    LeadPayload,
    parse_json_object,
    parse_lead_payload,
    raw_evidence,
)
from app.modules.integration.models import InboundMessage, InboundStatus, IntegrationSourceCode
from app.modules.integration.orders import OrderData, OrderIngestService
from app.modules.integration.security import record_signature_failure, verify_signature
from app.modules.integration.service import get_integration_principal, pick_least_loaded_owner

logger = structlog.get_logger(__name__)

_SOURCE = IntegrationSourceCode.CMS.value
_TITLE_MAX = 255
_ERROR_MAX = 1000
_INTERNAL_DETAIL = "Внутренняя ошибка сервера. Обратитесь к администратору с request_id."
# Итоги, которые повтор доставки не пересчитывает.
_FINAL_STATUSES = frozenset({InboundStatus.PROCESSED.value, InboundStatus.DUPLICATE.value})
_CLAIM_ATTEMPTS = 3


@dataclass(slots=True)
class LeadIngestResult:
    message: InboundMessage
    deal_id: uuid.UUID | None
    contact_id: uuid.UUID | None


@dataclass(slots=True)
class _Outcome:
    deal: Deal
    contact: Contact
    #: Новая сделка не заводилась: заявка совпала с уже существующей.
    duplicate: bool
    message_type: str


def _describe(exc: AppError) -> str:
    """Причина отказа для журнала входящих: текст ошибки каталога и поля, без значений из тела —
    ни SQL, ни трассировки, ни чужих данных."""
    if not exc.errors:
        return exc.detail[:_ERROR_MAX]
    if len({error.reason for error in exc.errors}) == 1:
        fields = ", ".join(error.field for error in exc.errors)
        return f"{exc.detail} ({fields})"[:_ERROR_MAX]
    problems = "; ".join(f"{error.field} — {error.reason}" for error in exc.errors)
    return f"{exc.detail}: {problems}"[:_ERROR_MAX]


class CmsLeadService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def ingest(
        self,
        *,
        raw_body: bytes,
        secret: str | None,
        signature_header: str | None,
        idempotency_key: str,
        ip: str | None = None,
        user_agent: str | None = None,
        timestamp: str | None = None,
        require_timestamp: bool = False,
    ) -> LeadIngestResult:
        if not verify_signature(
            secret,
            raw_body,
            signature_header,
            timestamp=timestamp,
            require_timestamp=require_timestamp,
        ):
            await self._reject_signature(
                raw_body, claimed_key=idempotency_key, ip=ip, user_agent=user_agent
            )

        try:
            payload = parse_json_object(raw_body)
        except AppError as exc:
            await self._store_evidence(
                raw_body, prefix="malformed", error=_describe(exc), signature_valid=True
            )
            # Тело отклонено штатно, а не сбоем: без явного commit запись-улика ушла бы в откат
            # вместе с ответом 422.
            await self._session.commit()
            raise

        message, replay = await self._claim(idempotency_key, payload)
        if replay:
            return await self._stored_result(message)

        try:
            async with self._session.begin_nested():
                outcome = await self._process(payload, idempotency_key)
        except AppError as exc:
            # Ошибка каталога (422 «нет курса», 503 «служебной учётки нет» и т.п.) — текст написан
            # нами, без SQL и чужих данных: он и уходит отправителю, и остаётся во входящих.
            log = logger.error if exc.status >= 500 else logger.info
            log("cms_lead_rejected", message_id=str(message.id), error_code=exc.code.value)
            await self._fail(message, _describe(exc))
            raise
        except Exception as exc:  # noqa: BLE001 — тело фиксируем в любом случае
            await self._fail_unexpected(message, exc)

        message.status = (
            InboundStatus.DUPLICATE.value if outcome.duplicate else InboundStatus.PROCESSED.value
        )
        message.message_type = outcome.message_type
        message.resulting_entity_type = "deal"
        message.resulting_entity_id = outcome.deal.id
        message.processed_at = dt.datetime.now(dt.UTC)
        await self._session.flush()
        return LeadIngestResult(
            message=message, deal_id=outcome.deal.id, contact_id=outcome.contact.id
        )

    # --- Журнал входящих ---------------------------------------------------------------

    async def _store_evidence(
        self, raw_body: bytes, *, prefix: str, error: str, signature_valid: bool
    ) -> InboundMessage:
        """Запись-улика: тело, которое нельзя привязать к ключу отправителя (чужая подпись, битый
        JSON). Собственный `<prefix>:<uuid>` вместо `Idempotency-Key` — иначе такой запрос занял
        бы ключ, под которым потом придёт настоящая доставка."""
        message = InboundMessage(
            source_code=_SOURCE,
            external_id=f"{prefix}:{uuid.uuid4()}",
            message_type="lead",
            raw_payload=raw_evidence(raw_body),
            signature_valid=signature_valid,
            status=InboundStatus.FAILED.value,
            error=error[:_ERROR_MAX],
        )
        self._session.add(message)
        await self._session.flush()
        return message

    async def _reject_signature(
        self, raw_body: bytes, *, claimed_key: str, ip: str | None, user_agent: str | None
    ) -> NoReturn:
        message = await self._store_evidence(
            raw_body, prefix="invalid", error="invalid_signature", signature_valid=False
        )
        await record_signature_failure(
            self._session,
            source_code=_SOURCE,
            external_id=claimed_key,
            ip=ip,
            user_agent=user_agent,
            details={"inbound_message_id": str(message.id)},
        )
        # `core.db.get_db_session` документированно откатывает ВСЮ транзакцию на любом
        # исключении, включая запись аудита (раздел 1: «требование атомарности») — правильно
        # для обычных ошибок, но здесь `raise` ниже как раз и есть штатный исход (запрос
        # корректно отклонён), а не сбой. Без явного commit здесь этот `InboundMessage`/
        # `SecurityEvent` откатывались бы вместе с ответом 401 — попытка подбора подписи не
        # оставляла бы следов, прямое нарушение раздела 3.3 («Отказы в доступе логируются
        # тоже»). Тот же (пока не исправленный) пробел уже есть в `identity.service`'s «нет
        # роли CRM» и `signing.service._fail_otp` — см. sprint9-integration-implementation.md.
        await self._session.commit()
        raise AppError(
            ErrorCode.INTEGRATION_BAD_SIGNATURE, "Подпись вебхука недействительна", status=401
        )

    async def _find_message(self, key: str) -> InboundMessage | None:
        # FOR UPDATE: два одновременных повтора одной неудачной доставки не должны оба
        # обработать заявку — второй дождётся первого и увидит его итог.
        return (
            await self._session.execute(
                select(InboundMessage)
                .where(InboundMessage.source_code == _SOURCE, InboundMessage.external_id == key)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    async def _claim(self, key: str, payload: dict[str, Any]) -> tuple[InboundMessage, bool]:
        """Место под `Idempotency-Key`: `(сообщение, это_повтор_с_итогом)`."""
        for _attempt in range(_CLAIM_ATTEMPTS):
            existing = await self._find_message(key)
            if existing is not None:
                if existing.status in _FINAL_STATUSES:
                    return existing, True
                # `failed` (или недописанное `received`): доставка повторена после неудачи —
                # обрабатываем заново на той же записи, ключ уникален.
                existing.raw_payload = payload
                existing.signature_valid = True
                existing.status = InboundStatus.RECEIVED.value
                existing.error = None
                existing.resulting_entity_type = None
                existing.resulting_entity_id = None
                existing.processed_at = None
                await self._session.flush()
                return existing, False

            message = InboundMessage(
                source_code=_SOURCE,
                external_id=key,
                message_type="lead",
                raw_payload=payload,
                signature_valid=True,
                status=InboundStatus.RECEIVED.value,
            )
            try:
                async with self._session.begin_nested():
                    self._session.add(message)
                    await self._session.flush()
            except IntegrityError:
                # Тот же ключ одновременно занял параллельный запрос: читаем его итог.
                continue
            return message, False
        raise AppError(ErrorCode.INTERNAL, _INTERNAL_DETAIL)

    async def _stored_result(self, message: InboundMessage) -> LeadIngestResult:
        deal_id = message.resulting_entity_id if message.resulting_entity_type == "deal" else None
        contact_id = None
        if deal_id is not None:
            contact_id = await self._session.scalar(
                select(Deal.contact_id).where(Deal.id == deal_id)
            )
        return LeadIngestResult(message=message, deal_id=deal_id, contact_id=contact_id)

    async def _fail(self, message: InboundMessage, error: str) -> None:
        """Фиксирует неудачу вместе с телом: коммит до ответа, иначе откат запроса унесёт запись."""
        message.status = InboundStatus.FAILED.value
        message.error = error[:_ERROR_MAX]
        message.resulting_entity_type = None
        message.resulting_entity_id = None
        await self._session.flush()
        await self._session.commit()

    async def _fail_unexpected(self, message: InboundMessage, exc: Exception) -> NoReturn:
        # Трассировка — в лог сервера; во входящих и в ответе только обезличенный текст: в тексте
        # исключения бывают SQL и значения из тела.
        logger.exception(
            "cms_lead_failed", message_id=str(message.id), error_type=type(exc).__name__
        )
        request_id = get_request_id()
        note = "Внутренняя ошибка обработки заявки"
        await self._fail(message, f"{note} (request_id={request_id})" if request_id else note)
        raise AppError(ErrorCode.INTERNAL, _INTERNAL_DETAIL) from exc

    # --- Обработка ---------------------------------------------------------------------

    async def _process(self, payload: dict[str, Any], key: str) -> _Outcome:
        lead = parse_lead_payload(payload)
        principal = await get_integration_principal(self._session)
        products = ProductIndex(self._session)
        if lead.is_order:
            return await self._process_order(principal, lead, products)
        return await self._process_lead(principal, lead, products, key)

    @staticmethod
    async def _find_product(products: ProductIndex, lead: LeadPayload) -> Product | None:
        """Продукт каталога по названию или коду из тела (первый нашедшийся). Не находится — `None`:
        для обычного лида продукт не заводится."""
        for candidate in lead.course_candidates:
            product = await products.get(candidate)
            if product is not None:
                return product
        return None

    async def _process_order(
        self, principal: Principal, lead: LeadPayload, products: ProductIndex
    ) -> _Outcome:
        product = await self._find_product(products, lead)
        # `OrderIngestService` ищет продукт по одному названию: если сайт прислал код, отдаём ему
        # каноническое название найденного продукта, а не заводим второй с названием-кодом.
        course = product.name if product is not None else lead.course
        result = await OrderIngestService(
            self._session, source=_SOURCE, initiator_id=principal.user_id, products=products
        ).ingest(
            OrderData(
                order_number=lead.order_number or "",
                course=course or "",
                last_name=lead.last_name,
                first_name=lead.first_name,
                middle_name=lead.middle_name,
                email=lead.email,
                phone=lead.phone,
                stream_number=lead.stream_number,
                amount=lead.amount,
            ),
            mode="upsert",
        )
        if result.created_deal:
            await self._add_note(result.deal, lead, product_line=True)
        return _Outcome(
            deal=result.deal,
            contact=result.contact,
            duplicate=not result.created_deal,
            message_type="order",
        )

    async def _process_lead(
        self, principal: Principal, lead: LeadPayload, products: ProductIndex, key: str
    ) -> _Outcome:
        first_name, last_name, middle_name, nameless = lead.lead_names()
        contact_external_ids: dict[str, Any] = {"cms_lead_id": key}
        if nameless:
            # Имя не прислали: контакт заведён с заглушкой и ждёт, пока менеджер его довёдет.
            contact_external_ids["needs_normalization"] = True
        upsert = await ContactService(self._session).find_or_create(
            first_name=first_name,
            last_name=last_name,
            middle_name=middle_name,
            email=lead.email,
            phone=lead.phone,
            source=_SOURCE,
            created_by=principal.user_id,
            external_ids=contact_external_ids,
        )
        contact = upsert.contact
        product = await self._find_product(products, lead)

        if not upsert.created:
            existing = await self._open_duplicate(contact, product, lead)
            if existing is not None:
                await self._add_note(existing, lead, product_line=True, repeated=True)
                return _Outcome(deal=existing, contact=contact, duplicate=True, message_type="lead")

        deal_external_ids: dict[str, Any] = {"cms_lead_id": key}
        if lead.external_id:
            deal_external_ids["cms_external_id"] = lead.external_id
        price = (
            lead.amount if lead.amount is not None else (product.base_price if product else None)
        )
        deal = await DealService(self._session).create(
            principal,
            DealCreateRequest(
                title=(lead.course or f"Заявка с сайта {key}")[:_TITLE_MAX],
                deal_type="b2c",
                contact_id=contact.id,
                owner_id=await pick_least_loaded_owner(self._session),
                amount=lead.amount,
                source=_SOURCE,
                external_ids=deal_external_ids,
                products=(
                    [
                        DealProductIn(
                            product_id=product.id,
                            quantity=1,
                            price=price,
                            total=price,
                            stream_number=lead.stream_number,
                        )
                    ]
                    if product is not None
                    else []
                ),
            ),
        )
        await self._add_note(deal, lead, product_line=product is not None)
        return _Outcome(deal=deal, contact=contact, duplicate=False, message_type="lead")

    async def _open_duplicate(
        self, contact: Contact, product: Product | None, lead: LeadPayload
    ) -> Deal | None:
        """Открытая B2C-сделка человека, которую эта заявка повторяет (dop.md-стиль дедупликации,
        раздел 11.6, применённый к контактам). Раньше дублем считалась ЛЮБАЯ открытая сделка — и
        заявка того же человека на другой курс терялась. Теперь: заявка без продукта повторяет любую
        открытую сделку; с продуктом — только сделку с тем же продуктом (нет в каталоге — с тем же
        названием)."""
        deals = list(
            (
                await self._session.execute(
                    select(Deal)
                    .where(
                        Deal.contact_id == contact.id,
                        Deal.deal_type == "b2c",
                        Deal.closed_at.is_(None),
                        Deal.deleted_at.is_(None),
                    )
                    .order_by(Deal.created_at.desc(), Deal.id.desc())
                )
            )
            .scalars()
            .all()
        )
        if not deals or not lead.course_candidates:
            return deals[0] if deals else None
        if product is not None:
            with_product = set(
                (
                    await self._session.execute(
                        select(DealProduct.deal_id).where(
                            DealProduct.deal_id.in_([deal.id for deal in deals]),
                            DealProduct.product_id == product.id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            return next((deal for deal in deals if deal.id in with_product), None)
        wanted = company_key(lead.course)
        return next((deal for deal in deals if company_key(deal.title) == wanted), None)

    async def _add_note(
        self,
        deal: Deal,
        lead: LeadPayload,
        *,
        product_line: bool,
        repeated: bool = False,
    ) -> None:
        """Что сайт сообщил сверх данных клиента и курса — системным комментарием в ленте сделки:
        менеджер видит его в карточке, а не только в журнале входящих."""
        lines: list[str] = []
        if lead.comment:
            lines.append(f"Комментарий: {lead.comment}")
        if lead.source_url:
            lines.append(f"Страница: {lead.source_url}")
        if lead.stream_number is not None and not product_line:
            # Потоку некуда лечь: продукт из каталога не определился, строки продукта нет.
            lines.append(f"Номер потока: {lead.stream_number}")
        if not lines:
            return
        if lead.created_at is not None:
            sent_at = lead.created_at
            lines.append(f"Время заявки на сайте: {sent_at:%d.%m.%Y %H:%M} ({sent_at.tzname()})")
        title = "Повторная заявка с сайта" if repeated else "Заявка с сайта"
        self._session.add(
            DealComment(
                deal_id=deal.id, author_id=None, body="\n".join([title, *lines]), is_system=True
            )
        )
        await self._session.flush()
