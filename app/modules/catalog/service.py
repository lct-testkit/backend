"""Сервисный слой каталога: организации, контакты, справочники (раздел 6).

Скоуп организаций и контактов сознательно переиспользует
`crm.service.deal_scope_clause`, а не реализует параллельную версию: раздел
4 определяет видимость организации через сделки («организации, к которым
привязана хотя бы одна его сделка»), значит источник правды один — сервис
сделок. Импорт `catalog.service -> crm.service` не создаёт цикла: `crm`
ссылается только на `catalog.models` (для проверки существования при
создании/обновлении сделки), а не на `catalog.service`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, Select, exists, false, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    AppError,
    ErrorCode,
    FieldError,
    NotFoundError,
    ValidationError,
    VersionConflictError,
)
from app.core.security import Principal
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.catalog.models import (
    Contact,
    ContactChannel,
    CustomFieldDef,
    Direction,
    Holiday,
    LossReason,
    Organization,
    Product,
    Region,
)
from app.modules.catalog.validators import validate_inn
from app.modules.identity.models import Role
from app.modules.notification.service import NotificationPriority, get_notification_service

_SIMILARITY_THRESHOLD = 0.3
_SIMILARITY_LIMIT = 5


def _json_safe(value: Any) -> Any:
    """См. `crm.service._json_safe` — тот же приём, `changes` в аудите не
    умеет `UUID`/`Decimal`/`date` без явного приведения к строке."""
    if isinstance(value, dt.date | dt.datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID | Decimal):
        return str(value)
    return value


# =============================================================================
# Скоуп организаций и контактов (раздел 4)
# =============================================================================


async def organization_scope_clause(
    session: AsyncSession, principal: Principal
) -> ColumnElement[bool] | None:
    """`None` — без ограничения. Раздел 4: KAM/HEAD видят организацию, если
    они её ответственный `owner_id`, либо к ней привязана хотя бы одна
    сделка в их скоупе сделок. Организации не являются ПДн (dop.md §11.8),
    поэтому `INTEGRATION` читает без ограничения — иначе входящий лид не
    сможет найти существующую организацию для дедупликации."""
    from app.modules.crm.models import Deal
    from app.modules.crm.service import deal_scope_clause

    if principal.role in (Role.ADMIN.value, Role.INTEGRATION.value):
        return None
    if principal.role not in (Role.KAM.value, Role.HEAD.value):
        return false()

    deal_clause = await deal_scope_clause(session, principal)
    linked_orgs = select(Deal.organization_id).where(Deal.organization_id.is_not(None))
    if deal_clause is not None:
        linked_orgs = linked_orgs.where(deal_clause)
    return or_(Organization.owner_id == principal.user_id, Organization.id.in_(linked_orgs))


async def organization_in_scope(
    session: AsyncSession, principal: Principal, organization: Organization
) -> bool:
    clause = await organization_scope_clause(session, principal)
    if clause is None:
        return True
    result = await session.scalar(
        select(exists(select(Organization.id).where(Organization.id == organization.id, clause)))
    )
    return bool(result)


async def contact_scope_clause(
    session: AsyncSession, principal: Principal
) -> ColumnElement[bool] | None:
    """Контакты — ПДн (раздел 5.2), скоуп строже: только через организацию
    или сделку в скоупе пользователя, `INTEGRATION` сюда не заходит (нет
    `contact:read` в `permissions.py`, см. router-level `require_permission`)."""
    from app.modules.crm.models import Deal
    from app.modules.crm.service import deal_scope_clause

    if principal.role == Role.ADMIN.value:
        return None
    if principal.role not in (Role.KAM.value, Role.HEAD.value):
        return false()

    org_clause = await organization_scope_clause(session, principal)
    org_ids = select(Organization.id)
    if org_clause is not None:
        org_ids = org_ids.where(org_clause)

    deal_clause = await deal_scope_clause(session, principal)
    contact_ids_via_deal = select(Deal.contact_id).where(Deal.contact_id.is_not(None))
    if deal_clause is not None:
        contact_ids_via_deal = contact_ids_via_deal.where(deal_clause)

    return or_(Contact.organization_id.in_(org_ids), Contact.id.in_(contact_ids_via_deal))


async def contact_in_scope(session: AsyncSession, principal: Principal, contact: Contact) -> bool:
    clause = await contact_scope_clause(session, principal)
    if clause is None:
        return True
    result = await session.scalar(
        select(exists(select(Contact.id).where(Contact.id == contact.id, clause)))
    )
    return bool(result)


# =============================================================================
# Фильтры списков
# =============================================================================


@dataclass(slots=True)
class OrganizationFilters:
    name: str | None = None
    inn: str | None = None
    ogrn: str | None = None
    org_type: str | None = None
    region_id: uuid.UUID | None = None
    owner_id: uuid.UUID | None = None
    registry_status: str | None = None
    is_accredited: bool | None = None
    source: str | None = None
    q: str | None = None


@dataclass(slots=True)
class ContactFilters:
    organization_id: uuid.UUID | None = None
    position: str | None = None
    is_decision_maker: bool | None = None
    is_anonymized: bool | None = None
    source: str | None = None
    q: str | None = None


# =============================================================================
# OrganizationService
# =============================================================================


class OrganizationService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    async def list_query(
        self, principal: Principal, filters: OrganizationFilters
    ) -> Select[tuple[Organization]]:
        stmt = select(Organization).where(Organization.deleted_at.is_(None))
        clause = await organization_scope_clause(self._session, principal)
        if clause is not None:
            stmt = stmt.where(clause)

        if filters.name:
            stmt = stmt.where(Organization.name.ilike(f"%{filters.name.strip()}%"))
        if filters.inn:
            stmt = stmt.where(Organization.inn == filters.inn)
        if filters.ogrn:
            stmt = stmt.where(Organization.ogrn == filters.ogrn)
        if filters.org_type:
            stmt = stmt.where(Organization.org_type == filters.org_type)
        if filters.region_id:
            stmt = stmt.where(Organization.region_id == filters.region_id)
        if filters.owner_id:
            stmt = stmt.where(Organization.owner_id == filters.owner_id)
        if filters.registry_status:
            stmt = stmt.where(Organization.registry_status == filters.registry_status)
        if filters.is_accredited is not None:
            stmt = stmt.where(Organization.is_accredited == filters.is_accredited)
        if filters.source:
            stmt = stmt.where(Organization.source == filters.source)
        if filters.q:
            pattern = f"%{filters.q.strip()}%"
            stmt = stmt.where(
                Organization.name.ilike(pattern)
                | Organization.short_name.ilike(pattern)
                | Organization.inn.ilike(pattern)
            )
        return stmt

    async def get_or_404(self, organization_id: uuid.UUID, principal: Principal) -> Organization:
        organization = await self._session.get(Organization, organization_id)
        if organization is None or organization.deleted_at is not None:
            raise NotFoundError("Организация", organization_id)
        if not await organization_in_scope(self._session, principal, organization):
            # Раздел 3.2: чужой объект вне скоупа — 404, не 403.
            raise NotFoundError("Организация", organization_id)
        return organization

    async def _find_by_inn(self, inn: str) -> Organization | None:
        return await self._session.scalar(select(Organization).where(Organization.inn == inn))

    async def find_similar(self, name: str) -> list[Organization]:
        stmt = (
            select(Organization)
            .where(
                Organization.deleted_at.is_(None),
                func.similarity(Organization.name, name) > _SIMILARITY_THRESHOLD,
            )
            .order_by(func.similarity(Organization.name, name).desc())
            .limit(_SIMILARITY_LIMIT)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def check_duplicate(
        self, principal: Principal, *, inn: str | None, ogrn: str | None, name: str | None
    ) -> tuple[list[Organization], list[Organization]]:
        """Возвращает (по_реквизиту, похожие_по_названию) — раздел 6."""
        exact: list[Organization] = []
        if inn:
            found = await self._find_by_inn(inn)
            if found is not None:
                exact.append(found)
        if ogrn and not exact:
            found = await self._session.scalar(
                select(Organization).where(Organization.ogrn == ogrn)
            )
            if found is not None:
                exact.append(found)

        similar: list[Organization] = []
        if name and not exact:
            similar = await self.find_similar(name)
        return exact, similar

    async def _lookup_registry(self, inn: str) -> Any:
        """Раздел 6: «если организация найдена [в ЕГРЮЛ], поля заполняются
        из реестра». Прямой запрос к `egrul_entries`, не через
        `registry.OrgLookupService` — это внутреннее обогащение при создании
        карточки, а не пользовательский поиск с rate-limit и записью в
        `org_lookup_log` (та ручка — для автоподстановки до отправки формы,
        не для повторной проверки уже введённого и провалидированного ИНН)."""
        from app.modules.registry.models import EgrulEntry

        return await self._session.get(EgrulEntry, inn)

    async def create(self, principal: Principal, payload: Any) -> Organization:
        inn = payload.inn.strip() if payload.inn else None
        registry_entry = None
        if inn:
            check = validate_inn(inn)
            if not check.ok:
                reason = check.reason or "Некорректный ИНН"
                raise ValidationError(reason, [FieldError(field="inn", reason=reason)])
            existing = await self._find_by_inn(inn)
            if existing is not None:
                await self._audit.record(
                    AuditAction.ORGANIZATION_DUPLICATE_FOUND,
                    entity_type="organization",
                    entity_id=existing.id,
                    changes={"inn": {"old": None, "new": inn}},
                )
                raise AppError(
                    ErrorCode.ORGANIZATION_INN_EXISTS,
                    "Организация с таким ИНН уже существует",
                    extra={
                        "organization_id": str(existing.id),
                        "deleted": existing.deleted_at is not None,
                    },
                )
            registry_entry = await self._lookup_registry(inn)

        organization = Organization(
            name=payload.name or (registry_entry.full_name if registry_entry else None)
            or f"Организация (ИНН {inn})",
            short_name=payload.short_name
            or (registry_entry.short_name if registry_entry else None),
            org_type=payload.org_type,
            inn=inn,
            kpp=payload.kpp or (registry_entry.kpp if registry_entry else None),
            ogrn=payload.ogrn or (registry_entry.ogrn if registry_entry else None),
            legal_address=payload.legal_address
            or (registry_entry.legal_address if registry_entry else None),
            actual_address=payload.actual_address,
            region_id=payload.region_id,
            website=payload.website,
            main_phone=payload.main_phone,
            main_email=payload.main_email,
            students_count=payload.students_count,
            external_ids=payload.external_ids or {},
            owner_id=payload.owner_id or principal.user_id,
            source=payload.source,
            created_by=principal.user_id,
            custom_fields=payload.custom_fields or {},
        )
        if registry_entry is not None:
            # dop.md §11.5, п.5: «сохраняется снапшот ответа целиком».
            organization.verified_source = "fns_registry"
            organization.verified_at = dt.datetime.now(dt.UTC)
            organization.registry_version_id = registry_entry.registry_version_id
            organization.registry_status = registry_entry.status
            organization.registry_checked_at = organization.verified_at
            organization.registry_snapshot = {
                "full_name": registry_entry.full_name,
                "short_name": registry_entry.short_name,
                "ogrn": registry_entry.ogrn,
                "kpp": registry_entry.kpp,
                "opf_name": registry_entry.opf_name,
                "status": registry_entry.status,
                "legal_address": registry_entry.legal_address,
                "okved_main": registry_entry.okved_main,
                "registration_date": _json_safe(registry_entry.registration_date),
            }

        self._session.add(organization)
        await self._session.flush()
        await self._audit.record(
            AuditAction.ORGANIZATION_CREATED,
            entity_type="organization",
            entity_id=organization.id,
            changes={
                "name": {"old": None, "new": organization.name},
                "inn": {"old": None, "new": inn},
            },
        )
        return organization

    _PATCHABLE_FIELDS = frozenset(
        {
            "name",
            "short_name",
            "org_type",
            "kpp",
            "ogrn",
            "legal_address",
            "actual_address",
            "region_id",
            "website",
            "main_phone",
            "main_email",
            "students_count",
            "owner_id",
        }
    )

    async def update(
        self, organization: Organization, payload: Any, *, expected_version: int
    ) -> Organization:
        if organization.version != expected_version:
            raise VersionConflictError(organization.version, {"name": organization.name})

        data = payload.model_dump(exclude_unset=True)
        changes: dict[str, dict[str, Any]] = {}

        if "custom_fields" in data:
            new_custom = data.pop("custom_fields")
            if new_custom is not None:
                merged = {**organization.custom_fields, **new_custom}
                if merged != organization.custom_fields:
                    changes["custom_fields"] = {"old": organization.custom_fields, "new": merged}
                    organization.custom_fields = merged

        overridden: list[str] = list(organization.manual_overrides)
        for key, value in data.items():
            if key not in self._PATCHABLE_FIELDS:
                continue
            old = getattr(organization, key)
            old_cmp, new_cmp = _json_safe(old), _json_safe(value)
            if old_cmp == new_cmp:
                continue
            changes[key] = {"old": old_cmp, "new": new_cmp}
            setattr(organization, key, value)
            # Раздел 11.5 dop.md: поля, отредактированные вручную поверх
            # реестра, помечаются и не затираются при следующей сверке.
            if key not in overridden and organization.verified_source:
                overridden.append(key)

        if overridden != organization.manual_overrides:
            organization.manual_overrides = overridden

        if not changes:
            return organization

        organization.version += 1
        await self._session.flush()
        await self._audit.record(
            AuditAction.ORGANIZATION_UPDATED,
            entity_type="organization",
            entity_id=organization.id,
            changes=changes,
        )
        return organization

    async def apply_drift(
        self, organization: Organization, principal: Principal, *, fields: list[str]
    ) -> Organization:
        """Раздел 6: принять расхождения реквизитов, найденные при сверке с
        ЕГРЮЛ (`requisites_drift`, заполняется задачей спринта 5)."""
        drift = organization.requisites_drift or {}
        if not drift:
            raise AppError(ErrorCode.VALIDATION, "Нет расхождений для применения")

        keys = fields or list(drift.keys())
        changes: dict[str, dict[str, Any]] = {}
        remaining = dict(drift)
        for key in keys:
            if key not in drift or not hasattr(organization, key):
                continue
            old = getattr(organization, key)
            new = drift[key]
            if _json_safe(old) != _json_safe(new):
                changes[key] = {"old": _json_safe(old), "new": new}
                setattr(organization, key, new)
            remaining.pop(key, None)

        if not changes:
            raise AppError(ErrorCode.VALIDATION, "Указанные поля не найдены в расхождениях")

        organization.requisites_drift = remaining or None
        organization.registry_checked_at = dt.datetime.now(dt.UTC)
        organization.version += 1
        await self._session.flush()
        await self._audit.record(
            AuditAction.ORG_DRIFT_APPLIED,
            entity_type="organization",
            entity_id=organization.id,
            changes=changes,
        )
        if organization.owner_id:
            await get_notification_service().notify_user(
                self._session,
                recipient_id=organization.owner_id,
                template_code="ORG_DRIFT_APPLIED",
                priority=NotificationPriority.NORMAL,
                entity_type="organization",
                entity_id=organization.id,
            )
        return organization


# =============================================================================
# ContactService
# =============================================================================


class ContactService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    async def list_query(
        self, principal: Principal, filters: ContactFilters
    ) -> Select[tuple[Contact]]:
        stmt = select(Contact).where(Contact.deleted_at.is_(None))
        clause = await contact_scope_clause(self._session, principal)
        if clause is not None:
            stmt = stmt.where(clause)

        if filters.organization_id:
            stmt = stmt.where(Contact.organization_id == filters.organization_id)
        if filters.position:
            stmt = stmt.where(Contact.position.ilike(f"%{filters.position.strip()}%"))
        if filters.is_decision_maker is not None:
            stmt = stmt.where(Contact.is_decision_maker == filters.is_decision_maker)
        if filters.is_anonymized is not None:
            stmt = stmt.where(Contact.is_anonymized == filters.is_anonymized)
        if filters.source:
            stmt = stmt.where(Contact.source == filters.source)
        if filters.q:
            pattern = f"%{filters.q.strip()}%"
            stmt = stmt.where(
                Contact.first_name.ilike(pattern)
                | Contact.last_name.ilike(pattern)
                | Contact.email.ilike(pattern)
            )
        return stmt

    async def get_or_404(self, contact_id: uuid.UUID, principal: Principal) -> Contact:
        contact = await self._session.get(Contact, contact_id)
        if contact is None or contact.deleted_at is not None:
            raise NotFoundError("Контакт", contact_id)
        if not await contact_in_scope(self._session, principal, contact):
            raise NotFoundError("Контакт", contact_id)
        return contact

    async def create(self, principal: Principal, payload: Any) -> Contact:
        if payload.organization_id is not None:
            organization = await self._session.get(Organization, payload.organization_id)
            if organization is None or organization.deleted_at is not None:
                raise NotFoundError("Организация", payload.organization_id)
            if not await organization_in_scope(self._session, principal, organization):
                # Раздел 3.2: без этой проверки KAM мог бы писать контакты в
                # организацию чужого портфеля, зная только её id — раньше
                # проверялось только существование записи, не скоуп.
                raise NotFoundError("Организация", payload.organization_id)

        contact = Contact(
            organization_id=payload.organization_id,
            first_name=payload.first_name,
            last_name=payload.last_name,
            middle_name=payload.middle_name,
            position=payload.position,
            email=payload.email,
            phone=payload.phone,
            is_decision_maker=payload.is_decision_maker,
            consent_id=payload.consent_id,
            source=payload.source,
            external_ids=payload.external_ids or {},
        )
        self._session.add(contact)
        await self._session.flush()

        for channel in payload.channels:
            self._session.add(
                ContactChannel(
                    contact_id=contact.id,
                    type=channel.type,
                    value=channel.value,
                    is_primary=channel.is_primary,
                )
            )
        await self._session.flush()

        await self._audit.record(
            AuditAction.CONTACT_CREATED,
            entity_type="contact",
            entity_id=contact.id,
            changes={
                "organization_id": {
                    "old": None,
                    "new": str(payload.organization_id) if payload.organization_id else None,
                }
            },
        )
        return contact

    _PATCHABLE_FIELDS = frozenset(
        {
            "organization_id",
            "first_name",
            "last_name",
            "middle_name",
            "position",
            "email",
            "phone",
            "is_decision_maker",
        }
    )

    async def update(
        self, contact: Contact, payload: Any, *, principal: Principal, expected_version: int
    ) -> Contact:
        if contact.version != expected_version:
            raise VersionConflictError(contact.version, {"last_name": contact.last_name})

        data = payload.model_dump(exclude_unset=True)
        if data.get("organization_id") is not None:
            organization = await self._session.get(Organization, data["organization_id"])
            if organization is None or organization.deleted_at is not None:
                raise NotFoundError("Организация", data["organization_id"])
            if not await organization_in_scope(self._session, principal, organization):
                raise NotFoundError("Организация", data["organization_id"])

        changes: dict[str, dict[str, Any]] = {}
        for key, value in data.items():
            if key not in self._PATCHABLE_FIELDS:
                continue
            old = getattr(contact, key)
            old_cmp, new_cmp = _json_safe(old), _json_safe(value)
            if old_cmp == new_cmp:
                continue
            changes[key] = {"old": old_cmp, "new": new_cmp}
            setattr(contact, key, value)

        if not changes:
            return contact

        contact.version += 1
        await self._session.flush()
        await self._audit.record(
            AuditAction.CONTACT_UPDATED,
            entity_type="contact",
            entity_id=contact.id,
            changes=changes,
        )
        return contact

    async def channels(self, contact_id: uuid.UUID) -> list[ContactChannel]:
        rows = (
            await self._session.execute(
                select(ContactChannel).where(ContactChannel.contact_id == contact_id)
            )
        ).scalars().all()
        return list(rows)

    async def reveal(self, contact: Contact, principal: Principal) -> list[ContactChannel]:
        """Раздел 6.6: полные данные только через `reveal`, каждый вызов —
        отдельная запись `PII_ACCESS`-класса в аудите."""
        channels = await self.channels(contact.id)
        await self._audit.record(
            AuditAction.PII_REVEALED,
            entity_type="contact",
            entity_id=contact.id,
            changes={"revealed_by": {"old": None, "new": str(principal.user_id)}},
        )
        return channels


# =============================================================================
# Направления
# =============================================================================


@dataclass(slots=True)
class DirectionFilters:
    q: str | None = None
    parent_id: uuid.UUID | None = None


class DirectionService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    def list_query(self, filters: DirectionFilters) -> Select[tuple[Direction]]:
        stmt = select(Direction).where(Direction.deleted_at.is_(None))
        if filters.parent_id is not None:
            stmt = stmt.where(Direction.parent_id == filters.parent_id)
        if filters.q:
            pattern = f"%{filters.q.strip()}%"
            stmt = stmt.where(Direction.name.ilike(pattern) | Direction.code.ilike(pattern))
        return stmt

    async def get_or_404(self, direction_id: uuid.UUID) -> Direction:
        direction = await self._session.get(Direction, direction_id)
        if direction is None or direction.deleted_at is not None:
            raise NotFoundError("Направление", direction_id)
        return direction

    async def create(self, payload: Any) -> Direction:
        existing = await self._session.scalar(
            select(Direction).where(Direction.code == payload.code, Direction.deleted_at.is_(None))
        )
        if existing is not None:
            raise ValidationError(
                "Направление с таким кодом уже существует",
                [FieldError(field="code", reason="код уже используется")],
            )
        direction = Direction(code=payload.code, name=payload.name, parent_id=payload.parent_id)
        self._session.add(direction)
        await self._session.flush()
        await self._audit.record(
            AuditAction.DIRECTION_CREATED,
            entity_type="direction",
            entity_id=direction.id,
            changes={"code": {"old": None, "new": direction.code}},
        )
        return direction

    async def update(
        self, direction: Direction, payload: Any, *, expected_version: int
    ) -> Direction:
        if direction.version != expected_version:
            raise VersionConflictError(direction.version, {"name": direction.name})
        data = payload.model_dump(exclude_unset=True)
        changes: dict[str, dict[str, Any]] = {}
        for key, value in data.items():
            old = getattr(direction, key)
            old_cmp, new_cmp = _json_safe(old), _json_safe(value)
            if old_cmp == new_cmp:
                continue
            changes[key] = {"old": old_cmp, "new": new_cmp}
            setattr(direction, key, value)
        if not changes:
            return direction
        direction.version += 1
        await self._session.flush()
        await self._audit.record(
            AuditAction.DIRECTION_UPDATED,
            entity_type="direction",
            entity_id=direction.id,
            changes=changes,
        )
        return direction


# =============================================================================
# Продукты
# =============================================================================


@dataclass(slots=True)
class ProductFilters:
    direction_id: uuid.UUID | None = None
    code: str | None = None
    is_active: bool | None = None
    format: str | None = None
    q: str | None = None


class ProductService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    def list_query(self, filters: ProductFilters) -> Select[tuple[Product]]:
        stmt = select(Product).where(Product.deleted_at.is_(None))
        if filters.direction_id:
            stmt = stmt.where(Product.direction_id == filters.direction_id)
        if filters.code:
            stmt = stmt.where(Product.code == filters.code)
        if filters.is_active is not None:
            stmt = stmt.where(Product.is_active == filters.is_active)
        if filters.format:
            stmt = stmt.where(Product.format == filters.format)
        if filters.q:
            pattern = f"%{filters.q.strip()}%"
            stmt = stmt.where(Product.name.ilike(pattern) | Product.code.ilike(pattern))
        return stmt

    async def get_or_404(self, product_id: uuid.UUID) -> Product:
        product = await self._session.get(Product, product_id)
        if product is None or product.deleted_at is not None:
            raise NotFoundError("Продукт", product_id)
        return product

    async def create(self, payload: Any) -> Product:
        existing = await self._session.scalar(
            select(Product).where(Product.code == payload.code, Product.deleted_at.is_(None))
        )
        if existing is not None:
            raise ValidationError(
                "Продукт с таким кодом уже существует",
                [FieldError(field="code", reason="код уже используется")],
            )
        product = Product(
            code=payload.code,
            name=payload.name,
            description=payload.description,
            direction_id=payload.direction_id,
            duration_hours=payload.duration_hours,
            format=payload.format,
            base_price=payload.base_price,
            currency=payload.currency,
            is_active=payload.is_active,
            valid_from=payload.valid_from,
            valid_to=payload.valid_to,
            custom_fields=payload.custom_fields or {},
        )
        self._session.add(product)
        await self._session.flush()
        await self._audit.record(
            AuditAction.PRODUCT_CREATED,
            entity_type="product",
            entity_id=product.id,
            changes={"code": {"old": None, "new": product.code}},
        )
        return product

    async def update(self, product: Product, payload: Any, *, expected_version: int) -> Product:
        if product.version != expected_version:
            raise VersionConflictError(product.version, {"name": product.name})
        data = payload.model_dump(exclude_unset=True)
        changes: dict[str, dict[str, Any]] = {}

        if "custom_fields" in data:
            new_custom = data.pop("custom_fields")
            if new_custom is not None:
                merged = {**product.custom_fields, **new_custom}
                if merged != product.custom_fields:
                    changes["custom_fields"] = {"old": product.custom_fields, "new": merged}
                    product.custom_fields = merged

        for key, value in data.items():
            old = getattr(product, key)
            old_cmp, new_cmp = _json_safe(old), _json_safe(value)
            if old_cmp == new_cmp:
                continue
            changes[key] = {"old": old_cmp, "new": new_cmp}
            setattr(product, key, value)

        if not changes:
            return product
        product.version += 1
        await self._session.flush()
        await self._audit.record(
            AuditAction.PRODUCT_UPDATED,
            entity_type="product",
            entity_id=product.id,
            changes=changes,
        )
        return product


# =============================================================================
# Причины отказа
# =============================================================================


class LossReasonService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    def list_query(self, *, is_active: bool | None = None) -> Select[tuple[LossReason]]:
        stmt = select(LossReason)
        if is_active is not None:
            stmt = stmt.where(LossReason.is_active == is_active)
        return stmt.order_by(LossReason.sort_order, LossReason.name)

    async def get_or_404(self, loss_reason_id: uuid.UUID) -> LossReason:
        reason = await self._session.get(LossReason, loss_reason_id)
        if reason is None:
            raise NotFoundError("Причина отказа", loss_reason_id)
        return reason

    async def create(self, payload: Any) -> LossReason:
        existing = await self._session.scalar(
            select(LossReason).where(LossReason.code == payload.code)
        )
        if existing is not None:
            raise ValidationError(
                "Причина с таким кодом уже существует",
                [FieldError(field="code", reason="код уже используется")],
            )
        reason = LossReason(
            code=payload.code,
            name=payload.name,
            category=payload.category,
            is_active=payload.is_active,
            sort_order=payload.sort_order,
        )
        self._session.add(reason)
        await self._session.flush()
        await self._audit.record(
            AuditAction.LOSS_REASON_CREATED,
            entity_type="loss_reason",
            entity_id=reason.id,
            changes={"code": {"old": None, "new": reason.code}},
        )
        return reason

    async def update(
        self, reason: LossReason, payload: Any, *, expected_version: int
    ) -> LossReason:
        if reason.version != expected_version:
            raise VersionConflictError(reason.version, {"name": reason.name})
        data = payload.model_dump(exclude_unset=True)
        changes: dict[str, dict[str, Any]] = {}
        for key, value in data.items():
            old = getattr(reason, key)
            if old == value:
                continue
            changes[key] = {"old": old, "new": value}
            setattr(reason, key, value)
        if not changes:
            return reason
        reason.version += 1
        await self._session.flush()
        await self._audit.record(
            AuditAction.LOSS_REASON_UPDATED,
            entity_type="loss_reason",
            entity_id=reason.id,
            changes=changes,
        )
        return reason


# =============================================================================
# Производственный календарь
# =============================================================================


class HolidayService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    def list_query(
        self, *, date_from: dt.date | None = None, date_to: dt.date | None = None
    ) -> Select[tuple[Holiday]]:
        stmt = select(Holiday)
        if date_from:
            stmt = stmt.where(Holiday.date >= date_from)
        if date_to:
            stmt = stmt.where(Holiday.date <= date_to)
        return stmt.order_by(Holiday.date)

    async def get_or_404(self, holiday_id: uuid.UUID) -> Holiday:
        holiday = await self._session.get(Holiday, holiday_id)
        if holiday is None:
            raise NotFoundError("Запись календаря", holiday_id)
        return holiday

    async def create(self, payload: Any) -> Holiday:
        existing = await self._session.scalar(select(Holiday).where(Holiday.date == payload.date))
        if existing is not None:
            raise ValidationError(
                "Дата уже есть в производственном календаре",
                [FieldError(field="date", reason="дата уже существует")],
            )
        holiday = Holiday(
            date=payload.date, name=payload.name, is_working_day=payload.is_working_day
        )
        self._session.add(holiday)
        await self._session.flush()
        await self._audit.record(
            AuditAction.HOLIDAY_CREATED,
            entity_type="holiday",
            entity_id=holiday.id,
            changes={"date": {"old": None, "new": holiday.date.isoformat()}},
        )
        return holiday

    async def update(self, holiday: Holiday, payload: Any, *, expected_version: int) -> Holiday:
        if holiday.version != expected_version:
            raise VersionConflictError(holiday.version, {"date": holiday.date.isoformat()})
        data = payload.model_dump(exclude_unset=True)
        changes: dict[str, dict[str, Any]] = {}
        for key, value in data.items():
            old = getattr(holiday, key)
            old_cmp, new_cmp = _json_safe(old), _json_safe(value)
            if old_cmp == new_cmp:
                continue
            changes[key] = {"old": old_cmp, "new": new_cmp}
            setattr(holiday, key, value)
        if not changes:
            return holiday
        holiday.version += 1
        await self._session.flush()
        await self._audit.record(
            AuditAction.HOLIDAY_UPDATED,
            entity_type="holiday",
            entity_id=holiday.id,
            changes=changes,
        )
        return holiday


# =============================================================================
# Пользовательские поля
# =============================================================================


class CustomFieldDefService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    def list_query(
        self, *, entity_type: str | None = None, is_active: bool | None = None
    ) -> Select[tuple[CustomFieldDef]]:
        stmt = select(CustomFieldDef)
        if entity_type:
            stmt = stmt.where(CustomFieldDef.entity_type == entity_type)
        if is_active is not None:
            stmt = stmt.where(CustomFieldDef.is_active == is_active)
        return stmt.order_by(CustomFieldDef.entity_type, CustomFieldDef.sort_order)

    async def get_or_404(self, field_id: uuid.UUID) -> CustomFieldDef:
        field = await self._session.get(CustomFieldDef, field_id)
        if field is None:
            raise NotFoundError("Пользовательское поле", field_id)
        return field

    async def create(self, payload: Any) -> CustomFieldDef:
        existing = await self._session.scalar(
            select(CustomFieldDef).where(
                CustomFieldDef.entity_type == payload.entity_type,
                CustomFieldDef.code == payload.code,
            )
        )
        if existing is not None:
            raise ValidationError(
                "Поле с таким кодом уже существует для этого типа сущности",
                [FieldError(field="code", reason="код уже используется")],
            )
        field = CustomFieldDef(
            entity_type=payload.entity_type,
            code=payload.code,
            label=payload.label,
            field_type=payload.field_type,
            options=payload.options,
            is_required=payload.is_required,
            validation=payload.validation,
            workflow_id=payload.workflow_id,
            sort_order=payload.sort_order,
        )
        self._session.add(field)
        await self._session.flush()
        await self._audit.record(
            AuditAction.CUSTOM_FIELD_DEF_CREATED,
            entity_type="custom_field_def",
            entity_id=field.id,
            changes={"code": {"old": None, "new": field.code}},
        )
        return field

    async def update(
        self, field: CustomFieldDef, payload: Any, *, expected_version: int
    ) -> CustomFieldDef:
        if field.version != expected_version:
            raise VersionConflictError(field.version, {"label": field.label})
        data = payload.model_dump(exclude_unset=True)
        changes: dict[str, dict[str, Any]] = {}
        for key, value in data.items():
            old = getattr(field, key)
            old_cmp, new_cmp = _json_safe(old), _json_safe(value)
            if old_cmp == new_cmp:
                continue
            changes[key] = {"old": old_cmp, "new": new_cmp}
            setattr(field, key, value)
        if not changes:
            return field
        field.version += 1
        await self._session.flush()
        await self._audit.record(
            AuditAction.CUSTOM_FIELD_DEF_UPDATED,
            entity_type="custom_field_def",
            entity_id=field.id,
            changes=changes,
        )
        return field


# =============================================================================
# Регионы (только чтение — раздел 5.2, справочник без собственных ручек записи)
# =============================================================================


def region_list_query() -> Select[tuple[Region]]:
    return select(Region).order_by(Region.name)
