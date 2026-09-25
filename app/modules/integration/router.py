"""Административный контур интеграций (раздел 5: «Настройки интеграций» —
только ADMIN). Списки `outbox_events`/`inbound_messages`/`external_refs` —
не из раздела 8 буквально, но без них включение `bitrix_connector_enabled`
на живом стенде нечем было бы проверить, кроме прямых SQL-запросов — тот же
довод, что уже оправдал чтение `import_row_results`/`signature_documents`
через API в предыдущих спринтах."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy import select

from app.core.deps import DbSession, require_permission
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
from app.modules.integration.service import IntegrationSourceService

IntegrationAdminPerm = Annotated[
    Principal, Depends(require_permission(Permission.INTEGRATION_ADMIN))
]

integration_admin_router = APIRouter(prefix="/admin/integrations", tags=["integrations-admin"])


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


@integration_admin_router.get("/outbox-events", response_model=list[OutboxEventOut])
async def list_outbox_events(
    session: DbSession,
    _principal: IntegrationAdminPerm,
    status: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(le=200)] = 50,
) -> list[OutboxEventOut]:
    stmt = select(OutboxEvent).order_by(OutboxEvent.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(OutboxEvent.status == status)
    rows = (await session.execute(stmt)).scalars().all()
    return [OutboxEventOut.model_validate(r) for r in rows]


@integration_admin_router.get("/inbound-messages", response_model=list[InboundMessageOut])
async def list_inbound_messages(
    session: DbSession,
    _principal: IntegrationAdminPerm,
    source_code: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(le=200)] = 50,
) -> list[InboundMessageOut]:
    stmt = select(InboundMessage).order_by(InboundMessage.received_at.desc()).limit(limit)
    if source_code:
        stmt = stmt.where(InboundMessage.source_code == source_code)
    rows = (await session.execute(stmt)).scalars().all()
    return [InboundMessageOut.model_validate(r) for r in rows]


@integration_admin_router.get("/external-refs", response_model=list[ExternalRefOut])
async def list_external_refs(
    session: DbSession,
    _principal: IntegrationAdminPerm,
    source_code: Annotated[str | None, Query()] = None,
    entity_type: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(le=200)] = 50,
) -> list[ExternalRefOut]:
    stmt = select(ExternalRef).order_by(ExternalRef.last_synced_at.desc().nulls_last()).limit(limit)
    if source_code:
        stmt = stmt.where(ExternalRef.source_code == source_code)
    if entity_type:
        stmt = stmt.where(ExternalRef.entity_type == entity_type)
    rows = (await session.execute(stmt)).scalars().all()
    return [ExternalRefOut.model_validate(r) for r in rows]
