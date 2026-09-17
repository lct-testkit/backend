"""Ручки сделок, комментариев и задач (раздел 6.6).

Три router'а, а не один: `deals_router` живёт под `/deals` (включая вложенные
комментарии — `GET/POST /deals/{id}/comments`), но правка и удаление
конкретного комментария — плоские `/api/comments/{id}` (раздел 6.6), поэтому
`comments_router` и `tasks_router` регистрируются отдельно в `app/main.py`.

`POST /deals/bulk/reassign` объявлен раньше `/{deal_id}/...`: у обоих путей
одинаковая форма `/deals/<сегмент>/reassign`, и без явного порядка `bulk`
попал бы в `{deal_id}` первым совпавшим маршрутом.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request, status

from app.core.deps import DbSession, IdempotencyKeyHeader, IfMatch, Pagination, require_permission
from app.core.errors import AppError, ErrorCode
from app.core.idempotency import IdempotencyGuard
from app.core.pagination import Page, keyset_before
from app.core.permissions import Permission
from app.core.redis_client import distributed_lock
from app.core.security import Principal
from app.modules.crm.models import Deal, Task
from app.modules.crm.schemas import (
    AvailableTransitionOut,
    AvailableTransitionsResponse,
    BulkReassignRequest,
    BulkReassignResponse,
    CommentCreateRequest,
    CommentDeleteRequest,
    CommentListResponse,
    CommentOut,
    CommentUpdateRequest,
    DealCardOut,
    DealCreateRequest,
    DealEventOut,
    DealHistoryResponse,
    DealListResponse,
    DealOut,
    DealProductOut,
    DealStatusHistoryOut,
    DealUpdateRequest,
    ReassignRequest,
    TaskCreateRequest,
    TaskListResponse,
    TaskOut,
    TaskUpdateRequest,
    TransitionConditionOut,
    TransitionRequest,
    TransitionResponse,
)
from app.modules.crm.service import (
    CommentService,
    DealFilters,
    DealService,
    TaskFilters,
    TaskService,
    deal_scope_clause,
    touch_recent,
)
from app.modules.identity.schemas import OperationResult

deals_router = APIRouter(prefix="/deals", tags=["deals"])
comments_router = APIRouter(prefix="/comments", tags=["comments"])
tasks_router = APIRouter(prefix="/tasks", tags=["tasks"])

DealRead = Annotated[Principal, Depends(require_permission(Permission.DEAL_READ))]
DealCreatePerm = Annotated[Principal, Depends(require_permission(Permission.DEAL_CREATE))]
DealUpdatePerm = Annotated[Principal, Depends(require_permission(Permission.DEAL_UPDATE))]
DealTransitionPerm = Annotated[Principal, Depends(require_permission(Permission.DEAL_TRANSITION))]
DealReassignPerm = Annotated[Principal, Depends(require_permission(Permission.DEAL_REASSIGN))]
DealReassignBulkPerm = Annotated[
    Principal, Depends(require_permission(Permission.DEAL_REASSIGN_BULK))
]


# =============================================================================
# Сделки
# =============================================================================


@deals_router.get(
    "",
    summary="Список сделок",
    description=(
        "Курсорная пагинация, фильтры по статусу/воронке/типу/владельцу/приоритету/"
        "SLA/датам/тексту. Скоуп по роли (раздел 4): KAM — свои сделки, HEAD — "
        "команда, ADMIN — все. Роль: чтение сделок."
    ),
    response_model=DealListResponse,
)
async def list_deals(
    session: DbSession,
    page: Pagination,
    principal: DealRead,
    status_id: Annotated[uuid.UUID | None, Query()] = None,
    workflow_id: Annotated[uuid.UUID | None, Query()] = None,
    deal_type: Annotated[str | None, Query()] = None,
    organization_id: Annotated[uuid.UUID | None, Query()] = None,
    contact_id: Annotated[uuid.UUID | None, Query()] = None,
    owner_id: Annotated[uuid.UUID | None, Query()] = None,
    product_id: Annotated[uuid.UUID | None, Query()] = None,
    priority: Annotated[str | None, Query()] = None,
    sla_state: Annotated[str | None, Query()] = None,
    created_from: Annotated[dt.datetime | None, Query()] = None,
    created_to: Annotated[dt.datetime | None, Query()] = None,
    closed_from: Annotated[dt.datetime | None, Query()] = None,
    closed_to: Annotated[dt.datetime | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
) -> DealListResponse:
    filters = DealFilters(
        status_id=status_id,
        workflow_id=workflow_id,
        deal_type=deal_type,
        organization_id=organization_id,
        contact_id=contact_id,
        owner_id=owner_id,
        product_id=product_id,
        priority=priority,
        sla_state=sla_state,
        created_from=created_from,
        created_to=created_to,
        closed_from=closed_from,
        closed_to=closed_to,
        q=q,
    )
    stmt = (await DealService(session).list_query(principal, filters)).order_by(
        Deal.created_at.desc(), Deal.id.desc()
    )
    cursor = page.decoded_cursor
    if cursor:
        stmt = stmt.where(keyset_before(Deal.created_at, Deal.id, cursor))
    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())
    built = Page.build(rows, limit=page.limit, serializer=DealOut.model_validate)
    return DealListResponse(items=built.items, next_cursor=built.next_cursor)


@deals_router.post(
    "",
    summary="Создать сделку",
    description=(
        "Для B2B обязателен organization_id, для B2C — contact_id (new_spec §4.9). "
        "Сделка получает начальный статус опубликованной воронки по умолчанию для "
        "своего типа (или явно переданного workflow_id), номер, событие CREATED и "
        "аудит. Поддерживает Idempotency-Key. Роль: создание сделок."
    ),
    response_model=DealOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_deal(
    payload: DealCreateRequest,
    request: Request,
    session: DbSession,
    principal: DealCreatePerm,
    idempotency_key: IdempotencyKeyHeader,
) -> DealOut:
    guard = IdempotencyGuard(session, actor_id=principal.user_id)
    body = await request.body()
    if idempotency_key:
        cached = await guard.lookup(
            key=idempotency_key, method=request.method, path=request.url.path, body=body
        )
        if cached is not None:
            return DealOut.model_validate(cached.body)
        await guard.reserve(
            key=idempotency_key, method=request.method, path=request.url.path, body=body
        )

    deal = await DealService(session).create(principal, payload)
    result = DealOut.model_validate(deal)

    if idempotency_key:
        await guard.store(
            key=idempotency_key, status=status.HTTP_201_CREATED, body=result.model_dump(mode="json")
        )
    return result


@deals_router.post(
    "/bulk/reassign",
    summary="Массовая передача сделок",
    description=(
        "Меняет владельца у списка сделок в пределах скоупа вызывающего. "
        "Роль: массовое переназначение."
    ),
    response_model=BulkReassignResponse,
)
async def bulk_reassign_deals(
    payload: BulkReassignRequest, session: DbSession, principal: DealReassignBulkPerm
) -> BulkReassignResponse:
    count = await DealService(session).bulk_reassign(
        principal,
        deal_ids=payload.deal_ids,
        successor_id=payload.successor_id,
        reason=payload.reason,
    )
    return BulkReassignResponse(reassigned_count=count)


@deals_router.get(
    "/{deal_id}",
    summary="Карточка сделки",
    description=(
        "Сделка, продукты, счётчики открытых задач и комментариев. Чужая сделка "
        "вне скоупа — 404, не 403 (раздел 3.2). Роль: чтение сделок."
    ),
    response_model=DealCardOut,
)
async def get_deal(
    session: DbSession, principal: DealRead, deal_id: Annotated[uuid.UUID, Path()]
) -> DealCardOut:
    service = DealService(session)
    deal = await service.get_or_404(deal_id, principal)
    products = await service.load_products(deal.id)
    open_tasks_count, comments_count = await service.counters(deal.id)
    await touch_recent(principal.user_id, entity_type="deal", entity_id=deal.id, title=deal.title)
    return DealCardOut(
        deal=DealOut.model_validate(deal),
        products=[DealProductOut.model_validate(p) for p in products],
        open_tasks_count=open_tasks_count,
        comments_count=comments_count,
    )


@deals_router.patch(
    "/{deal_id}",
    summary="Обновить сделку",
    description=(
        "Частичное обновление. Обязателен If-Match. `owner_id` меняется только "
        "через `/reassign`, `status_id`/`workflow_id` — только через `/transition`. "
        "Роль: обновление сделок."
    ),
    response_model=DealOut,
)
async def update_deal(
    payload: DealUpdateRequest,
    session: DbSession,
    principal: DealUpdatePerm,
    if_match: IfMatch,
    deal_id: Annotated[uuid.UUID, Path()],
) -> DealOut:
    service = DealService(session)
    deal = await service.get_or_404(deal_id, principal)
    deal = await service.update(deal, payload, expected_version=if_match)
    return DealOut.model_validate(deal)


@deals_router.get(
    "/{deal_id}/available-transitions",
    summary="Доступные переходы",
    description=(
        "Для каждого перехода — условия с флагом satisfied и человекочитаемым "
        "полем/оператором/ожидаемым и фактическим значением. Фронтенд рисует "
        "чек-лист по этому ответу и не принимает решение сам (раздел 6.6). "
        "Роль: чтение сделок."
    ),
    response_model=AvailableTransitionsResponse,
)
async def list_available_transitions(
    session: DbSession, principal: DealRead, deal_id: Annotated[uuid.UUID, Path()]
) -> AvailableTransitionsResponse:
    service = DealService(session)
    deal = await service.get_or_404(deal_id, principal)
    items = await service.available_transitions(deal, principal)
    return AvailableTransitionsResponse(
        items=[
            AvailableTransitionOut(
                id=t.id,
                name=t.name,
                to_status_id=t.to_status_id,
                requires_comment=t.requires_comment,
                role_allowed=t.role_allowed,
                satisfied=t.satisfied,
                conditions=[
                    TransitionConditionOut(
                        field=c.field,
                        op=c.op,
                        expected=c.expected,
                        actual=c.actual,
                        satisfied=c.satisfied,
                    )
                    for c in t.conditions
                ],
                actions=t.actions,
            )
            for t in items
        ]
    )


@deals_router.post(
    "/{deal_id}/transition",
    summary="Перейти по статусу",
    description=(
        "Проверяет версию, наличие перехода в опубликованном графе, роль, "
        "guard-условия; пишет историю, аудит и запускает действия перехода. "
        "Обязателен If-Match. Двойной клик по одной и той же сделке сериализуется "
        "коротким Redis-локом `deal:{id}:transition` (раздел 3.5), а не "
        "Idempotency-Key — двойной переход опасен побочными эффектами, а не "
        "дублем самой записи. Роль: переход по статусу."
    ),
    response_model=TransitionResponse,
)
async def transition_deal(
    payload: TransitionRequest,
    session: DbSession,
    principal: DealTransitionPerm,
    if_match: IfMatch,
    deal_id: Annotated[uuid.UUID, Path()],
) -> TransitionResponse:
    service = DealService(session)
    deal = await service.get_or_404(deal_id, principal)

    async with distributed_lock(f"deal:{deal_id}:transition") as acquired:
        if not acquired:
            raise AppError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "Сделка уже обрабатывает другой переход, повторите попытку",
            )
        # Другой запрос мог обновить сделку между get_or_404 и получением
        # лока — перечитываем перед проверкой версии.
        await session.refresh(deal)
        deal = await service.transition(
            deal,
            principal,
            to_status_id=payload.to_status_id,
            comment=payload.comment,
            fields=payload.fields,
            expected_version=if_match,
        )

    await touch_recent(principal.user_id, entity_type="deal", entity_id=deal.id, title=deal.title)
    return TransitionResponse(deal=DealOut.model_validate(deal))


@deals_router.get(
    "/{deal_id}/history",
    summary="История статусов и событий",
    response_model=DealHistoryResponse,
)
async def get_deal_history(
    session: DbSession, principal: DealRead, deal_id: Annotated[uuid.UUID, Path()]
) -> DealHistoryResponse:
    service = DealService(session)
    deal = await service.get_or_404(deal_id, principal)
    statuses, events = await service.history(deal.id)
    return DealHistoryResponse(
        statuses=[DealStatusHistoryOut.model_validate(s) for s in statuses],
        events=[DealEventOut.model_validate(e) for e in events],
    )


@deals_router.post(
    "/{deal_id}/reassign",
    summary="Назначить ответственного",
    description="Обязателен If-Match. Создаёт системный комментарий, событие, уведомление и аудит.",
    response_model=DealOut,
)
async def reassign_deal(
    payload: ReassignRequest,
    session: DbSession,
    principal: DealReassignPerm,
    if_match: IfMatch,
    deal_id: Annotated[uuid.UUID, Path()],
) -> DealOut:
    service = DealService(session)
    deal = await service.get_or_404(deal_id, principal)
    deal = await service.reassign(
        deal, principal, owner_id=payload.owner_id, reason=payload.reason, expected_version=if_match
    )
    return DealOut.model_validate(deal)


@deals_router.get(
    "/{deal_id}/comments", summary="Комментарии сделки", response_model=CommentListResponse
)
async def list_comments(
    session: DbSession, principal: DealRead, deal_id: Annotated[uuid.UUID, Path()]
) -> CommentListResponse:
    service = DealService(session)
    deal = await service.get_or_404(deal_id, principal)
    rows = await CommentService(session).list(deal.id)
    return CommentListResponse(items=[CommentOut.model_validate(c) for c in rows])


@deals_router.post(
    "/{deal_id}/comments",
    summary="Добавить комментарий",
    response_model=CommentOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_comment(
    payload: CommentCreateRequest,
    session: DbSession,
    principal: DealUpdatePerm,
    deal_id: Annotated[uuid.UUID, Path()],
) -> CommentOut:
    service = DealService(session)
    deal = await service.get_or_404(deal_id, principal)
    comment = await CommentService(session).create(
        deal,
        principal,
        body=payload.body,
        parent_id=payload.parent_id,
        mentions=payload.mentions,
        is_internal=payload.is_internal,
    )
    return CommentOut.model_validate(comment)


# =============================================================================
# Комментарии (плоские ручки, раздел 6.6)
# =============================================================================


@comments_router.patch(
    "/{comment_id}", summary="Редактировать комментарий", response_model=CommentOut
)
async def update_comment(
    payload: CommentUpdateRequest,
    session: DbSession,
    principal: DealUpdatePerm,
    comment_id: Annotated[uuid.UUID, Path()],
) -> CommentOut:
    comment_service = CommentService(session)
    comment = await comment_service.get_or_404(comment_id)
    await DealService(session).get_or_404(comment.deal_id, principal)
    comment = await comment_service.update(comment, principal, body=payload.body)
    return CommentOut.model_validate(comment)


@comments_router.delete(
    "/{comment_id}", summary="Удалить комментарий", response_model=OperationResult
)
async def delete_comment(
    payload: CommentDeleteRequest,
    session: DbSession,
    principal: DealUpdatePerm,
    comment_id: Annotated[uuid.UUID, Path()],
) -> OperationResult:
    comment_service = CommentService(session)
    comment = await comment_service.get_or_404(comment_id)
    await DealService(session).get_or_404(comment.deal_id, principal)
    await comment_service.delete(comment, principal, reason=payload.reason)
    return OperationResult(ok=True, detail="Комментарий удалён")


# =============================================================================
# Задачи (раздел 6.6)
# =============================================================================


@tasks_router.get("", summary="Список задач", response_model=TaskListResponse)
async def list_tasks(
    session: DbSession,
    page: Pagination,
    principal: DealRead,
    deal_id: Annotated[uuid.UUID | None, Query()] = None,
    assignee_id: Annotated[uuid.UUID | None, Query()] = None,
    task_status: Annotated[str | None, Query(alias="status")] = None,
    priority: Annotated[str | None, Query()] = None,
    due_before: Annotated[dt.datetime | None, Query()] = None,
    overdue: Annotated[bool, Query()] = False,
) -> TaskListResponse:
    filters = TaskFilters(
        deal_id=deal_id,
        assignee_id=assignee_id,
        status=task_status,
        priority=priority,
        due_before=due_before,
        overdue=overdue,
    )
    scope_clause = await deal_scope_clause(session, principal)
    stmt = TaskService(session).list_query(filters, scope_clause).order_by(
        Task.created_at.desc(), Task.id.desc()
    )
    cursor = page.decoded_cursor
    if cursor:
        stmt = stmt.where(keyset_before(Task.created_at, Task.id, cursor))
    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())
    built = Page.build(rows, limit=page.limit, serializer=TaskOut.model_validate)
    return TaskListResponse(items=built.items, next_cursor=built.next_cursor)


@tasks_router.post(
    "", summary="Создать задачу", response_model=TaskOut, status_code=status.HTTP_201_CREATED
)
async def create_task(
    payload: TaskCreateRequest, session: DbSession, principal: DealUpdatePerm
) -> TaskOut:
    # Проверяет и существование, и скоуп родительской сделки.
    deal = await DealService(session).get_or_404(payload.deal_id, principal)
    task = await TaskService(session).create(
        principal,
        deal_id=deal.id,
        title=payload.title,
        description=payload.description,
        assignee_id=payload.assignee_id,
        due_at=payload.due_at,
        priority=payload.priority,
    )
    return TaskOut.model_validate(task)


@tasks_router.patch("/{task_id}", summary="Обновить задачу", response_model=TaskOut)
async def update_task(
    payload: TaskUpdateRequest,
    session: DbSession,
    principal: DealUpdatePerm,
    task_id: Annotated[uuid.UUID, Path()],
) -> TaskOut:
    task_service = TaskService(session)
    task = await task_service.get_or_404(task_id)
    await DealService(session).get_or_404(task.deal_id, principal)
    task = await task_service.update(task, payload)
    return TaskOut.model_validate(task)


@tasks_router.post("/{task_id}/complete", summary="Завершить задачу", response_model=TaskOut)
async def complete_task(
    session: DbSession, principal: DealUpdatePerm, task_id: Annotated[uuid.UUID, Path()]
) -> TaskOut:
    task_service = TaskService(session)
    task = await task_service.get_or_404(task_id)
    await DealService(session).get_or_404(task.deal_id, principal)
    task = await task_service.complete(task, principal)
    return TaskOut.model_validate(task)
