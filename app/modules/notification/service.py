"""Сервисный интерфейс уведомлений.

Полный модуль (шаблоны, каналы, `notification_deliveries`, тихие часы)
делается в своём спринте. Но события безопасности из new_spec §4.4–4.5
обязаны уведомлять пользователя уже сейчас: «Пароль изменён <дата>, IP
<...>» — это не украшение, а часть реакции на инцидент.

До появления реализации зарегистрирован логирующий адаптер: факт
уведомления виден в журнале и в `audit_log`, но в канал не уходит. Это
честнее, чем тихо ничего не делать.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol, runtime_checkable

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


class NotificationPriority:
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


# Коды шаблонов, на которые опирается identity.
TPL_PASSWORD_CHANGED = "USER_PASSWORD_CHANGED"
TPL_PASSWORD_RESET = "USER_PASSWORD_RESET"
TPL_ACCOUNT_BLOCKED = "USER_ACCOUNT_BLOCKED"
TPL_ACCOUNT_UNBLOCKED = "USER_ACCOUNT_UNBLOCKED"
TPL_ROLE_CHANGED = "USER_ROLE_CHANGED"
TPL_OFFBOARD_SUCCESSOR = "USER_OFFBOARD_SUCCESSOR"
TPL_ERASURE_BLOCKED = "ERASURE_REQUEST_BLOCKED"


@runtime_checkable
class NotificationService(Protocol):
    async def notify_user(
        self,
        session: AsyncSession,
        *,
        recipient_id: uuid.UUID,
        template_code: str,
        payload: dict[str, Any] | None = None,
        priority: str = NotificationPriority.NORMAL,
        entity_type: str | None = None,
        entity_id: uuid.UUID | None = None,
    ) -> None: ...


class LoggingNotificationService:
    """Пишет уведомление в структурный лог. ПДн в лог не попадают."""

    async def notify_user(
        self,
        session: AsyncSession,
        *,
        recipient_id: uuid.UUID,
        template_code: str,
        payload: dict[str, Any] | None = None,
        priority: str = NotificationPriority.NORMAL,
        entity_type: str | None = None,
        entity_id: uuid.UUID | None = None,
    ) -> None:
        logger.info(
            "notification_queued",
            recipient_id=str(recipient_id),
            template_code=template_code,
            priority=priority,
            entity_type=entity_type,
        )


_service: NotificationService = LoggingNotificationService()


def register_notification_service(service: NotificationService) -> None:
    global _service
    _service = service


def get_notification_service() -> NotificationService:
    return _service
