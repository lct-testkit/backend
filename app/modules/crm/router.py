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
from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request, status

from app.core.deps import (
    AuditDep,
    DbSession,
    IdempotencyKeyHeader,
    IfMatch,
    Pagination,
    require_permission,
)
from app.core.errors import AppError, ErrorCode, NotFoundError
from app.core.idempotency import IdempotencyGuard
from app.core.pagination import MAX_LIMIT, Cursor, Page, keyset_before
from app.core.permissions import Permission
from app.core.redis_client import distributed_lock
from app.core.security import Principal
from app.modules.audit.actions import AuditAction
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
    DealProductsReplaceRequest,
    DealStatusHistoryOut,
    DealUpdateRequest,
    ParticipantAddRequest,
    ParticipantListResponse,
    ParticipantOut,
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
    ParticipantService,
    TaskFilters,
    TaskService,
    apply_deal_order,
    deal_scope_clause,
    get_cached_deal_card,
    parse_deal_sort,
    set_cached_deal_card,
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
        "SLA/датам/тексту. `status_id` можно повторять (`?status_id=a&status_id=b`), "
        "`is_closed` отделяет закрытые сделки (won/lost/parked) от открытых. "
        "`sort` — поле сортировки, минус в начале — по убыванию: created_at "
        "(по умолчанию `-created_at`), updated_at, status_changed_at, number, "
        "title, amount, expected_close_date, sla_due_at; сделки без значения "
        "всегда в конце. `total` — сколько сделок подходит под фильтры, без "
        "учёта курсора. Скоуп по роли (раздел 4): KAM — свои сделки, HEAD — "
        "команда, ADMIN — все. Роль: чтение сделок."
    ),
    response_model=DealListResponse,
)
async def list_deals(
    session: DbSession,
    page: Pagination,
    principal: DealRead,
    status_id: Annotated[list[uuid.UUID] | None, Query()] = None,
    is_closed: Annotated[bool | None, Query()] = None,
    sort: Annotated[str | None, Query()] = None,
    workflow_id: Annotated[uuid.UUID | None, Query()] = None,
    deal_type: Annotated[str | None, Query()] = None,
    organization_id: Annotated[uuid.UUID | None, Query()] = None,
    contact_id: Annotated[uuid.UUID | None, Query()] = None,
    owner_id: Annotated[uuid.UUID | None, Query()] = None,
    product_id: Annotated[uuid.UUID | None, Query()] = None,
    direction_id: Annotated[uuid.UUID | None, Query()] = None,
    region_id: Annotated[uuid.UUID | None, Query()] = None,
    priority: Annotated[str | None, Query()] = None,
    sla_state: Annotated[str | None, Query()] = None,
    created_from: Annotated[dt.datetime | None, Query()] = None,
    created_to: Annotated[dt.datetime | None, Query()] = None,
    closed_from: Annotated[dt.datetime | None, Query()] = None,
    closed_to: Annotated[dt.datetime | None, Query()] = None,
    q: Annotated[str | None, Query()] = None,
) -> DealListResponse:
    filters = DealFilters(
        status_ids=status_id,
        is_closed=is_closed,
        workflow_id=workflow_id,
        deal_type=deal_type,
        organization_id=organization_id,
        contact_id=contact_id,
        owner_id=owner_id,
        product_id=product_id,
        direction_id=direction_id,
        region_id=region_id,
        priority=priority,
        sla_state=sla_state,
        created_from=created_from,
        created_to=created_to,
        closed_from=closed_from,
        closed_to=closed_to,
        q=q,
    )
    service = DealService(session)
    sort_by = parse_deal_sort(sort)
    filtered = await service.list_query(principal, filters)
    total = await service.count(filtered)
    stmt = apply_deal_order(filtered, sort_by, page.decoded_cursor)
    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())
    visible = rows[: page.limit]
    next_cursor = (
        Cursor(value=getattr(visible[-1], sort_by.field), id=visible[-1].id).encode()
        if len(rows) > page.limit
        else None
    )
    return DealListResponse(
        items=await _deal_outs(service, principal, visible), next_cursor=next_cursor, total=total
    )


@deals_router.post(
    "",
    summary="Создать сделку",
    description=(
        "Для B2B обязателен organization_id, для B2C — contact_id (new_spec §4.9). "
        "Сделка получает начальный статус опубликованной воронки по умолчанию для "
        "своего типа (или явно переданного workflow_id), номер, событие CREATED и "
        "аудит. Ответственный (owner_id): без него или свой — сам создатель; чужого "
        "назначает ADMIN (любого активного сотрудника) и HEAD (из своей команды), "
        "остальным — 403. Поддерживает Idempotency-Key. Роль: создание сделок."
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

    service = DealService(session)
    deal = await service.create(principal, payload)
    result = (await _deal_outs(service, principal, [deal]))[0]

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
        "вне скоупа — 404, не 403 (раздел 3.2). Кэшируется по версии сделки "
        "(`cache:deal:{id}:v{version}`, раздел 3.4/16). Роль: чтение сделок."
    ),
    response_model=DealCardOut,
)
async def get_deal(
    session: DbSession,
    principal: DealRead,
    audit: AuditDep,
    deal_id: Annotated[uuid.UUID, Path()],
) -> DealCardOut:
    service = DealService(session)
    # Скоуп проверяется здесь всегда, даже на кэш-хит ниже: кэш экономит
    # только повторные запросы продуктов/счётчиков, не сам контроль доступа.
    deal = await service.get_or_404(deal_id, principal)

    if principal.is_admin:
        # new_spec §3.2: «каждое чтение карточки админом логируется отдельным
        # типом события PII_ACCESS» — независимо от того, откуда дальше
        # берутся данные, кэша или БД.
        await audit.record(AuditAction.PII_ACCESS, entity_type="deal", entity_id=deal.id)

    await touch_recent(principal.user_id, entity_type="deal", entity_id=deal.id, title=deal.title)

    cached = await get_cached_deal_card(deal.id, deal.version)
    if cached is not None:
        card = DealCardOut.model_validate(cached)
    else:
        card = await _build_card(service, deal)
        await set_cached_deal_card(deal.id, deal.version, card.model_dump(mode="json"))
    # Названия в кэш не попадают: переименование организации версию сделки не меняет.
    card.deal = (await _deal_outs(service, principal, [deal]))[0]
    return card


async def _build_card(service: DealService, deal: Deal) -> DealCardOut:
    products = await service.load_products(deal.id)
    open_tasks_count, comments_count = await service.counters(deal.id)
    return DealCardOut(
        deal=DealOut.model_validate(deal),
        products=[DealProductOut.model_validate(p) for p in products],
        open_tasks_count=open_tasks_count,
        comments_count=comments_count,
    )


async def _deal_outs(
    service: DealService, principal: Principal, deals: Sequence[Deal]
) -> list[DealOut]:
    """`DealOut` с названием организации и именем контакта — по запросу на тип, не на сделку."""
    organizations, contacts = await service.party_names(principal, deals)
    return [
        DealOut.model_validate(deal).model_copy(
            update={
                "organization_name": organizations.get(deal.organization_id),
                "contact_name": contacts.get(deal.contact_id),
            }
        )
        for deal in deals
    ]


@deals_router.patch(
    "/{deal_id}",
    summary="Обновить сделку",
    description=(
        "Частичное обновление. Обязателен If-Match (версия проверяется атомарно: "
        "параллельная правка с той же версией — 409 CRM-1002). `null` в обязательных "
        "полях (title, currency, priority) — 422. Участник-наблюдатель (watcher) "
        "менять сделку не может — 403. `owner_id` меняется только через `/reassign`, "
        "`status_id`/`workflow_id` — только через `/transition`. "
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
    deal = await service.get_or_404(deal_id, principal, write=True)
    deal = await service.update(deal, payload, expected_version=if_match, principal=principal)
    return (await _deal_outs(service, principal, [deal]))[0]


@deals_router.put(
    "/{deal_id}/products",
    summary="Заменить продукты сделки",
    description=(
        "Тело — полный новый список продуктов сделки (пустой очищает). Обязателен "
        "If-Match; версия сделки растёт. Ответ — карточка сделки с новым списком. "
        "Роль: обновление сделок."
    ),
    response_model=DealCardOut,
)
async def replace_deal_products(
    payload: DealProductsReplaceRequest,
    session: DbSession,
    principal: DealUpdatePerm,
    if_match: IfMatch,
    deal_id: Annotated[uuid.UUID, Path()],
) -> DealCardOut:
    service = DealService(session)
    deal = await service.get_or_404(deal_id, principal, write=True)
    await service.replace_products(deal, payload.items, expected_version=if_match)
    card = await _build_card(service, deal)
    card.deal = (await _deal_outs(service, principal, [deal]))[0]
    return card


@deals_router.get(
    "/{deal_id}/available-transitions",
    summary="Доступные переходы",
    description=(
        "Для каждого перехода — условия с флагом satisfied и человекочитаемым "
        "полем/оператором/ожидаемым и фактическим значением, а также те же "
        "условия деревом all/any (conditions_tree) с satisfied на каждом узле: "
        "по плоскому списку не понять, что достаточно одного из условий any. "
        "Фронтенд рисует чек-лист по этому ответу и не принимает решение сам "
        "(раздел 6.6). Роль: чтение сделок."
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
                conditions_tree=t.conditions_tree,
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
        "guard-условия и обязательные поля целевого статуса (не заполнены — 422 "
        "CRM-1205 с перечнем полей; закрыть их можно через `fields`); пишет "
        "историю, аудит и запускает действия перехода. "
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
    deal = await service.get_or_404(deal_id, principal, write=True)

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
    return TransitionResponse(deal=(await _deal_outs(service, principal, [deal]))[0])


@deals_router.get(
    "/{deal_id}/history",
    summary="История статусов и событий",
    description=(
        "Без `limit` — целиком. С `limit` (1–100) каждый из двух списков отдаётся "
        "страницей в хронологическом порядке; продолжение — `statuses_cursor` / "
        "`events_cursor` из `next_statuses_cursor` / `next_events_cursor` "
        "предыдущего ответа, у списков курсоры независимы. Роль: чтение сделок."
    ),
    response_model=DealHistoryResponse,
)
async def get_deal_history(
    session: DbSession,
    principal: DealRead,
    deal_id: Annotated[uuid.UUID, Path()],
    limit: Annotated[int | None, Query(ge=1, le=MAX_LIMIT)] = None,
    statuses_cursor: Annotated[str | None, Query()] = None,
    events_cursor: Annotated[str | None, Query()] = None,
) -> DealHistoryResponse:
    service = DealService(session)
    deal = await service.get_or_404(deal_id, principal)
    history = await service.history(
        deal.id,
        limit=limit,
        statuses_cursor=Cursor.decode(statuses_cursor) if statuses_cursor else None,
        events_cursor=Cursor.decode(events_cursor) if events_cursor else None,
    )
    return DealHistoryResponse(
        statuses=[DealStatusHistoryOut.model_validate(s) for s in history.statuses],
        events=[DealEventOut.model_validate(e) for e in history.events],
        next_statuses_cursor=history.next_statuses_cursor,
        next_events_cursor=history.next_events_cursor,
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
    deal = await service.get_or_404(deal_id, principal, write=True)
    deal = await service.reassign(
        deal, principal, owner_id=payload.owner_id, reason=payload.reason, expected_version=if_match
    )
    return (await _deal_outs(service, principal, [deal]))[0]


# =============================================================================
# Участники (раздел 5.5) — даёт видимость сверх ownership для KAM
# =============================================================================


@deals_router.get(
    "/{deal_id}/participants", summary="Участники сделки", response_model=ParticipantListResponse
)
async def list_participants(
    session: DbSession, principal: DealRead, deal_id: Annotated[uuid.UUID, Path()]
) -> ParticipantListResponse:
    deal = await DealService(session).get_or_404(deal_id, principal)
    rows = await ParticipantService(session).list(deal.id)
    return ParticipantListResponse(items=[ParticipantOut.model_validate(p) for p in rows])


@deals_router.post(
    "/{deal_id}/participants",
    summary="Добавить участника",
    response_model=ParticipantOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_participant(
    payload: ParticipantAddRequest,
    session: DbSession,
    principal: DealUpdatePerm,
    deal_id: Annotated[uuid.UUID, Path()],
) -> ParticipantOut:
    deal = await DealService(session).get_or_404(deal_id, principal, write=True)
    participant = await ParticipantService(session).add(
        deal, principal, user_id=payload.user_id, role_in_deal=payload.role_in_deal
    )
    return ParticipantOut.model_validate(participant)


@deals_router.delete(
    "/{deal_id}/participants/{participant_id}",
    summary="Удалить участника",
    response_model=OperationResult,
)
async def remove_participant(
    session: DbSession,
    principal: DealUpdatePerm,
    deal_id: Annotated[uuid.UUID, Path()],
    participant_id: Annotated[uuid.UUID, Path()],
) -> OperationResult:
    deal = await DealService(session).get_or_404(deal_id, principal, write=True)
    service = ParticipantService(session)
    participant = await service.get_or_404(participant_id)
    if participant.deal_id != deal_id:
        raise NotFoundError("Участник сделки", participant_id)
    await service.remove(deal, principal, participant)
    return OperationResult(ok=True, detail="Участник удалён")


@deals_router.get(
    "/{deal_id}/comments",
    summary="Комментарии сделки",
    description=(
        "Страница в хронологическом порядке: без `limit` — первые 100, с `limit` (1–100) — "
        "столько; продолжение — `cursor` из `next_cursor` предыдущего ответа. "
        "Роль: чтение сделок."
    ),
    response_model=CommentListResponse,
)
async def list_comments(
    session: DbSession,
    principal: DealRead,
    deal_id: Annotated[uuid.UUID, Path()],
    limit: Annotated[int | None, Query(ge=1, le=MAX_LIMIT)] = None,
    cursor: Annotated[str | None, Query()] = None,
) -> CommentListResponse:
    service = DealService(session)
    deal = await service.get_or_404(deal_id, principal)
    rows, next_cursor = await CommentService(session).list(
        deal.id, limit=limit, cursor=Cursor.decode(cursor) if cursor else None
    )
    return CommentListResponse(
        items=[CommentOut.model_validate(c) for c in rows], next_cursor=next_cursor
    )


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
    deal = await service.get_or_404(deal_id, principal, write=True)
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
    await DealService(session).get_or_404(comment.deal_id, principal, write=True)
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
    await DealService(session).get_or_404(comment.deal_id, principal, write=True)
    await comment_service.delete(comment, principal, reason=payload.reason)
    return OperationResult(ok=True, detail="Комментарий удалён")


# =============================================================================
# Задачи (раздел 6.6)
# =============================================================================


def _task_out(task: Task, deal_number: str | None, deal_title: str | None) -> TaskOut:
    return TaskOut.model_validate(task).model_copy(
        update={"deal_number": deal_number, "deal_title": deal_title}
    )


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
    task_service = TaskService(session)
    stmt = task_service.list_query(filters, scope_clause).order_by(
        Task.created_at.desc(), Task.id.desc()
    )
    cursor = page.decoded_cursor
    if cursor:
        stmt = stmt.where(keyset_before(Task.created_at, Task.id, cursor))
    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())
    deals = await task_service.deal_titles({task.deal_id for task in rows})
    built = Page.build(
        rows,
        limit=page.limit,
        serializer=lambda task: _task_out(task, *deals.get(task.deal_id, (None, None))),
    )
    return TaskListResponse(items=built.items, next_cursor=built.next_cursor)


@tasks_router.post(
    "", summary="Создать задачу", response_model=TaskOut, status_code=status.HTTP_201_CREATED
)
async def create_task(
    payload: TaskCreateRequest, session: DbSession, principal: DealUpdatePerm
) -> TaskOut:
    # Проверяет и существование, и скоуп родительской сделки.
    deal = await DealService(session).get_or_404(payload.deal_id, principal, write=True)
    task = await TaskService(session).create(
        principal,
        deal_id=deal.id,
        title=payload.title,
        description=payload.description,
        assignee_id=payload.assignee_id,
        due_at=payload.due_at,
        priority=payload.priority,
    )
    return _task_out(task, deal.number, deal.title)


@tasks_router.patch("/{task_id}", summary="Обновить задачу", response_model=TaskOut)
async def update_task(
    payload: TaskUpdateRequest,
    session: DbSession,
    principal: DealUpdatePerm,
    task_id: Annotated[uuid.UUID, Path()],
) -> TaskOut:
    task_service = TaskService(session)
    task = await task_service.get_or_404(task_id)
    deal = await DealService(session).get_or_404(task.deal_id, principal, write=True)
    task = await task_service.update(task, principal, payload)
    return _task_out(task, deal.number, deal.title)


@tasks_router.post("/{task_id}/complete", summary="Завершить задачу", response_model=TaskOut)
async def complete_task(
    session: DbSession, principal: DealUpdatePerm, task_id: Annotated[uuid.UUID, Path()]
) -> TaskOut:
    task_service = TaskService(session)
    task = await task_service.get_or_404(task_id)
    deal = await DealService(session).get_or_404(task.deal_id, principal, write=True)
    task = await task_service.complete(task, principal)
    return _task_out(task, deal.number, deal.title)
