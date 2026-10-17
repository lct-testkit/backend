"""Администрирование пользователей, команд и подтверждений (раздел 6.2).

Все ручки требуют роли ADMIN (право `user:write`, чтение — `user:read`).
Каждое изменение пишется в аудит в той же транзакции, необратимые операции
требуют подтверждения вторым администратором.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, status
from sqlalchemy import select

from app.core.config import get_settings
from app.core.deps import (
    AuditDep,
    DbSession,
    IfMatch,
    Pagination,
    require_permission,
)
from app.core.errors import AppError, ErrorCode, ForbiddenError, NotFoundError, VersionConflictError
from app.core.optimistic import claim_version
from app.core.pagination import Cursor, Page, keyset_after, keyset_before
from app.core.permissions import Permission
from app.core.rate_limit import enforce as rate_limit
from app.core.security import Principal
from app.modules.admin.models import AdminApproval
from app.modules.audit.actions import AuditAction
from app.modules.catalog.models import Organization
from app.modules.catalog.service import ContactService, OrganizationService
from app.modules.crm.handover import HandoverPlan
from app.modules.crm.models import Deal
from app.modules.crm.service import get_ownership_service
from app.modules.files.models import File
from app.modules.files.service import FileService
from app.modules.identity.access_rules import (
    AccessRulesService,
    AccessRulesView,
    DimensionInput,
    preview_access_rules,
)
from app.modules.identity.admin_service import (
    INVITE_EXPIRED_REASON,
    AdminUserService,
    ApprovalService,
    UserFilters,
)
from app.modules.identity.erasure_service import ErasureExecutionService
from app.modules.identity.models import DataErasureRequest, Role, Team, User, UserStatus
from app.modules.identity.schemas import (
    AccessRuleDimensionOut,
    AccessRuleItemOut,
    AccessRuleModeLiteral,
    AccessRulesOut,
    AccessRulesPreviewCounts,
    AccessRulesPreviewResponse,
    AccessRulesPutRequest,
    ApprovalDecision,
    ApprovalListResponse,
    ApprovalOut,
    DistributionItem,
    ErasureBlocker,
    ErasureRejectRequest,
    ErasureRequestBody,
    ErasureRequestDetail,
    ErasureRequestListResponse,
    ErasureRequestOut,
    HandoverExecuteRequest,
    HandoverExecuteResponse,
    OffboardItemListResponse,
    OffboardItemOut,
    OffboardPreviewItem,
    OffboardRequest,
    OffboardResponse,
    OperationResult,
    PasswordResetRequest,
    TeamCreateRequest,
    TeamListResponse,
    TeamOut,
    TeamPatchRequest,
    UserBlockRequest,
    UserCreateRequest,
    UserCreateResponse,
    UserListResponse,
    UserOut,
    UserPatchRequest,
    UserUnblockRequest,
)
from app.modules.identity.service import IdentityService
from app.modules.signing.schemas import DownloadUrlOut

router = APIRouter(prefix="/admin", tags=["admin-users"])

AdminRead = Annotated[Principal, Depends(require_permission(Permission.USER_READ))]
AdminWrite = Annotated[Principal, Depends(require_permission(Permission.USER_WRITE))]
ErasureManager = Annotated[Principal, Depends(require_permission(Permission.ERASURE_MANAGE))]
# B4-4: мастер передачи дел без увольнения — оба права сразу (раздел спринта).
HandoverPerm = Annotated[
    Principal,
    Depends(require_permission(Permission.DEAL_REASSIGN_BULK, Permission.ORG_OWNER_ASSIGN)),
]
# B4-5: ограничения видимости по вузам/направлениям — только ADMIN.
AccessScopeManage = Annotated[
    Principal, Depends(require_permission(Permission.ACCESS_SCOPE_MANAGE))
]

# B4-4: контракт спринта — `POST /api/users/{id}/handover`, без `/admin`, в отличие от
# `offboard`. Отдельный роутер (`router` выше объявлен с `prefix="/admin"`), но живёт в
# этом же файле — рядом с `offboard`, как и просит план задачи.
handover_router = APIRouter(prefix="/users", tags=["admin-users"])


async def _erasure_details(
    session: DbSession, requests: list[DataErasureRequest]
) -> list[ErasureRequestDetail]:
    """Карточки запросов с именами субъектов (по одному запросу на тип субъекта)."""
    names = await ErasureExecutionService(session).subject_displays(requests)
    return [
        ErasureRequestDetail.from_model(request, subject_display=names.get(request.subject_id))
        for request in requests
    ]


async def _erasure_detail(session: DbSession, request: DataErasureRequest) -> ErasureRequestDetail:
    return (await _erasure_details(session, [request]))[0]


# --- Пользователи ---------------------------------------------------------


@router.get(
    "/users",
    summary="Список пользователей",
    description=(
        "Курсорная пагинация, фильтры по роли, команде, статусу, руководителю, "
        "региону, тексту и ограничению видимости (B4-5). Роль: ADMIN."
    ),
    response_model=UserListResponse,
)
async def list_users(
    session: DbSession,
    page: Pagination,
    _: AdminRead,
    role: Annotated[str | None, Query(description="Фильтр по роли")] = None,
    team_id: Annotated[uuid.UUID | None, Query()] = None,
    manager_id: Annotated[uuid.UUID | None, Query()] = None,
    region_id: Annotated[uuid.UUID | None, Query()] = None,
    user_status: Annotated[str | None, Query(alias="status")] = None,
    q: Annotated[str | None, Query(description="Поиск по ФИО и email")] = None,
    access_restricted: Annotated[
        bool | None, Query(description="Только пользователи с ограничением видимости (B4-5)")
    ] = None,
) -> UserListResponse:
    service = AdminUserService(session)
    stmt = service.list_query(
        UserFilters(
            role=role,
            team_id=team_id,
            status=user_status,
            manager_id=manager_id,
            region_id=region_id,
            q=q,
            access_restricted=access_restricted,
        )
    ).order_by(User.created_at.desc(), User.id.desc())

    cursor = page.decoded_cursor
    if cursor:
        stmt = stmt.where(keyset_before(User.created_at, User.id, cursor))

    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())
    built: Page = Page.build(rows, limit=page.limit, serializer=UserOut.model_validate)
    return UserListResponse(items=built.items, next_cursor=built.next_cursor)


@router.get(
    "/users/{user_id}",
    summary="Карточка пользователя",
    description="Локальная проекция пользователя. Роль: ADMIN.",
    response_model=UserOut,
)
async def get_user(
    session: DbSession,
    _: AdminRead,
    user_id: Annotated[uuid.UUID, Path()],
) -> UserOut:
    user = await AdminUserService(session).get_or_404(user_id)
    return UserOut.model_validate(user)


@router.post(
    "/users",
    summary="Создать пользователя",
    description=(
        "Создаёт учётку сначала в Keycloak, затем локально. Роль ADMIN требует "
        "подтверждения вторым администратором (CRM-1902). Если Keycloak "
        "недоступен, локальная запись не создаётся. Роль: ADMIN."
    ),
    response_model=UserCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_user(
    payload: UserCreateRequest,
    session: DbSession,
    principal: AdminWrite,
) -> UserCreateResponse:
    service = AdminUserService(session)
    user, invite_url, expires_at, email_sent = await service.create_user(
        principal=principal,
        full_name=payload.full_name,
        email=str(payload.email),
        role=payload.role,
        team_id=payload.team_id,
        manager_id=payload.manager_id,
        position=payload.position,
        require_totp=payload.require_totp,
        approval_id=payload.approval_id,
        base_url=get_settings().base_url,
    )
    return UserCreateResponse(
        user=UserOut.model_validate(user),
        # Ссылка показывается один раз и только администратору: в закрытом
        # контуре это единственный канал доставки, если SMTP не настроен.
        invite_url=None if email_sent else invite_url,
        invite_expires_at=expires_at,
        invite_email_sent=email_sent,
    )


@router.patch(
    "/users/{user_id}",
    summary="Изменить пользователя",
    description=(
        "Меняет роль, команду, руководителя и локальные настройки. Обязателен "
        "`If-Match`. При смене роли обновляется маппинг в Keycloak, растёт "
        "`perm_epoch` и сбрасывается кэш прав. Понизить последнего "
        "администратора нельзя (CRM-1903). Повышение до ADMIN — операция «четырёх глаз»: без "
        "`approval_id` ответ 409 CRM-1902 с id заявки, которую подтверждает другой "
        "администратор. Статус правкой не меняется: активация "
        "происходит при первом входе, блокировка, разблокировка и увольнение — "
        "отдельные операции (422). Роль: ADMIN."
    ),
    response_model=UserOut,
)
async def patch_user(
    payload: UserPatchRequest,
    session: DbSession,
    principal: AdminWrite,
    if_match: IfMatch,
    user_id: Annotated[uuid.UUID, Path()],
) -> UserOut:
    service = AdminUserService(session)
    user = await service.get_or_404(user_id)
    updates = payload.model_dump(exclude_unset=True)
    approval_id = updates.pop("approval_id", None)
    updated = await service.patch_user(
        user=user,
        principal=principal,
        expected_version=if_match,
        updates=updates,
        approval_id=approval_id,
    )
    return UserOut.model_validate(updated)


@router.post(
    "/users/{user_id}/block",
    summary="Заблокировать пользователя",
    description=(
        "Отключает учётку в Keycloak, завершает все сессии, помечает сделки "
        "признаком недоступного владельца. Причина обязательна. Роль: ADMIN."
    ),
    response_model=OperationResult,
)
async def block_user(
    payload: UserBlockRequest,
    session: DbSession,
    principal: AdminWrite,
    user_id: Annotated[uuid.UUID, Path()],
) -> OperationResult:
    service = AdminUserService(session)
    user = await service.get_or_404(user_id)
    terminated = await service.block(
        user=user,
        principal=principal,
        reason=payload.reason,
        auto_unblock_at=payload.auto_unblock_at,
    )
    return OperationResult(
        ok=True, detail=f"Учётная запись заблокирована, сессий завершено: {terminated}"
    )


@router.post(
    "/users/{user_id}/unblock",
    summary="Разблокировать пользователя",
    description="Включает учётку. Старые сессии не восстанавливаются. Роль: ADMIN.",
    response_model=OperationResult,
)
async def unblock_user(
    payload: UserUnblockRequest,
    session: DbSession,
    _: AdminWrite,
    user_id: Annotated[uuid.UUID, Path()],
) -> OperationResult:
    service = AdminUserService(session)
    user = await service.get_or_404(user_id)
    await service.unblock(user=user, reason=payload.reason)
    return OperationResult(ok=True, detail="Учётная запись разблокирована")


@router.post(
    "/users/{user_id}/reset-password",
    summary="Сбросить пароль",
    description=(
        "Инициирует принудительный сброс: обязательное действие в Keycloak, "
        "завершение всех сессий, аннулирование незавершённых запросов подписи. "
        "Ответ одинаков независимо от существования учётной записи. Роль: ADMIN."
    ),
    response_model=OperationResult,
)
async def reset_password(
    payload: PasswordResetRequest,
    session: DbSession,
    principal: AdminWrite,
    user_id: Annotated[uuid.UUID, Path()],
) -> OperationResult:
    service = AdminUserService(session)
    try:
        user = await service.get_or_404(user_id)
    except NotFoundError:
        # Единый ответ: ручка не должна раскрывать существование учётки.
        return OperationResult(ok=True, detail="Если учётная запись существует, сброс запущен")

    await service.reset_password(
        user=user, reason=payload.reason, suspect_compromise=payload.suspect_compromise
    )
    return OperationResult(ok=True, detail="Если учётная запись существует, сброс запущен")


@router.post(
    "/users/{user_id}/invite",
    summary="Повторно выдать приглашение",
    description=(
        "Перевыпускает одноразовую ссылку приглашения. Ограничения: не чаще "
        "одного раза в 5 минут и не более 5 раз в сутки. Роль: ADMIN."
    ),
    response_model=UserCreateResponse,
)
async def resend_invite(
    session: DbSession,
    principal: AdminWrite,
    user_id: Annotated[uuid.UUID, Path()],
) -> UserCreateResponse:
    settings = get_settings()
    service = AdminUserService(session)
    user = await service.get_or_404(user_id)
    # Учётку, отключённую за непринятое приглашение (INVITE_EXPIRED), приглашают снова.
    expired = (
        user.status == UserStatus.BLOCKED.value and user.status_reason == INVITE_EXPIRED_REASON
    )
    if user.status != UserStatus.INVITED.value and not expired:
        raise AppError(
            ErrorCode.VALIDATION,
            "Приглашение выдаётся только пользователю в статусе invited",
            extra={"status": user.status},
        )

    await rate_limit(
        str(user.id),
        "invite:interval",
        limit=1,
        window_seconds=settings.invite_resend_interval_seconds,
        detail="Повторное приглашение можно отправить не чаще одного раза в 5 минут",
    )
    await rate_limit(
        str(user.id),
        "invite:daily",
        limit=settings.invite_resend_per_day,
        window_seconds=86400,
        detail="Исчерпан суточный лимит повторных приглашений",
    )

    if expired:
        await service.reopen_expired_invite(user)
    identity = IdentityService(session)
    await identity.revoke_invites(user.id)
    token = await identity.issue_invite(user, created_by=principal.user_id)
    expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(hours=settings.invite_ttl_hours)
    return UserCreateResponse(
        user=UserOut.model_validate(user),
        invite_url=f"{settings.base_url.rstrip('/')}/invite/{token}",
        invite_expires_at=expires_at,
        invite_email_sent=False,
    )


@router.post(
    "/users/{user_id}/offboard",
    summary="Мастер передачи дел",
    description=(
        "Режим `preview` показывает, что держит пользователь; режим `confirm` "
        "переназначает сделки преемнику, переводит учётку в `terminated`, "
        "отключает её в Keycloak и завершает сессии. `deal_successors` "
        "(`{id сделки: id преемника}`) отдаёт названные сделки другим преемникам — "
        "остальное уходит `successor_id`. История и авторство комментариев "
        "сохраняются. Роль: ADMIN."
    ),
    response_model=OffboardResponse,
)
async def offboard_user(
    payload: OffboardRequest,
    session: DbSession,
    principal: AdminWrite,
    user_id: Annotated[uuid.UUID, Path()],
) -> OffboardResponse:
    service = AdminUserService(session)
    user = await service.get_or_404(user_id)
    workload, pending_signatures = await service.offboard_preview(user)

    items = [
        OffboardPreviewItem(
            kind="deals",
            count=workload.active_deals,
            supported=workload.supported,
            details=workload.critical_deals,
        ),
        # B4-4: закрытые сделки тоже переходят преемнику (`reassign_all`/`HandoverService`
        # их не пропускают) — без этого числа в превью расходились с результатом.
        OffboardPreviewItem(
            kind="closed_deals", count=workload.closed_deals, supported=workload.supported
        ),
        OffboardPreviewItem(kind="tasks", count=workload.open_tasks, supported=workload.supported),
        OffboardPreviewItem(
            kind="organizations", count=workload.organizations, supported=workload.supported
        ),
        OffboardPreviewItem(
            kind="imports", count=workload.running_imports, supported=workload.supported
        ),
        OffboardPreviewItem(
            kind="reports", count=workload.running_reports, supported=workload.supported
        ),
        OffboardPreviewItem(kind="signature_requests", count=pending_signatures),
    ]
    warnings: list[str] = []
    if not workload.supported:
        warnings.append(
            "Модуль сделок ещё не подключён: список передаваемых сделок пуст не потому, "
            "что их нет, а потому что источник данных недоступен"
        )

    if payload.mode == "preview":
        distribution: list[DistributionItem] = []
        if payload.successor_id is not None:
            # B4-4: план рассчитывается, но не пишется — админ видит разбивку по
            # преемникам ДО подтверждения.
            plan = HandoverPlan(
                default_successor_id=payload.successor_id,
                deal_successors=payload.deal_successors,
                organization_successors=payload.organization_successors,
                rules=payload.rules,
                pool_ids=payload.pool_ids,
                include_closed_deals=payload.include_closed_deals,
                include_organizations=payload.include_organizations,
            )
            resolved = await get_ownership_service().preview_plan(
                session, user.id, plan, principal=principal
            )
            distribution = [
                DistributionItem(
                    successor_id=successor_id,
                    deals=c.deals,
                    organizations=c.organizations,
                    tasks=c.tasks,
                )
                for successor_id, c in resolved.distribution
            ]
        return OffboardResponse(
            mode="preview",
            user_id=user.id,
            workload=items,
            distribution=distribution,
            warnings=warnings,
        )

    # Сужение типов: для mode="confirm" схема уже потребовала оба поля.
    assert payload.successor_id is not None and payload.reason is not None  # noqa: S101
    result = await service.offboard_confirm(
        user=user,
        principal=principal,
        successor_id=payload.successor_id,
        reason=payload.reason,
        deal_successors=payload.deal_successors,
        organization_successors=payload.organization_successors,
        rules=payload.rules,
        pool_ids=payload.pool_ids,
        include_closed_deals=payload.include_closed_deals,
        include_organizations=payload.include_organizations,
    )
    distribution = [
        DistributionItem(
            successor_id=successor_id, deals=c.deals, organizations=c.organizations, tasks=c.tasks
        )
        for successor_id, c in result["distribution"]
    ]
    return OffboardResponse(
        mode="confirm",
        user_id=user.id,
        successor_id=payload.successor_id,
        workload=items,
        reassigned_deals=result["reassigned_deals"],
        reassigned_organizations=result["reassigned_organizations"],
        distribution=distribution,
        signature_requests_reassigned=result["signature_requests_reassigned"],
        sessions_terminated=result["sessions_terminated"],
        status=user.status,
        warnings=warnings,
    )


@router.get(
    "/users/{user_id}/offboard/items",
    summary="Все сделки/вузы уходящего (постранично)",
    description=(
        "Превью `POST .../offboard` ограничено 20 критичными сделками; эта ручка листает "
        "все сделки (`kind=deals`) или вузы (`kind=organizations`) уходящего постранично "
        "(курсор по `created_at`). Роль: ADMIN."
    ),
    response_model=OffboardItemListResponse,
)
async def offboard_items(
    session: DbSession,
    principal: AdminWrite,
    page: Pagination,
    user_id: Annotated[uuid.UUID, Path()],
    kind: Annotated[Literal["deals", "organizations"], Query()] = "deals",
) -> OffboardItemListResponse:
    service = AdminUserService(session)
    await service.get_or_404(user_id)

    if kind == "organizations":
        org_stmt = (
            select(Organization)
            .where(Organization.owner_id == user_id, Organization.deleted_at.is_(None))
            .order_by(Organization.created_at, Organization.id)
        )
        if page.decoded_cursor is not None:
            org_stmt = org_stmt.where(
                keyset_after(Organization.created_at, Organization.id, page.decoded_cursor)
            )
        org_rows = list((await session.execute(org_stmt.limit(page.limit + 1))).scalars().all())
        org_visible = org_rows[: page.limit]
        org_next_cursor = (
            Cursor(value=org_visible[-1].created_at, id=org_visible[-1].id).encode()
            if len(org_rows) > page.limit
            else None
        )
        org_items = [
            OffboardItemOut(id=o.id, title=o.name, organization_name=o.name) for o in org_visible
        ]
        return OffboardItemListResponse(items=org_items, next_cursor=org_next_cursor)

    deal_stmt = (
        select(Deal.id, Deal.number, Deal.title, Deal.status_id, Deal.closed_at, Deal.created_at)
        .where(Deal.owner_id == user_id, Deal.deleted_at.is_(None))
        .order_by(Deal.created_at, Deal.id)
    )
    if page.decoded_cursor is not None:
        deal_stmt = deal_stmt.where(keyset_after(Deal.created_at, Deal.id, page.decoded_cursor))
    deal_rows = list((await session.execute(deal_stmt.limit(page.limit + 1))).all())
    deal_visible = deal_rows[: page.limit]
    deal_next_cursor = (
        Cursor(value=deal_visible[-1].created_at, id=deal_visible[-1].id).encode()
        if len(deal_rows) > page.limit
        else None
    )
    deal_items = [
        OffboardItemOut(
            id=row.id,
            number=row.number,
            title=row.title,
            status_id=row.status_id,
            closed=row.closed_at is not None,
        )
        for row in deal_visible
    ]
    return OffboardItemListResponse(items=deal_items, next_cursor=deal_next_cursor)


@handover_router.post(
    "/{user_id}/handover",
    summary="Передать портфель без увольнения",
    description=(
        "Тот же мастер передачи дел, что `offboard`, но без изменения статуса "
        "сотрудника: отпуск, смена региона, временная разгрузка. ADMIN передаёт "
        "портфель любого сотрудника; HEAD — только сотрудника своей команды "
        "(`head_scope_member_ids`). Права: `deal:reassign_bulk` и "
        "`organization:assign_owner`."
    ),
    response_model=HandoverExecuteResponse,
)
async def handover_user(
    payload: HandoverExecuteRequest,
    session: DbSession,
    principal: HandoverPerm,
    user_id: Annotated[uuid.UUID, Path()],
) -> HandoverExecuteResponse:
    service = AdminUserService(session)
    user = await service.get_or_404(user_id)
    if principal.role == Role.HEAD.value:
        from app.modules.crm.service import head_scope_member_ids

        if user.id not in await head_scope_member_ids(session, principal):
            raise ForbiddenError("Передать портфель можно только сотруднику своей команды")

    plan = HandoverPlan(
        default_successor_id=payload.default_successor_id,
        deal_successors=payload.deal_successors,
        organization_successors=payload.organization_successors,
        rules=payload.rules,
        pool_ids=payload.pool_ids,
        include_closed_deals=payload.include_closed_deals,
        include_organizations=payload.include_organizations,
    )
    execution = await get_ownership_service().reassign_plan(
        session, user.id, plan, principal=principal, reason=payload.reason
    )
    distribution = [
        DistributionItem(
            successor_id=successor_id, deals=c.deals, organizations=c.organizations, tasks=c.tasks
        )
        for successor_id, c in execution.distribution
    ]
    return HandoverExecuteResponse(
        user_id=user.id,
        reassigned_deals=execution.reassigned_deals,
        reassigned_organizations=execution.reassigned_organizations,
        reassigned_tasks=execution.reassigned_tasks,
        distribution=distribution,
    )


# --- B4-5/B4-6: ограничения видимости по вузам/направлениям ---------------


def _access_rules_out(view: AccessRulesView) -> AccessRulesOut:
    return AccessRulesOut(
        user_id=view.user_id,
        version=view.version,
        access_restricted=view.access_restricted,
        organizations=AccessRuleDimensionOut(
            mode=view.organizations.mode,
            items=[AccessRuleItemOut(id=i, name=name) for i, name in view.organizations.items],
        ),
        directions=AccessRuleDimensionOut(
            mode=view.directions.mode,
            items=[AccessRuleItemOut(id=i, name=name) for i, name in view.directions.items],
        ),
    )


@router.get(
    "/users/{user_id}/access-rules",
    summary="Ограничения видимости пользователя",
    description=(
        "Текущие правила по вузам и направлениям (B4-5): режим `all`/`allow`/`deny` и "
        "список для каждой размерности. Роль: ADMIN (`access_scope:manage`)."
    ),
    response_model=AccessRulesOut,
)
async def get_access_rules(
    session: DbSession, _: AccessScopeManage, user_id: Annotated[uuid.UUID, Path()]
) -> AccessRulesOut:
    service = AdminUserService(session)
    user = await service.get_or_404(user_id)
    view = await AccessRulesService(session).view(user)
    return _access_rules_out(view)


@router.put(
    "/users/{user_id}/access-rules",
    summary="Задать ограничения видимости пользователя",
    description=(
        "Заменяет правила целиком (не патч). Только для KAM/HEAD — 422 для ADMIN/"
        "AUDITOR/INTEGRATION. `allow` без элементов — 422 (для «не видит ничего» есть "
        'блокировка). `{"mode": "all", "ids": []}` в обеих размерностях снимает '
        "ограничение. Требует If-Match. Роль: ADMIN (`access_scope:manage`)."
    ),
    response_model=AccessRulesOut,
)
async def put_access_rules(
    payload: AccessRulesPutRequest,
    session: DbSession,
    principal: AccessScopeManage,
    if_match: IfMatch,
    user_id: Annotated[uuid.UUID, Path()],
) -> AccessRulesOut:
    service = AdminUserService(session)
    user = await service.get_or_404(user_id)
    view = await AccessRulesService(session).replace(
        user,
        principal,
        organizations=DimensionInput(
            mode=payload.organizations.mode, ids=payload.organizations.ids
        ),
        directions=DimensionInput(mode=payload.directions.mode, ids=payload.directions.ids),
        reason=payload.reason,
        expected_version=if_match,
    )
    return _access_rules_out(view)


@router.get(
    "/users/{user_id}/access-rules/preview",
    summary="Предпросмотр последствий ограничения",
    description=(
        "Сухой прогон: считает сделки/вузы/контакты, видимые ДО и ПОСЛЕ правил из "
        "запроса (ещё не сохранённых). Параметры: `organizations.mode`/"
        "`organizations.ids`, `directions.mode`/`directions.ids`. В БД ничего не "
        "меняется. Роль: ADMIN (`access_scope:manage`)."
    ),
    response_model=AccessRulesPreviewResponse,
)
async def preview_access_rules_route(
    session: DbSession,
    _: AccessScopeManage,
    user_id: Annotated[uuid.UUID, Path()],
    organizations_mode: Annotated[AccessRuleModeLiteral, Query(alias="organizations.mode")] = "all",
    organizations_ids: Annotated[list[uuid.UUID] | None, Query(alias="organizations.ids")] = None,
    directions_mode: Annotated[AccessRuleModeLiteral, Query(alias="directions.mode")] = "all",
    directions_ids: Annotated[list[uuid.UUID] | None, Query(alias="directions.ids")] = None,
) -> AccessRulesPreviewResponse:
    service = AdminUserService(session)
    user = await service.get_or_404(user_id)
    preview = await preview_access_rules(
        session,
        user,
        organizations=DimensionInput(mode=organizations_mode, ids=organizations_ids or []),
        directions=DimensionInput(mode=directions_mode, ids=directions_ids or []),
    )
    return AccessRulesPreviewResponse(
        deals=AccessRulesPreviewCounts(
            visible_before=preview.deals.visible_before,
            visible_after=preview.deals.visible_after,
            hidden=preview.deals.hidden,
        ),
        organizations=AccessRulesPreviewCounts(
            visible_before=preview.organizations.visible_before,
            visible_after=preview.organizations.visible_after,
            hidden=preview.organizations.hidden,
        ),
        contacts=AccessRulesPreviewCounts(
            visible_before=preview.contacts.visible_before,
            visible_after=preview.contacts.visible_after,
            hidden=preview.contacts.hidden,
        ),
    )


@router.post(
    "/users/{user_id}/erasure-request",
    summary="Запрос на удаление или обезличивание",
    description=(
        "Создаёт запрос по 152-ФЗ и сразу возвращает блокеры: активные сделки, "
        "незакрытые задачи, роль последнего администратора, наличие подписей. "
        "Требует подтверждения вторым администратором. Роль: ADMIN."
    ),
    response_model=ErasureRequestOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_erasure_request(
    payload: ErasureRequestBody,
    session: DbSession,
    principal: ErasureManager,
    user_id: Annotated[uuid.UUID, Path()],
) -> ErasureRequestOut:
    service = AdminUserService(session)
    user = await service.get_or_404(user_id)
    request, blockers = await service.create_erasure_request(
        user=user,
        principal=principal,
        mode=payload.mode,
        reason=payload.reason,
        legal_basis=payload.legal_basis,
        comment=payload.comment,
        approval_id=payload.approval_id,
    )
    return ErasureRequestOut(
        id=request.id,
        subject_type=request.subject_type,
        subject_id=request.subject_id,
        mode=payload.mode,
        status=request.status,
        deadline_at=request.deadline_at,
        blockers=[ErasureBlocker(**item) for item in blockers],
        # Сохранённое значение: у `blocked` отсрочка не начинается (None).
        grace_until=request.grace_until,
    )


# --- Спринт 10: жизненный цикл запроса на удаление/обезличивание ----------
# (new_spec §4.8.4-4.8.5). Создание запроса для сотрудника осталось на
# `POST /admin/users/{user_id}/erasure-request` выше (спринт 1) — здесь то,
# чего не хватало: список для экрана «Удаляемые» с отсчётом до `grace_until`,
# пересчёт блокеров, обоснованный отказ, восстановление в период отсрочки,
# создание запроса для контакта и ссылка на акт об уничтожении ПДн.


@router.get(
    "/erasure-requests",
    summary="Запросы на удаление/обезличивание",
    description=(
        "Экран «Удаляемые» (new_spec §4.8.4 шаг 4): фильтры по статусу и типу "
        "субъекта, курсорная пагинация. Роль: ADMIN."
    ),
    response_model=ErasureRequestListResponse,
)
async def list_erasure_requests(
    session: DbSession,
    page: Pagination,
    _: ErasureManager,
    erasure_status: Annotated[str | None, Query(alias="status")] = None,
    subject_type: Annotated[str | None, Query()] = None,
    subject_id: Annotated[uuid.UUID | None, Query()] = None,
) -> ErasureRequestListResponse:
    stmt = select(DataErasureRequest).order_by(
        DataErasureRequest.created_at.desc(), DataErasureRequest.id.desc()
    )
    if erasure_status:
        stmt = stmt.where(DataErasureRequest.status == erasure_status)
    if subject_type:
        stmt = stmt.where(DataErasureRequest.subject_type == subject_type)
    if subject_id:
        stmt = stmt.where(DataErasureRequest.subject_id == subject_id)
    cursor = page.decoded_cursor
    if cursor:
        stmt = stmt.where(
            keyset_before(DataErasureRequest.created_at, DataErasureRequest.id, cursor)
        )

    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())
    built: Page = Page.build(rows, limit=page.limit)
    return ErasureRequestListResponse(
        items=await _erasure_details(session, built.items), next_cursor=built.next_cursor
    )


@router.get(
    "/erasure-requests/{request_id}",
    summary="Карточка запроса на удаление/обезличивание",
    response_model=ErasureRequestDetail,
)
async def get_erasure_request(
    session: DbSession,
    _: ErasureManager,
    request_id: Annotated[uuid.UUID, Path()],
) -> ErasureRequestDetail:
    request = await ErasureExecutionService(session).get_or_404(request_id)
    return await _erasure_detail(session, request)


@router.post(
    "/erasure-requests/{request_id}/recheck",
    summary="Пересчитать блокеры",
    description=(
        "У заблокированного запроса пересчитывает блокеры (например, после "
        "передачи дел): если их больше нет, запрос переходит в отсрочку. "
        "Роль: ADMIN."
    ),
    response_model=ErasureRequestDetail,
)
async def recheck_erasure_request(
    session: DbSession,
    principal: ErasureManager,
    request_id: Annotated[uuid.UUID, Path()],
) -> ErasureRequestDetail:
    service = ErasureExecutionService(session)
    request = await service.get_or_404(request_id)
    await service.recheck(request, principal)
    return await _erasure_detail(session, request)


@router.post(
    "/erasure-requests/{request_id}/reject",
    summary="Отклонить запрос",
    description=(
        "Обоснованный отказ (new_spec §4.8.4 шаг 2, §4.8.5 — например, "
        "действующий договор): терминальный статус, субъект подаёт запрос "
        "заново при необходимости. Роль: ADMIN."
    ),
    response_model=ErasureRequestDetail,
)
async def reject_erasure_request(
    payload: ErasureRejectRequest,
    session: DbSession,
    _: ErasureManager,
    request_id: Annotated[uuid.UUID, Path()],
) -> ErasureRequestDetail:
    service = ErasureExecutionService(session)
    request = await service.get_or_404(request_id)
    await service.reject(request, reason=payload.reason)
    return await _erasure_detail(session, request)


@router.post(
    "/erasure-requests/{request_id}/restore",
    summary="Восстановить (отменить удаление в период отсрочки)",
    description=(
        "Кнопка «Восстановить» (new_spec §4.8.4 шаг 4) — работает только пока "
        "не истёк `grace_until`. Не снимает блокировку самой учётной записи, "
        "это отдельное действие (`/unblock`). Роль: ADMIN."
    ),
    response_model=ErasureRequestDetail,
)
async def restore_erasure_request(
    session: DbSession,
    _: ErasureManager,
    request_id: Annotated[uuid.UUID, Path()],
) -> ErasureRequestDetail:
    service = ErasureExecutionService(session)
    request = await service.get_or_404(request_id)
    await service.restore(request)
    return await _erasure_detail(session, request)


@router.get(
    "/erasure-requests/{request_id}/act",
    summary="Ссылка на акт об уничтожении ПДн",
    description="Доступна только после исполнения запроса. Роль: ADMIN.",
    response_model=DownloadUrlOut,
)
async def get_erasure_act(
    session: DbSession,
    _: ErasureManager,
    request_id: Annotated[uuid.UUID, Path()],
) -> DownloadUrlOut:
    request = await ErasureExecutionService(session).get_or_404(request_id)
    if request.act_file_id is None:
        raise NotFoundError("Акт об уничтожении ПДн", request_id)
    file = await session.get(File, request.act_file_id)
    if file is None:
        raise NotFoundError("Акт об уничтожении ПДн", request_id)
    url, expires_at = await FileService(session).download_url(file)
    return DownloadUrlOut(download_url=url, expires_at=expires_at)


@router.post(
    "/contacts/{contact_id}/erasure-request",
    summary="Запрос на удаление/обезличивание контакта",
    description=(
        "new_spec §4.8.5: инициатор — оператор по письменному обращению "
        "субъекта (форма с сайта → CMS-вебхук — отдельный, не входящий в этот "
        "спринт путь). Возвращает блокеры сразу, требует подтверждения вторым "
        "администратором. Роль: ADMIN."
    ),
    response_model=ErasureRequestDetail,
    status_code=status.HTTP_201_CREATED,
)
async def create_contact_erasure_request(
    payload: ErasureRequestBody,
    session: DbSession,
    principal: ErasureManager,
    contact_id: Annotated[uuid.UUID, Path()],
) -> ErasureRequestDetail:
    contact = await ContactService(session).get_or_404(contact_id, principal)
    request, _blockers = await ErasureExecutionService(session).create_contact_request(
        contact=contact,
        principal=principal,
        mode=payload.mode,
        reason=payload.reason,
        legal_basis=payload.legal_basis,
        comment=payload.comment,
        approval_id=payload.approval_id,
    )
    return await _erasure_detail(session, request)


@router.post(
    "/organizations/{organization_id}/erasure-request",
    summary="Запрос на удаление/обезличивание ИП",
    description=(
        "dop.md §11.8: применимо только к организациям с "
        "org_type='individual_entrepreneur' — данные ИП это ПДн физлица, а "
        "не сведения о юрлице. Для остальных типов организаций отклоняется "
        "как неприменимое (CRM-1001). Роль: ADMIN."
    ),
    response_model=ErasureRequestDetail,
    status_code=status.HTTP_201_CREATED,
)
async def create_organization_erasure_request(
    payload: ErasureRequestBody,
    session: DbSession,
    principal: ErasureManager,
    organization_id: Annotated[uuid.UUID, Path()],
) -> ErasureRequestDetail:
    organization = await OrganizationService(session).get_or_404(organization_id, principal)
    request, _blockers = await ErasureExecutionService(session).create_organization_request(
        organization=organization,
        principal=principal,
        mode=payload.mode,
        reason=payload.reason,
        legal_basis=payload.legal_basis,
        comment=payload.comment,
        approval_id=payload.approval_id,
    )
    return await _erasure_detail(session, request)


# --- Подтверждения «четырёх глаз» ----------------------------------------


@router.get(
    "/approvals",
    summary="Заявки на подтверждение",
    description="Операции, ожидающие второго администратора. Роль: ADMIN.",
    response_model=ApprovalListResponse,
)
async def list_approvals(
    session: DbSession,
    page: Pagination,
    _: AdminRead,
    approval_status: Annotated[str | None, Query(alias="status")] = "pending",
) -> ApprovalListResponse:
    stmt = select(AdminApproval).order_by(AdminApproval.created_at.desc(), AdminApproval.id.desc())
    if approval_status:
        stmt = stmt.where(AdminApproval.status == approval_status)
    cursor = page.decoded_cursor
    if cursor:
        stmt = stmt.where(keyset_before(AdminApproval.created_at, AdminApproval.id, cursor))

    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())
    built: Page = Page.build(rows, limit=page.limit, serializer=ApprovalOut.model_validate)
    return ApprovalListResponse(items=built.items, next_cursor=built.next_cursor)


@router.post(
    "/approvals/{approval_id}/approve",
    summary="Подтвердить операцию",
    description=(
        "Подтверждение выдаёт администратор, отличный от инициатора. "
        "После подтверждения инициатор повторяет исходный запрос с "
        "`approval_id`. Роль: ADMIN."
    ),
    response_model=ApprovalOut,
)
async def approve_operation(
    session: DbSession,
    principal: AdminWrite,
    approval_id: Annotated[uuid.UUID, Path()],
) -> ApprovalOut:
    approval = await ApprovalService(session).approve(approval_id, principal)
    return ApprovalOut.model_validate(approval)


@router.post(
    "/approvals/{approval_id}/reject",
    summary="Отклонить операцию",
    description="Отклоняет заявку с указанием причины. Роль: ADMIN.",
    response_model=ApprovalOut,
)
async def reject_operation(
    payload: ApprovalDecision,
    session: DbSession,
    principal: AdminWrite,
    approval_id: Annotated[uuid.UUID, Path()],
) -> ApprovalOut:
    approval = await ApprovalService(session).reject(approval_id, principal, reason=payload.reason)
    return ApprovalOut.model_validate(approval)


# --- Команды --------------------------------------------------------------


@router.get(
    "/teams",
    summary="Список команд",
    description=("Иерархия команд: на ней строится рекурсивный скоуп руководителя. Роль: ADMIN."),
    response_model=TeamListResponse,
)
async def list_teams(
    session: DbSession,
    page: Pagination,
    _: AdminRead,
    parent_id: Annotated[uuid.UUID | None, Query()] = None,
) -> TeamListResponse:
    stmt = (
        select(Team)
        .where(Team.deleted_at.is_(None))
        .order_by(Team.created_at.desc(), Team.id.desc())
    )
    if parent_id is not None:
        stmt = stmt.where(Team.parent_id == parent_id)
    cursor = page.decoded_cursor
    if cursor:
        stmt = stmt.where(keyset_before(Team.created_at, Team.id, cursor))

    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())
    built: Page = Page.build(rows, limit=page.limit, serializer=TeamOut.model_validate)
    return TeamListResponse(items=built.items, next_cursor=built.next_cursor)


async def _get_team(session: DbSession, team_id: uuid.UUID) -> Team:
    team = (await session.execute(select(Team).where(Team.id == team_id))).scalar_one_or_none()
    if team is None or team.deleted_at is not None:
        raise NotFoundError("Команда", team_id)
    return team


@router.get(
    "/teams/{team_id}",
    summary="Карточка команды",
    description="Команда с версией и временем последней правки. Роль: ADMIN.",
    response_model=TeamOut,
)
async def get_team(
    session: DbSession,
    _: AdminRead,
    team_id: Annotated[uuid.UUID, Path()],
) -> TeamOut:
    return TeamOut.model_validate(await _get_team(session, team_id))


@router.post(
    "/teams",
    summary="Создать команду",
    description="Создаёт команду в иерархии. Роль: ADMIN.",
    response_model=TeamOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_team(
    payload: TeamCreateRequest,
    session: DbSession,
    audit: AuditDep,
    _: AdminWrite,
) -> TeamOut:
    if payload.parent_id is not None:
        parent = (
            await session.execute(select(Team).where(Team.id == payload.parent_id))
        ).scalar_one_or_none()
        if parent is None or parent.deleted_at is not None:
            raise NotFoundError("Родительская команда", payload.parent_id)

    team = Team(
        name=payload.name,
        parent_id=payload.parent_id,
        head_id=payload.head_id,
        region_id=payload.region_id,
    )
    session.add(team)
    await session.flush()
    await audit.record(
        AuditAction.TEAM_CREATED,
        entity_type="team",
        entity_id=team.id,
        changes={"name": {"old": None, "new": team.name}},
    )
    return TeamOut.model_validate(team)


@router.patch(
    "/teams/{team_id}",
    summary="Изменить команду",
    description=(
        "Меняет название, родителя, руководителя или регион. Обязателен `If-Match` с "
        "текущей версией команды (без него — 422, как у сделок/организаций); устаревшая "
        "версия — 409 CRM-1002. Каждая правка поднимает `version`. Роль: ADMIN."
    ),
    response_model=TeamOut,
)
async def patch_team(
    payload: TeamPatchRequest,
    session: DbSession,
    audit: AuditDep,
    _: AdminWrite,
    team_id: Annotated[uuid.UUID, Path()],
    if_match: IfMatch,
) -> TeamOut:
    team = await _get_team(session, team_id)
    if team.version != if_match:
        raise VersionConflictError(team.version, {"name": team.name})

    updates = payload.model_dump(exclude_unset=True)
    if updates.get("parent_id") == team.id:
        raise AppError(ErrorCode.VALIDATION, "Команда не может быть родителем самой себя")

    before = {
        "name": team.name,
        "parent_id": str(team.parent_id) if team.parent_id else None,
        "head_id": str(team.head_id) if team.head_id else None,
        "region_id": str(team.region_id) if team.region_id else None,
    }
    for field_name, value in updates.items():
        setattr(team, field_name, value)
    await session.flush()

    if await _has_team_cycle(session, team.id):
        raise AppError(ErrorCode.VALIDATION, "Иерархия команд не должна содержать циклов")

    after = {
        "name": team.name,
        "parent_id": str(team.parent_id) if team.parent_id else None,
        "head_id": str(team.head_id) if team.head_id else None,
        "region_id": str(team.region_id) if team.region_id else None,
    }
    from app.modules.audit.service import diff_changes

    changes = diff_changes(before, after)
    if changes:
        # Атомарно, с версией клиента: два параллельных PATCH команды не проходят проверку
        # версии оба.
        await claim_version(session, team, if_match)
        await session.flush()
    await audit.record(
        AuditAction.TEAM_UPDATED,
        entity_type="team",
        entity_id=team.id,
        changes=changes,
    )
    return TeamOut.model_validate(team)


@router.delete(
    "/teams/{team_id}",
    summary="Удалить команду",
    description=(
        "Мягкое удаление, только если в команде нет ни одного активного сотрудника и нет "
        "живых дочерних команд — иначе 409 CRM-1303. Открытые сделки участников отдельно не "
        "проверяются: у сделки нет ссылки на команду, только на владельца, а проверка «нет "
        "активных сотрудников» уже покрывает случай, когда сделками некому распоряжаться от "
        "имени команды. Роль: ADMIN."
    ),
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_team(
    session: DbSession,
    audit: AuditDep,
    _: AdminWrite,
    team_id: Annotated[uuid.UUID, Path()],
) -> None:
    team = await _get_team(session, team_id)
    active_member = await session.scalar(
        select(User.id)
        .where(User.team_id == team.id, User.status == UserStatus.ACTIVE.value)
        .limit(1)
    )
    if active_member is not None:
        raise AppError(
            ErrorCode.ENTITY_IN_USE,
            "В команде есть активные сотрудники — удаление невозможно",
        )
    child_team = await session.scalar(
        select(Team.id).where(Team.parent_id == team.id, Team.deleted_at.is_(None)).limit(1)
    )
    if child_team is not None:
        raise AppError(
            ErrorCode.ENTITY_IN_USE,
            "У команды есть дочерние команды — удаление невозможно",
        )
    team.deleted_at = dt.datetime.now(dt.UTC)
    await session.flush()
    await audit.record(
        AuditAction.TEAM_DELETED,
        entity_type="team",
        entity_id=team.id,
        changes={"name": {"old": team.name, "new": None}},
    )


async def _has_team_cycle(session: DbSession, team_id: uuid.UUID) -> bool:
    """Обход вверх по дереву: цикл сломал бы рекурсивный скоуп руководителя."""
    seen: set[uuid.UUID] = set()
    current: uuid.UUID | None = team_id
    while current is not None:
        if current in seen:
            return True
        seen.add(current)
        current = (
            await session.execute(select(Team.parent_id).where(Team.id == current))
        ).scalar_one_or_none()
    return False
