"""Ручки автоподстановки по ИНН и администрирования реестра (dop.md §11.10)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status

from app.core.deps import DbSession, Pagination, require_permission
from app.core.pagination import Page, keyset_before
from app.core.permissions import Permission
from app.core.security import Principal
from app.modules.registry.models import RegistryVersion
from app.modules.registry.schemas import (
    OrgDetailsOut,
    OrgSuggestionOut,
    OrgSuggestResponse,
    RegistryImportRequest,
    RegistryVersionListResponse,
    RegistryVersionOut,
    ValidateRequisiteRequest,
    ValidateRequisiteResponse,
)
from app.modules.registry.service import (
    OrgLookupService,
    RegistryImportService,
    validate_requisite_value,
)

org_lookup_router = APIRouter(prefix="/org-lookup", tags=["org-lookup"])
registry_admin_router = APIRouter(prefix="/admin/registry", tags=["registry"])

OrgLookupPerm = Annotated[Principal, Depends(require_permission(Permission.ORG_LOOKUP_USE))]
RegistryImportPerm = Annotated[Principal, Depends(require_permission(Permission.REGISTRY_IMPORT))]


@org_lookup_router.get(
    "/suggest",
    summary="Автоподстановка по названию или ИНН",
    description=(
        "Цепочка провайдеров: локальный реестр ЕГРЮЛ → подтверждённые организации "
        "нашей БД → внешние (если включён флаг, по умолчанию выключены). "
        "Ограничено по частоте (30/мин на пользователя), каждый запрос логируется."
    ),
    response_model=OrgSuggestResponse,
)
async def suggest_organizations(
    session: DbSession,
    principal: OrgLookupPerm,
    q: Annotated[str, Query(min_length=1, max_length=255)],
    limit: Annotated[int, Query(ge=1, le=20)] = 10,
) -> OrgSuggestResponse:
    result = await OrgLookupService(session).suggest(principal, query=q, limit=limit)
    return OrgSuggestResponse(
        items=[
            OrgSuggestionOut(
                inn=item.inn,
                name=item.name,
                region=item.region,
                status=item.status,
                is_liquidated=item.is_liquidated,
                provider=item.provider,
            )
            for item in result.items
        ]
    )


@org_lookup_router.get(
    "/inn/{inn}",
    summary="Детали организации по ИНН",
    description=(
        "Проверяет контрольную сумму ИНН, затем возвращает полные детали из "
        "локального реестра (ОГРН, КПП, ОПФ, статус, адрес, руководитель)."
    ),
    response_model=OrgDetailsOut,
)
async def get_organization_by_inn(
    session: DbSession,
    principal: OrgLookupPerm,
    inn: Annotated[str, Path(min_length=10, max_length=12)],
) -> OrgDetailsOut:
    details = await OrgLookupService(session).get_by_inn(principal, inn)
    return OrgDetailsOut.from_details(details)


@org_lookup_router.post(
    "/validate",
    summary="Проверить контрольную сумму реквизита",
    description="ИНН, ОГРН, ОГРНИП или КПП — проверяется формат и контрольная сумма.",
    response_model=ValidateRequisiteResponse,
)
async def validate_requisite_endpoint(
    payload: ValidateRequisiteRequest, _: OrgLookupPerm
) -> ValidateRequisiteResponse:
    ok, reason = validate_requisite_value(payload.kind, payload.value)
    return ValidateRequisiteResponse(ok=ok, reason=reason)


@registry_admin_router.post(
    "/import",
    summary="Загрузить новую выгрузку ЕГРЮЛ",
    description=(
        "Тело содержит `file_id` уже загруженного и подтверждённого файла (см. "
        "`/api/files/upload-intent` + `/commit`) и источник. Создаёт запись "
        "`registry_versions`, фоновая задача разбирает XML потоково и наполняет "
        "локальный реестр. Роль: ADMIN."
    ),
    response_model=RegistryVersionOut,
    status_code=status.HTTP_201_CREATED,
)
async def start_registry_import(
    payload: RegistryImportRequest, session: DbSession, principal: RegistryImportPerm
) -> RegistryVersionOut:
    version = await RegistryImportService(session).start_import(
        principal, file_id=payload.file_id, source=payload.source
    )
    return RegistryVersionOut.model_validate(version)


@registry_admin_router.get(
    "/versions",
    summary="Список версий реестра",
    response_model=RegistryVersionListResponse,
)
async def list_registry_versions(
    session: DbSession, page: Pagination, _: RegistryImportPerm
) -> RegistryVersionListResponse:
    stmt = (
        (await RegistryImportService(session).list_query())
        .order_by(RegistryVersion.created_at.desc(), RegistryVersion.id.desc())
    )
    cursor = page.decoded_cursor
    if cursor:
        stmt = stmt.where(keyset_before(RegistryVersion.created_at, RegistryVersion.id, cursor))

    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())
    built: Page = Page.build(rows, limit=page.limit, serializer=RegistryVersionOut.model_validate)
    return RegistryVersionListResponse(items=built.items, next_cursor=built.next_cursor)


@registry_admin_router.delete(
    "/versions/{version_id}",
    summary="Удалить версию реестра",
    description=(
        "Нельзя удалить версию, которая сейчас импортируется, и нельзя "
        "удалить последнюю успешно завершённую версию — иначе 409 CRM-1303. "
        "Роль: ADMIN."
    ),
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_registry_version(
    session: DbSession, principal: RegistryImportPerm, version_id: Annotated[uuid.UUID, Path()]
) -> None:
    service = RegistryImportService(session)
    version = await service.get_or_404(version_id)
    await service.delete(version)
