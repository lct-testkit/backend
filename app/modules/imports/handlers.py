"""Обработчики типов импорта, строка которых порождает несколько записей.

Общий конвейер (`imports.service`) знает «одна строка — одна запись типа задания»: `entity_id` и
`before_snapshot` описывают ровно её. Строка вендорского файла создаёт организацию, продукты,
контакт и связи между ними; строка оплат — контакт, продукт и сделку; строка шаблона LMS — контакт
и профиль учащегося. Поэтому такие типы применяются обработчиками этого модуля, а результат
строки — список `effects` (что создано, что изменено и каким было раньше). Откат идёт по этому
списку в обратном порядке.

Все обработчики идемпотентны: повторная загрузка того же файла не плодит ни людей, ни компании, ни
сделки — ключи дедупликации те же, что у ручного ввода и вебхука (`core.normalize`,
`ContactService.find_or_create`).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import FieldError, ValidationError
from app.core.normalize import clean_text, name_key, normalize_email, normalize_phone
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.catalog import learner
from app.modules.catalog.lookup import OrganizationIndex, ProductIndex
from app.modules.catalog.models import (
    Contact,
    ContactLearnerProfile,
    ContactProduct,
    Organization,
    Product,
)
from app.modules.catalog.service import ContactService
from app.modules.crm.models import Deal, DealProduct
from app.modules.imports.models import ImportJob, ImportMode, ImportRowResult
from app.modules.integration.orders import OrderData, OrderIngestService

_CHUNK = 1000


def _chunks(items: list[str], size: int = _CHUNK) -> list[list[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def _json(value: Any) -> Any:
    """Значение для `effects` (JSONB): даты, UUID и Decimal — строками."""
    if isinstance(value, dt.date | dt.datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID | Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_json(v) for v in value]
    return value


@dataclass(slots=True)
class RowCheck:
    """Итог проверки строки при dry-run (без записи в БД)."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Запись/заказ/человек уже есть в БД — от режима зависит, что будет со строкой.
    exists: bool = False
    #: Ключ для поиска повторов внутри файла; `None` — повторы не считаются ошибкой.
    key: str | None = None


@dataclass(slots=True)
class ApplyOutcome:
    effects: list[dict[str, Any]] = field(default_factory=list)
    #: Главная запись строки (контакт или сделка) — `import_row_results.entity_id`.
    entity_id: uuid.UUID | None = None
    skipped: bool = False
    notes: list[str] = field(default_factory=list)


def person_names(row: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    return (
        clean_text(row.get("last_name")),
        clean_text(row.get("first_name")),
        clean_text(row.get("middle_name")),
    )


def _require_names(row: dict[str, Any]) -> tuple[str, str, str | None]:
    """Фамилия и имя обязательны; dry-run уже отклонил такие строки, но применение не должно
    полагаться на то, что проверка была (данные могли измениться, задание — возобновиться)."""
    last, first, middle = person_names(row)
    if not last or not first:
        raise ValidationError(
            "Не указаны фамилия и имя", [FieldError(field="full_name", reason="обязательное поле")]
        )
    return last, first, middle


def _names_error(row: dict[str, Any]) -> str | None:
    last, first, _middle = person_names(row)
    if not last or not first:
        return "Укажите фамилию и имя (колонка «ФИО» или колонки «Фамилия» и «Имя»)"
    return None


def _identity_error(row: dict[str, Any]) -> str | None:
    if not row.get("email") and not row.get("phone"):
        return "Укажите email или телефон: по ним контакт отличают от уже существующих"
    return None


class Batch:
    """Состояние на время одной партии: индексы каталога живут, пока живёт партия."""

    def __init__(self, session: AsyncSession, job: ImportJob) -> None:
        self.session = session
        self.job = job
        self.products = ProductIndex(session)
        self.organizations = OrganizationIndex(session)
        self.contacts = ContactService(session)

    def invalidate(self) -> None:
        """После отката SAVEPOINT'а строки объекты, изменённые внутри неё, «протухли»: индексы
        читаются заново, а не отдают их из кэша."""
        self.products = ProductIndex(self.session)
        self.organizations = OrganizationIndex(self.session)


class EntityHandler:
    entity_type = ""
    #: Повтор ключа внутри файла — предупреждение и последняя запись побеждает.
    dedupe_in_file = True

    async def prepare(self, session: AsyncSession, rows: list[dict[str, Any]]) -> Any:
        return None

    def check_row(self, row: dict[str, Any], ctx: Any, mode: str) -> RowCheck:
        raise NotImplementedError

    async def apply_row(self, batch: Batch, row: ImportRowResult) -> ApplyOutcome:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Общее: сопоставление контактов файла с БД пачкой (не по запросу на строку)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ContactLookup:
    by_email: dict[str, list[Contact]] = field(default_factory=dict)
    by_phone: dict[str, list[Contact]] = field(default_factory=dict)

    def find(self, row: dict[str, Any]) -> Contact | None:
        email = row.get("email")
        if email and self.by_email.get(email):
            return self.by_email[email][0]
        phone = row.get("phone")
        if phone:
            last, _first, _middle = person_names(row)
            wanted = name_key(last) if last else ""
            for contact in self.by_phone.get(phone, []):
                if not wanted or name_key(contact.last_name) == wanted:
                    return contact
        return None


async def load_contacts(session: AsyncSession, rows: list[dict[str, Any]]) -> ContactLookup:
    lookup = ContactLookup()
    emails = sorted({r["email"] for r in rows if r.get("email")})
    phones = sorted({r["phone"] for r in rows if r.get("phone")})
    base = select(Contact).where(Contact.deleted_at.is_(None), Contact.is_anonymized.is_(False))
    for chunk in _chunks(emails):
        for contact in (await session.execute(base.where(Contact.email.in_(chunk)))).scalars():
            lookup.by_email.setdefault(contact.email or "", []).append(contact)
    for chunk in _chunks(phones):
        for contact in (await session.execute(base.where(Contact.phone.in_(chunk)))).scalars():
            lookup.by_phone.setdefault(contact.phone or "", []).append(contact)
    return lookup


def _apply_mode_to_check(check: RowCheck, mode: str, what: str) -> None:
    if check.exists and mode == ImportMode.INSERT.value:
        check.warnings.append(
            f"{what} уже существует — строка будет пропущена (режим «только создание»)"
        )
    elif not check.exists and mode == ImportMode.UPDATE.value:
        check.errors.append(f"{what} для обновления не найден")


# ---------------------------------------------------------------------------
# «Вендоры»
# ---------------------------------------------------------------------------


class VendorContactHandler(EntityHandler):
    entity_type = "vendor_contact"
    dedupe_in_file = False  # один человек отвечает за несколько продуктов — в нескольких строках

    async def prepare(self, session: AsyncSession, rows: list[dict[str, Any]]) -> ContactLookup:
        return await load_contacts(session, rows)

    def check_row(self, row: dict[str, Any], ctx: ContactLookup, mode: str) -> RowCheck:
        check = RowCheck()
        for error in (_names_error(row), _identity_error(row)):
            if error:
                check.errors.append(error)
        if not row.get("product_names"):
            check.warnings.append("Продукт не указан — контакт будет создан без связи с продуктом")
        check.exists = ctx.find(row) is not None
        _apply_mode_to_check(check, mode, "Контакт")
        return check

    async def apply_row(self, batch: Batch, row: ImportRowResult) -> ApplyOutcome:
        data = row.row_data
        job = batch.job
        outcome = ApplyOutcome()
        last, first, middle = _require_names(data)

        existing = await batch.contacts.match_contact(
            email=data.get("email"), phone=data.get("phone"), last_name=last
        )
        if existing is not None and job.mode == ImportMode.INSERT.value:
            outcome.skipped = True
            outcome.notes.append("Контакт уже существует — пропущена (режим «только создание»)")
            return outcome
        if existing is None and job.mode == ImportMode.UPDATE.value:
            outcome.skipped = True
            outcome.notes.append("Контакт для обновления не найден")
            return outcome

        organization = await batch.organizations.get(data["vendor_name"])
        if organization is None:
            organization = await batch.organizations.create_vendor(
                data["vendor_name"], owner_id=job.initiated_by, import_job_id=job.id
            )
            outcome.effects.append(
                {"kind": "organization", "op": "create", "id": str(organization.id)}
            )

        products: list[Product] = []
        for name in data.get("product_names") or []:
            product = await batch.products.get(name)
            if product is None:
                product = await batch.products.create(
                    name, vendor_id=organization.id, import_job_id=job.id
                )
                outcome.effects.append({"kind": "product", "op": "create", "id": str(product.id)})
            elif product.vendor_id is None:
                product.vendor_id = organization.id
                product.version += 1
                await batch.session.flush()
                outcome.effects.append(
                    {
                        "kind": "product",
                        "op": "update",
                        "id": str(product.id),
                        "before": {"vendor_id": None},
                    }
                )
            elif product.vendor_id != organization.id:
                outcome.notes.append(
                    f"Продукт «{name}» уже принадлежит другому вендору — не изменён"
                )
            products.append(product)

        upsert = await batch.contacts.find_or_create(
            first_name=first,
            last_name=last,
            middle_name=middle,
            email=data.get("email"),
            phone=data.get("phone"),
            organization_id=organization.id,
            contact_methods=data.get("contact_methods"),
            source="import",
            created_by=job.initiated_by,
        )
        contact = upsert.contact
        outcome.entity_id = contact.id
        outcome.notes.extend(upsert.notes)
        if upsert.created:
            outcome.effects.append({"kind": "contact", "op": "create", "id": str(contact.id)})
        elif upsert.changed:
            outcome.effects.append(
                {
                    "kind": "contact",
                    "op": "update",
                    "id": str(contact.id),
                    "before": _json(upsert.changed),
                }
            )

        for product in products:
            if await batch.session.get(ContactProduct, (contact.id, product.id)) is None:
                batch.session.add(
                    ContactProduct(contact_id=contact.id, product_id=product.id, role="responsible")
                )
                await batch.session.flush()
                outcome.effects.append(
                    {
                        "kind": "contact_product",
                        "op": "create",
                        "contact_id": str(contact.id),
                        "product_id": str(product.id),
                    }
                )
        return outcome


# ---------------------------------------------------------------------------
# «Данные оплат»
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PaymentLookup:
    orders: set[str]
    products: ProductIndex
    known_products: dict[str, Product]


class PaymentHandler(EntityHandler):
    entity_type = "payment"

    async def prepare(self, session: AsyncSession, rows: list[dict[str, Any]]) -> PaymentLookup:
        numbers = sorted({r["order_number"] for r in rows if r.get("order_number")})
        orders: set[str] = set()
        for chunk in _chunks(numbers):
            found = await session.execute(
                select(Deal.order_number).where(
                    Deal.order_number.in_(chunk), Deal.deleted_at.is_(None)
                )
            )
            orders.update(number for (number,) in found.all() if number)
        index = ProductIndex(session)
        known: dict[str, Product] = {}
        for name in {r["product_name"] for r in rows if r.get("product_name")}:
            product = await index.get(name)
            if product is not None:
                known[name] = product
        return PaymentLookup(orders=orders, products=index, known_products=known)

    def check_row(self, row: dict[str, Any], ctx: PaymentLookup, mode: str) -> RowCheck:
        check = RowCheck(key=row.get("order_number"))
        for error in (_names_error(row), _identity_error(row)):
            if error:
                check.errors.append(error)
        product = ctx.known_products.get(row.get("product_name") or "")
        if row.get("product_name") and product is None:
            check.warnings.append(
                f"Курс «{row['product_name']}» не найден в каталоге — будет создан новый продукт"
            )
        if row.get("amount") is None and (product is None or product.base_price is None):
            check.warnings.append("Сумма не указана: заполните её в сделке до закрытия")
        check.exists = row.get("order_number") in ctx.orders
        _apply_mode_to_check(check, mode, "Заказ")
        return check

    async def apply_row(self, batch: Batch, row: ImportRowResult) -> ApplyOutcome:
        data = row.row_data
        job = batch.job
        last, first, middle = person_names(data)
        amount = data.get("amount")
        service = OrderIngestService(
            batch.session,
            source="import",
            initiator_id=job.initiated_by,
            import_job_id=job.id,
            products=batch.products,
        )
        result = await service.ingest(
            OrderData(
                order_number=data["order_number"],
                course=data["product_name"],
                last_name=last,
                first_name=first,
                middle_name=middle,
                email=data.get("email"),
                phone=data.get("phone"),
                stream_number=data.get("stream_number"),
                amount=Decimal(str(amount)) if amount is not None else None,
                currency=data.get("currency"),
            ),
            mode=job.mode,
        )
        outcome = ApplyOutcome(entity_id=result.deal.id, notes=list(result.notes))
        if result.skipped:
            outcome.skipped = True
            return outcome
        if result.created_contact:
            outcome.effects.append(
                {"kind": "contact", "op": "create", "id": str(result.contact.id)}
            )
        elif result.contact_changed:
            outcome.effects.append(
                {
                    "kind": "contact",
                    "op": "update",
                    "id": str(result.contact.id),
                    "before": _json(result.contact_changed),
                }
            )
        if result.created_product and result.product is not None:
            outcome.effects.append(
                {"kind": "product", "op": "create", "id": str(result.product.id)}
            )
        if result.created_deal:
            outcome.effects.append({"kind": "deal", "op": "create", "id": str(result.deal.id)})
        elif result.deal_changed:
            outcome.effects.append(
                {
                    "kind": "deal",
                    "op": "update",
                    "id": str(result.deal.id),
                    "before": _json(result.deal_changed),
                }
            )
        return outcome


# ---------------------------------------------------------------------------
# «Загрузка пользователей» (шаблон LMS)
# ---------------------------------------------------------------------------


class LearnerHandler(EntityHandler):
    entity_type = "learner"

    async def prepare(self, session: AsyncSession, rows: list[dict[str, Any]]) -> ContactLookup:
        return await load_contacts(session, rows)

    def check_row(self, row: dict[str, Any], ctx: ContactLookup, mode: str) -> RowCheck:
        check = RowCheck(key=row.get("email") or row.get("phone"))
        for error in (_names_error(row), _identity_error(row)):
            if error:
                check.errors.append(error)
        check.exists = ctx.find(row) is not None
        _apply_mode_to_check(check, mode, "Учащийся")
        return check

    async def apply_row(self, batch: Batch, row: ImportRowResult) -> ApplyOutcome:
        data = row.row_data
        job = batch.job
        outcome = ApplyOutcome()
        last, first, middle = _require_names(data)

        existing = await batch.contacts.match_contact(
            email=data.get("email"), phone=data.get("phone"), last_name=last
        )
        if existing is not None and job.mode == ImportMode.INSERT.value:
            outcome.skipped = True
            outcome.notes.append("Учащийся уже существует — пропущена (режим «только создание»)")
            return outcome
        if existing is None and job.mode == ImportMode.UPDATE.value:
            outcome.skipped = True
            outcome.notes.append("Учащийся для обновления не найден")
            return outcome

        upsert = await batch.contacts.find_or_create(
            first_name=first,
            last_name=last,
            middle_name=middle,
            email=data.get("email"),
            phone=data.get("phone"),
            source="import",
            created_by=job.initiated_by,
        )
        contact = upsert.contact
        outcome.entity_id = contact.id
        outcome.notes.extend(upsert.notes)
        if upsert.created:
            outcome.effects.append({"kind": "contact", "op": "create", "id": str(contact.id)})
        elif upsert.changed:
            outcome.effects.append(
                {
                    "kind": "contact",
                    "op": "update",
                    "id": str(contact.id),
                    "before": _json(upsert.changed),
                }
            )

        values = {
            target: _profile_value(target, data[target])
            for target in learner.PROFILE_TARGETS
            if data.get(target) not in (None, "")
        }
        if values:
            effect = await _upsert_profile(batch.session, contact.id, values)
            outcome.effects.append(effect)
            await AuditService(batch.session).record(
                AuditAction.CONTACT_UPDATED,
                entity_type="contact",
                entity_id=contact.id,
                # Только имена полей: СНИЛС, паспорт и адрес не должны попадать в журнал аудита.
                changes={"learner_profile": {"old": None, "new": sorted(values)}},
            )
        return outcome


_DATE_TARGETS = {"passport_issued_at", "birth_date", "diploma_issued_at"}


def _profile_value(target: str, value: Any) -> Any:
    """`row_data` хранит даты ISO-строкой (JSONB); в профиль они возвращаются датой."""
    if target in _DATE_TARGETS and isinstance(value, str):
        return dt.date.fromisoformat(value)
    return value


async def _upsert_profile(
    session: AsyncSession, contact_id: uuid.UUID, values: dict[str, Any]
) -> dict[str, Any]:
    profile = await session.get(ContactLearnerProfile, contact_id)
    if profile is None:
        session.add(ContactLearnerProfile(contact_id=contact_id, **values))
        await session.flush()
        return {"kind": "learner_profile", "op": "create", "contact_id": str(contact_id)}
    before: dict[str, Any] = {}
    for target, value in values.items():
        old = getattr(profile, target)
        if old != value:
            before[target] = _json(old)
            setattr(profile, target, value)
    await session.flush()
    return {
        "kind": "learner_profile",
        "op": "update",
        "contact_id": str(contact_id),
        "before": before,
    }


HANDLERS: dict[str, EntityHandler] = {
    handler.entity_type: handler
    for handler in (VendorContactHandler(), PaymentHandler(), LearnerHandler())
}


# ---------------------------------------------------------------------------
# Откат по effects
# ---------------------------------------------------------------------------


async def rollback_effects(session: AsyncSession, effects: list[dict[str, Any]]) -> list[str]:
    """Откатывает эффекты строки в обратном порядке и помечает откатанные `rolled_back`, чтобы
    повторный откат (после разбора блокеров) не трогал сделанное. Возвращает причины блокировок —
    пусто, если откатано всё."""
    blocked: list[str] = []
    now = dt.datetime.now(dt.UTC)
    for effect in reversed(effects):
        if effect.get("rolled_back"):
            continue
        reason = await _rollback_one(session, effect, now)
        if reason is None:
            effect["rolled_back"] = True
        else:
            blocked.append(reason)
        # Сессии приложения работают без автосброса (`autoflush=False`): следующая проверка
        # «нет ли зависимых» должна видеть только что снятые связи и записи.
        await session.flush()
    return blocked


async def _rollback_one(
    session: AsyncSession, effect: dict[str, Any], now: dt.datetime
) -> str | None:
    kind, op = effect["kind"], effect["op"]

    if kind == "contact_product":
        link = await session.get(
            ContactProduct,
            (uuid.UUID(effect["contact_id"]), uuid.UUID(effect["product_id"])),
        )
        if link is not None:
            await session.delete(link)
        return None

    if kind == "learner_profile":
        contact_id = uuid.UUID(effect["contact_id"])
        profile = await session.get(ContactLearnerProfile, contact_id)
        if profile is None:
            return None
        if op == "create":
            await session.delete(profile)
        else:
            for target, old in (effect.get("before") or {}).items():
                setattr(profile, target, _profile_value(target, old))
        return None

    entity_id = uuid.UUID(effect["id"])
    models: dict[str, Any] = {
        "organization": Organization,
        "product": Product,
        "contact": Contact,
        "deal": Deal,
    }
    model = models[kind]
    entity = await session.get(model, entity_id)
    if entity is None or entity.deleted_at is not None:
        return None

    if op == "update":
        for attr, old in (effect.get("before") or {}).items():
            if kind == "deal" and attr == "stream_number":
                line = await session.get(DealProduct, uuid.UUID(old["deal_product_id"]))
                if line is not None:
                    line.stream_number = old["old"]
                continue
            if attr in ("vendor_id", "organization_id") and old is not None:
                old = uuid.UUID(old)
            if attr == "amount" and old is not None:
                old = Decimal(str(old))
            setattr(entity, attr, old)
        entity.version += 1
        return None

    label = {
        "organization": "организация",
        "product": "продукт",
        "contact": "контакт",
        "deal": "сделка",
    }[kind]
    dependents = await _dependents(session, kind, entity_id)
    if dependents:
        return f"Откат заблокирован: {label} используется ({dependents})"
    entity.deleted_at = now
    return None


async def _dependents(session: AsyncSession, kind: str, entity_id: uuid.UUID) -> str | None:
    """Чем запись уже пользуются помимо самого импорта; `None` — ничем."""
    if kind == "organization":
        checks = {
            "сделки": select(Deal.id).where(
                Deal.organization_id == entity_id, Deal.deleted_at.is_(None)
            ),
            "контакты": select(Contact.id).where(
                Contact.organization_id == entity_id, Contact.deleted_at.is_(None)
            ),
            "продукты": select(Product.id).where(
                Product.vendor_id == entity_id, Product.deleted_at.is_(None)
            ),
        }
    elif kind == "product":
        checks = {
            # Строки продуктов мягко удалённой сделки остаются в таблице — считаем только живые.
            "сделки": select(DealProduct.id)
            .join(Deal, Deal.id == DealProduct.deal_id)
            .where(DealProduct.product_id == entity_id, Deal.deleted_at.is_(None)),
            "ответственные": select(ContactProduct.contact_id).where(
                ContactProduct.product_id == entity_id
            ),
        }
    elif kind == "contact":
        checks = {
            "сделки": select(Deal.id).where(
                Deal.contact_id == entity_id, Deal.deleted_at.is_(None)
            ),
        }
    else:  # deal: сделка, которую уже вели, откатывать нельзя
        deal = await session.get(Deal, entity_id)
        if deal is not None and deal.version > 1:
            return "сделка уже в работе"
        return None
    used = [
        title
        for title, stmt in checks.items()
        if (await session.execute(stmt.limit(1))).first() is not None
    ]
    return ", ".join(used) or None


def raise_row_errors(errors: list[str]) -> None:
    """Для вызывающих, которым нужна исключением: собирает `ValidationError` по строкам."""
    if errors:
        raise ValidationError(
            "Строка не прошла проверку", [FieldError(field="row", reason=e) for e in errors]
        )


__all__ = [
    "HANDLERS",
    "ApplyOutcome",
    "Batch",
    "EntityHandler",
    "RowCheck",
    "normalize_email",
    "normalize_phone",
    "rollback_effects",
]
