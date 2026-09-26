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
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, Select, delete, exists, false, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dependents import restrict_dependents
from app.core.errors import (
    AppError,
    ErrorCode,
    FieldError,
    NotFoundError,
    ValidationError,
    VersionConflictError,
)
from app.core.normalize import clean_text, company_key, name_key, normalize_email, normalize_phone
from app.core.optimistic import claim_version
from app.core.security import Principal
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService, defer_denied_audit
from app.modules.catalog import learner
from app.modules.catalog.drift import drift_new_value
from app.modules.catalog.models import (
    Contact,
    ContactChannel,
    ContactLearnerProfile,
    ContactProduct,
    CustomFieldDef,
    Direction,
    Holiday,
    LossReason,
    Organization,
    OrganizationLicense,
    Product,
    Region,
)
from app.modules.catalog.validators import validate_inn
from app.modules.identity.models import Role
from app.modules.notification.service import NotificationPriority, get_notification_service

_SIMILARITY_THRESHOLD = 0.3
_SIMILARITY_LIMIT = 5
# Сколько организаций с тем же названием показывать в ответе 409 при создании без ИНН.
_SAME_NAME_LIMIT = 5
# Ответственных за один продукт на практике единицы; предел нужен только как страховка выдачи.
_PRODUCT_CONTACTS_LIMIT = 500


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

    return or_(
        Contact.organization_id.in_(org_ids),
        Contact.id.in_(contact_ids_via_deal),
        # Свой только что созданный контакт: организации и сделок у него ещё нет, и без этой ветки
        # `POST /contacts` отвечал 201, а следующий `GET` тут же 404.
        Contact.created_by == principal.user_id,
        # Ответственные за продукты (каталог «Вендоры») — справочные данные для всех менеджеров:
        # без них не узнать, к кому идти по лицензии. Телефон и email в ответе всё равно
        # маскируются, раскрытие — только через `reveal` с записью аудита.
        Contact.id.in_(select(ContactProduct.contact_id)),
    )


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
class ContactUpsertResult:
    """Итог `ContactService.find_or_create`: `changed` — поле → значение ДО (для отката импорта),
    `notes` — то, что применить не удалось и о чём стоит предупредить."""

    contact: Contact
    created: bool
    changed: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


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
            defer_denied_audit(
                self._session,
                entity_type="organization",
                entity_id=organization_id,
                reason="out_of_scope",
            )
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

    async def find_same_name(self, name: str) -> list[Organization]:
        """Неудалённые организации с тем же названием: `ООО «Базис»`, `ооо "Базис"` и `ООО  Базис` —
        одна компания (`company_key`: регистр, кавычки, «ё», тире и пробелы не различают названия).
        Сравниваются полное и краткое название существующих организаций.

        Ключ считается в Python, а не в SQL: результат не зависит от локали БД (`lower()` кириллицы
        в базе с локалью C не работает). Организации создают вручную и редко, а выбираются три
        короткие колонки, поэтому полный проход по каталогу дёшев."""
        key = company_key(name)
        if not key:
            return []
        rows = (
            await self._session.execute(
                select(Organization.id, Organization.name, Organization.short_name).where(
                    Organization.deleted_at.is_(None)
                )
            )
        ).all()
        ids = [
            row.id for row in rows if key in (company_key(row.name), company_key(row.short_name))
        ]
        if not ids:
            return []
        found = (
            await self._session.execute(
                select(Organization)
                .where(Organization.id.in_(ids))
                .order_by(Organization.created_at, Organization.id)
                .limit(_SAME_NAME_LIMIT)
            )
        ).scalars()
        return list(found.all())

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

    async def _reject_same_name(self, principal: Principal, name: str | None) -> None:
        """Организация без ИНН: отличить её от уже заведённой можно только названием. Дубль по
        нормализованному названию — 409 CRM-1301 с кандидатами. Организации с ИНН проверяются по
        ИНН (`ORGANIZATION_INN_EXISTS`), название у них не ключ: у разных юрлиц оно бывает одним.

        `id` кандидата отдаётся только тому, кому организация видна по скоупу; название совпадает с
        введённым, так что существование чужой организации — единственное, что узнаёт вызывающий
        (та же граница, что у `GET /organizations/check-duplicate`)."""
        duplicates = await self.find_same_name(name or "")
        if not duplicates:
            return
        candidates: list[dict[str, Any]] = []
        for existing in duplicates:
            accessible = await organization_in_scope(self._session, principal, existing)
            candidates.append(
                {
                    "id": str(existing.id) if accessible else None,
                    "name": existing.name,
                    "match": "same_name",
                    "accessible": accessible,
                }
            )
        raise AppError(
            ErrorCode.DUPLICATE,
            "Организация с таким названием уже существует",
            extra={"candidates": candidates},
        )

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
        else:
            await self._reject_same_name(principal, payload.name)

        organization = Organization(
            name=payload.name
            or (registry_entry.full_name if registry_entry else None)
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
        self,
        organization: Organization,
        principal: Principal,
        *,
        fields: list[str],
        expected_version: int,
    ) -> Organization:
        """Раздел 6: принять расхождения реквизитов, найденные при сверке с
        ЕГРЮЛ (`requisites_drift`, заполняется задачей спринта 5).

        `expected_version` — та же оптимистичная блокировка, что и у
        `update()` выше: без неё КАМ, правящий карточку организации, мог бы
        молча потерять свою правку, если в этот момент HEAD принимает баннер
        расхождений по тому же полю (раздел 3.5).
        """
        if organization.version != expected_version:
            raise VersionConflictError(organization.version, {"name": organization.name})

        drift = organization.requisites_drift or {}
        if not drift:
            raise AppError(ErrorCode.VALIDATION, "Нет расхождений для применения")

        keys = fields or list(drift.keys())
        changes: dict[str, dict[str, Any]] = {}
        remaining = dict(drift)
        resolved: list[str] = []
        for key in keys:
            if key not in drift or not hasattr(organization, key):
                continue
            old = getattr(organization, key)
            new = drift_new_value(drift[key])
            if _json_safe(old) != _json_safe(new):
                changes[key] = {"old": _json_safe(old), "new": new}
                setattr(organization, key, new)
            # поле, которое уже стоит в карточке (сверка сама выставляет
            # `registry_status`, а расхождение оставляет), тоже принято: баннер
            # не должен висеть на значении, которое менять нечем
            remaining.pop(key, None)
            resolved.append(key)

        if not resolved:
            raise AppError(ErrorCode.VALIDATION, "Указанные поля не найдены в расхождениях")

        organization.requisites_drift = remaining or None
        organization.registry_checked_at = dt.datetime.now(dt.UTC)
        organization.version += 1
        await self._session.flush()
        if not changes:
            return organization
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

    async def reveal(self, organization: Organization) -> None:
        """dop.md §11.8: телефон/email ИП маскированы по умолчанию

        (`OrganizationOut.from_model`) — тот же приём и то же событие
        аудита, что `ContactService.reveal`."""
        await self._audit.record(
            AuditAction.PII_REVEALED,
            entity_type="organization",
            entity_id=organization.id,
            changes={},
        )

    # --- 152-ФЗ: удаление/обезличивание ИП (dop.md §11.8) ------------------

    def _ensure_erasure_applicable(self, organization: Organization) -> None:
        if organization.org_type != "individual_entrepreneur":
            # Сведения о юрлице персональными данными не являются (dop.md
            # §11.8) — для вуза/компании «удаление» просто не тот сценарий:
            # обычное `DELETE`/архивирование организации, не 152-ФЗ-процедура.
            raise AppError(
                ErrorCode.VALIDATION,
                "Удаление/обезличивание по 152-ФЗ применимо только к "
                "организациям типа individual_entrepreneur",
                extra={"org_type": organization.org_type},
            )

    async def collect_erasure_blockers(self, organization: Organization) -> list[dict[str, Any]]:
        """Тот же принцип, что `ContactService.collect_erasure_blockers`:

        действующий договор блокирует, подписей как отдельного субъекта у
        организации нет (подписант — всегда конкретный `contact`/`user`, см.
        dop.md §10.9 `signer_contact_id`/`signer_user_id`, не `organization`).
        """
        self._ensure_erasure_applicable(organization)
        from app.modules.crm.service import count_active_deals_for_organization

        blockers: list[dict[str, Any]] = []
        active_deals = await count_active_deals_for_organization(self._session, organization.id)
        if active_deals:
            blockers.append(
                {
                    "code": "active_contract",
                    "detail": (
                        "Организация связана с действующим договором: обработка ПДн "
                        "остаётся законной до его окончания"
                    ),
                    "count": active_deals,
                    "legal_basis": "ст. 6 ч. 1 п. 5 152-ФЗ",
                }
            )
        return blockers

    async def anonymize(self, organization: Organization) -> None:
        """Режим B: те же поля, что dop.md §11.8 называет ПДн ИП (ФИО = имя

        карточки, адрес регистрации), плюс контакты. `inn`/`kpp`/`ogrn`
        сознательно не трогаются — это регистрационные номера, а не сами
        персональные данные, и они нужны, чтобы отличить одну обезличенную
        запись от другой в истории сделок."""
        self._ensure_erasure_applicable(organization)
        # Хвост UUIDv7, а не начало: старшие биты — метка времени, и объекты, созданные в одну
        # минуту, получали бы одинаковый «псевдоним».
        short_id = organization.id.hex[-8:]
        organization.name = f"ИП #{short_id}"
        organization.short_name = None
        organization.legal_address = None
        organization.actual_address = None
        organization.main_phone = None
        organization.main_email = None
        await claim_version(self._session, organization)
        await self._session.flush()

    async def hard_delete_blockers(self, organization: Organization) -> list[dict[str, Any]]:
        """Что мешает физически удалить организацию: записи, ссылающиеся на неё внешним ключом
        без каскада (сделки, включая завершённые, контакты, лицензии…), в том числе мягко
        удалённые — БД их всё равно видит.

        Раньше проверялись только сделки, а остальное обнаруживалось уже при `DELETE`: запрос
        падал, вечно висел в очереди и каждые 15 минут заново выпускал акт уничтожения."""
        self._ensure_erasure_applicable(organization)
        return await restrict_dependents(self._session, Organization.__tablename__, organization.id)

    async def hard_delete_eligible(self, organization: Organization) -> bool:
        """Режим C (new_spec §4.8.2): только если на организацию нет ссылок вообще — ни сделок
        (включая завершённые), ни контактов, лицензий, продуктов."""
        return not await self.hard_delete_blockers(organization)


# =============================================================================
# ContactService
# =============================================================================


def _is_placeholder_last_name(value: object) -> bool:
    """Заглушка вместо фамилии («—», «-», пусто) не различает людей: см. `find_matches`."""
    return not name_key(value).strip("-. ")


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
            needle = filters.q.strip()
            pattern = f"%{needle}%"
            condition = Contact.first_name.ilike(pattern) | Contact.last_name.ilike(pattern)
            # Email в выдаче маскирован, а поиск по подстроке позволял подбирать его по символам
            # (`a%`, `ab%`, …). Полный адрес находится только точным совпадением: подобрать
            # чужой адрес так нельзя, а известный — найти можно.
            if "@" in needle:
                condition = condition | (func.lower(Contact.email) == needle.lower())
            stmt = stmt.where(condition)
        return stmt

    async def get_or_404(self, contact_id: uuid.UUID, principal: Principal) -> Contact:
        contact = await self._session.get(Contact, contact_id)
        if contact is None or contact.deleted_at is not None:
            raise NotFoundError("Контакт", contact_id)
        if not await contact_in_scope(self._session, principal, contact):
            defer_denied_audit(
                self._session, entity_type="contact", entity_id=contact_id, reason="out_of_scope"
            )
            raise NotFoundError("Контакт", contact_id)
        return contact

    # --- Дедупликация и приём из внешних потоков ---------------------------

    @staticmethod
    def _identity(email: object, phone: object) -> tuple[str | None, str | None]:
        """Нормализованные email и телефон; непустое значение, которое не разобралось, — 422, а не
        молчаливая потеря ключа дедупликации."""
        email_n, phone_n = normalize_email(email), normalize_phone(phone)
        errors: list[FieldError] = []
        if clean_text(email) and email_n is None:
            errors.append(FieldError(field="email", reason="некорректный адрес электронной почты"))
        if clean_text(phone) and phone_n is None:
            errors.append(FieldError(field="phone", reason="некорректный номер телефона"))
        if errors:
            raise ValidationError("Некорректные контактные данные", errors)
        return email_n, phone_n

    async def _lock_identity(self, email: str | None, phone: str | None) -> None:
        """Два одновременных запроса с одним email проходят проверку «такого ещё нет» оба и создают
        два контакта. Транзакционный advisory-lock на ключ идентичности сериализует их: второй
        дождётся коммита первого и найдёт его контакт. Ключи берутся в фиксированном порядке — иначе
        пара «email+телефон» и «телефон+email» взаимно заблокируется."""
        keys = sorted(
            key
            for key in (
                f"contact-email:{email}" if email else None,
                f"contact-phone:{phone}" if phone else None,
            )
            if key
        )
        for key in keys:
            await self._session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": key}
            )

    async def find_matches(
        self,
        *,
        email: str | None,
        phone: str | None,
        last_name: str | None = None,
        exclude_id: uuid.UUID | None = None,
    ) -> list[tuple[Contact, str]]:
        """Контакты, которые это тот же человек: `[(контакт, "email" | "phone"), …]`, сначала по
        email.

        Правило: совпал email (без регистра) — тот же человек. Телефон один и тот же у разных
        людей бывает (общий номер кафедры, семейный номер), поэтому по телефону совпадение только
        при той же фамилии; если фамилии в запросе нет — по телефону одному. Заглушка вместо
        фамилии (`—` у лида «только телефон») — тоже «нет фамилии», причём с обеих сторон: и у
        запроса, и у уже заведённого контакта. Иначе лид «— …» и следующий с тем же телефоном, но
        уже с настоящей фамилией, становились двумя людьми. Удалённые и обезличенные контакты не
        участвуют: обезличенный человек, подавший заявку снова, — новый контакт (new_spec §4.8.5).
        Аргументы уже нормализованы (`_identity`)."""
        if not email and not phone:
            return []
        base = select(Contact).where(Contact.deleted_at.is_(None), Contact.is_anonymized.is_(False))
        if exclude_id is not None:
            base = base.where(Contact.id != exclude_id)

        found: list[tuple[Contact, str]] = []
        seen: set[uuid.UUID] = set()
        if email:
            rows = (
                (
                    await self._session.execute(
                        base.where(Contact.email == email)
                        .order_by(Contact.created_at, Contact.id)
                        .limit(20)
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                seen.add(row.id)
                found.append((row, "email"))
        if phone:
            wanted = "" if _is_placeholder_last_name(last_name) else name_key(last_name)
            rows = (
                (
                    await self._session.execute(
                        base.where(Contact.phone == phone)
                        .order_by(Contact.created_at, Contact.id)
                        .limit(20)
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                if row.id in seen:
                    continue
                if (
                    not wanted
                    or _is_placeholder_last_name(row.last_name)
                    or name_key(row.last_name) == wanted
                ):
                    found.append((row, "phone"))
        return found

    async def match_contact(
        self,
        *,
        email: object = None,
        phone: object = None,
        last_name: str | None = None,
        exclude_id: uuid.UUID | None = None,
    ) -> Contact | None:
        email_n, phone_n = self._identity(email, phone)
        matches = await self.find_matches(
            email=email_n, phone=phone_n, last_name=last_name, exclude_id=exclude_id
        )
        return matches[0][0] if matches else None

    async def _insert(
        self,
        *,
        organization_id: uuid.UUID | None,
        first_name: str,
        last_name: str,
        middle_name: str | None,
        position: str | None,
        email: str | None,
        phone: str | None,
        is_decision_maker: bool,
        consent_id: uuid.UUID | None,
        source: str | None,
        external_ids: dict[str, Any] | None,
        contact_methods: list[str] | None,
        created_by: uuid.UUID | None,
        channels: list[Any] | None = None,
    ) -> Contact:
        contact = Contact(
            organization_id=organization_id,
            first_name=first_name,
            last_name=last_name,
            middle_name=middle_name,
            position=position,
            email=email,
            phone=phone,
            is_decision_maker=is_decision_maker,
            consent_id=consent_id,
            source=source,
            external_ids=external_ids or {},
            contact_methods=list(contact_methods or []),
            created_by=created_by,
        )
        self._session.add(contact)
        await self._session.flush()

        for channel in channels or []:
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
                    "new": str(organization_id) if organization_id else None,
                },
                "source": {"old": None, "new": source},
            },
        )
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

        email, phone = self._identity(payload.email, payload.phone)
        await self._lock_identity(email, phone)
        matches = await self.find_matches(email=email, phone=phone, last_name=payload.last_name)
        if matches:
            # Раньше дубли не проверялись вовсе: те же пять человек, что уже пришли с сайта,
            # заводились вторыми экземплярами. Ответ не раскрывает чужие контакты — `id` отдаётся
            # только тем, кому контакт виден по скоупу.
            candidates: list[dict[str, Any]] = []
            for existing, by in matches:
                accessible = await contact_in_scope(self._session, principal, existing)
                candidates.append(
                    {
                        "id": str(existing.id) if accessible else None,
                        "match": by,
                        "accessible": accessible,
                    }
                )
            raise AppError(
                ErrorCode.DUPLICATE,
                "Контакт с таким email или телефоном уже существует",
                extra={"candidates": candidates},
            )

        return await self._insert(
            organization_id=payload.organization_id,
            first_name=payload.first_name,
            last_name=payload.last_name,
            middle_name=payload.middle_name,
            position=payload.position,
            email=email,
            phone=phone,
            is_decision_maker=payload.is_decision_maker,
            consent_id=payload.consent_id,
            source=payload.source,
            external_ids=payload.external_ids,
            contact_methods=list(getattr(payload, "contact_methods", None) or []),
            created_by=principal.user_id,
            channels=list(payload.channels),
        )

    async def find_or_create(
        self,
        *,
        first_name: str,
        last_name: str,
        middle_name: str | None = None,
        email: object = None,
        phone: object = None,
        organization_id: uuid.UUID | None = None,
        position: str | None = None,
        contact_methods: list[str] | None = None,
        source: str,
        created_by: uuid.UUID | None = None,
        external_ids: dict[str, Any] | None = None,
    ) -> ContactUpsertResult:
        """Приём человека из внешнего потока (вебхук сайта, импорт файлов): найти того же
        человека или завести нового. Существующему только ДОЗАПОЛНЯЕТСЯ пустое (отчество, email,
        телефон, должность, организация, способы связи): то, что в карточке уже есть, файл не
        перезаписывает — менеджер мог поправить вручную.

        `changed` — прежние значения дозаполненных полей: по ним импорт откатывает строку."""
        email_n, phone_n = self._identity(email, phone)
        await self._lock_identity(email_n, phone_n)
        matches = await self.find_matches(email=email_n, phone=phone_n, last_name=last_name)
        if not matches:
            contact = await self._insert(
                organization_id=organization_id,
                first_name=first_name,
                last_name=last_name,
                middle_name=middle_name,
                position=position,
                email=email_n,
                phone=phone_n,
                is_decision_maker=False,
                consent_id=None,
                source=source,
                external_ids=external_ids,
                contact_methods=contact_methods,
                created_by=created_by,
            )
            return ContactUpsertResult(contact=contact, created=True)

        existing = matches[0][0]
        result = ContactUpsertResult(contact=existing, created=False)
        proposed: dict[str, Any] = {
            "middle_name": middle_name,
            "email": email_n,
            "phone": phone_n,
            "position": position,
        }
        for attr, value in proposed.items():
            if value and not getattr(existing, attr):
                result.changed[attr] = getattr(existing, attr)
                setattr(existing, attr, value)
        if organization_id is not None:
            if existing.organization_id is None:
                result.changed["organization_id"] = None
                existing.organization_id = organization_id
            elif existing.organization_id != organization_id:
                result.notes.append("Контакт уже привязан к другой организации — не изменена")
        methods = [m for m in (contact_methods or []) if m not in (existing.contact_methods or [])]
        if methods:
            result.changed["contact_methods"] = list(existing.contact_methods or [])
            existing.contact_methods = [*(existing.contact_methods or []), *methods]
        if result.changed:
            existing.version += 1
            await self._session.flush()
            await self._audit.record(
                AuditAction.CONTACT_UPDATED,
                entity_type="contact",
                entity_id=existing.id,
                changes={
                    key: {"old": _json_safe(old), "new": _json_safe(getattr(existing, key))}
                    for key, old in result.changed.items()
                },
            )
        return result

    # --- 152-ФЗ: удаление/обезличивание (new_spec §4.8.5) -----------------

    async def collect_erasure_blockers(self, contact: Contact) -> list[dict[str, Any]]:
        """Блокеры для контакта — тот же принцип, что `AdminUserService.

        collect_erasure_blockers` для сотрудника (`app/modules/identity/
        admin_service.py`), но с блокерами, специфичными для субъекта B2C/
        представителя вуза: действующий договор вместо активных сделок
        сотрудника, подпись контура B вместо подписи контура A.
        """
        from app.modules.crm.service import count_active_deals_for_contact
        from app.modules.signing.service import get_signing_service

        blockers: list[dict[str, Any]] = []

        active_deals = await count_active_deals_for_contact(self._session, contact.id)
        if active_deals:
            blockers.append(
                {
                    "code": "active_contract",
                    "detail": (
                        "Контакт связан с действующим договором: обработка ПДн остаётся "
                        "законной до его окончания"
                    ),
                    "count": active_deals,
                    "legal_basis": "ст. 6 ч. 1 п. 5 152-ФЗ",
                }
            )

        signatures = await get_signing_service().count_signatures_for_contact(
            self._session, contact.id
        )
        if signatures:
            # dop §10.7: правило одинаково для обоих контуров подписания —
            # подпись без идентификации подписанта теряет юридическую силу.
            blockers.append(
                {
                    "code": "has_signatures",
                    "detail": (
                        "Подписи не обезличиваются: подпись без идентификации подписанта "
                        "теряет юридическую силу"
                    ),
                    "count": signatures,
                    "legal_basis": "ст. 6 ч. 1 п. 5 и п. 7 152-ФЗ",
                }
            )

        if contact.is_anonymized:
            blockers.append(
                {"code": "already_anonymized", "detail": "Контакт уже обезличен", "count": 1}
            )
        return blockers

    async def anonymize(self, contact: Contact) -> None:
        """Режим B (new_spec §4.8.2) для контакта: поля затираются, запись

        остаётся — на неё ссылаются `deals.contact_id`/`deal_comments` и т.д.
        Повторное появление того же человека после обезличивания — новый
        контакт, не попытка склейки (new_spec §4.8.5, «это правильное
        поведение, а не баг»): дедупликации по обезличенным полям здесь
        сознательно нет.
        """
        # Хвост UUIDv7, а не начало: старшие биты — метка времени, и объекты, созданные в одну
        # минуту, получали бы одинаковый «псевдоним».
        short_id = contact.id.hex[-8:]
        contact.first_name = f"Контакт #{short_id}"
        contact.last_name = ""
        contact.middle_name = None
        contact.email = None
        contact.phone = None
        contact.is_anonymized = True
        contact.anonymized_at = dt.datetime.now(dt.UTC)
        await self._session.execute(
            ContactChannel.__table__.delete().where(ContactChannel.contact_id == contact.id)
        )
        # Профиль учащегося (СНИЛС, паспорт, адрес, диплом) — те же ПДн, что и у контакта: запись
        # контакта остаётся ради истории сделок, а эти данные после обезличивания хранить нельзя.
        await self._session.execute(
            delete(ContactLearnerProfile).where(ContactLearnerProfile.contact_id == contact.id)
        )
        await self._session.flush()

    async def hard_delete_blockers(self, contact: Contact) -> list[dict[str, Any]]:
        """Записи, ссылающиеся на контакт внешним ключом без каскада (сделки, включая
        завершённые, подписи, лицензии…): они не дают физически удалить строку. Таблицы берутся
        из метаданных ORM (`core.dependents`), а не из списка, который устаревает с миграциями."""
        return await restrict_dependents(self._session, Contact.__tablename__, contact.id)

    async def hard_delete_eligible(self, contact: Contact) -> bool:
        """Режим C (new_spec §4.8.2): только если на контакт нет ссылок вообще — ни в сделках
        (включая завершённые), ни в других таблицах."""
        return not await self.hard_delete_blockers(contact)

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
            "contact_methods",
        }
    )

    async def update(
        self, contact: Contact, payload: Any, *, principal: Principal, expected_version: int
    ) -> Contact:
        if contact.version != expected_version:
            raise VersionConflictError(contact.version, {"last_name": contact.last_name})
        if contact.is_anonymized:
            # Обезличенный контакт нельзя снова заполнить ПДн: иначе обезличивание (152-ФЗ)
            # отменялось бы обычным PATCH.
            raise AppError(
                ErrorCode.ERASURE_BLOCKED,
                "Контакт обезличен: его данные больше не редактируются",
            )

        data = payload.model_dump(exclude_unset=True)
        # Явный `null` в обязательных полях — ошибка клиента (422), а не нарушение NOT NULL в БД.
        null_required = [k for k in ("first_name", "last_name") if k in data and data[k] is None]
        if null_required:
            raise ValidationError(
                "Обязательные поля нельзя очистить",
                [FieldError(field=k, reason="обязательное поле") for k in null_required],
            )
        if data.get("organization_id") is not None:
            organization = await self._session.get(Organization, data["organization_id"])
            if organization is None or organization.deleted_at is not None:
                raise NotFoundError("Организация", data["organization_id"])
            if not await organization_in_scope(self._session, principal, organization):
                raise NotFoundError("Организация", data["organization_id"])

        if "email" in data or "phone" in data:
            email_n, phone_n = self._identity(
                data.get("email", contact.email), data.get("phone", contact.phone)
            )
            if "email" in data:
                data["email"] = email_n
            if "phone" in data:
                data["phone"] = phone_n
            if email_n != contact.email or phone_n != contact.phone:
                await self._lock_identity(email_n, phone_n)
                last_name = data.get("last_name") or contact.last_name
                clash = await self.find_matches(
                    email=email_n if email_n != contact.email else None,
                    phone=phone_n if phone_n != contact.phone else None,
                    last_name=last_name,
                    exclude_id=contact.id,
                )
                if clash:
                    raise AppError(
                        ErrorCode.DUPLICATE,
                        "Контакт с таким email или телефоном уже существует",
                        extra={
                            "candidates": [
                                {"match": by, "accessible": False} for _existing, by in clash
                            ]
                        },
                    )

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
            (
                await self._session.execute(
                    select(ContactChannel).where(ContactChannel.contact_id == contact_id)
                )
            )
            .scalars()
            .all()
        )
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

    async def _check_parent(self, direction: Direction | None, parent_id: uuid.UUID) -> None:
        """Родитель существует и не замыкает цикл: родителем направления не может быть оно
        само или его потомок. Идём вверх по предкам нового родителя — встретили само
        направление, значит, новый родитель его потомок."""
        parent = await self._session.get(Direction, parent_id)
        if parent is None or parent.deleted_at is not None:
            raise NotFoundError("Направление", parent_id)
        if direction is None:
            return
        visited: set[uuid.UUID] = set()
        ancestor: Direction | None = parent
        while ancestor is not None and ancestor.id not in visited:
            if ancestor.id == direction.id:
                raise ValidationError(
                    "Направление не может быть родителем самого себя или своего потомка",
                    [FieldError(field="parent_id", reason="цикл в иерархии направлений")],
                )
            visited.add(ancestor.id)
            ancestor = (
                await self._session.get(Direction, ancestor.parent_id)
                if ancestor.parent_id is not None
                else None
            )

    async def create(self, payload: Any) -> Direction:
        existing = await self._session.scalar(
            select(Direction).where(Direction.code == payload.code, Direction.deleted_at.is_(None))
        )
        if existing is not None:
            raise ValidationError(
                "Направление с таким кодом уже существует",
                [FieldError(field="code", reason="код уже используется")],
            )
        if payload.parent_id is not None:
            await self._check_parent(None, payload.parent_id)
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
        if data.get("parent_id") is not None:
            await self._check_parent(direction, data["parent_id"])
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

    async def delete(self, direction: Direction) -> None:
        """П4: мягкое удаление, только если ничего не сломает — раздел 4:
        нет дочерних направлений (иерархия, `parent_id`) и ни один продукт
        на него не ссылается (`Product.direction_id`; сделки — только
        транзитивно через продукт, отдельной FK на направление у них нет,
        так что проверки продуктов достаточно)."""
        child_exists = await self._session.scalar(
            select(Direction.id)
            .where(Direction.parent_id == direction.id, Direction.deleted_at.is_(None))
            .limit(1)
        )
        if child_exists is not None:
            raise AppError(
                ErrorCode.ENTITY_IN_USE,
                "У направления есть дочерние направления — удаление невозможно",
            )
        product_exists = await self._session.scalar(
            select(Product.id)
            .where(Product.direction_id == direction.id, Product.deleted_at.is_(None))
            .limit(1)
        )
        if product_exists is not None:
            raise AppError(
                ErrorCode.ENTITY_IN_USE,
                "На направление ссылаются продукты — удаление невозможно",
            )
        direction.deleted_at = dt.datetime.now(dt.UTC)
        await self._session.flush()
        await self._audit.record(
            AuditAction.DIRECTION_DELETED,
            entity_type="direction",
            entity_id=direction.id,
            changes={"code": {"old": direction.code, "new": None}},
        )


# =============================================================================
# Продукты
# =============================================================================


@dataclass(slots=True)
class ProductFilters:
    direction_id: uuid.UUID | None = None
    vendor_id: uuid.UUID | None = None
    code: str | None = None
    is_active: bool | None = None
    format: str | None = None
    q: str | None = None


def _check_validity_period(valid_from: dt.date | None, valid_to: dt.date | None) -> None:
    if valid_from is not None and valid_to is not None and valid_from > valid_to:
        raise ValidationError(
            "Срок действия продукта заканчивается раньше, чем начинается",
            [FieldError(field="valid_to", reason="не может быть раньше valid_from")],
        )


class ProductService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    def list_query(self, filters: ProductFilters) -> Select[tuple[Product]]:
        stmt = select(Product).where(Product.deleted_at.is_(None))
        if filters.direction_id:
            stmt = stmt.where(Product.direction_id == filters.direction_id)
        if filters.vendor_id:
            stmt = stmt.where(Product.vendor_id == filters.vendor_id)
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

    async def vendor_names(self, products: Iterable[Product]) -> dict[uuid.UUID, str]:
        """Названия вендоров для выдачи (`ProductOut.vendor_name`): один запрос на всю страницу.
        Название удалённой организации тоже отдаётся: продукт по-прежнему на неё ссылается."""
        vendor_ids = {p.vendor_id for p in products if p.vendor_id is not None}
        if not vendor_ids:
            return {}
        rows = (
            await self._session.execute(
                select(Organization.id, Organization.name).where(Organization.id.in_(vendor_ids))
            )
        ).all()
        return {row.id: row.name for row in rows}

    async def _require_vendor(self, vendor_id: uuid.UUID) -> None:
        """Вендор — обычная организация каталога: несуществующая или удалённая — 404, как любая
        другая ссылка из тела запроса."""
        organization = await self._session.get(Organization, vendor_id)
        if organization is None or organization.deleted_at is not None:
            raise NotFoundError("Организация", vendor_id)

    async def create(self, payload: Any) -> Product:
        # Уникальный индекс `uq_products_code` не смотрит на `deleted_at`: код удалённого продукта
        # остаётся занятым. Без явной проверки это было бы нарушение ограничения уже при вставке.
        existing = await self._session.scalar(select(Product).where(Product.code == payload.code))
        if existing is not None and existing.deleted_at is not None:
            raise AppError(
                ErrorCode.DUPLICATE,
                "Код занят удалённым продуктом: выберите другой код",
                errors=[FieldError(field="code", reason="код занят удалённым продуктом")],
                extra={"product_id": str(existing.id), "deleted": True},
            )
        if existing is not None:
            raise ValidationError(
                "Продукт с таким кодом уже существует",
                [FieldError(field="code", reason="код уже используется")],
            )
        _check_validity_period(payload.valid_from, payload.valid_to)
        if payload.vendor_id is not None:
            await self._require_vendor(payload.vendor_id)
        product = Product(
            code=payload.code,
            name=payload.name,
            description=payload.description,
            direction_id=payload.direction_id,
            vendor_id=payload.vendor_id,
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
        # Дата из запроса сверяется с сохранённой второй: в PATCH может прийти только одна.
        _check_validity_period(
            data.get("valid_from", product.valid_from), data.get("valid_to", product.valid_to)
        )
        # Вендор проверяется только при смене: продукт, чей вендор потом удалён, редактируется как
        # обычно, пока вендора не трогают.
        if data.get("vendor_id") is not None and data["vendor_id"] != product.vendor_id:
            await self._require_vendor(data["vendor_id"])
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
# Ответственные за продукты: связь контакт — продукт (каталог «Вендоры»)
# =============================================================================


class ProductContactService:
    """`contact_products`: кто отвечает за продукт. Связи заводит импорт «Вендоров» и
    администратор каталога вручную; читать их могут все, кому видны сами контакты (телефон и email
    в ответе маскированы, полные значения — только через `reveal` с записью аудита).

    Запись аудита привязана к продукту (`entity_type='product'`): вопрос «кто и когда назначил
    ответственного» задают с карточки продукта."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    async def contacts_of_product(
        self, product: Product, principal: Principal
    ) -> list[tuple[Contact, str]]:
        """Ответственные продукта, видимые вызывающему по скоупу контактов. Удалённые и
        обезличенные контакты не показываются: связаться с ними нельзя."""
        stmt = (
            select(Contact, ContactProduct.role)
            .join(ContactProduct, ContactProduct.contact_id == Contact.id)
            .where(
                ContactProduct.product_id == product.id,
                Contact.deleted_at.is_(None),
                Contact.is_anonymized.is_(False),
            )
            .order_by(Contact.last_name, Contact.first_name, Contact.id)
            .limit(_PRODUCT_CONTACTS_LIMIT)
        )
        clause = await contact_scope_clause(self._session, principal)
        if clause is not None:
            stmt = stmt.where(clause)
        return [(contact, role) for contact, role in (await self._session.execute(stmt)).all()]

    async def products_of_contact(self, contact: Contact) -> list[tuple[Product, str]]:
        """Продукты, за которые отвечает контакт (доступ к самому контакту проверен вызывающим)."""
        stmt = (
            select(Product, ContactProduct.role)
            .join(ContactProduct, ContactProduct.product_id == Product.id)
            .where(ContactProduct.contact_id == contact.id, Product.deleted_at.is_(None))
            .order_by(Product.name, Product.id)
        )
        return [(product, role) for product, role in (await self._session.execute(stmt)).all()]

    async def link(self, product: Product, contact: Contact, role: str) -> None:
        """Ставит связь или меняет роль у существующей; повтор с той же ролью ничего не пишет."""
        link = await self._session.get(ContactProduct, (contact.id, product.id))
        old_role: str | None = None
        if link is None:
            self._session.add(
                ContactProduct(contact_id=contact.id, product_id=product.id, role=role)
            )
        elif link.role != role:
            old_role = link.role
            link.role = role
        else:
            return
        await self._session.flush()
        await self._audit.record(
            AuditAction.CONTACT_PRODUCT_LINKED,
            entity_type="product",
            entity_id=product.id,
            changes={
                "contact_id": {
                    "old": None if link is None else str(contact.id),
                    "new": str(contact.id),
                },
                "role": {"old": old_role, "new": role},
            },
        )

    async def unlink(self, product: Product, contact: Contact) -> None:
        link = await self._session.get(ContactProduct, (contact.id, product.id))
        if link is None:
            raise NotFoundError("Ответственный за продукт", contact.id)
        role = link.role
        await self._session.delete(link)
        await self._session.flush()
        await self._audit.record(
            AuditAction.CONTACT_PRODUCT_UNLINKED,
            entity_type="product",
            entity_id=product.id,
            changes={
                "contact_id": {"old": str(contact.id), "new": None},
                "role": {"old": role, "new": None},
            },
        )


# =============================================================================
# Профиль учащегося: ПДн для шаблона LMS «Загрузка пользователей»
# =============================================================================


class LearnerProfileService:
    """`contact_learner_profiles`: СНИЛС, паспорт, адрес регистрации, диплом. Наружу значения
    уходят только маскированными (`LearnerProfileOut`) либо через `reveal` с записью аудита. В
    аудит попадают имена изменённых полей, но не значения: журнал читает больше людей, чем видит
    сам профиль."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    async def get(self, contact: Contact) -> ContactLearnerProfile | None:
        return await self._session.get(ContactLearnerProfile, contact.id)

    async def reveal(self, contact: Contact) -> ContactLearnerProfile | None:
        """Полный профиль. Аудит — только категория данных: кто раскрыл и когда, знает запись
        журнала (актор), сами значения в неё не попадают."""
        profile = await self.get(contact)
        await self._audit.record(
            AuditAction.PII_REVEALED,
            entity_type="contact",
            entity_id=contact.id,
            changes={"category": {"old": None, "new": "learner_profile"}},
        )
        return profile

    async def update(
        self, contact: Contact, values: dict[str, str | None]
    ) -> ContactLearnerProfile | None:
        """Частичное обновление: меняются только переданные поля, пустое значение очищает поле.
        Каждое значение разбирается тем же `learner.parse_profile_value`, что и при импорте шаблона,
        поэтому ручной ввод и файл дают одинаковые данные и одинаковые сообщения об ошибках."""
        if contact.is_anonymized:
            raise ValidationError("Контакт обезличен: данные учащегося не сохраняются")

        parsed: dict[str, object | None] = {}
        errors: list[FieldError] = []
        for name, raw in values.items():
            if name not in learner.PROFILE_TARGETS:
                errors.append(FieldError(field=name, reason="неизвестное поле профиля"))
                continue
            text_value = (raw or "").strip()
            if not text_value:
                parsed[name] = None
                continue
            try:
                parsed[name] = learner.parse_profile_value(name, text_value)
            except ValueError as exc:
                errors.append(FieldError(field=name, reason=str(exc)))
        if errors:
            raise ValidationError("Некорректные данные учащегося", errors)

        profile = await self._session.get(ContactLearnerProfile, contact.id)
        if profile is None:
            if all(value is None for value in parsed.values()):
                return None  # очищать нечего, пустую строку в таблице заводить незачем
            profile = ContactLearnerProfile(contact_id=contact.id)
            self._session.add(profile)

        changed = sorted(name for name, value in parsed.items() if getattr(profile, name) != value)
        if not changed:
            return profile
        for name in changed:
            setattr(profile, name, parsed[name])
        await self._session.flush()
        await self._audit.record(
            AuditAction.CONTACT_UPDATED,
            entity_type="contact",
            entity_id=contact.id,
            # Только имена полей: СНИЛС, паспорт и адрес в журнал аудита не попадают.
            changes={"learner_profile": {"old": None, "new": changed}},
        )
        return profile


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

    async def delete(self, reason: LossReason) -> None:
        """П4: жёсткое удаление (таблица без `deleted_at` — докстринг модели:
        «деактивируется, не удаляется», это верно вплоть до этого пункта) —
        только если причина не используется ни в одной сделке
        (`Deal.loss_reason_id`)."""
        from app.modules.crm.models import Deal

        in_use = await self._session.scalar(
            select(Deal.id).where(Deal.loss_reason_id == reason.id).limit(1)
        )
        if in_use is not None:
            raise AppError(
                ErrorCode.ENTITY_IN_USE,
                "Причина используется в сделках — удаление невозможно",
            )
        reason_id, code = reason.id, reason.code
        await self._session.delete(reason)
        await self._session.flush()
        await self._audit.record(
            AuditAction.LOSS_REASON_DELETED,
            entity_type="loss_reason",
            entity_id=reason_id,
            changes={"code": {"old": code, "new": None}},
        )


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


# =============================================================================
# Лицензии/договоры вуз↔вендор↔ПО (П3, rtk_requiriments.md разд. 4, Треб.1)
#
# Только чтение здесь: единственный путь записи — импорт (`imports.service`,
# entity_type='license'), у ручки нет отдельных create/update/delete —
# карточка организации показывает уже загруженное, полноценный экран
# управления не входит в этот минимум (см. отчёт по П3).
# =============================================================================


class OrganizationLicenseService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_query(
        self, principal: Principal, *, organization_id: uuid.UUID | None = None
    ) -> Select[tuple[OrganizationLicense]]:
        """Лицензии только организаций из скоупа вызывающего. Раньше хватало права на чтение
        каталога, и любой KAM/HEAD читал договоры (с именами менеджеров) всех вузов."""
        stmt = select(OrganizationLicense).where(OrganizationLicense.deleted_at.is_(None))
        clause = await organization_scope_clause(self._session, principal)
        if clause is not None:
            stmt = stmt.where(
                OrganizationLicense.organization_id.in_(select(Organization.id).where(clause))
            )
        if organization_id is not None:
            stmt = stmt.where(OrganizationLicense.organization_id == organization_id)
        return stmt

    async def get_or_404(self, license_id: uuid.UUID, principal: Principal) -> OrganizationLicense:
        license_ = await self._session.get(OrganizationLicense, license_id)
        if license_ is None or license_.deleted_at is not None:
            raise NotFoundError("Лицензия", license_id)
        organization = await self._session.get(Organization, license_.organization_id)
        if organization is None or not await organization_in_scope(
            self._session, principal, organization
        ):
            # Как и чужая организация: 404, а не 403 — существование договора не раскрывается.
            defer_denied_audit(
                self._session,
                entity_type="organization_license",
                entity_id=license_id,
                reason="out_of_scope",
            )
            raise NotFoundError("Лицензия", license_id)
        return license_
