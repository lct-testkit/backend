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
from app.core.errors import ForbiddenError
from app.core.idempotency import IdempotencyGuard
from app.core.masking import mask_channel_value
from app.core.pagination import Page, keyset_before
from app.core.permissions import Permission, has_permission
from app.core.security import Principal
from app.modules.audit.service import defer_denied_audit
from app.modules.catalog.access_requests import AccessRequestService
from app.modules.catalog.models import (
    Contact,
    Direction,
    Organization,
    OrganizationLicense,
    Product,
)
from app.modules.catalog.schemas import (
    AccessRequestCreateRequest,
    AccessRequestDecisionRequest,
    AccessRequestListResponse,
    AccessRequestOut,
    ApplyDriftRequest,
    BranchCreateRequest,
    BranchListResponse,
    BranchOut,
    BranchUpdateRequest,
    BulkOrganizationOwnerRequest,
    BulkOrganizationOwnerResponse,
    BulkOwnerSkipOut,
    ContactChannelListResponse,
    ContactChannelOut,
    ContactCreateRequest,
    ContactListResponse,
    ContactOut,
    ContactProductListResponse,
    ContactProductOut,
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
    LearnerProfileOut,
    LearnerProfileRevealOut,
    LearnerProfileUpdateRequest,
    LicenseCreateRequest,
    LicenseUpdateRequest,
    LossReasonCreateRequest,
    LossReasonListResponse,
    LossReasonOut,
    LossReasonUpdateRequest,
    OrganizationCreateRequest,
    OrganizationDeleteRequest,
    OrganizationLicenseListResponse,
    OrganizationLicenseOut,
    OrganizationListResponse,
    OrganizationOut,
    OrganizationOwnerRemoveRequest,
    OrganizationOwnerSetRequest,
    OrganizationRevealOut,
    OrganizationUpdateRequest,
    ProductContactLinkRequest,
    ProductContactListResponse,
    ProductContactOut,
    ProductCreateRequest,
    ProductKindLiteral,
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
    LearnerProfileService,
    LicenseFilters,
    LossReasonService,
    OrganizationBranchService,
    OrganizationFilters,
    OrganizationLicenseService,
    OrganizationService,
    ProductContactService,
    ProductFilters,
    ProductService,
    organization_in_scope,
    region_list_query,
)
from app.modules.identity.schemas import OperationResult

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
# B4-12: запрос доступа к чужой записи — отдельный маршрут, решение проверяется объектно
# (владелец вуза/его руководитель/ADMIN), не правом роли.
access_requests_router = APIRouter(prefix="/access-requests", tags=["access-requests"])

OrgRead = Annotated[Principal, Depends(require_permission(Permission.ORG_READ))]
OrgWrite = Annotated[Principal, Depends(require_permission(Permission.ORG_WRITE))]
OrgReveal = Annotated[Principal, Depends(require_permission(Permission.ORG_REVEAL))]
OrgRestore = Annotated[Principal, Depends(require_permission(Permission.ORG_RESTORE))]
OrgOwnerAssign = Annotated[Principal, Depends(require_permission(Permission.ORG_OWNER_ASSIGN))]
OrgDelete = Annotated[Principal, Depends(require_permission(Permission.ORG_DELETE))]
ContactRead = Annotated[Principal, Depends(require_permission(Permission.CONTACT_READ))]
ContactWrite = Annotated[Principal, Depends(require_permission(Permission.CONTACT_WRITE))]
ContactReveal = Annotated[Principal, Depends(require_permission(Permission.CONTACT_REVEAL))]
CatalogRead = Annotated[Principal, Depends(require_permission(Permission.CATALOG_READ))]
CatalogWrite = Annotated[Principal, Depends(require_permission(Permission.CATALOG_WRITE))]
LicenseWrite = Annotated[Principal, Depends(require_permission(Permission.LICENSE_WRITE))]
LicenseDelete = Annotated[Principal, Depends(require_permission(Permission.LICENSE_DELETE))]


# =============================================================================
# Организации
# =============================================================================


@organizations_router.get(
    "",
    summary="Список организаций",
    description=(
        "Фильтры: `name`, `inn`, `ogrn`, `org_type`, `region_id`, `owner_id`, "
        "`registry_status`, `is_accredited`, `source`, `q` (поиск по названию, короткому "
        "названию или ИНН). Роль: KAM, HEAD, ADMIN."
    ),
    response_model=OrganizationListResponse,
)
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
    deleted: Annotated[bool, Query()] = False,
) -> OrganizationListResponse:
    # B4-3: список мягко удалённых вузов — иначе восстановить что-то нельзя, id
    # удалённой организации больше нигде не виден. Отдельное право: `ORG_RESTORE`,
    # не `ORG_DELETE` (само удаление организации — право, которого нет даже у HEAD).
    if deleted and not has_permission(principal.role, Permission.ORG_RESTORE):
        defer_denied_audit(session, entity_type="organization", reason="deleted_list_forbidden")
        raise ForbiddenError("Список удалённых организаций доступен только с правом восстановления")
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
        deleted=deleted,
    )
    stmt = (await OrganizationService(session).list_query(principal, filters)).order_by(
        Organization.created_at.desc(), Organization.id.desc()
    )
    cursor = page.decoded_cursor
    if cursor:
        stmt = stmt.where(keyset_before(Organization.created_at, Organization.id, cursor))
    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())
    built: Page = Page.build(rows, limit=page.limit, serializer=OrganizationOut.from_model)
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
    # B4-1: KAM заводит вуз только на себя — чужого `owner_id` без права
    # `ORG_OWNER_ASSIGN` принять нельзя (раньше проходило молча, `service.py:463`).
    if (
        payload.owner_id is not None
        and payload.owner_id != principal.user_id
        and not has_permission(principal.role, Permission.ORG_OWNER_ASSIGN)
    ):
        defer_denied_audit(session, entity_type="organization", reason="owner_assign_forbidden")
        raise ForbiddenError("Назначить ответственным другого сотрудника может только руководитель")

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


@organizations_router.post(
    "/bulk/owner",
    summary="Массовая смена ответственного за вуз",
    description=(
        "B4-2: передать все вузы одного менеджера другому (или снять ответственного) "
        "одним действием, до 100 вузов за вызов. Объявлена раньше /{organization_id}, "
        "иначе 'bulk' попал бы в параметризованный маршрут."
    ),
    response_model=BulkOrganizationOwnerResponse,
)
async def bulk_set_organization_owner(
    payload: BulkOrganizationOwnerRequest,
    session: DbSession,
    principal: OrgOwnerAssign,
) -> BulkOrganizationOwnerResponse:
    result = await OrganizationService(session).bulk_set_owner(
        principal,
        organization_ids=payload.organization_ids,
        from_owner_id=payload.from_owner_id,
        owner_id=payload.owner_id,
        reason=payload.reason,
        carry_open_deals=payload.carry_open_deals,
    )
    return BulkOrganizationOwnerResponse(
        batch_id=result.batch_id,
        changed=len(result.changed),
        skipped=[BulkOwnerSkipOut(**item) for item in result.skipped],
        deals_reassigned=result.deals_reassigned,
        remaining=result.remaining,
    )


@organizations_router.get(
    "/check-duplicate",
    summary="Проверить дубль по ИНН/ОГРН",
    description=(
        "Точные совпадения по `inn`/`ogrn` и похожие по названию (`name`). Организация вне "
        "скоупа вызывающего — только факт существования без реквизитов (`accessible=false`), "
        "иначе КАМ мог бы подобрать ИНН и узнать название чужого вуза. Роль: KAM, HEAD, ADMIN."
    ),
    response_model=DuplicateCheckResponse,
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
    "/{organization_id}",
    summary="Карточка организации",
    description="Вне скоупа вызывающего или не существует — 404 CRM-9004. Роль: KAM, HEAD, ADMIN.",
    response_model=OrganizationOut,
)
async def get_organization(
    session: DbSession, principal: OrgRead, organization_id: Annotated[uuid.UUID, Path()]
) -> OrganizationOut:
    organization = await OrganizationService(session).get_or_404(organization_id, principal)
    return OrganizationOut.from_model(organization)


@organizations_router.patch(
    "/{organization_id}",
    summary="Обновить организацию",
    description=(
        "Правка реквизитов и атрибутов организации. Обязателен `If-Match` с текущей "
        "версией — расхождение даёт 409 CRM-1002; вне скоупа или не существует — 404 "
        "CRM-9004. Роль: KAM, HEAD, ADMIN."
    ),
    response_model=OrganizationOut,
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
    version = if_match

    # B4-1: `owner_id` больше не правится обычным PATCH — только через `set_owner`
    # (убран из `_PATCHABLE_FIELDS`), и только с правом `ORG_OWNER_ASSIGN`. KAM,
    # шлющий поле без изменений (та же форма, что раньше отправляла весь объект),
    # не должен получать 403 — только попытка реальной смены.
    if "owner_id" in payload.model_fields_set and payload.owner_id != organization.owner_id:
        if not has_permission(principal.role, Permission.ORG_OWNER_ASSIGN):
            defer_denied_audit(
                session,
                entity_type="organization",
                entity_id=organization_id,
                reason="owner_assign_forbidden",
            )
            raise ForbiddenError(
                "Сменить ответственного за вуз может только руководитель или администратор"
            )
        organization = await service.set_owner(
            organization,
            principal,
            owner_id=payload.owner_id,
            reason="Изменено в карточке организации",
            expected_version=version,
        )
        version = organization.version

    organization = await service.update(organization, payload, expected_version=version)
    return OrganizationOut.from_model(organization)


@organizations_router.put(
    "/{organization_id}/owner",
    summary="Назначить или сменить ответственного за вуз",
    description=(
        "B4-1: ТЗ Функц.11 — Руководитель меняет ответственных за вузы. "
        "Роль: HEAD (только в своей команде), ADMIN."
    ),
    response_model=OrganizationOut,
)
async def set_organization_owner(
    payload: OrganizationOwnerSetRequest,
    session: DbSession,
    principal: OrgOwnerAssign,
    if_match: IfMatch,
    organization_id: Annotated[uuid.UUID, Path()],
) -> OrganizationOut:
    service = OrganizationService(session)
    organization = await service.get_or_404(organization_id, principal)
    organization = await service.set_owner(
        organization,
        principal,
        owner_id=payload.owner_id,
        reason=payload.reason,
        expected_version=if_match,
    )
    return OrganizationOut.from_model(organization)


@organizations_router.delete(
    "/{organization_id}/owner",
    summary="Снять ответственного за вуз",
    description="B4-2: вуз становится «ничьим». Роль: HEAD (в своей команде), ADMIN.",
    response_model=OrganizationOut,
)
async def remove_organization_owner(
    payload: OrganizationOwnerRemoveRequest,
    session: DbSession,
    principal: OrgOwnerAssign,
    if_match: IfMatch,
    organization_id: Annotated[uuid.UUID, Path()],
) -> OrganizationOut:
    service = OrganizationService(session)
    organization = await service.get_or_404(organization_id, principal)
    organization = await service.set_owner(
        organization,
        principal,
        owner_id=None,
        reason=payload.reason,
        expected_version=if_match,
    )
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
    "/{organization_id}/dismiss-drift",
    summary="Отклонить расхождения реквизитов («Оставить как есть»)",
    description=(
        "B4-11 (A-22): отмечает выбранные (или все) поля из requisites_drift как осознанно "
        "оставленные — баннер не возвращается, пока в реестре не появится ДРУГОЕ значение. "
        "Ликвидацию (registry_status=liquidating/liquidated) отклонить нельзя — 422."
    ),
    response_model=OrganizationOut,
)
async def dismiss_organization_drift(
    payload: ApplyDriftRequest,
    session: DbSession,
    principal: OrgWrite,
    if_match: IfMatch,
    organization_id: Annotated[uuid.UUID, Path()],
) -> OrganizationOut:
    service = OrganizationService(session)
    organization = await service.get_or_404(organization_id, principal)
    organization = await service.dismiss_drift(
        organization, fields=payload.fields, expected_version=if_match
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


@organizations_router.delete(
    "/{organization_id}",
    summary="Удалить организацию",
    description=(
        "B4-3 (A-16): мягкое удаление. 409 CRM-1303 со списком `blockers`, если есть "
        "живые (не мягко удалённые) сделки, контакты или лицензии. Роль: ADMIN."
    ),
    response_model=OperationResult,
)
async def delete_organization(
    payload: OrganizationDeleteRequest,
    session: DbSession,
    principal: OrgDelete,
    if_match: IfMatch,
    organization_id: Annotated[uuid.UUID, Path()],
) -> OperationResult:
    service = OrganizationService(session)
    organization = await service.get_or_404(organization_id, principal)
    await service.delete(organization, reason=payload.reason, expected_version=if_match)
    return OperationResult(ok=True, detail="Организация удалена")


@organizations_router.post(
    "/{organization_id}/restore",
    summary="Восстановить удалённую организацию",
    description=(
        "Возвращает мягко удалённую организацию в активное состояние (A-18). "
        "Активную организацию — 409 CRM-1304. Роль: ADMIN, HEAD."
    ),
    response_model=OrganizationOut,
)
async def restore_organization(
    session: DbSession, principal: OrgRestore, organization_id: Annotated[uuid.UUID, Path()]
) -> OrganizationOut:
    service = OrganizationService(session)
    organization = await service.get_deleted_or_404(organization_id, principal)
    organization = await service.restore(organization)
    return OrganizationOut.from_model(organization)


# --- B4-10 (A-19): филиалы и институты внутри вуза --------------------------


@organizations_router.get(
    "/{organization_id}/branches",
    summary="Филиалы и институты вуза",
    description=(
        "Дерево собирает клиент по parent_id. Скоуп — как у карточки вуза (404, если вуз "
        "вне скоупа или не существует). Роль: organization:read."
    ),
    response_model=BranchListResponse,
)
async def list_organization_branches(
    session: DbSession, principal: OrgRead, organization_id: Annotated[uuid.UUID, Path()]
) -> BranchListResponse:
    organization = await OrganizationService(session).get_or_404(organization_id, principal)
    items = await OrganizationBranchService(session).list(organization)
    return BranchListResponse(items=[BranchOut.model_validate(b) for b in items])


@organizations_router.post(
    "/{organization_id}/branches",
    summary="Завести филиал/институт",
    description=(
        "`parent_id` — другой филиал ЭТОГО ЖЕ вуза, иначе 422; цикл в иерархии — тоже 422. "
        "Лимит 200 филиалов на вуз. Роль: organization:write."
    ),
    response_model=BranchOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_organization_branch(
    payload: BranchCreateRequest,
    session: DbSession,
    principal: OrgWrite,
    organization_id: Annotated[uuid.UUID, Path()],
) -> BranchOut:
    organization = await OrganizationService(session).get_or_404(organization_id, principal)
    branch = await OrganizationBranchService(session).create(organization, payload)
    return BranchOut.model_validate(branch)


@organizations_router.patch(
    "/{organization_id}/branches/{branch_id}",
    summary="Изменить филиал/институт",
    description="Поля, которые не присланы, не трогаются. Роль: organization:write.",
    response_model=BranchOut,
)
async def update_organization_branch(
    payload: BranchUpdateRequest,
    session: DbSession,
    principal: OrgWrite,
    organization_id: Annotated[uuid.UUID, Path()],
    branch_id: Annotated[uuid.UUID, Path()],
) -> BranchOut:
    organization = await OrganizationService(session).get_or_404(organization_id, principal)
    service = OrganizationBranchService(session)
    branch = await service.get_or_404(organization, branch_id)
    branch = await service.update(organization, branch, payload)
    return BranchOut.model_validate(branch)


@organizations_router.delete(
    "/{organization_id}/branches/{branch_id}",
    summary="Удалить филиал/институт",
    description=(
        "Мягкое удаление. Есть дочерние филиалы — 409 CRM-1303, удалите их первыми. "
        "Роль: organization:write."
    ),
    response_model=OperationResult,
)
async def delete_organization_branch(
    session: DbSession,
    principal: OrgWrite,
    organization_id: Annotated[uuid.UUID, Path()],
    branch_id: Annotated[uuid.UUID, Path()],
) -> OperationResult:
    organization = await OrganizationService(session).get_or_404(organization_id, principal)
    service = OrganizationBranchService(session)
    branch = await service.get_or_404(organization, branch_id)
    await service.delete(branch)
    return OperationResult(ok=True, detail="Филиал удалён")


# =============================================================================
# Контакты
# =============================================================================


@contacts_router.get(
    "",
    summary="Список контактов",
    description=(
        "Фильтры: `organization_id`, `position`, `is_decision_maker`, `is_anonymized`, "
        "`source`, `q`. Телефон и email маскированы — полные значения через "
        "`POST /api/contacts/{id}/reveal`. Роль: KAM, HEAD, ADMIN."
    ),
    response_model=ContactListResponse,
)
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
    built: Page = Page.build(rows, limit=page.limit, serializer=ContactOut.from_model)
    return ContactListResponse(items=built.items, next_cursor=built.next_cursor)


@contacts_router.post(
    "",
    summary="Создать контакт",
    description="ФИО, должность, телефон/email и признак ЛПР. Поддерживает Idempotency-Key.",
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


@contacts_router.get(
    "/{contact_id}",
    summary="Карточка контакта",
    description=(
        "Телефон и email маскированы — полные значения через "
        "`POST /api/contacts/{id}/reveal`; не существует — 404 CRM-9004. "
        "Роль: KAM, HEAD, ADMIN."
    ),
    response_model=ContactOut,
)
async def get_contact(
    session: DbSession, principal: ContactRead, contact_id: Annotated[uuid.UUID, Path()]
) -> ContactOut:
    contact = await ContactService(session).get_or_404(contact_id, principal)
    return ContactOut.from_model(contact)


@contacts_router.patch(
    "/{contact_id}",
    summary="Обновить контакт",
    description=(
        "Обязателен `If-Match` с текущей версией — расхождение даёт 409 CRM-1002; "
        "не существует — 404 CRM-9004. Роль: KAM, HEAD, ADMIN."
    ),
    response_model=ContactOut,
)
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


@contacts_router.get(
    "/{contact_id}/channels",
    summary="Каналы связи контакта (маскированные)",
    description=(
        "Telegram/WhatsApp/доп. телефон/доп. email контакта с маскированными значениями "
        "(B4-9). Полные значения — только `POST /contacts/{id}/reveal`. Скоуп — как у "
        "карточки контакта. Роль: чтение контактов."
    ),
    response_model=ContactChannelListResponse,
)
async def list_contact_channels(
    session: DbSession, principal: ContactRead, contact_id: Annotated[uuid.UUID, Path()]
) -> ContactChannelListResponse:
    service = ContactService(session)
    contact = await service.get_or_404(contact_id, principal)
    channels = await service.channels(contact.id)
    return ContactChannelListResponse(
        items=[
            ContactChannelOut(
                id=row.id,
                type=row.type,
                value=mask_channel_value(row.type, row.value) or "",
                is_primary=row.is_primary,
                is_verified=row.is_verified,
            )
            for row in channels
        ]
    )


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
        contact_methods=list(contact.contact_methods or []),
        is_decision_maker=contact.is_decision_maker,
        channels=channels,
    )


@contacts_router.get(
    "/{contact_id}/products",
    summary="Продукты, за которые отвечает контакт",
    description=(
        "Каталог «Вендоры»: связи контакт — продукт. Доступ — как к карточке контакта "
        "(чужой контакт — 404). Удалённые продукты не показываются."
    ),
    response_model=ContactProductListResponse,
)
async def list_contact_products(
    session: DbSession, principal: ContactRead, contact_id: Annotated[uuid.UUID, Path()]
) -> ContactProductListResponse:
    contact = await ContactService(session).get_or_404(contact_id, principal)
    rows = await ProductContactService(session).products_of_contact(contact)
    names = await ProductService(session).vendor_names(product for product, _role in rows)
    return ContactProductListResponse(
        items=[
            ContactProductOut(
                product=ProductOut.from_model(
                    product,
                    names.get(product.vendor_id) if product.vendor_id is not None else None,
                ),
                role=role,
            )
            for product, role in rows
        ]
    )


# --- Профиль учащегося (ПДн шаблона LMS) ---------------------------------------------------


@contacts_router.get(
    "/{contact_id}/learner-profile",
    summary="Профиль учащегося (маскированный)",
    description=(
        "СНИЛС, паспорт, адрес регистрации и реквизиты диплома — `***` и три последних знака "
        "(короткие значения закрыты целиком), даты — только год. Профиля нет — все поля `null`. "
        "Полные значения — через `POST .../learner-profile/reveal`."
    ),
    response_model=LearnerProfileOut,
)
async def get_learner_profile(
    session: DbSession, principal: ContactRead, contact_id: Annotated[uuid.UUID, Path()]
) -> LearnerProfileOut:
    contact = await ContactService(session).get_or_404(contact_id, principal)
    return LearnerProfileOut.from_model(await LearnerProfileService(session).get(contact))


@contacts_router.post(
    "/{contact_id}/learner-profile/reveal",
    summary="Раскрыть полный профиль учащегося",
    description=(
        "Каждый вызов пишет событие аудита PII_REVEALED (категория данных, без значений). "
        "Профиля нет — все поля `null`."
    ),
    response_model=LearnerProfileRevealOut,
)
async def reveal_learner_profile(
    session: DbSession, principal: ContactReveal, contact_id: Annotated[uuid.UUID, Path()]
) -> LearnerProfileRevealOut:
    contact = await ContactService(session).get_or_404(contact_id, principal)
    return LearnerProfileRevealOut.from_model(await LearnerProfileService(session).reveal(contact))


@contacts_router.put(
    "/{contact_id}/learner-profile",
    summary="Обновить профиль учащегося",
    description=(
        "Частичное обновление: меняются только переданные поля, пустая строка очищает поле. "
        "Значения проверяются так же, как при импорте шаблона LMS; ошибки — 422 с перечнем "
        "полей. В аудит попадают имена изменённых полей, но не значения. Ответ маскирован."
    ),
    response_model=LearnerProfileOut,
)
async def update_learner_profile(
    payload: LearnerProfileUpdateRequest,
    session: DbSession,
    principal: ContactWrite,
    contact_id: Annotated[uuid.UUID, Path()],
) -> LearnerProfileOut:
    contact = await ContactService(session).get_or_404(contact_id, principal)
    profile = await LearnerProfileService(session).update(
        contact, payload.model_dump(exclude_unset=True)
    )
    return LearnerProfileOut.from_model(profile)


# =============================================================================
# Продукты
# =============================================================================


@products_router.get(
    "",
    summary="Справочник продуктов",
    description=(
        "Фильтр `kind` различает ИТ-программу/курс (`course`, по умолчанию у старых записей) "
        "и ИТ-продукт — ПО вендора (`software`, B3-7). Роль: чтение каталога."
    ),
    response_model=ProductListResponse,
)
async def list_products(
    session: DbSession,
    page: Pagination,
    principal: CatalogRead,
    direction_id: Annotated[uuid.UUID | None, Query()] = None,
    vendor_id: Annotated[uuid.UUID | None, Query()] = None,
    code: Annotated[str | None, Query()] = None,
    is_active: Annotated[bool | None, Query()] = None,
    product_format: Annotated[str | None, Query(alias="format")] = None,
    kind: Annotated[ProductKindLiteral | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
) -> ProductListResponse:
    filters = ProductFilters(
        direction_id=direction_id,
        vendor_id=vendor_id,
        code=code,
        is_active=is_active,
        format=product_format,
        kind=kind,
        q=q,
    )
    service = ProductService(session)
    stmt = service.list_query(filters).order_by(Product.created_at.desc(), Product.id.desc())
    cursor = page.decoded_cursor
    if cursor:
        stmt = stmt.where(keyset_before(Product.created_at, Product.id, cursor))
    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())
    names = await service.vendor_names(rows)
    built: Page = Page.build(
        rows,
        limit=page.limit,
        serializer=lambda product: ProductOut.from_model(product, names.get(product.vendor_id)),
    )
    return ProductListResponse(items=built.items, next_cursor=built.next_cursor)


@products_router.post(
    "",
    summary="Создать продукт",
    description=(
        "`valid_from` не позже `valid_to`, иначе 422. `vendor_id` — организация-вендор "
        "(несуществующая или удалённая — 404). Код, занятый удалённым продуктом, — 409 CRM-1301. "
        "Роль: запись каталога."
    ),
    response_model=ProductOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_product(
    payload: ProductCreateRequest, session: DbSession, principal: CatalogWrite
) -> ProductOut:
    service = ProductService(session)
    product = await service.create(payload)
    names = await service.vendor_names([product])
    vendor_name = names.get(product.vendor_id) if product.vendor_id is not None else None
    return ProductOut.from_model(product, vendor_name)


@products_router.patch(
    "/{product_id}",
    summary="Обновить продукт",
    description=(
        "`valid_from` не позже `valid_to` (новая дата сверяется с сохранённой), "
        "иначе 422. `vendor_id: null` снимает вендора. Роль: запись каталога."
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
    names = await service.vendor_names([product])
    vendor_name = names.get(product.vendor_id) if product.vendor_id is not None else None
    return ProductOut.from_model(product, vendor_name)


@products_router.delete(
    "/{product_id}",
    summary="Удалить продукт",
    description=(
        "Только если продукт не указан ни в одной сделке — иначе 409 CRM-1303. "
        "Роль: запись каталога."
    ),
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_product(
    session: DbSession, principal: CatalogWrite, product_id: Annotated[uuid.UUID, Path()]
) -> None:
    service = ProductService(session)
    product = await service.get_or_404(product_id)
    await service.delete(product)


# --- Ответственные за продукт: связь контакт — продукт -------------------------------------


@products_router.get(
    "/{product_id}/contacts",
    summary="Ответственные за продукт",
    description=(
        "Каталог «Вендоры»: люди, к которым идут по вопросам продукта. Показываются только "
        "контакты, видимые вызывающему по скоупу; телефон и email маскированы. "
        "Роль: чтение каталога."
    ),
    response_model=ProductContactListResponse,
)
async def list_product_contacts(
    session: DbSession, principal: CatalogRead, product_id: Annotated[uuid.UUID, Path()]
) -> ProductContactListResponse:
    product = await ProductService(session).get_or_404(product_id)
    rows = await ProductContactService(session).contacts_of_product(product, principal)
    return ProductContactListResponse(
        items=[
            ProductContactOut(contact=ContactOut.from_model(contact), role=role)
            for contact, role in rows
        ]
    )


@products_router.put(
    "/{product_id}/contacts/{contact_id}",
    summary="Назначить ответственного за продукт",
    description=(
        "Создаёт связь контакт — продукт или меняет роль у существующей (повтор идемпотентен). "
        "Пишет событие аудита CONTACT_PRODUCT_LINKED. Роль: запись каталога."
    ),
    response_model=ProductContactOut,
)
async def link_product_contact(
    payload: ProductContactLinkRequest,
    session: DbSession,
    principal: CatalogWrite,
    product_id: Annotated[uuid.UUID, Path()],
    contact_id: Annotated[uuid.UUID, Path()],
) -> ProductContactOut:
    product = await ProductService(session).get_or_404(product_id)
    contact = await ContactService(session).get_or_404(contact_id, principal)
    await ProductContactService(session).link(product, contact, payload.role)
    return ProductContactOut(contact=ContactOut.from_model(contact), role=payload.role)


@products_router.delete(
    "/{product_id}/contacts/{contact_id}",
    summary="Снять ответственного с продукта",
    description=(
        "Связи нет — 404. Пишет событие аудита CONTACT_PRODUCT_UNLINKED. Роль: запись каталога."
    ),
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unlink_product_contact(
    session: DbSession,
    principal: CatalogWrite,
    product_id: Annotated[uuid.UUID, Path()],
    contact_id: Annotated[uuid.UUID, Path()],
) -> None:
    product = await ProductService(session).get_or_404(product_id)
    contact = await ContactService(session).get_or_404(contact_id, principal)
    await ProductContactService(session).unlink(product, contact)


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
    built: Page = Page.build(rows, limit=page.limit, serializer=DirectionOut.model_validate)
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
    "",
    summary="Справочник причин отказа",
    description=(
        "Причины для перехода сделки в lost (код, название, активность). Фильтр "
        "`is_active`. Роль: KAM, HEAD, ADMIN."
    ),
    response_model=LossReasonListResponse,
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
    description="Код, название, категория и порядок сортировки новой причины отказа. Роль: ADMIN.",
    response_model=LossReasonOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_loss_reason(
    payload: LossReasonCreateRequest, session: DbSession, principal: CatalogWrite
) -> LossReasonOut:
    reason = await LossReasonService(session).create(payload)
    return LossReasonOut.model_validate(reason)


@loss_reasons_router.patch(
    "/{loss_reason_id}",
    summary="Обновить причину отказа",
    description=(
        "Правка названия, категории, активности или порядка сортировки; обязателен "
        "`If-Match` с текущей версией — расхождение даёт 409 CRM-1002. Роль: ADMIN."
    ),
    response_model=LossReasonOut,
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


@holidays_router.get(
    "",
    summary="Производственный календарь",
    description=(
        "Выходные и перенесённые рабочие дни (`is_working_day`) — основа расчёта SLA в "
        "рабочих днях. Фильтр по периоду `date_from`/`date_to`. Роль: KAM, HEAD, ADMIN."
    ),
    response_model=HolidayListResponse,
)
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
    description="Дата, название и признак рабочего дня (перенос выходного). Роль: ADMIN.",
    response_model=HolidayOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_holiday(
    payload: HolidayCreateRequest, session: DbSession, principal: CatalogWrite
) -> HolidayOut:
    holiday = await HolidayService(session).create(payload)
    return HolidayOut.model_validate(holiday)


@holidays_router.patch(
    "/{holiday_id}",
    summary="Изменить дату календаря",
    description=(
        "Правка даты, названия или признака рабочего дня; обязателен `If-Match` с текущей "
        "версией — расхождение даёт 409 CRM-1002. Роль: ADMIN."
    ),
    response_model=HolidayOut,
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


@holidays_router.delete(
    "/{holiday_id}",
    summary="Удалить дату календаря",
    description=(
        "Ничего на дату календаря не ссылается — удаление без ограничений. "
        "Роль: запись каталога."
    ),
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_holiday(
    session: DbSession, principal: CatalogWrite, holiday_id: Annotated[uuid.UUID, Path()]
) -> None:
    service = HolidayService(session)
    holiday = await service.get_or_404(holiday_id)
    await service.delete(holiday)


# =============================================================================
# Пользовательские поля
# =============================================================================


@custom_field_defs_router.get(
    "",
    summary="Определения пользовательских полей",
    description=(
        "Каталог определений `custom_fields` (тип, обязательность, правила проверки "
        "значения) для сделок, организаций и продуктов. Фильтры: `entity_type`, "
        "`is_active`. Роль: KAM, HEAD, ADMIN."
    ),
    response_model=CustomFieldDefListResponse,
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
    description=(
        "Определение поля задаёт и проверку значений в `custom_fields` сделок, организаций и "
        "продуктов (при создании и PATCH — 422 с `errors[].field = custom_fields.<код>`). "
        "`select`/`multiselect`: варианты в `options.choices`; `number`: `validation.min` и "
        "`validation.max`; `string`: `validation.max_length` и `validation.pattern` (regexp, "
        "поиск по значению); `date` — `ГГГГ-ММ-ДД`; `bool` — true/false. Значения ключей без "
        "определения (и выключенных полей) сохраняются без проверки; `null` сбрасывает значение. "
        "`is_required` проверяется при создании записи (кроме `bool` и служебной учётки "
        "интеграции), а при PATCH — только для присланного ключа. "
        "Роль: `catalog:write`."
    ),
    response_model=CustomFieldDefOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_custom_field_def(
    payload: CustomFieldDefCreateRequest, session: DbSession, principal: CatalogWrite
) -> CustomFieldDefOut:
    field = await CustomFieldDefService(session).create(payload)
    return CustomFieldDefOut.model_validate(field)


@custom_field_defs_router.patch(
    "/{field_id}",
    summary="Обновить пользовательское поле",
    description=(
        "Правка названия, правил проверки или активности определения; не меняет уже "
        "сохранённые значения `custom_fields`. Обязателен `If-Match` с текущей версией — "
        "расхождение даёт 409 CRM-1002. Роль: ADMIN."
    ),
    response_model=CustomFieldDefOut,
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


@custom_field_defs_router.delete(
    "/{field_id}",
    summary="Удалить пользовательское поле",
    description=(
        "Только если ни у одной сущности этого типа нет непустого значения по коду поля — "
        "иначе 409 CRM-1303. Роль: запись каталога."
    ),
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_custom_field_def(
    session: DbSession, principal: CatalogWrite, field_id: Annotated[uuid.UUID, Path()]
) -> None:
    service = CustomFieldDefService(session)
    field = await service.get_or_404(field_id)
    await service.delete(field)


# =============================================================================
# Регионы, валидация реквизитов
# =============================================================================


@regions_router.get(
    "",
    summary="Справочник регионов",
    description=(
        "Регионы РФ для поля организации `region_id` (код, название). Роль: KAM, HEAD, ADMIN."
    ),
    response_model=RegionListResponse,
)
async def list_regions(session: DbSession, principal: CatalogRead) -> RegionListResponse:
    rows = (await session.execute(region_list_query())).scalars().all()
    return RegionListResponse(items=[RegionOut.model_validate(r) for r in rows])


# =============================================================================
# Лицензии/договоры вуз↔вендор↔ПО (rtk_requiriments.md разд. 4, Треб.1; B3-6)
#
# До B3-6 была только читалка (загрузка — POST /api/imports,
# entity_type='license', см. `imports.router`). CRUD здесь использует тот
# же составной ключ (B3-1) и разбор значений (B3-3), что и импорт.
# =============================================================================


def _license_out(
    license_: OrganizationLicense,
    *,
    reveal: bool,
    names: dict[uuid.UUID, tuple[str, str | None]],
) -> OrganizationLicenseOut:
    name, short_name = names.get(license_.organization_id, (None, None))
    return OrganizationLicenseOut.from_model(
        license_, reveal=reveal, organization_name=name, organization_short_name=short_name
    )


@organization_licenses_router.get(
    "",
    summary="Список лицензий/договоров вуз-вендор-ПО",
    description=(
        "Фильтры: organization_id, vendor_id, product_id, transfer_status (можно несколько раз), "
        "is_signed, year (лицензия действует в этом году), signed_year, q (поиск по вузу, "
        "вендору, ПО, номеру договора). `with_total=true` добавляет точное число строк "
        "(отдельный COUNT-запрос). Только организации из скоупа вызывающего (KAM/HEAD — свои "
        "и по своим сделкам, ADMIN — все). Роль: чтение каталога."
    ),
    response_model=OrganizationLicenseListResponse,
)
async def list_organization_licenses(
    session: DbSession,
    page: Pagination,
    principal: CatalogRead,
    organization_id: Annotated[uuid.UUID | None, Query()] = None,
    vendor_id: Annotated[uuid.UUID | None, Query()] = None,
    product_id: Annotated[uuid.UUID | None, Query()] = None,
    transfer_status: Annotated[list[str] | None, Query()] = None,
    is_signed: Annotated[bool | None, Query()] = None,
    year: Annotated[int | None, Query()] = None,
    signed_year: Annotated[int | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
    with_total: Annotated[bool, Query()] = False,
) -> OrganizationLicenseListResponse:
    filters = LicenseFilters(
        organization_id=organization_id,
        vendor_id=vendor_id,
        product_id=product_id,
        transfer_statuses=tuple(transfer_status or ()),
        is_signed=is_signed,
        year=year,
        signed_year=signed_year,
        q=q,
    )
    service = OrganizationLicenseService(session)
    stmt = (await service.list_query(principal, filters)).order_by(
        OrganizationLicense.created_at.desc(), OrganizationLicense.id.desc()
    )
    cursor = page.decoded_cursor
    if cursor:
        stmt = stmt.where(
            keyset_before(OrganizationLicense.created_at, OrganizationLicense.id, cursor)
        )
    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())
    reveal = has_permission(principal.role, Permission.CONTACT_REVEAL)
    names = await service.organization_names(rows)
    built: Page = Page.build(
        rows,
        limit=page.limit,
        serializer=lambda row: _license_out(row, reveal=reveal, names=names),
    )
    total = await service.count(principal, filters) if with_total else None
    return OrganizationLicenseListResponse(
        items=built.items, next_cursor=built.next_cursor, total=total
    )


@organization_licenses_router.get(
    "/{license_id}",
    summary="Карточка лицензии/договора",
    description=(
        "ФИО менеджера и ответственные от вуза маскированы без права contact:reveal. "
        "Организация вне скоупа или запись не существует — 404 CRM-9004. "
        "Роль: KAM, HEAD, ADMIN."
    ),
    response_model=OrganizationLicenseOut,
)
async def get_organization_license(
    session: DbSession, principal: CatalogRead, license_id: Annotated[uuid.UUID, Path()]
) -> OrganizationLicenseOut:
    service = OrganizationLicenseService(session)
    license_ = await service.get_or_404(license_id, principal)
    names = await service.organization_names([license_])
    return _license_out(
        license_, reveal=has_permission(principal.role, Permission.CONTACT_REVEAL), names=names
    )


@organization_licenses_router.post(
    "",
    summary="Создать лицензию/договор",
    description=(
        "Вендор и ПО — либо ссылкой (vendor_id/product_id из каталога), либо текстом "
        "(vendor/product_name: сервис пытается найти однозначное совпадение, не находит — "
        "сохраняет как текст без ссылки). Дубль составного ключа (вуз, вендор, ПО, номер "
        "договора) — 409 CRM-1301 с license_id существующей записи. transfer_status принимает "
        "английские коды и русские синонимы (BUG-10). Поддерживает Idempotency-Key. "
        "Роль: license:write (KAM — по своим вузам)."
    ),
    response_model=OrganizationLicenseOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_organization_license(
    payload: LicenseCreateRequest,
    request: Request,
    session: DbSession,
    principal: LicenseWrite,
    idempotency_key: IdempotencyKeyHeader,
) -> OrganizationLicenseOut:
    guard = IdempotencyGuard(session, actor_id=principal.user_id)
    body = await request.body()
    if idempotency_key:
        cached = await guard.lookup(
            key=idempotency_key, method=request.method, path=request.url.path, body=body
        )
        if cached is not None:
            return OrganizationLicenseOut.model_validate(cached.body)
        await guard.reserve(
            key=idempotency_key, method=request.method, path=request.url.path, body=body
        )

    service = OrganizationLicenseService(session)
    license_ = await service.create(principal, payload)
    names = await service.organization_names([license_])
    result = _license_out(license_, reveal=True, names=names)

    if idempotency_key:
        await guard.store(
            key=idempotency_key,
            status=status.HTTP_201_CREATED,
            body=result.model_dump(mode="json"),
        )
    return result


@organization_licenses_router.patch(
    "/{license_id}",
    summary="Изменить лицензию/договор",
    description=(
        "`organization_id` не редактируется — удалить и создать заново. Смена вендора/ПО/номера "
        "договора, приводящая к дублю ключа у другой записи, — 409 CRM-1301. Роль: license:write "
        "(KAM — по своим вузам)."
    ),
    response_model=OrganizationLicenseOut,
)
async def update_organization_license(
    payload: LicenseUpdateRequest,
    session: DbSession,
    principal: LicenseWrite,
    if_match: IfMatch,
    license_id: Annotated[uuid.UUID, Path()],
) -> OrganizationLicenseOut:
    service = OrganizationLicenseService(session)
    license_ = await service.get_or_404(license_id, principal)
    license_ = await service.update(license_, payload, expected_version=if_match)
    names = await service.organization_names([license_])
    return _license_out(license_, reveal=True, names=names)


@organization_licenses_router.delete(
    "/{license_id}",
    summary="Удалить лицензию/договор",
    description="Мягкое удаление. Роль: license:delete (HEAD, ADMIN).",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_organization_license(
    session: DbSession, principal: LicenseDelete, license_id: Annotated[uuid.UUID, Path()]
) -> None:
    service = OrganizationLicenseService(session)
    license_ = await service.get_or_404(license_id, principal)
    await service.delete(license_)


# =============================================================================
# B4-12 (A-18): запрос доступа к чужой записи
# =============================================================================


@access_requests_router.get(
    "",
    summary="Мои запросы или входящие на решение",
    description=(
        "`box=mine` (по умолчанию) — запросы, которые создал вызывающий. `box=inbox` — "
        "запросы, которые он может решить (владелец вуза, руководитель его команды, ADMIN). "
        "Фильтр `status`. Курсорная пагинация. Роль: organization:read."
    ),
    response_model=AccessRequestListResponse,
)
async def list_access_requests(
    session: DbSession,
    page: Pagination,
    principal: OrgRead,
    box: Annotated[Literal["mine", "inbox"], Query()] = "mine",
    request_status: Annotated[str | None, Query(alias="status")] = None,
) -> AccessRequestListResponse:
    service = AccessRequestService(session)
    if box == "inbox":
        result = await service.list_inbox(principal, page, status=request_status)
    else:
        result = await service.list_mine(principal, page)
    return AccessRequestListResponse(
        items=[AccessRequestOut.model_validate(r) for r in result.items],
        next_cursor=result.next_cursor,
    )


@access_requests_router.post(
    "",
    summary="Запросить доступ к чужому вузу",
    description=(
        "Вуз уже существует, вне скоупа вызывающего, с ответственным и вне ограничений "
        "видимости (B4-5) — иначе 422. Второй запрос к тому же вузу в статусе pending — 409. "
        "Роль: organization:read."
    ),
    response_model=AccessRequestOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_access_request(
    payload: AccessRequestCreateRequest, session: DbSession, principal: OrgRead
) -> AccessRequestOut:
    request = await AccessRequestService(session).create(
        principal, entity_id=payload.entity_id, reason=payload.reason
    )
    return AccessRequestOut.model_validate(request)


@access_requests_router.post(
    "/{request_id}/decision",
    summary="Одобрить или отклонить запрос доступа",
    description=(
        "Решает владелец вуза, руководитель его команды или ADMIN — объектная проверка, не "
        "право роли. `approve`: `days` — срок доступа (1..365, по умолчанию 30). `reject` "
        "требует `comment` — иначе 422. Роль: organization:read (сама проверка — объектная)."
    ),
    response_model=AccessRequestOut,
)
async def decide_access_request(
    payload: AccessRequestDecisionRequest,
    session: DbSession,
    principal: OrgRead,
    request_id: Annotated[uuid.UUID, Path()],
) -> AccessRequestOut:
    service = AccessRequestService(session)
    request = await service.get_or_404(request_id)
    if payload.decision == "approve":
        request = await service.approve(
            request, principal, days=payload.days, comment=payload.comment
        )
    else:
        request = await service.reject(request, principal, comment=payload.comment or "")
    return AccessRequestOut.model_validate(request)


@access_requests_router.post(
    "/{request_id}/cancel",
    summary="Отменить свой ожидающий запрос",
    description=(
        "Только сам инициатор запроса и только пока он ещё `pending` — объектная проверка, не "
        "право роли. Решённый (одобренный/отклонённый) или уже просроченный запрос отменить "
        "нельзя. Роль: organization:read (сама проверка — объектная)."
    ),
    response_model=AccessRequestOut,
)
async def cancel_access_request(
    session: DbSession, principal: OrgRead, request_id: Annotated[uuid.UUID, Path()]
) -> AccessRequestOut:
    service = AccessRequestService(session)
    request = await service.get_or_404(request_id)
    request = await service.cancel(request, principal)
    return AccessRequestOut.model_validate(request)


@access_requests_router.post(
    "/{request_id}/revoke",
    summary="Снять ранее выданный доступ",
    description=(
        "Решает владелец вуза, руководитель его команды или ADMIN. Роль: organization:read."
    ),
    response_model=AccessRequestOut,
)
async def revoke_access_request(
    session: DbSession, principal: OrgRead, request_id: Annotated[uuid.UUID, Path()]
) -> AccessRequestOut:
    service = AccessRequestService(session)
    request = await service.get_or_404(request_id)
    request = await service.revoke(request, principal)
    return AccessRequestOut.model_validate(request)
