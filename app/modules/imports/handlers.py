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
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import FieldError, ValidationError
from app.core.masking import mask_email, mask_name
from app.core.normalize import clean_text, name_key, normalize_email, normalize_phone, slugify_code
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.catalog import learner
from app.modules.catalog.license_matching import LicenseMatcher
from app.modules.catalog.lookup import OrganizationIndex, ProductIndex
from app.modules.catalog.models import (
    Contact,
    ContactLearnerProfile,
    ContactProduct,
    Direction,
    Organization,
    Product,
)
from app.modules.catalog.service import ContactService
from app.modules.crm.models import Deal, DealProduct
from app.modules.identity.models import Role, User, UserStatus
from app.modules.imports.models import ImportJob, ImportMode, ImportRowResult
from app.modules.integration.orders import OrderData, OrderIngestService
from app.modules.notification.service import NotificationPriority, get_notification_service

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
class RowIssue:
    """B3-5: один код проблемы строки — машиночитаемый (`code`), в отличие от свободного текста
    `RowCheck.errors`/`.warnings`. Старые обработчики (`VendorContactHandler`/`PaymentHandler`/
    `LearnerHandler`) продолжают писать в `errors`/`warnings` напрямую — их строки оборачиваются в
    `RowIssue(code="other", ...)` на выходе из dry-run (`imports.service._dry_run_handled`), а не
    здесь: сами эти три обработчика не меняются (риск спринта B3-5)."""

    code: str
    message: str
    field: str | None = None
    hint: str | None = None
    severity: Literal["error", "warning"] = "error"


@dataclass(slots=True)
class RowCheck:
    """Итог проверки строки при dry-run (без записи в БД)."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: B3-5: источник правды для новых обработчиков (`DirectionHandler`/`ResponsibleHandler`).
    #: `errors`/`warnings` остаются производными (через `add_issue`) для обратной совместимости.
    issues: list[RowIssue] = field(default_factory=list)
    #: Запись/заказ/человек уже есть в БД — от режима зависит, что будет со строкой.
    exists: bool = False
    #: Ключ для поиска повторов внутри файла; `None` — повторы не считаются ошибкой.
    key: str | None = None

    def add_issue(self, issue: RowIssue) -> None:
        self.issues.append(issue)
        text = issue.message if not issue.hint else f"{issue.message} ({issue.hint})"
        if issue.severity == "error":
            self.errors.append(text)
        else:
            self.warnings.append(text)


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
        # B3-7: курсы (оплаты, `PaymentHandler`) и ПО (вендорский файл,
        # `VendorContactHandler`) — разные индексы по `kind`, иначе курс и
        # ПО с одним названием перехватывали бы поиск друг друга.
        self.products = ProductIndex(session, kind="course")
        self.software = ProductIndex(session, kind="software")
        self.organizations = OrganizationIndex(session)
        self.contacts = ContactService(session)
        # B3-8: `ResponsibleHandler` ищет вуз тем же многоступенчатым поиском, что и лицензии
        # (B3-4) — один инстанс на партию, кандидаты кэшируются внутри него (докстринг класса).
        self.license_matcher = LicenseMatcher(session)

    def invalidate(self) -> None:
        """После отката SAVEPOINT'а строки объекты, изменённые внутри неё, «протухли»: индексы
        читаются заново, а не отдают их из кэша."""
        self.products = ProductIndex(self.session, kind="course")
        self.software = ProductIndex(self.session, kind="software")
        self.organizations = OrganizationIndex(self.session)
        self.license_matcher = LicenseMatcher(self.session)


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
        check.add_issue(
            RowIssue(
                code="exists_insert_skip",
                severity="warning",
                message=f"{what} уже существует — строка будет пропущена (режим «только создание»)",
            )
        )
    elif not check.exists and mode == ImportMode.UPDATE.value:
        check.add_issue(
            RowIssue(code="update_not_found", message=f"{what} для обновления не найден")
        )


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
            product = await batch.software.get(name)
            if product is None:
                product = await batch.software.create(
                    name, vendor_id=organization.id, import_job_id=job.id, kind="software"
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
        # B3-7: оплаты заводят курсы, а не ИТ-продукты (ПО) — те приходят
        # только вендорским файлом (`VendorContactHandler`/`batch.software`).
        index = ProductIndex(session, kind="course")
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


# ---------------------------------------------------------------------------
# «Направления» (B3-8)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class DirectionContext:
    #: Код -> запись в БД, включая мягко удалённые (занятый код удалённого направления — ошибка
    #: строки, а не тихое совпадение).
    by_code_db: dict[str, Direction]
    #: Итоговый код родителя для каждого кода из файла (файл побеждает БД: если строка в файле
    #: переопределяет `parent_code`, проверка цикла должна видеть новое значение).
    parent_of: dict[str, str | None]
    #: Код родителя для направлений, которых в файле нет, но на которые он ссылается (родитель или
    #: его предки из БД): цикл может замкнуться через них, а `parent_of` их не знает.
    db_parent_of: dict[str, str | None]
    #: Коды, участвующие в цикле ссылок «родитель» (файл плюс цепочки из БД).
    cycle_codes: set[str]


class DirectionHandler(EntityHandler):
    """B3-8: импорт ИТ-направлений (ТЗ §4 п.1). Ключ строки — `code`; пустой код строится из
    названия (`slugify_code`). Родитель может стоять в файле ниже ребёнка — `apply_row` создаёт
    его заранее по данным, уже провалидированным dry-run'ом и лежащим в `import_row_results.
    row_data` (см. `_ensure_parent`): инстанс обработчика не хранит состояние между партиями и
    тиками фоновой задачи, а БД — хранит."""

    entity_type = "direction"

    async def prepare(self, session: AsyncSession, rows: list[dict[str, Any]]) -> DirectionContext:
        # Код пустой ячейки — из названия; мутация `row` на месте: тот же объект обработает
        # `check_row` ниже и, через `import_row_results.row_data`, `apply_row`.
        for row in rows:
            if not row.get("code") and row.get("name"):
                row["code"] = slugify_code(row["name"])

        codes = {row["code"] for row in rows if row.get("code")}
        # Родитель из ячейки «Родитель» может уже лежать в БД и не повторяться в файле отдельной
        # строкой (новое дочернее направление к существующему корню) — ищем и такие коды: иначе
        # строка получала «родитель не найден» при том, что он есть в каталоге.
        parent_codes = {c for c in (clean_text(row.get("parent_code")) for row in rows) if c}
        by_code_db: dict[str, Direction] = {}
        for chunk in _chunks(sorted(codes | parent_codes)):
            found = await session.execute(select(Direction).where(Direction.code.in_(chunk)))
            by_code_db.update({d.code: d for d in found.scalars()})

        # «Последняя строка побеждает» — тот же приём, что и для `_FK_TARGETS` в `imports.service`.
        parent_of: dict[str, str | None] = {}
        for row in rows:
            code = row.get("code")
            if code:
                parent_of[code] = clean_text(row.get("parent_code"))

        # Цепочки родителей направлений, которых нет в файле: цикл может замкнуться через БД
        # (файл ставит `a` под `b`, а `b` в каталоге уже под `a`). `visited` — предохранитель от
        # цикла, который уже лежит в самой БД.
        db_parent_of: dict[str, str | None] = {}
        frontier = [code for code in by_code_db if code not in parent_of]
        while frontier:
            code = frontier.pop()
            if code in db_parent_of:
                continue
            existing = by_code_db[code]
            parent_direction = (
                await session.get(Direction, existing.parent_id)
                if existing.parent_id is not None
                else None
            )
            db_parent_of[code] = parent_direction.code if parent_direction else None
            if parent_direction is not None and parent_direction.code not in parent_of:
                by_code_db.setdefault(parent_direction.code, parent_direction)
                frontier.append(parent_direction.code)

        cycle_codes: set[str] = set()
        for start in parent_of:
            seen: list[str] = []
            current: str | None = start
            while current is not None:
                if current in seen:
                    if current == start or start in seen:
                        cycle_codes.update(seen)
                    break
                seen.append(current)
                current = parent_of[current] if current in parent_of else db_parent_of.get(current)

        return DirectionContext(
            by_code_db=by_code_db,
            parent_of=parent_of,
            db_parent_of=db_parent_of,
            cycle_codes=cycle_codes,
        )

    def check_row(self, row: dict[str, Any], ctx: DirectionContext, mode: str) -> RowCheck:
        code = row.get("code")
        check = RowCheck(key=code)
        if not code or not row.get("name"):
            # Поле `name` обязательное — `_extract_row` уже дал `required_empty`; без кода/названия
            # дальше проверять нет смысла (checks ниже обратились бы к пустому ключу).
            return check

        existing = ctx.by_code_db.get(code)
        if existing is not None and existing.deleted_at is not None:
            check.add_issue(
                RowIssue(
                    code="direction_code_deleted",
                    field="code",
                    message=f"Код «{code}» занят удалённым направлением",
                    hint="Восстановите прежнее направление или укажите другой код",
                )
            )
        check.exists = existing is not None and existing.deleted_at is None
        _apply_mode_to_check(check, mode, "Направление")

        if code in ctx.cycle_codes:
            check.add_issue(
                RowIssue(
                    code="direction_cycle",
                    field="parent_code",
                    message="Циклическая связь в родительских направлениях",
                    hint="Проверьте цепочку «родитель» — она не должна возвращаться к этому коду",
                )
            )
            return check

        parent_code = clean_text(row.get("parent_code"))
        if parent_code and parent_code not in ctx.parent_of and parent_code not in ctx.by_code_db:
            check.add_issue(
                RowIssue(
                    code="direction_parent_not_found",
                    field="parent_code",
                    message=f"Направление-родитель «{parent_code}» не найдено",
                    hint="Проверьте код/название или добавьте строку с этим направлением",
                )
            )
        elif (
            parent_code
            and parent_code in ctx.by_code_db
            and ctx.by_code_db[parent_code].deleted_at is not None
            and parent_code not in ctx.parent_of
        ):
            check.add_issue(
                RowIssue(
                    code="direction_parent_not_found",
                    field="parent_code",
                    message=f"Направление-родитель «{parent_code}» удалено",
                    hint="Укажите другого родителя или восстановите удалённое направление",
                )
            )
        return check

    async def _ensure_parent(
        self, batch: Batch, code: str, job: ImportJob, outcome: ApplyOutcome, *, depth: int = 0
    ) -> uuid.UUID | None:
        """Создаёт направление-родитель заранее, если его строка в файле стоит ниже ребёнка —
        данные берёт из уже сохранённого `import_row_results.row_data` этой же партии импорта
        (dry-run их туда положил), а не из `prepare()`: `apply_row` не получает контекст dry-run.
        Созданный родитель попадает в `effects` строки-ребёнка (первым, до самого ребёнка — откат
        идёт в обратном порядке) и в журнал: иначе откат импорта оставлял бы его в каталоге, а
        строка самого родителя, дойдя до очереди, находила бы его уже готовым и ничего не писала."""
        if depth > 50:  # dry-run уже отсеял циклы — предохранитель от бага, не нормальный путь
            return None
        existing = await batch.session.scalar(
            select(Direction).where(Direction.code == code, Direction.deleted_at.is_(None))
        )
        if existing is not None:
            return existing.id

        parent_row = await batch.session.scalar(
            select(ImportRowResult).where(
                ImportRowResult.import_job_id == job.id,
                ImportRowResult.row_data["code"].astext == code,
            )
        )
        if parent_row is None:
            return None
        parent_data = parent_row.row_data
        grandparent_code = clean_text(parent_data.get("parent_code"))
        parent_id = (
            await self._ensure_parent(batch, grandparent_code, job, outcome, depth=depth + 1)
            if grandparent_code
            else None
        )
        name = parent_data.get("name") or code
        direction = Direction(code=code, name=name, parent_id=parent_id)
        batch.session.add(direction)
        await batch.session.flush()
        outcome.effects.append({"kind": "direction", "op": "create", "id": str(direction.id)})
        await AuditService(batch.session).record(
            AuditAction.DIRECTION_CREATED,
            entity_type="direction",
            entity_id=direction.id,
            changes={"code": {"old": None, "new": code}, "name": {"old": None, "new": name}},
        )
        return direction.id

    async def apply_row(self, batch: Batch, row: ImportRowResult) -> ApplyOutcome:
        data = row.row_data
        job = batch.job
        outcome = ApplyOutcome()
        code = data.get("code")
        name = data.get("name")
        if not code or not name:
            raise ValidationError(
                "Строка без кода/названия направления",
                [FieldError(field="name", reason="обязательное поле")],
            )

        parent_code = clean_text(data.get("parent_code"))
        parent_id = (
            await self._ensure_parent(batch, parent_code, job, outcome) if parent_code else None
        )

        existing = await batch.session.scalar(
            select(Direction).where(Direction.code == code, Direction.deleted_at.is_(None))
        )
        if existing is None:
            if job.mode == ImportMode.UPDATE.value:
                outcome.skipped = True
                outcome.notes.append("Направление для обновления не найдено")
                return outcome
            direction = Direction(code=code, name=name, parent_id=parent_id)
            batch.session.add(direction)
            await batch.session.flush()
            outcome.entity_id = direction.id
            outcome.effects.append({"kind": "direction", "op": "create", "id": str(direction.id)})
            await AuditService(batch.session).record(
                AuditAction.DIRECTION_CREATED,
                entity_type="direction",
                entity_id=direction.id,
                changes={"code": {"old": None, "new": code}, "name": {"old": None, "new": name}},
            )
            return outcome

        if job.mode == ImportMode.INSERT.value:
            outcome.skipped = True
            outcome.notes.append("Направление уже существует — пропущена (режим «только создание»)")
            return outcome

        before: dict[str, Any] = {}
        changes: dict[str, Any] = {}
        if existing.name != name:
            before["name"] = existing.name
            changes["name"] = {"old": existing.name, "new": name}
            existing.name = name
        if existing.parent_id != parent_id:
            old_parent = str(existing.parent_id) if existing.parent_id else None
            before["parent_id"] = old_parent
            changes["parent_id"] = {"old": old_parent, "new": str(parent_id) if parent_id else None}
            existing.parent_id = parent_id
        outcome.entity_id = existing.id
        if before:
            existing.version += 1
            # `before` - то, что откат вернёт обратно (`handlers._rollback_one`, ветка `update`).
            outcome.effects.append(
                {"kind": "direction", "op": "update", "id": str(existing.id), "before": before}
            )
            await AuditService(batch.session).record(
                AuditAction.DIRECTION_UPDATED,
                entity_type="direction",
                entity_id=existing.id,
                changes=changes,
            )
        await batch.session.flush()
        return outcome


# ---------------------------------------------------------------------------
# «Ответственные» (B3-8) — КАМ/руководитель вуза
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ResponsibleContext:
    #: `(название, ИНН)` -> итог поиска вуза (`LicenseMatcher.match_university`, B3-4) — поиск
    #: асинхронный, а `check_row` синхронный, поэтому все уникальные пары разрешаются заранее.
    org_results: dict[tuple[str, str], Any]
    #: Текущий ответственный найденного вуза, человекочитаемо (для предупреждения о замене).
    current_owner_name: dict[uuid.UUID, str]
    users_by_email: dict[str, User]
    users_by_name: dict[str, list[User]]


_RESPONSIBLE_ROLES = (Role.KAM.value, Role.HEAD.value)


class ResponsibleHandler(EntityHandler):
    """B3-8: список ответственных за вуз (КАМ/руководитель). Строка меняет `Organization.
    owner_id` — побочные эффекты (аудит, уведомления) те же, что у ручной смены через
    `PUT /api/organizations/{id}/owner` (`OrganizationService.set_owner`, B4-1), но вызвать сам
    метод здесь нельзя: он принимает `Principal` живого запроса, а импорт применяется фоновой
    задачей без него — эффекты повторены напрямую (см. `apply_row`)."""

    entity_type = "responsible"
    dedupe_in_file = True  # повтор вуза в файле — предупреждение, последняя строка побеждает

    async def prepare(
        self, session: AsyncSession, rows: list[dict[str, Any]]
    ) -> ResponsibleContext:
        matcher = LicenseMatcher(session)
        org_pairs = {
            (
                clean_text(r.get("organization_name")) or "",
                clean_text(r.get("organization_inn")) or "",
            )
            for r in rows
            if r.get("organization_name")
        }
        org_results: dict[tuple[str, str], Any] = {}
        current_owner_name: dict[uuid.UUID, str] = {}
        owner_ids: set[uuid.UUID] = set()
        for name, inn in org_pairs:
            result = await matcher.match_university(name, inn or None)
            org_results[(name, inn)] = result
            if result.entity is not None and result.entity.owner_id is not None:
                owner_ids.add(result.entity.owner_id)

        if owner_ids:
            owners = await session.execute(select(User).where(User.id.in_(owner_ids)))
            current_owner_name.update({u.id: u.full_name for u in owners.scalars()})

        emails = {normalize_email(r.get("manager_email")) for r in rows if r.get("manager_email")}
        emails.discard(None)
        users_by_email: dict[str, User] = {}
        if emails:
            found = await session.execute(
                select(User).where(
                    func.lower(User.email).in_(emails),
                    User.role.in_(_RESPONSIBLE_ROLES),
                    User.status == UserStatus.ACTIVE.value,
                    User.deleted_at.is_(None),
                )
            )
            users_by_email = {(u.email or "").lower(): u for u in found.scalars()}

        names = {name_key(r.get("manager_full_name")) for r in rows if r.get("manager_full_name")}
        names.discard("")
        users_by_name: dict[str, list[User]] = {}
        if names:
            found = await session.execute(
                select(User).where(
                    User.role.in_(_RESPONSIBLE_ROLES),
                    User.status == UserStatus.ACTIVE.value,
                    User.deleted_at.is_(None),
                )
            )
            for user in found.scalars():
                key = name_key(user.full_name)
                if key in names:
                    users_by_name.setdefault(key, []).append(user)

        return ResponsibleContext(
            org_results=org_results,
            current_owner_name=current_owner_name,
            users_by_email=users_by_email,
            users_by_name=users_by_name,
        )

    def check_row(self, row: dict[str, Any], ctx: ResponsibleContext, mode: str) -> RowCheck:
        org_key = (
            clean_text(row.get("organization_name")) or "",
            clean_text(row.get("organization_inn")) or "",
        )
        check = RowCheck()
        if not org_key[0]:
            return check  # «ВУЗ» обязателен — `required_empty` уже дан выше по конвейеру

        match = ctx.org_results.get(org_key)
        if match is None or match.entity is None:
            if match is not None and match.how == "ambiguous":
                check.add_issue(
                    RowIssue(
                        code="org_ambiguous",
                        field="organization_name",
                        message=f"Вуз неоднозначен: найдено {len(match.candidates)}",
                        hint="Добавьте колонку «ИНН вуза»",
                    )
                )
            else:
                suggestions = ", ".join(match.suggestions) if match else ""
                check.add_issue(
                    RowIssue(
                        code="org_not_found",
                        field="organization_name",
                        message=f"Вуз не найден: «{org_key[0]}»",
                        hint=f"Возможно: {suggestions}" if suggestions else "Проверьте название",
                    )
                )
            return check
        check.key = str(match.entity.id)
        if match.how not in ("name", "short_name"):
            check.add_issue(
                RowIssue(
                    code="org_found_fuzzy",
                    field="organization_name",
                    message=f"Вуз найден как «{match.entity.name}»",
                    severity="warning",
                )
            )

        email = normalize_email(row.get("manager_email"))
        full_name = clean_text(row.get("manager_full_name"))
        manager: User | None = None
        if email:
            manager = ctx.users_by_email.get(email)
            if manager is None:
                # B3-M: сообщение не открывает почту в явном виде (та же защита, что у
                # `manager_email`/`manager_full_name` в `row_data` — `core.masking.mask_field`).
                check.add_issue(
                    RowIssue(
                        code="manager_not_found",
                        field="manager_email",
                        message=f"Менеджер с почтой «{mask_email(email)}» не найден среди "
                        "активных КАМ/РОП",
                        hint="Проверьте почту или укажите ФИО",
                    )
                )
        elif full_name:
            candidates = ctx.users_by_name.get(name_key(full_name), [])
            if len(candidates) == 1:
                manager = candidates[0]
            elif len(candidates) > 1:
                check.add_issue(
                    RowIssue(
                        code="manager_ambiguous",
                        field="manager_full_name",
                        message=f"Менеджер неоднозначен: найдено {len(candidates)}",
                        hint="Укажите почту менеджера",
                    )
                )
            else:
                check.add_issue(
                    RowIssue(
                        code="manager_not_found",
                        field="manager_full_name",
                        message=f"Менеджер «{mask_name(full_name)}» не найден среди активных "
                        "КАМ/РОП",
                        hint="Проверьте ФИО или укажите почту",
                    )
                )
        else:
            check.add_issue(
                RowIssue(
                    code="required_empty",
                    field="manager_email",
                    message="Не указаны ни почта, ни ФИО менеджера",
                    hint="Заполните один из двух столбцов",
                )
            )

        if manager is not None and match.entity.owner_id and match.entity.owner_id != manager.id:
            current = ctx.current_owner_name.get(match.entity.owner_id, "")
            check.add_issue(
                RowIssue(
                    code="owner_replaced",
                    field="manager_email",
                    message=f"Текущий ответственный {mask_name(current) or '—'} будет заменён",
                    severity="warning",
                )
            )
        return check

    async def apply_row(self, batch: Batch, row: ImportRowResult) -> ApplyOutcome:
        data = row.row_data
        outcome = ApplyOutcome()

        match = await batch.license_matcher.match_university(
            data.get("organization_name"), data.get("organization_inn")
        )
        organization = match.entity
        if organization is None:
            outcome.skipped = True
            outcome.notes.append("Вуз не найден или неоднозначен — пропущена")
            return outcome

        manager: User | None = None
        email = normalize_email(data.get("manager_email"))
        if email:
            manager = await batch.session.scalar(
                select(User).where(
                    func.lower(User.email) == email,
                    User.role.in_(_RESPONSIBLE_ROLES),
                    User.status == UserStatus.ACTIVE.value,
                    User.deleted_at.is_(None),
                )
            )
        elif data.get("manager_full_name"):
            candidates = (
                await batch.session.execute(
                    select(User).where(
                        User.role.in_(_RESPONSIBLE_ROLES),
                        User.status == UserStatus.ACTIVE.value,
                        User.deleted_at.is_(None),
                    )
                )
            ).scalars()
            wanted = name_key(data["manager_full_name"])
            matches = [u for u in candidates if name_key(u.full_name) == wanted]
            manager = matches[0] if len(matches) == 1 else None

        if manager is None:
            outcome.skipped = True
            outcome.notes.append("Менеджер не найден или неоднозначен — пропущена")
            return outcome

        outcome.entity_id = organization.id
        if organization.owner_id == manager.id:
            return outcome  # уже назначен — повторная загрузка файла не меняет версию

        old_owner_id = organization.owner_id
        organization.owner_id = manager.id
        organization.version += 1
        await batch.session.flush()
        outcome.effects.append(
            {
                "kind": "organization",
                "op": "update",
                "id": str(organization.id),
                "before": {"owner_id": str(old_owner_id) if old_owner_id else None},
            }
        )
        audit = AuditService(batch.session)
        await audit.record(
            AuditAction.ORGANIZATION_OWNER_CHANGED,
            entity_type="organization",
            entity_id=organization.id,
            changes={
                "owner_id": {
                    "old": str(old_owner_id) if old_owner_id else None,
                    "new": str(manager.id),
                },
                "reason": {"old": None, "new": "Импорт списка ответственных"},
            },
        )
        notifications = get_notification_service()
        await notifications.notify_user(
            batch.session,
            recipient_id=manager.id,
            template_code="ORG_OWNER_ASSIGNED",
            priority=NotificationPriority.NORMAL,
            entity_type="organization",
            entity_id=organization.id,
        )
        if old_owner_id is not None:
            await notifications.notify_user(
                batch.session,
                recipient_id=old_owner_id,
                template_code="ORG_OWNER_REMOVED",
                priority=NotificationPriority.NORMAL,
                entity_type="organization",
                entity_id=organization.id,
            )
        return outcome


HANDLERS: dict[str, EntityHandler] = {
    handler.entity_type: handler
    for handler in (
        VendorContactHandler(),
        PaymentHandler(),
        LearnerHandler(),
        DirectionHandler(),
        ResponsibleHandler(),
    )
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
        "direction": Direction,
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
            uuid_attrs = ("vendor_id", "organization_id", "owner_id", "parent_id")
            if attr in uuid_attrs and old is not None:
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
        "direction": "направление",
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
    elif kind == "direction":
        checks = {
            "продукты": select(Product.id).where(
                Product.direction_id == entity_id, Product.deleted_at.is_(None)
            ),
            "дочерние направления": select(Direction.id).where(
                Direction.parent_id == entity_id, Direction.deleted_at.is_(None)
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
    "DirectionHandler",
    "EntityHandler",
    "ResponsibleHandler",
    "RowCheck",
    "RowIssue",
    "normalize_email",
    "normalize_phone",
    "rollback_effects",
]
