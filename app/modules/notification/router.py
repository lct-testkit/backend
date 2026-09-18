"""Ручки уведомлений (spec.txt §6.11) и администрирования их шаблонов
(dop.md §13: «шаблоны администрируются через отдельный административный
контур»)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status

from app.core.deps import ConsentedUser, DbSession, IfMatch, Pagination, require_permission
from app.core.pagination import Page, keyset_before
from app.core.permissions import Permission
from app.core.security import Principal
from app.modules.notification.models import Notification, NotificationTemplate
from app.modules.notification.schemas import (
    NotificationListResponse,
    NotificationOut,
    NotificationPrefListResponse,
    NotificationPrefOut,
    NotificationPrefsUpdateRequest,
    NotificationReadRequest,
    NotificationReadResponse,
    NotificationTemplateCreateRequest,
    NotificationTemplateListResponse,
    NotificationTemplateOut,
    NotificationTemplateUpdateRequest,
)
from app.modules.notification.service import (
    NotificationFilters,
    NotificationPrefService,
    NotificationQueryService,
    NotificationReadFilters,
    NotificationTemplateService,
)

notifications_router = APIRouter(prefix="/notifications", tags=["notifications"])
me_notification_prefs_router = APIRouter(prefix="/me", tags=["notifications"])
notification_templates_admin_router = APIRouter(
    prefix="/admin/notification-templates", tags=["notifications"]
)

NotificationTemplateManage = Annotated[
    Principal, Depends(require_permission(Permission.NOTIFICATION_TEMPLATE_MANAGE))
]


# =============================================================================
# Мои уведомления
# =============================================================================


@notifications_router.get(
    "", summary="Мои уведомления", response_model=NotificationListResponse
)
async def list_notifications(
    session: DbSession,
    page: Pagination,
    principal: ConsentedUser,
    is_read: Annotated[bool | None, Query()] = None,
    priority: Annotated[str | None, Query()] = None,
    entity_type: Annotated[str | None, Query()] = None,
    event_code: Annotated[str | None, Query()] = None,
) -> NotificationListResponse:
    service = NotificationQueryService(session)
    filters = NotificationFilters(
        is_read=is_read, priority=priority, entity_type=entity_type, event_code=event_code
    )
    stmt = service.list_query(principal.user_id, filters)
    cursor = page.decoded_cursor
    if cursor:
        stmt = stmt.where(keyset_before(Notification.created_at, Notification.id, cursor))
    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())
    built = Page.build(rows, limit=page.limit)

    templates = await service.in_app_templates_by_code({n.template_code for n in built.items})
    items: list[NotificationOut] = []
    for notification in built.items:
        subject, body = service.render_for_display(
            notification, templates.get(notification.template_code)
        )
        out = NotificationOut.model_validate(notification)
        out.subject = subject
        out.body = body
        items.append(out)

    return NotificationListResponse(items=items, next_cursor=built.next_cursor)


@notifications_router.post(
    "/read", summary="Отметить уведомления прочитанными", response_model=NotificationReadResponse
)
async def mark_notifications_read(
    payload: NotificationReadRequest, session: DbSession, principal: ConsentedUser
) -> NotificationReadResponse:
    service = NotificationQueryService(session)
    updated = await service.mark_read(
        principal.user_id,
        NotificationReadFilters(
            ids=payload.ids,
            priority=payload.priority,
            entity_type=payload.entity_type,
            event_code=payload.event_code,
        ),
    )
    return NotificationReadResponse(updated=updated)


# =============================================================================
# Настройки уведомлений пользователя
# =============================================================================


@me_notification_prefs_router.get(
    "/notification-prefs",
    summary="Мои настройки уведомлений",
    response_model=NotificationPrefListResponse,
)
async def get_notification_prefs(
    session: DbSession, principal: ConsentedUser
) -> NotificationPrefListResponse:
    prefs = await NotificationPrefService(session).list_for_user(principal.user_id)
    return NotificationPrefListResponse(
        items=[NotificationPrefOut.model_validate(p) for p in prefs]
    )


@me_notification_prefs_router.put(
    "/notification-prefs",
    summary="Обновить настройки уведомлений",
    response_model=NotificationPrefListResponse,
)
async def update_notification_prefs(
    payload: NotificationPrefsUpdateRequest, session: DbSession, principal: ConsentedUser
) -> NotificationPrefListResponse:
    prefs = await NotificationPrefService(session).upsert(principal.user_id, payload.prefs)
    return NotificationPrefListResponse(
        items=[NotificationPrefOut.model_validate(p) for p in prefs]
    )


# =============================================================================
# Администрирование шаблонов
# =============================================================================


@notification_templates_admin_router.get(
    "", summary="Список шаблонов уведомлений", response_model=NotificationTemplateListResponse
)
async def list_notification_templates(
    session: DbSession,
    page: Pagination,
    principal: NotificationTemplateManage,
    code: Annotated[str | None, Query()] = None,
    channel: Annotated[str | None, Query()] = None,
) -> NotificationTemplateListResponse:
    service = NotificationTemplateService(session)
    stmt = service.list_query(code=code, channel=channel).order_by(
        NotificationTemplate.created_at.desc(), NotificationTemplate.id.desc()
    )
    cursor = page.decoded_cursor
    if cursor:
        stmt = stmt.where(
            keyset_before(NotificationTemplate.created_at, NotificationTemplate.id, cursor)
        )
    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())
    built = Page.build(rows, limit=page.limit, serializer=NotificationTemplateOut.model_validate)
    return NotificationTemplateListResponse(items=built.items, next_cursor=built.next_cursor)


@notification_templates_admin_router.post(
    "",
    summary="Создать шаблон уведомления",
    response_model=NotificationTemplateOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_notification_template(
    payload: NotificationTemplateCreateRequest,
    session: DbSession,
    principal: NotificationTemplateManage,
) -> NotificationTemplateOut:
    template = await NotificationTemplateService(session).create(payload)
    return NotificationTemplateOut.model_validate(template)


@notification_templates_admin_router.patch(
    "/{template_id}",
    summary="Обновить шаблон уведомления",
    response_model=NotificationTemplateOut,
)
async def update_notification_template(
    payload: NotificationTemplateUpdateRequest,
    session: DbSession,
    principal: NotificationTemplateManage,
    if_match: IfMatch,
    template_id: Annotated[uuid.UUID, Path()],
) -> NotificationTemplateOut:
    service = NotificationTemplateService(session)
    template = await service.get_or_404(template_id)
    template = await service.update(template, payload, expected_version=if_match)
    return NotificationTemplateOut.model_validate(template)
