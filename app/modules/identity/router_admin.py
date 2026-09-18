"""Администрирование пользователей, команд и подтверждений (раздел 6.2).

Все ручки требуют роли ADMIN (право `user:write`, чтение — `user:read`).
Каждое изменение пишется в аудит в той же транзакции, необратимые операции
требуют подтверждения вторым администратором.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status
from sqlalchemy import select

from app.core.config import get_settings
from app.core.deps import AuditDep, DbSession, IfMatch, Pagination, require_permission
from app.core.errors import AppError, ErrorCode, NotFoundError
from app.core.pagination import Page, keyset_before
from app.core.permissions import Permission
from app.core.rate_limit import enforce as rate_limit
from app.core.security import Principal
from app.modules.admin.models import AdminApproval
from app.modules.audit.actions import AuditAction
from app.modules.catalog.service import ContactService, OrganizationService
from app.modules.files.models import File
from app.modules.files.service import FileService
from app.modules.identity.admin_service import (
    AdminUserService,
    ApprovalService,
    UserFilters,
)
from app.modules.identity.erasure_service import ErasureExecutionService
from app.modules.identity.models import DataErasureRequest, Team, User
from app.modules.identity.schemas import (
    ApprovalDecision,
    ApprovalListResponse,
    ApprovalOut,
    ErasureBlocker,
    ErasureRejectRequest,
    ErasureRequestBody,
    ErasureRequestDetail,
    ErasureRequestListResponse,
    ErasureRequestOut,
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


# --- Пользователи ---------------------------------------------------------


@router.get(
    "/users",
    summary="Список пользователей",
    description=(
        "Курсорная пагинация, фильтры по роли, команде, статусу, руководителю, "
        "региону и тексту. Роль: ADMIN."
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
        "Меняет роль, команду, руководителя, статус и локальные настройки. "
        "Обязателен `If-Match`. При смене роли обновляется маппинг в Keycloak, "
        "растёт `perm_epoch` и сбрасывается кэш прав. Понизить последнего "
        "администратора нельзя (CRM-1903). Роль: ADMIN."
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
    updated = await service.patch_user(
        user=user,
        principal=principal,
        expected_version=if_match,
        updates=payload.model_dump(exclude_unset=True),
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
    if user.status not in ("invited",):
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
        "отключает её в Keycloak и завершает сессии. История и авторство "
        "комментариев сохраняются. Роль: ADMIN."
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
        OffboardPreviewItem(kind="tasks", count=workload.open_tasks, supported=workload.supported),
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
        return OffboardResponse(
            mode="preview", user_id=user.id, workload=items, warnings=warnings
        )

    assert payload.successor_id is not None and payload.reason is not None
    result = await service.offboard_confirm(
        user=user,
        principal=principal,
        successor_id=payload.successor_id,
        reason=payload.reason,
    )
    return OffboardResponse(
        mode="confirm",
        user_id=user.id,
        successor_id=payload.successor_id,
        workload=items,
        reassigned_deals=result["reassigned_deals"],
        signature_requests_reassigned=result["signature_requests_reassigned"],
        sessions_terminated=result["sessions_terminated"],
        status=user.status,
        warnings=warnings,
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
    settings = get_settings()
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
        grace_until=dt.datetime.now(dt.UTC) + dt.timedelta(days=settings.erasure_grace_days),
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
) -> ErasureRequestListResponse:
    stmt = select(DataErasureRequest).order_by(
        DataErasureRequest.created_at.desc(), DataErasureRequest.id.desc()
    )
    if erasure_status:
        stmt = stmt.where(DataErasureRequest.status == erasure_status)
    if subject_type:
        stmt = stmt.where(DataErasureRequest.subject_type == subject_type)
    cursor = page.decoded_cursor
    if cursor:
        stmt = stmt.where(
            keyset_before(DataErasureRequest.created_at, DataErasureRequest.id, cursor)
        )

    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())
    built: Page = Page.build(rows, limit=page.limit, serializer=ErasureRequestDetail.from_model)
    return ErasureRequestListResponse(items=built.items, next_cursor=built.next_cursor)


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
    return ErasureRequestDetail.from_model(request)


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
    return ErasureRequestDetail.from_model(request)


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
    return ErasureRequestDetail.from_model(request)


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
    return ErasureRequestDetail.from_model(request)


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
    return ErasureRequestDetail.from_model(request)


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
    return ErasureRequestDetail.from_model(request)


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
    stmt = select(AdminApproval).order_by(
        AdminApproval.created_at.desc(), AdminApproval.id.desc()
    )
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
    approval = await ApprovalService(session).reject(
        approval_id, principal, reason=payload.reason
    )
    return ApprovalOut.model_validate(approval)


# --- Команды --------------------------------------------------------------


@router.get(
    "/teams",
    summary="Список команд",
    description=(
        "Иерархия команд: на ней строится рекурсивный скоуп руководителя. "
        "Роль: ADMIN."
    ),
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
    description="Меняет название, родителя, руководителя или регион. Роль: ADMIN.",
    response_model=TeamOut,
)
async def patch_team(
    payload: TeamPatchRequest,
    session: DbSession,
    audit: AuditDep,
    _: AdminWrite,
    team_id: Annotated[uuid.UUID, Path()],
) -> TeamOut:
    team = (
        await session.execute(select(Team).where(Team.id == team_id))
    ).scalar_one_or_none()
    if team is None or team.deleted_at is not None:
        raise NotFoundError("Команда", team_id)

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

    await audit.record(
        AuditAction.TEAM_UPDATED,
        entity_type="team",
        entity_id=team.id,
        changes=diff_changes(before, after),
    )
    return TeamOut.model_validate(team)


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
