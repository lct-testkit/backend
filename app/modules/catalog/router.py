"""Ручки каталога: организации, контакты, продукты, справочники (раздел 6).

`GET /organizations/check-duplicate` объявлен раньше `GET /organizations/{id}`
по той же причине, что и `POST /deals/bulk/reassign` в `crm.router`: без
явного порядка литеральный путь мог бы попасть в параметризованный маршрут.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request, status

from app.core.deps import DbSession, IdempotencyKeyHeader, IfMatch, Pagination, require_permission
from app.core.idempotency import IdempotencyGuard
from app.core.pagination import Page, keyset_before
from app.core.permissions import Permission
from app.core.security import Principal
from app.modules.catalog.models import (
    Contact,
    Direction,
    Organization,
    OrganizationLicense,
    Product,
)
from app.modules.catalog.schemas import (
    ApplyDriftRequest,
    ContactCreateRequest,
    ContactListResponse,
    ContactOut,
    ContactRevealOut,
    ContactUpdateRequest,
    CustomFieldDefCreateRequest,
    CustomFieldDefListResponse,
    CustomFieldDefOut,
    CustomFieldDefUpdateRequest,
    DirectionCreateRequest,
    DirectionListResponse,
    DirectionOut,
    DirectionUpdateRequest,
    DuplicateCandidateOut,
    DuplicateCheckResponse,
    HolidayCreateRequest,
    HolidayListResponse,
    HolidayOut,
    HolidayUpdateRequest,
    LossReasonCreateRequest,
    LossReasonListResponse,
    LossReasonOut,
    LossReasonUpdateRequest,
    OrganizationCreateRequest,
    OrganizationLicenseListResponse,
    OrganizationLicenseOut,
    OrganizationListResponse,
    OrganizationOut,
    OrganizationRevealOut,
    OrganizationUpdateRequest,
    ProductCreateRequest,
    ProductListResponse,
    ProductOut,
    ProductUpdateRequest,
    RegionListResponse,
    RegionOut,
)
from app.modules.catalog.service import (
    ContactFilters,
    ContactService,
    CustomFieldDefService,
    DirectionFilters,
    DirectionService,
    HolidayService,
    LossReasonService,
    OrganizationFilters,
    OrganizationLicenseService,
    OrganizationService,
    ProductFilters,
    ProductService,
    organization_in_scope,
    region_list_query,
)

organizations_router = APIRouter(prefix="/organizations", tags=["organizations"])
contacts_router = APIRouter(prefix="/contacts", tags=["contacts"])
products_router = APIRouter(prefix="/products", tags=["products"])
directions_router = APIRouter(prefix="/directions", tags=["directions"])
loss_reasons_router = APIRouter(prefix="/loss-reasons", tags=["loss-reasons"])
holidays_router = APIRouter(prefix="/holidays", tags=["holidays"])
custom_field_defs_router = APIRouter(prefix="/custom-field-defs", tags=["custom-field-defs"])
regions_router = APIRouter(prefix="/regions", tags=["regions"])
organization_licenses_router = APIRouter(
    prefix="/organization-licenses", tags=["organization-licenses"]
)

OrgRead = Annotated[Principal, Depends(require_permission(Permission.ORG_READ))]
OrgWrite = Annotated[Principal, Depends(require_permission(Permission.ORG_WRITE))]
OrgReveal = Annotated[Principal, Depends(require_permission(Permission.ORG_REVEAL))]
ContactRead = Annotated[Principal, Depends(require_permission(Permission.CONTACT_READ))]
ContactWrite = Annotated[Principal, Depends(require_permission(Permission.CONTACT_WRITE))]
ContactReveal = Annotated[Principal, Depends(require_permission(Permission.CONTACT_REVEAL))]
CatalogRead = Annotated[Principal, Depends(require_permission(Permission.CATALOG_READ))]
CatalogWrite = Annotated[Principal, Depends(require_permission(Permission.CATALOG_WRITE))]


# =============================================================================
# Организации
# =============================================================================


@organizations_router.get("", summary="Список организаций", response_model=OrganizationListResponse)
async def list_organizations(
    session: DbSession,
    page: Pagination,
    principal: OrgRead,
    name: Annotated[str | None, Query()] = None,
    inn: Annotated[str | None, Query()] = None,
    ogrn: Annotated[str | None, Query()] = None,
    org_type: Annotated[str | None, Query()] = None,
    region_id: Annotated[uuid.UUID | None, Query()] = None,
    owner_id: Annotated[uuid.UUID | None, Query()] = None,
    registry_status: Annotated[str | None, Query()] = None,
    is_accredited: Annotated[bool | None, Query()] = None,
    source: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
) -> OrganizationListResponse:
    filters = OrganizationFilters(
        name=name,
        inn=inn,
        ogrn=ogrn,
        org_type=org_type,
        region_id=region_id,
        owner_id=owner_id,
        registry_status=registry_status,
        is_accredited=is_accredited,
        source=source,
        q=q,
    )
    stmt = (await OrganizationService(session).list_query(principal, filters)).order_by(
        Organization.created_at.desc(), Organization.id.desc()
    )
    cursor = page.decoded_cursor
    if cursor:
        stmt = stmt.where(keyset_before(Organization.created_at, Organization.id, cursor))
    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())
    built = Page.build(rows, limit=page.limit, serializer=OrganizationOut.from_model)
    return OrganizationListResponse(items=built.items, next_cursor=built.next_cursor)


@organizations_router.post(
    "",
    summary="Создать организацию",
    description=(
        "Либо ИНН (с проверкой контрольной суммы и дедупликацией), либо "
        "название и базовые реквизиты. Дубль по ИНН — 409 CRM-1302. "
        "Поддерживает Idempotency-Key."
    ),
    response_model=OrganizationOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_organization(
    payload: OrganizationCreateRequest,
    request: Request,
    session: DbSession,
    principal: OrgWrite,
    idempotency_key: IdempotencyKeyHeader,
) -> OrganizationOut:
    guard = IdempotencyGuard(session, actor_id=principal.user_id)
    body = await request.body()
    if idempotency_key:
        cached = await guard.lookup(
            key=idempotency_key, method=request.method, path=request.url.path, body=body
        )
        if cached is not None:
            return OrganizationOut.model_validate(cached.body)
        await guard.reserve(
            key=idempotency_key, method=request.method, path=request.url.path, body=body
        )

    organization = await OrganizationService(session).create(principal, payload)
    result = OrganizationOut.from_model(organization)

    if idempotency_key:
        await guard.store(
            key=idempotency_key,
            status=status.HTTP_201_CREATED,
            body=result.model_dump(mode="json"),
        )
    return result


@organizations_router.get(
    "/check-duplicate", summary="Проверить дубль по ИНН/ОГРН", response_model=DuplicateCheckResponse
)
async def check_duplicate_organization(
    session: DbSession,
    principal: OrgRead,
    inn: Annotated[str | None, Query()] = None,
    ogrn: Annotated[str | None, Query()] = None,
    name: Annotated[str | None, Query()] = None,
) -> DuplicateCheckResponse:
    exact, similar = await OrganizationService(session).check_duplicate(
        principal, inn=inn, ogrn=ogrn, name=name
    )

    def _candidate(
        org: Organization, match: Literal["inn", "similar_name"], accessible: bool
    ) -> DuplicateCandidateOut:
        # Раздел 6: организация вне скоупа — только факт существования, без
        # реквизитов. Без этого КАМ мог бы узнать точное название/ИНН
        # чужого проспекта у другой команды, подобрав ИНН в этом эндпоинте.
        return DuplicateCandidateOut(
            id=org.id,
            name=org.name if accessible else None,
            inn=org.inn if accessible else None,
            match=match,
            accessible=accessible,
        )

    candidates: list[DuplicateCandidateOut] = []
    for org in exact:
        accessible = await organization_in_scope(session, principal, org)
        candidates.append(_candidate(org, "inn", accessible))
    for org in similar:
        accessible = await organization_in_scope(session, principal, org)
        candidates.append(_candidate(org, "similar_name", accessible))
    return DuplicateCheckResponse(found=bool(candidates), candidates=candidates)


@organizations_router.get(
    "/{organization_id}", summary="Карточка организации", response_model=OrganizationOut
)
async def get_organization(
    session: DbSession, principal: OrgRead, organization_id: Annotated[uuid.UUID, Path()]
) -> OrganizationOut:
    organization = await OrganizationService(session).get_or_404(organization_id, principal)
    return OrganizationOut.from_model(organization)


@organizations_router.patch(
    "/{organization_id}", summary="Обновить организацию", response_model=OrganizationOut
)
async def update_organization(
    payload: OrganizationUpdateRequest,
    session: DbSession,
    principal: OrgWrite,
    if_match: IfMatch,
    organization_id: Annotated[uuid.UUID, Path()],
) -> OrganizationOut:
    service = OrganizationService(session)
    organization = await service.get_or_404(organization_id, principal)
    organization = await service.update(organization, payload, expected_version=if_match)
    return OrganizationOut.from_model(organization)


@organizations_router.post(
    "/{organization_id}/apply-drift",
    summary="Принять изменения реквизитов",
    description=(
        "Применяет выбранные (или все) поля из requisites_drift, найденные при сверке с ЕГРЮЛ."
    ),
    response_model=OrganizationOut,
)
async def apply_organization_drift(
    payload: ApplyDriftRequest,
    session: DbSession,
    principal: OrgWrite,
    if_match: IfMatch,
    organization_id: Annotated[uuid.UUID, Path()],
) -> OrganizationOut:
    service = OrganizationService(session)
    organization = await service.get_or_404(organization_id, principal)
    organization = await service.apply_drift(
        organization, principal, fields=payload.fields, expected_version=if_match
    )
    return OrganizationOut.from_model(organization)


@organizations_router.post(
    "/{organization_id}/reveal",
    summary="Раскрыть полные реквизиты организации",
    description=(
        "Для org_type='individual_entrepreneur' — полные телефон/email "
        "(dop.md §11.8). Для остальных типов маскировать нечего, но ручка "
        "работает единообразно. Каждый вызов пишет PII_REVEALED."
    ),
    response_model=OrganizationRevealOut,
)
async def reveal_organization(
    session: DbSession, principal: OrgReveal, organization_id: Annotated[uuid.UUID, Path()]
) -> OrganizationRevealOut:
    service = OrganizationService(session)
    organization = await service.get_or_404(organization_id, principal)
    await service.reveal(organization)
    return OrganizationRevealOut.model_validate(organization)


# =============================================================================
# Контакты
# =============================================================================


@contacts_router.get("", summary="Список контактов", response_model=ContactListResponse)
async def list_contacts(
    session: DbSession,
    page: Pagination,
    principal: ContactRead,
    organization_id: Annotated[uuid.UUID | None, Query()] = None,
    position: Annotated[str | None, Query()] = None,
    is_decision_maker: Annotated[bool | None, Query()] = None,
    is_anonymized: Annotated[bool | None, Query()] = None,
    source: Annotated[str | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
) -> ContactListResponse:
    filters = ContactFilters(
        organization_id=organization_id,
        position=position,
        is_decision_maker=is_decision_maker,
        is_anonymized=is_anonymized,
        source=source,
        q=q,
    )
    stmt = (await ContactService(session).list_query(principal, filters)).order_by(
        Contact.created_at.desc(), Contact.id.desc()
    )
    cursor = page.decoded_cursor
    if cursor:
        stmt = stmt.where(keyset_before(Contact.created_at, Contact.id, cursor))
    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())
    built = Page.build(rows, limit=page.limit, serializer=ContactOut.from_model)
    return ContactListResponse(items=built.items, next_cursor=built.next_cursor)


@contacts_router.post(
    "",
    summary="Создать контакт",
    response_model=ContactOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_contact(
    payload: ContactCreateRequest,
    request: Request,
    session: DbSession,
    principal: ContactWrite,
    idempotency_key: IdempotencyKeyHeader,
) -> ContactOut:
    guard = IdempotencyGuard(session, actor_id=principal.user_id)
    body = await request.body()
    if idempotency_key:
        cached = await guard.lookup(
            key=idempotency_key, method=request.method, path=request.url.path, body=body
        )
        if cached is not None:
            return ContactOut.model_validate(cached.body)
        await guard.reserve(
            key=idempotency_key, method=request.method, path=request.url.path, body=body
        )

    contact = await ContactService(session).create(principal, payload)
    result = ContactOut.from_model(contact)

    if idempotency_key:
        await guard.store(
            key=idempotency_key, status=status.HTTP_201_CREATED, body=result.model_dump(mode="json")
        )
    return result


@contacts_router.get("/{contact_id}", summary="Карточка контакта", response_model=ContactOut)
async def get_contact(
    session: DbSession, principal: ContactRead, contact_id: Annotated[uuid.UUID, Path()]
) -> ContactOut:
    contact = await ContactService(session).get_or_404(contact_id, principal)
    return ContactOut.from_model(contact)


@contacts_router.patch("/{contact_id}", summary="Обновить контакт", response_model=ContactOut)
async def update_contact(
    payload: ContactUpdateRequest,
    session: DbSession,
    principal: ContactWrite,
    if_match: IfMatch,
    contact_id: Annotated[uuid.UUID, Path()],
) -> ContactOut:
    service = ContactService(session)
    contact = await service.get_or_404(contact_id, principal)
    contact = await service.update(contact, payload, principal=principal, expected_version=if_match)
    return ContactOut.from_model(contact)


@contacts_router.post(
    "/{contact_id}/reveal",
    summary="Раскрыть полные контактные данные",
    description="Каждый вызов пишет отдельное событие аудита PII_REVEALED (раздел 6.6).",
    response_model=ContactRevealOut,
)
async def reveal_contact(
    session: DbSession, principal: ContactReveal, contact_id: Annotated[uuid.UUID, Path()]
) -> ContactRevealOut:
    service = ContactService(session)
    contact = await service.get_or_404(contact_id, principal)
    channels = await service.reveal(contact, principal)
    return ContactRevealOut(
        id=contact.id,
        organization_id=contact.organization_id,
        first_name=contact.first_name,
        last_name=contact.last_name,
        middle_name=contact.middle_name,
        position=contact.position,
        email=contact.email,
        phone=contact.phone,
        is_decision_maker=contact.is_decision_maker,
        channels=channels,
    )


# =============================================================================
# Продукты
# =============================================================================


@products_router.get("", summary="Справочник продуктов", response_model=ProductListResponse)
async def list_products(
    session: DbSession,
    page: Pagination,
    principal: CatalogRead,
    direction_id: Annotated[uuid.UUID | None, Query()] = None,
    code: Annotated[str | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
    product_format: Annotated[str | None, Query(alias="format")] = None,
    q: Annotated[str | None, Query()] = None,
) -> ProductListResponse:
    filters = ProductFilters(
        direction_id=direction_id, code=code, is_active=is_active, format=product_format, q=q
    )
    stmt = (
        ProductService(session)
        .list_query(filters)
        .order_by(Product.created_at.desc(), Product.id.desc())
    )
    cursor = page.decoded_cursor
    if cursor:
        stmt = stmt.where(keyset_before(Product.created_at, Product.id, cursor))
    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())
    built = Page.build(rows, limit=page.limit, serializer=ProductOut.model_validate)
    return ProductListResponse(items=built.items, next_cursor=built.next_cursor)


@products_router.post(
    "",
    summary="Создать продукт",
    description="`valid_from` не позже `valid_to`, иначе 422. Роль: запись каталога.",
    response_model=ProductOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_product(
    payload: ProductCreateRequest, session: DbSession, principal: CatalogWrite
) -> ProductOut:
    product = await ProductService(session).create(payload)
    return ProductOut.model_validate(product)


@products_router.patch(
    "/{product_id}",
    summary="Обновить продукт",
    description=(
        "`valid_from` не позже `valid_to` (новая дата сверяется с сохранённой), "
        "иначе 422. Роль: запись каталога."
    ),
    response_model=ProductOut,
)
async def update_product(
    payload: ProductUpdateRequest,
    session: DbSession,
    principal: CatalogWrite,
    if_match: IfMatch,
    product_id: Annotated[uuid.UUID, Path()],
) -> ProductOut:
    service = ProductService(session)
    product = await service.get_or_404(product_id)
    product = await service.update(product, payload, expected_version=if_match)
    return ProductOut.model_validate(product)


# =============================================================================
# Направления
# =============================================================================


@directions_router.get(
    "",
    summary="Иерархия ИТ-направлений",
    description=(
        "Курсорная пагинация. `all=true` отдаёт весь справочник одним ответом, "
        "`limit` и `cursor` игнорируются — для построения дерева целиком. "
        "Роль: чтение каталога."
    ),
    response_model=DirectionListResponse,
)
async def list_directions(
    session: DbSession,
    page: Pagination,
    principal: CatalogRead,
    parent_id: Annotated[uuid.UUID | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    return_all: Annotated[bool, Query(alias="all")] = False,
) -> DirectionListResponse:
    filters = DirectionFilters(q=q, parent_id=parent_id)
    stmt = (
        DirectionService(session)
        .list_query(filters)
        .order_by(Direction.created_at.desc(), Direction.id.desc())
    )
    if return_all:
        everything = (await session.execute(stmt)).scalars().all()
        return DirectionListResponse(items=[DirectionOut.model_validate(d) for d in everything])
    cursor = page.decoded_cursor
    if cursor:
        stmt = stmt.where(keyset_before(Direction.created_at, Direction.id, cursor))
    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())
    built = Page.build(rows, limit=page.limit, serializer=DirectionOut.model_validate)
    return DirectionListResponse(items=built.items, next_cursor=built.next_cursor)


@directions_router.post(
    "",
    summary="Создать направление",
    description="Родитель (`parent_id`) должен существовать, иначе 404. Роль: запись каталога.",
    response_model=DirectionOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_direction(
    payload: DirectionCreateRequest, session: DbSession, principal: CatalogWrite
) -> DirectionOut:
    direction = await DirectionService(session).create(payload)
    return DirectionOut.model_validate(direction)


@directions_router.patch(
    "/{direction_id}",
    summary="Обновить направление",
    description=(
        "Новый родитель должен существовать (404) и не замыкать цикл: направление "
        "не может стать потомком самого себя или своего потомка (422). Роль: запись "
        "каталога."
    ),
    response_model=DirectionOut,
)
async def update_direction(
    payload: DirectionUpdateRequest,
    session: DbSession,
    principal: CatalogWrite,
    if_match: IfMatch,
    direction_id: Annotated[uuid.UUID, Path()],
) -> DirectionOut:
    service = DirectionService(session)
    direction = await service.get_or_404(direction_id)
    direction = await service.update(direction, payload, expected_version=if_match)
    return DirectionOut.model_validate(direction)


@directions_router.delete(
    "/{direction_id}",
    summary="Удалить направление",
    description=(
        "Только если нет дочерних направлений и ни один продукт на него не "
        "ссылается — иначе 409 CRM-1303. Роль: запись каталога."
    ),
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_direction(
    session: DbSession, principal: CatalogWrite, direction_id: Annotated[uuid.UUID, Path()]
) -> None:
    service = DirectionService(session)
    direction = await service.get_or_404(direction_id)
    await service.delete(direction)


# =============================================================================
# Причины отказа
# =============================================================================


@loss_reasons_router.get(
    "", summary="Справочник причин отказа", response_model=LossReasonListResponse
)
async def list_loss_reasons(
    session: DbSession,
    principal: CatalogRead,
    is_active: Annotated[bool | None, Query()] = None,
) -> LossReasonListResponse:
    rows = (
        (await session.execute(LossReasonService(session).list_query(is_active=is_active)))
        .scalars()
        .all()
    )
    return LossReasonListResponse(items=[LossReasonOut.model_validate(r) for r in rows])


@loss_reasons_router.post(
    "",
    summary="Создать причину отказа",
    response_model=LossReasonOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_loss_reason(
    payload: LossReasonCreateRequest, session: DbSession, principal: CatalogWrite
) -> LossReasonOut:
    reason = await LossReasonService(session).create(payload)
    return LossReasonOut.model_validate(reason)


@loss_reasons_router.patch(
    "/{loss_reason_id}", summary="Обновить причину отказа", response_model=LossReasonOut
)
async def update_loss_reason(
    payload: LossReasonUpdateRequest,
    session: DbSession,
    principal: CatalogWrite,
    if_match: IfMatch,
    loss_reason_id: Annotated[uuid.UUID, Path()],
) -> LossReasonOut:
    service = LossReasonService(session)
    reason = await service.get_or_404(loss_reason_id)
    reason = await service.update(reason, payload, expected_version=if_match)
    return LossReasonOut.model_validate(reason)


@loss_reasons_router.delete(
    "/{loss_reason_id}",
    summary="Удалить причину отказа",
    description=(
        "Только если причина не используется ни в одной сделке — иначе 409 "
        "CRM-1303. Роль: запись каталога."
    ),
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_loss_reason(
    session: DbSession, principal: CatalogWrite, loss_reason_id: Annotated[uuid.UUID, Path()]
) -> None:
    service = LossReasonService(session)
    reason = await service.get_or_404(loss_reason_id)
    await service.delete(reason)


# =============================================================================
# Производственный календарь
# =============================================================================


@holidays_router.get("", summary="Производственный календарь", response_model=HolidayListResponse)
async def list_holidays(
    session: DbSession,
    principal: CatalogRead,
    date_from: Annotated[str | None, Query()] = None,
    date_to: Annotated[str | None, Query()] = None,
) -> HolidayListResponse:
    parsed_from = dt.date.fromisoformat(date_from) if date_from else None
    parsed_to = dt.date.fromisoformat(date_to) if date_to else None
    rows = (
        (
            await session.execute(
                HolidayService(session).list_query(date_from=parsed_from, date_to=parsed_to)
            )
        )
        .scalars()
        .all()
    )
    return HolidayListResponse(items=[HolidayOut.model_validate(r) for r in rows])


@holidays_router.post(
    "",
    summary="Добавить дату в календарь",
    response_model=HolidayOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_holiday(
    payload: HolidayCreateRequest, session: DbSession, principal: CatalogWrite
) -> HolidayOut:
    holiday = await HolidayService(session).create(payload)
    return HolidayOut.model_validate(holiday)


@holidays_router.patch(
    "/{holiday_id}", summary="Изменить дату календаря", response_model=HolidayOut
)
async def update_holiday(
    payload: HolidayUpdateRequest,
    session: DbSession,
    principal: CatalogWrite,
    if_match: IfMatch,
    holiday_id: Annotated[uuid.UUID, Path()],
) -> HolidayOut:
    service = HolidayService(session)
    holiday = await service.get_or_404(holiday_id)
    holiday = await service.update(holiday, payload, expected_version=if_match)
    return HolidayOut.model_validate(holiday)


# =============================================================================
# Пользовательские поля
# =============================================================================


@custom_field_defs_router.get(
    "", summary="Определения пользовательских полей", response_model=CustomFieldDefListResponse
)
async def list_custom_field_defs(
    session: DbSession,
    principal: CatalogRead,
    entity_type: Annotated[str | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
) -> CustomFieldDefListResponse:
    rows = (
        (
            await session.execute(
                CustomFieldDefService(session).list_query(
                    entity_type=entity_type, is_active=is_active
                )
            )
        )
        .scalars()
        .all()
    )
    return CustomFieldDefListResponse(items=[CustomFieldDefOut.model_validate(r) for r in rows])


@custom_field_defs_router.post(
    "",
    summary="Создать пользовательское поле",
    response_model=CustomFieldDefOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_custom_field_def(
    payload: CustomFieldDefCreateRequest, session: DbSession, principal: CatalogWrite
) -> CustomFieldDefOut:
    field = await CustomFieldDefService(session).create(payload)
    return CustomFieldDefOut.model_validate(field)


@custom_field_defs_router.patch(
    "/{field_id}", summary="Обновить пользовательское поле", response_model=CustomFieldDefOut
)
async def update_custom_field_def(
    payload: CustomFieldDefUpdateRequest,
    session: DbSession,
    principal: CatalogWrite,
    if_match: IfMatch,
    field_id: Annotated[uuid.UUID, Path()],
) -> CustomFieldDefOut:
    service = CustomFieldDefService(session)
    field = await service.get_or_404(field_id)
    field = await service.update(field, payload, expected_version=if_match)
    return CustomFieldDefOut.model_validate(field)


# =============================================================================
# Регионы, валидация реквизитов
# =============================================================================


@regions_router.get("", summary="Справочник регионов", response_model=RegionListResponse)
async def list_regions(session: DbSession, principal: CatalogRead) -> RegionListResponse:
    rows = (await session.execute(region_list_query())).scalars().all()
    return RegionListResponse(items=[RegionOut.model_validate(r) for r in rows])


# =============================================================================
# Лицензии/договоры вуз↔вендор↔ПО (П3, rtk_requiriments.md разд. 4, Треб.1)
#
# Только чтение: загружаются через POST /api/imports (entity_type='license'),
# см. `imports.router`. Достаточно для показа на карточке организации —
# отдельного экрана управления в этом минимуме нет (см. отчёт по П3).
# =============================================================================


@organization_licenses_router.get(
    "",
    summary="Список лицензий/договоров вуз-вендор-ПО",
    description="Фильтр: organization_id. Роль: чтение каталога.",
    response_model=OrganizationLicenseListResponse,
)
async def list_organization_licenses(
    session: DbSession,
    page: Pagination,
    principal: CatalogRead,
    organization_id: Annotated[uuid.UUID | None, Query()] = None,
) -> OrganizationLicenseListResponse:
    stmt = (
        OrganizationLicenseService(session)
        .list_query(organization_id=organization_id)
        .order_by(OrganizationLicense.created_at.desc(), OrganizationLicense.id.desc())
    )
    cursor = page.decoded_cursor
    if cursor:
        stmt = stmt.where(
            keyset_before(OrganizationLicense.created_at, OrganizationLicense.id, cursor)
        )
    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())
    built = Page.build(rows, limit=page.limit, serializer=OrganizationLicenseOut.model_validate)
    return OrganizationLicenseListResponse(items=built.items, next_cursor=built.next_cursor)


@organization_licenses_router.get(
    "/{license_id}",
    summary="Карточка лицензии/договора",
    response_model=OrganizationLicenseOut,
)
async def get_organization_license(
    session: DbSession, principal: CatalogRead, license_id: Annotated[uuid.UUID, Path()]
) -> OrganizationLicenseOut:
    license_ = await OrganizationLicenseService(session).get_or_404(license_id)
    return OrganizationLicenseOut.model_validate(license_)
