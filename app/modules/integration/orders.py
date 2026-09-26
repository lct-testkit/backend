"""Оплаченный заказ физлица на курс — общий путь для вебхука сайта и импорта файла оплат.

Один и тот же заказ («Номер заявки» `ORD-…`) приходит разными дорогами: сайт шлёт его вебхуком,
менеджер загружает выгрузку «Данные оплат». Логика приёма одна, иначе два пути дали бы разные
контакты и разные сделки на одного человека:

1. **Контакт** — `ContactService.find_or_create`: тот же человек по email (без регистра) либо по
   телефону при той же фамилии; пустое дозаполняется, заполненное не трогается.
2. **Продукт** — по названию курса (`ProductIndex`); нет в каталоге — заводится и помечается
   `custom_fields.auto_created`, чтобы оплата не терялась из-за того, что каталог курсов ещё не
   загружен.
3. **Сделка B2C** сразу в статусе «Оплата и договор оферты» с `payment_confirmed=true`: заказ уже
   оплачен, выдумывать для него консультацию и верификацию контакта не нужно. Номер потока лежит
   в продукте сделки (`deal_products.stream_number`), «Номер заявки» — в `deals.order_number`
   (уникален: повторная загрузка не плодит сделки). Суммы в данных оплат нет, поэтому она берётся из
   прайса продукта; нет и его — `NULL`, и до закрытия сделки её заполнит менеджер (для `won`
   сумма обязательна).
4. **Идемпотентность.** Заказ с уже известным номером сделку не создаёт: дополняются поток, сумма и
   признак оплаты (`mode="insert"` — только пропуск).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import get_actor, set_actor
from app.core.errors import FieldError, ValidationError
from app.core.normalize import clean_text
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.catalog.lookup import ProductIndex
from app.modules.catalog.models import Contact, Product
from app.modules.catalog.service import ContactService
from app.modules.crm.models import Deal, DealProduct
from app.modules.crm.schemas import DealCreateRequest, DealProductIn
from app.modules.crm.service import DealService
from app.modules.integration.service import get_integration_principal, pick_least_loaded_owner

# Код статуса сид-воронки `b2c_individual_v1`; нет такого статуса в воронке — начальный.
PAID_STATUS_CODE = "payment_contract"
_ORDER_NUMBER_MAX = 64
_TITLE_MAX = 255


@dataclass(slots=True)
class OrderData:
    """Заказ в том виде, в каком его ждёт `OrderIngestService`: значения разобраны вызывающим
    (email и телефон допускаются в любом написании — сервис нормализует их сам)."""

    order_number: str
    course: str
    last_name: str | None
    first_name: str | None
    middle_name: str | None = None
    email: str | None = None
    phone: str | None = None
    stream_number: int | None = None
    amount: Decimal | None = None
    currency: str | None = None


@dataclass(slots=True)
class OrderResult:
    deal: Deal
    contact: Contact
    product: Product | None
    created_deal: bool = False
    created_contact: bool = False
    created_product: bool = False
    #: Строка пропущена: режим «только создание», а заказ уже есть.
    skipped: bool = False
    #: Контакт: поле → значение ДО (дозаполнение существующего человека).
    contact_changed: dict[str, Any] = field(default_factory=dict)
    #: Существующая сделка: поле → значение ДО (для отката импорта).
    deal_changed: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


class OrderIngestService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        source: str,
        initiator_id: uuid.UUID | None = None,
        import_job_id: uuid.UUID | None = None,
        create_missing_product: bool = True,
        products: ProductIndex | None = None,
    ) -> None:
        self._session = session
        self._source = source
        self._initiator_id = initiator_id
        self._import_job_id = import_job_id
        self._create_missing_product = create_missing_product
        self._products = products or ProductIndex(session)
        self._contacts = ContactService(session)

    async def find_deal(self, order_number: str) -> Deal | None:
        return (
            await self._session.execute(
                select(Deal).where(Deal.order_number == order_number, Deal.deleted_at.is_(None))
            )
        ).scalar_one_or_none()

    @staticmethod
    def _validate(data: OrderData) -> tuple[str, str, str, str]:
        order_number = clean_text(data.order_number)
        course = clean_text(data.course)
        last_name = clean_text(data.last_name)
        first_name = clean_text(data.first_name)
        errors: list[FieldError] = []
        if not order_number:
            errors.append(FieldError(field="order_number", reason="обязательное поле"))
        elif len(order_number) > _ORDER_NUMBER_MAX:
            errors.append(
                FieldError(field="order_number", reason=f"не больше {_ORDER_NUMBER_MAX} символов")
            )
        if not course:
            errors.append(FieldError(field="course", reason="обязательное поле"))
        if not last_name:
            errors.append(FieldError(field="last_name", reason="обязательное поле"))
        if not first_name:
            errors.append(FieldError(field="first_name", reason="обязательное поле"))
        if data.amount is not None and (not data.amount.is_finite() or data.amount <= 0):
            errors.append(
                FieldError(field="amount", reason="сумма должна быть положительным числом")
            )
        if data.stream_number is not None and data.stream_number < 1:
            errors.append(FieldError(field="stream_number", reason="номер потока — целое от 1"))
        if errors:
            raise ValidationError("Некорректные данные заказа", errors)
        return order_number or "", course or "", last_name or "", first_name or ""

    async def ingest(self, data: OrderData, *, mode: str = "upsert") -> OrderResult:
        order_number, course, last_name, first_name = self._validate(data)

        existing = await self.find_deal(order_number)
        if existing is not None:
            return await self._merge_into_existing(existing, data, mode)
        if mode == "update":
            raise ValidationError(
                "Заказ для обновления не найден",
                [FieldError(field="order_number", reason="нет сделки с таким номером заявки")],
            )

        # Контакт до сделки: даже если сделку не удастся создать, человек остаётся в базе — это не
        # ошибка, заказ можно повторить.
        upsert = await self._contacts.find_or_create(
            first_name=first_name,
            last_name=last_name,
            middle_name=clean_text(data.middle_name),
            email=data.email,
            phone=data.phone,
            source=self._source,
            created_by=self._initiator_id,
            external_ids={"order_number": order_number},
        )
        contact = upsert.contact

        product = await self._products.get(course)
        created_product = False
        if product is None and self._create_missing_product:
            product = await self._products.create(
                course, import_job_id=self._import_job_id, source=self._source
            )
            created_product = True

        amount = (
            data.amount if data.amount is not None else (product.base_price if product else None)
        )
        currency = data.currency or (product.currency if product and product.currency else "RUB")
        notes = list(upsert.notes)
        if amount is None:
            notes.append("Сумма не указана: заполните её до закрытия сделки")

        payload = DealCreateRequest(
            title=f"{course} · {order_number}"[:_TITLE_MAX],
            deal_type="b2c",
            contact_id=contact.id,
            owner_id=await pick_least_loaded_owner(self._session),
            amount=amount,
            currency=currency,
            source=self._source,
            external_ids={"order_number": order_number},
            order_number=order_number,
            custom_fields={"payment_confirmed": True},
            products=(
                [
                    DealProductIn(
                        product_id=product.id,
                        quantity=1,
                        price=amount,
                        total=amount,
                        stream_number=data.stream_number,
                    )
                ]
                if product is not None
                else []
            ),
        )

        # Атрибуция аудита: если вызывающий (импорт) уже выставил актора-инициатора, служебная
        # учётка его не перетирает.
        actor = get_actor()
        principal = await get_integration_principal(self._session)
        if actor is not None:
            set_actor(actor)
        try:
            async with self._session.begin_nested():
                deal = await DealService(self._session).create(
                    principal, payload, initial_status_code=PAID_STATUS_CODE
                )
        except IntegrityError:
            # Тот же заказ одновременно принял другой запрос: уникальный индекс по номеру заявки
            # пропустил одного. Берём его сделку и дополняем, как повторную загрузку.
            concurrent = await self.find_deal(order_number)
            if concurrent is None:
                raise
            return await self._merge_into_existing(concurrent, data, mode)

        return OrderResult(
            deal=deal,
            contact=contact,
            product=product,
            created_deal=True,
            created_contact=upsert.created,
            created_product=created_product,
            contact_changed=upsert.changed,
            notes=notes,
        )

    async def _merge_into_existing(self, deal: Deal, data: OrderData, mode: str) -> OrderResult:
        """Заказ уже есть: сделка не дублируется, дополняются только факты оплаты."""
        contact = (
            await self._session.get(Contact, deal.contact_id)
            if deal.contact_id is not None
            else None
        )
        if contact is None:  # B2C-сделка без контакта запрещена CHECK'ом; защита на случай данных
            raise ValidationError(
                "У сделки заказа нет контакта",
                [FieldError(field="order_number", reason="сделка заказа повреждена")],
            )
        lines = (
            (
                await self._session.execute(
                    select(DealProduct)
                    .where(DealProduct.deal_id == deal.id)
                    .order_by(DealProduct.created_at, DealProduct.id)
                )
            )
            .scalars()
            .all()
        )
        product = await self._session.get(Product, lines[0].product_id) if lines else None
        result = OrderResult(deal=deal, contact=contact, product=product)

        if mode == "insert":
            result.skipped = True
            result.notes.append("Заказ уже загружен — пропущен (режим «только создание»)")
            return result
        if deal.closed_at is not None:
            result.skipped = True
            result.notes.append("Сделка по заказу уже закрыта — не изменена")
            return result

        changed: dict[str, Any] = {}
        changes: dict[str, dict[str, Any]] = {}
        custom = dict(deal.custom_fields or {})
        if not custom.get("payment_confirmed"):
            changed["custom_fields"] = dict(deal.custom_fields or {})
            custom["payment_confirmed"] = True
            deal.custom_fields = custom
            changes["custom_fields"] = {"old": changed["custom_fields"], "new": custom}
        if data.amount is not None and deal.amount is None:
            changed["amount"] = None
            deal.amount = data.amount
            changes["amount"] = {"old": None, "new": str(data.amount)}
        if data.stream_number is not None and lines and lines[0].stream_number is None:
            changed["stream_number"] = {"deal_product_id": str(lines[0].id), "old": None}
            lines[0].stream_number = data.stream_number
            changes["stream_number"] = {"old": None, "new": data.stream_number}

        if changed:
            deal.version += 1
            await self._session.flush()
            await AuditService(self._session).record(
                AuditAction.DEAL_UPDATED,
                entity_type="deal",
                entity_id=deal.id,
                changes=changes,
            )
        else:
            result.notes.append("Заказ уже загружен — изменений нет")
        result.deal_changed = changed
        return result
