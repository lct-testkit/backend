"""Административный контур интеграций (раздел 5: «Настройки интеграций» —
только ADMIN). Списки `outbox_events`/`inbound_messages`/`external_refs` —
не из раздела 8 буквально, но без них включение `bitrix_connector_enabled`
на живом стенде нечем было бы проверить, кроме прямых SQL-запросов — тот же
довод, что уже оправдал чтение `import_row_results`/`signature_documents`
через API в предыдущих спринтах.

Списки отдают массив, как и раньше, а продолжение — курсором: `cursor` в запросе и
заголовок `X-Next-Cursor` в ответе (его нет на последней странице). Так форма тела
не меняется; порядок — от новых к старым, границы периода `from` (включительно) и
`to` (исключительно) — по времени создания/получения/синхронизации записи."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Response
from sqlalchemy import DateTime, func, literal, select

from app.core.deps import DbSession, require_permission
from app.core.pagination import Cursor, Page, keyset_before
from app.core.permissions import Permission
from app.core.security import Principal
from app.modules.integration.models import ExternalRef, InboundMessage, OutboxEvent
from app.modules.integration.schemas import (
    ExternalRefOut,
    InboundMessageOut,
    IntegrationSourceOut,
    IntegrationSourceUpdateRequest,
    OutboxEventOut,
)
from app.modules.integration.service import IntegrationSourceService, OutboxEventService

IntegrationAdminPerm = Annotated[
    Principal, Depends(require_permission(Permission.INTEGRATION_ADMIN))
]

integration_admin_router = APIRouter(prefix="/admin/integrations", tags=["integrations-admin"])

NEXT_CURSOR_HEADER = "X-Next-Cursor"
# У записи без синхронизации нет времени: в сортировке она идёт последней.
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
_PAGED: dict[int | str, dict[str, Any]] = {
    200: {
        "headers": {
            NEXT_CURSOR_HEADER: {
                "description": "Курсор следующей страницы; на последней странице заголовка нет.",
                "schema": {"type": "string"},
            }
        }
    }
}

CursorParam = Annotated[str | None, Query(description="Курсор из `X-Next-Cursor` прошлой страницы")]
FromParam = Annotated[
    dt.datetime | None, Query(alias="from", description="Не раньше (включительно)")
]
ToParam = Annotated[dt.datetime | None, Query(alias="to", description="Раньше (исключительно)")]
LimitParam = Annotated[int, Query(ge=1, le=200)]


def _page(
    response: Response, rows: list[Any], limit: int, sort_value: Callable[[Any], Any]
) -> list[Any]:
    """Отрезает лишнюю строку и кладёт курсор следующей страницы в заголовок."""
    built: Page = Page.build(rows, limit=limit, cursor_value=sort_value)
    if built.next_cursor:
        response.headers[NEXT_CURSOR_HEADER] = built.next_cursor
    return list(built.items)


@integration_admin_router.get("/sources", response_model=list[IntegrationSourceOut])
async def list_sources(
    session: DbSession, _principal: IntegrationAdminPerm
) -> list[IntegrationSourceOut]:
    sources = await IntegrationSourceService(session).list_all()
    return [IntegrationSourceOut.model_validate(s) for s in sources]


@integration_admin_router.patch("/sources/{code}", response_model=IntegrationSourceOut)
async def update_source(
    session: DbSession,
    _principal: IntegrationAdminPerm,
    code: Annotated[str, Path()],
    payload: IntegrationSourceUpdateRequest,
) -> IntegrationSourceOut:
    source = await IntegrationSourceService(session).update(code, payload)
    return IntegrationSourceOut.model_validate(source)


@integration_admin_router.get(
    "/outbox-events",
    summary="Исходящие события",
    description=(
        "Очередь доставки, новые первыми. Фильтры: `status`, период `from`/`to` по "
        "`created_at`. Продолжение — `cursor` из заголовка `X-Next-Cursor`."
    ),
    response_model=list[OutboxEventOut],
    responses=_PAGED,
)
async def list_outbox_events(
    session: DbSession,
    response: Response,
    _principal: IntegrationAdminPerm,
    status: Annotated[str | None, Query()] = None,
    date_from: FromParam = None,
    date_to: ToParam = None,
    limit: LimitParam = 50,
    cursor: CursorParam = None,
) -> list[OutboxEventOut]:
    stmt = select(OutboxEvent).order_by(OutboxEvent.created_at.desc(), OutboxEvent.id.desc())
    if status:
        stmt = stmt.where(OutboxEvent.status == status)
    if date_from:
        stmt = stmt.where(OutboxEvent.created_at >= date_from)
    if date_to:
        stmt = stmt.where(OutboxEvent.created_at < date_to)
    if cursor:
        stmt = stmt.where(
            keyset_before(OutboxEvent.created_at, OutboxEvent.id, Cursor.decode(cursor))
        )
    rows = list((await session.execute(stmt.limit(limit + 1))).scalars().all())
    visible = _page(response, rows, limit, lambda r: r.created_at)
    return [OutboxEventOut.model_validate(r) for r in visible]


@integration_admin_router.post(
    "/outbox-events/{event_id}/retry",
    summary="Повторить доставку события",
    description=(
        "Возвращает событие из `failed`/`dead` в очередь: статус `pending`, счётчик "
        "попыток и ошибка сброшены, доставка — на ближайшем тике воркера. Для "
        "остальных статусов — 409 (CRM-1704). Причина сбоя должна быть устранена до "
        "повтора, например включён источник. Пишется в аудит. Роль: ADMIN."
    ),
    response_model=OutboxEventOut,
)
async def retry_outbox_event(
    session: DbSession,
    _principal: IntegrationAdminPerm,
    event_id: Annotated[uuid.UUID, Path()],
) -> OutboxEventOut:
    event = await OutboxEventService(session).retry(event_id)
    return OutboxEventOut.model_validate(event)


@integration_admin_router.get(
    "/inbound-messages",
    summary="Входящие сообщения",
    description=(
        "Журнал входящих вызовов, новые первыми. Фильтры: `source_code`, период `from`/`to` "
        "по `received_at`. Продолжение — `cursor` из заголовка `X-Next-Cursor`."
    ),
    response_model=list[InboundMessageOut],
    responses=_PAGED,
)
async def list_inbound_messages(
    session: DbSession,
    response: Response,
    _principal: IntegrationAdminPerm,
    source_code: Annotated[str | None, Query()] = None,
    date_from: FromParam = None,
    date_to: ToParam = None,
    limit: LimitParam = 50,
    cursor: CursorParam = None,
) -> list[InboundMessageOut]:
    stmt = select(InboundMessage).order_by(
        InboundMessage.received_at.desc(), InboundMessage.id.desc()
    )
    if source_code:
        stmt = stmt.where(InboundMessage.source_code == source_code)
    if date_from:
        stmt = stmt.where(InboundMessage.received_at >= date_from)
    if date_to:
        stmt = stmt.where(InboundMessage.received_at < date_to)
    if cursor:
        stmt = stmt.where(
            keyset_before(InboundMessage.received_at, InboundMessage.id, Cursor.decode(cursor))
        )
    rows = list((await session.execute(stmt.limit(limit + 1))).scalars().all())
    visible = _page(response, rows, limit, lambda r: r.received_at)
    return [InboundMessageOut.model_validate(r) for r in visible]


@integration_admin_router.get(
    "/external-refs",
    summary="Связи с внешними системами",
    description=(
        "Соответствие «наша сущность — id во внешней системе», недавно синхронизированные "
        "первыми (несинхронизированные — в конце). Фильтры: `source_code`, `entity_type`, "
        "период `from`/`to` по `last_synced_at`. Продолжение — `cursor` из заголовка "
        "`X-Next-Cursor`."
    ),
    response_model=list[ExternalRefOut],
    responses=_PAGED,
)
async def list_external_refs(
    session: DbSession,
    response: Response,
    _principal: IntegrationAdminPerm,
    source_code: Annotated[str | None, Query()] = None,
    entity_type: Annotated[str | None, Query()] = None,
    date_from: FromParam = None,
    date_to: ToParam = None,
    limit: LimitParam = 50,
    cursor: CursorParam = None,
) -> list[ExternalRefOut]:
    synced = func.coalesce(ExternalRef.last_synced_at, literal(_EPOCH, DateTime(timezone=True)))
    stmt = select(ExternalRef).order_by(synced.desc(), ExternalRef.id.desc())
    if source_code:
        stmt = stmt.where(ExternalRef.source_code == source_code)
    if entity_type:
        stmt = stmt.where(ExternalRef.entity_type == entity_type)
    if date_from:
        stmt = stmt.where(ExternalRef.last_synced_at >= date_from)
    if date_to:
        stmt = stmt.where(ExternalRef.last_synced_at < date_to)
    if cursor:
        stmt = stmt.where(keyset_before(synced, ExternalRef.id, Cursor.decode(cursor)))
    rows = list((await session.execute(stmt.limit(limit + 1))).scalars().all())
    visible = _page(response, rows, limit, lambda r: r.last_synced_at or _EPOCH)
    return [ExternalRefOut.model_validate(r) for r in visible]
