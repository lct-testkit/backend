"""Реестр инцидентов безопасности (B6-12, 152-ФЗ ст. 21 ч. 3.1).

Отдельный роутер, не `identity/router_admin.py`: права `incident:manage`/`incident:read`
(не `user:*`), чтобы попасть в явный список обязательных прав B6-15 отдельной строкой, а не
потеряться среди администрирования пользователей.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query

from app.core.deps import DbSession, IfMatch, Pagination, require_permission
from app.core.errors import AppError, ErrorCode
from app.core.pagination import Page, keyset_before
from app.core.permissions import Permission
from app.core.security import Principal
from app.modules.identity.incident_service import IncidentFilters, IncidentService
from app.modules.identity.models import SecurityIncident
from app.modules.identity.schemas import (
    IncidentCloseRequest,
    IncidentConfirmRequest,
    IncidentCreateRequest,
    IncidentListResponse,
    IncidentMarkSentRequest,
    IncidentOut,
    IncidentPatchRequest,
    IncidentRknNoticeOut,
    IncidentSubjectsNoticeOut,
)

router = APIRouter(prefix="/admin/incidents", tags=["admin"])

IncidentManage = Annotated[Principal, Depends(require_permission(Permission.INCIDENT_MANAGE))]
IncidentRead = Annotated[Principal, Depends(require_permission(Permission.INCIDENT_READ))]


@router.get(
    "",
    summary="Список инцидентов",
    description="Реестр инцидентов безопасности с фильтром по статусу. Роль: ADMIN, AUDITOR.",
    response_model=IncidentListResponse,
)
async def list_incidents(
    session: DbSession,
    _: IncidentRead,
    page: Pagination,
    status_filter: Annotated[list[str] | None, Query(alias="status")] = None,
) -> IncidentListResponse:
    stmt = (
        IncidentService(session)
        .list_query(IncidentFilters(status=status_filter))
        .order_by(SecurityIncident.created_at.desc(), SecurityIncident.id.desc())
    )
    cursor = page.decoded_cursor
    if cursor:
        stmt = stmt.where(keyset_before(SecurityIncident.created_at, SecurityIncident.id, cursor))
    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())
    built: Page = Page.build(rows, limit=page.limit, serializer=IncidentOut.model_validate)
    return IncidentListResponse(items=built.items, next_cursor=built.next_cursor)


@router.post(
    "",
    summary="Завести инцидент вручную",
    description=(
        "Ручное заведение (минуя автоматический кандидат по событию безопасности). Роль: ADMIN."
    ),
    response_model=IncidentOut,
    status_code=201,
)
async def create_incident(
    payload: IncidentCreateRequest, session: DbSession, principal: IncidentManage
) -> IncidentOut:
    incident = await IncidentService(session).create_manual(
        title=payload.title,
        severity=payload.severity,
        description=payload.description,
        data_categories=payload.data_categories,
        subjects_estimate=payload.subjects_estimate,
        created_by=principal.user_id,
    )
    return IncidentOut.model_validate(incident)


@router.get(
    "/{incident_id}",
    summary="Карточка инцидента",
    description="Роль: ADMIN, AUDITOR.",
    response_model=IncidentOut,
)
async def get_incident(
    session: DbSession, _: IncidentRead, incident_id: Annotated[uuid.UUID, Path()]
) -> IncidentOut:
    incident = await IncidentService(session).get_or_404(incident_id)
    return IncidentOut.model_validate(incident)


@router.patch(
    "/{incident_id}",
    summary="Править карточку инцидента",
    description="Обязателен `If-Match`. Роль: ADMIN.",
    response_model=IncidentOut,
)
async def patch_incident(
    payload: IncidentPatchRequest,
    session: DbSession,
    _: IncidentManage,
    if_match: IfMatch,
    incident_id: Annotated[uuid.UUID, Path()],
) -> IncidentOut:
    service = IncidentService(session)
    incident = await service.get_or_404(incident_id)
    updates = payload.model_dump(exclude_unset=True)
    updated = await service.update(incident, updates, if_match)
    return IncidentOut.model_validate(updated)


@router.post(
    "/{incident_id}/confirm",
    summary="Подтвердить кандидата",
    description=(
        "Кандидат → `open`: отсюда считаются сроки РКН 24 и 72 часа. Обязателен `If-Match`. "
        "Роль: ADMIN."
    ),
    response_model=IncidentOut,
)
async def confirm_incident(
    payload: IncidentConfirmRequest,
    session: DbSession,
    _: IncidentManage,
    if_match: IfMatch,
    incident_id: Annotated[uuid.UUID, Path()],
) -> IncidentOut:
    service = IncidentService(session)
    incident = await service.get_or_404(incident_id)
    updated = await service.confirm(
        incident,
        detected_at=payload.detected_at,
        data_categories=payload.data_categories,
        subjects_estimate=payload.subjects_estimate,
        causes=payload.causes,
        harm=payload.harm,
        measures=payload.measures,
        responsible_user_id=payload.responsible_user_id,
        expected_version=if_match,
    )
    return IncidentOut.model_validate(updated)


@router.post(
    "/{incident_id}/rkn-notice/{stage}",
    summary="Сформировать текст уведомления РКН",
    description=(
        "Генерирует PDF и структурированные поля для портала РКН (stage: 24h или 72h; "
        "72h — после отметки об отправке 24h). Шаблон помечен как проект, требующий "
        "утверждения юристом (B6-12/B6-14). Роль: ADMIN."
    ),
    response_model=IncidentRknNoticeOut,
)
async def generate_rkn_notice(
    session: DbSession,
    _: IncidentManage,
    incident_id: Annotated[uuid.UUID, Path()],
    stage: Annotated[str, Path(pattern="^(24h|72h)$")],
) -> IncidentRknNoticeOut:
    service = IncidentService(session)
    incident = await service.get_or_404(incident_id)
    file, fields = await service.generate_rkn_notice(incident, stage=stage)
    return IncidentRknNoticeOut(
        stage=stage, due_at=fields.get("due_at"), file_id=file.id, fields=fields
    )


@router.post(
    "/{incident_id}/rkn-notice/{stage}/sent",
    summary="Отметить отправку уведомления РКН",
    description=(
        "Фиксирует номер/ссылку подачи (подачу делает человек на портале). "
        "Обязателен `If-Match`. Роль: ADMIN."
    ),
    response_model=IncidentOut,
)
async def mark_rkn_notice_sent(
    payload: IncidentMarkSentRequest,
    session: DbSession,
    _: IncidentManage,
    if_match: IfMatch,
    incident_id: Annotated[uuid.UUID, Path()],
    stage: Annotated[str, Path(pattern="^(24h|72h)$")],
) -> IncidentOut:
    service = IncidentService(session)
    incident = await service.get_or_404(incident_id)
    updated = await service.mark_sent(
        incident,
        stage=stage,
        sent_at=payload.sent_at,
        reference=payload.reference,
        expected_version=if_match,
    )
    return IncidentOut.model_validate(updated)


@router.post(
    "/{incident_id}/subjects-notice",
    summary="Сформировать уведомление субъектам",
    description=(
        "Решение, уведомлять ли субъектов лично, принимает юрист по обстоятельствам "
        "конкретного инцидента — ручка только готовит текст и файл. Роль: ADMIN."
    ),
    response_model=IncidentSubjectsNoticeOut,
)
async def generate_subjects_notice(
    session: DbSession, _: IncidentManage, incident_id: Annotated[uuid.UUID, Path()]
) -> IncidentSubjectsNoticeOut:
    service = IncidentService(session)
    incident = await service.get_or_404(incident_id)
    if incident.subjects_notified_at is not None:
        raise AppError(ErrorCode.DUPLICATE, "Уведомление субъектам уже сформировано")
    file = await service.generate_subjects_notice(incident)
    return IncidentSubjectsNoticeOut(
        file_id=file.id, subjects_notified_at=incident.subjects_notified_at
    )


@router.post(
    "/{incident_id}/close",
    summary="Закрыть инцидент",
    description=(
        "Кандидат закрывается как `dismissed`, подтверждённый — как `closed`. "
        "Обязателен `If-Match`. Роль: ADMIN."
    ),
    response_model=IncidentOut,
)
async def close_incident(
    payload: IncidentCloseRequest,
    session: DbSession,
    _: IncidentManage,
    if_match: IfMatch,
    incident_id: Annotated[uuid.UUID, Path()],
) -> IncidentOut:
    service = IncidentService(session)
    incident = await service.get_or_404(incident_id)
    updated = await service.close(incident, reason=payload.reason, expected_version=if_match)
    return IncidentOut.model_validate(updated)
