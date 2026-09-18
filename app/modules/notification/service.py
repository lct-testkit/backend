"""Сервисный слой уведомлений (spec.txt §5.7/§6.11, new_spec §7.7).

До этого спринта модуль существовал только как логирующая заглушка:
`LoggingNotificationService` писала факт в структурный лог, а `notify_user`
использовался 11 местами в 7 модулях (`identity`, `crm`, `catalog`,
`registry`, `signing`), ожидая реализации. `register_notification_service`
никогда не вызывался ни в `app/main.py`, ни в `app/worker/main.py` — то есть
до сих пор в реальном приложении включена только заглушка, а не что-то,
похожее на реализацию. Этот спринт добавляет `RealNotificationService` и
регистрирует её на старте API и воркера (см. соответствующие файлы).

Протокол `NotificationService` и код `TPL_*` не менялись — это сохраняет
обратную совместимость со всеми существующими вызовами `notify_user`.

**Провайдерная архитектура каналов** — тот же приём, что `SignatureProvider`
(dop.md §10.3) и `OrgLookupProvider` (dop.md §11.2): доставка `in_app`
реализована напрямую (запись и есть доставка), а `email`/`telegram` идут
через `ChannelGateway`, зарегистрированный в `_channel_gateways`. По
умолчанию оба — честная заглушка `LoggingChannelGateway`: в этом контуре нет
ни настроенного SMTP, ни SMS/Telegram-шлюза (то же самое ограничение, что
`app/modules/signing/service.py` уже документирует для доставки OTP,
dop.md §10.11). Подключение реального SMTP-клиента позже — это
`register_channel_gateway("email", SmtpGateway(...))`, без изменений в
`RealNotificationService`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

import jinja2
import structlog
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import FieldError, NotFoundError, ValidationError, VersionConflictError
from app.core.masking import mask_email
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.identity.models import User, UserStatus
from app.modules.notification.models import (
    DeliveryStatus,
    Notification,
    NotificationChannel,
    NotificationDelivery,
    NotificationPriority,
    NotificationTemplate,
    UserNotificationPref,
)

logger = structlog.get_logger(__name__)

# Коды шаблонов, на которые опирается identity (сохранены для обратной
# совместимости — те же 11 мест продолжают импортировать их отсюда).
TPL_PASSWORD_CHANGED = "USER_PASSWORD_CHANGED"
TPL_PASSWORD_RESET = "USER_PASSWORD_RESET"
TPL_ACCOUNT_BLOCKED = "USER_ACCOUNT_BLOCKED"
TPL_ACCOUNT_UNBLOCKED = "USER_ACCOUNT_UNBLOCKED"
TPL_ROLE_CHANGED = "USER_ROLE_CHANGED"
TPL_OFFBOARD_SUCCESSOR = "USER_OFFBOARD_SUCCESSOR"
TPL_ERASURE_BLOCKED = "ERASURE_REQUEST_BLOCKED"
# Спринт 10: исполнение запроса на удаление/обезличивание (new_spec §4.8.4
# шаг 7) — уведомляется инициатор запроса, а не сам субъект (для режима B
# уже некому: e-mail затёрт до того, как уведомление успело бы уйти).
TPL_ERASURE_COMPLETED = "ERASURE_COMPLETED"

# Только `blocked` — дословно spec.txt §4.5: «Уведомления, адресованные
# заблокированному, не доставляются ему, а эскалируются руководителю».
# `terminated`/`anonymized` здесь не трогаем: увольнение (§4.7) уже
# переносит открытые задачи и сделки на нового владельца до того, как для
# уволенного вообще создаётся новое уведомление.
_ESCALATE_ON_STATUS = frozenset({UserStatus.BLOCKED.value})


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
    """Пишет уведомление в структурный лог, без БД. Используется в тестах и
    как безопасный запасной вариант, если `RealNotificationService` не
    зарегистрирован явно."""

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


def render_template(template_str: str, payload: dict[str, Any]) -> str:
    """Простая текстовая подстановка (не HTML-документ, поэтому без
    `autoescape` — тело уведомления возвращается как обычная строка JSON-поля,
    экранирование при показе в HTML — забота фронтенда, как и для любого
    другого текстового поля этого API (комментарии сделок, названия
    организаций и т.д. тоже не экранируются на бэкенде)."""
    return jinja2.Template(template_str).render(**payload)


def _in_quiet_hours(
    pref: UserNotificationPref, tz_name: str, *, now_local: dt.time | None = None
) -> bool:
    """`now_local` — точка внедрения для тестов (`tests/test_notifications.py`);
    в проде всегда `None`, и текущее время считается по `tz_name`."""
    if pref.quiet_hours_start is None or pref.quiet_hours_end is None:
        return False
    if now_local is None:
        try:
            tz = ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001 — некорректный IANA-таймзон не должен ронять доставку
            tz = ZoneInfo("UTC")
        now_local = dt.datetime.now(tz).time()
    start, end = pref.quiet_hours_start, pref.quiet_hours_end
    if start <= end:
        return start <= now_local <= end
    # Окно через полночь (например, 22:00–08:00).
    return now_local >= start or now_local <= end


class RealNotificationService:
    """Создаёт `notifications` + `notification_deliveries`. Единственная
    сторона, которая реально отправляет внешние каналы — это
    `tasks.dispatch_pending_notifications`: здесь только `pending`/`sent`
    (для `in_app`) заводятся синхронно в той же транзакции, что и вызывающий
    код (см. `notify_user` во всех 7 модулях — они не оборачивают вызов в
    отдельный commit)."""

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
        payload = dict(payload or {})
        recipient = await session.get(User, recipient_id)
        if recipient is None:
            logger.warning("notification_recipient_missing", recipient_id=str(recipient_id))
            return

        target_id = recipient_id
        target_user = recipient
        if recipient.status in _ESCALATE_ON_STATUS:
            if recipient.manager_id is None:
                logger.warning(
                    "notification_undeliverable_no_manager",
                    recipient_id=str(recipient_id),
                    template_code=template_code,
                )
                return
            target_user = await session.get(User, recipient.manager_id)
            if target_user is None:
                return
            target_id = target_user.id
            payload["escalated_from"] = str(recipient_id)

        notification = Notification(
            recipient_id=target_id,
            template_code=template_code,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=payload,
            priority=priority,
        )
        session.add(notification)
        await session.flush()
        await self._create_deliveries(session, notification, target_user, template_code)

    async def _create_deliveries(
        self,
        session: AsyncSession,
        notification: Notification,
        user: User,
        event_code: str,
    ) -> None:
        templates = (
            (
                await session.execute(
                    select(NotificationTemplate).where(
                        NotificationTemplate.code == event_code,
                        NotificationTemplate.is_active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        if not templates:
            # Динамический `event_code` из конструктора воркфлоу
            # (`workflow_transitions.actions[].event_code`, dop.md §10.6) может
            # не иметь заведённого шаблона — уведомление остаётся видимым в
            # `GET /api/notifications` через сырой `payload`, просто без
            # каналов доставки. Тихий, а не падающий, путь: админ, забывший
            # завести шаблон, не должен ронять переход по воронке.
            return

        pref = await session.scalar(
            select(UserNotificationPref).where(
                UserNotificationPref.user_id == user.id,
                UserNotificationPref.event_code == event_code,
            )
        )
        if pref is not None and not pref.is_enabled:
            return
        allowed_channels = set(pref.channels) if pref is not None and pref.channels else None
        quiet = pref is not None and _in_quiet_hours(pref, user.timezone)

        for template in templates:
            if allowed_channels is not None and template.channel not in allowed_channels:
                continue

            if template.channel == NotificationChannel.IN_APP.value:
                session.add(
                    NotificationDelivery(
                        notification_id=notification.id,
                        channel=template.channel,
                        status=DeliveryStatus.SENT.value,
                        sent_at=dt.datetime.now(dt.UTC),
                    )
                )
                continue

            if quiet:
                session.add(
                    NotificationDelivery(
                        notification_id=notification.id,
                        channel=template.channel,
                        status=DeliveryStatus.SKIPPED.value,
                        error="Тихие часы получателя",
                    )
                )
                continue

            address = user.email if template.channel == NotificationChannel.EMAIL.value else None
            session.add(
                NotificationDelivery(
                    notification_id=notification.id,
                    channel=template.channel,
                    address_masked=mask_email(address) if address else None,
                    status=DeliveryStatus.PENDING.value,
                )
            )
        await session.flush()


_service: NotificationService = LoggingNotificationService()


def register_notification_service(service: NotificationService) -> None:
    global _service
    _service = service


def get_notification_service() -> NotificationService:
    return _service


# =============================================================================
# Каналы внешней доставки (email/telegram) — провайдерная абстракция
# =============================================================================


class ChannelDeliveryError(Exception):
    """`retryable=True` — временная ошибка, `dispatch_pending_notifications`
    оставит попытку в очереди и повторит на следующем тике; `False` —
    постоянная (например, канал в этом контуре в принципе не настроен),
    делавери сразу помечается `skipped`, а не крутится до `max_attempts`."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@runtime_checkable
class ChannelGateway(Protocol):
    async def send(self, *, address_masked: str | None, subject: str | None, body: str) -> None: ...


class LoggingChannelGateway:
    def __init__(self, channel: str) -> None:
        self._channel = channel

    async def send(self, *, address_masked: str | None, subject: str | None, body: str) -> None:
        logger.info(
            "notification_channel_not_configured",
            channel=self._channel,
            address_masked=address_masked,
        )
        raise ChannelDeliveryError(
            f"Шлюз канала {self._channel!r} не настроен в этом контуре "
            "(dop.md §10.11 — реальный SMTP/SMS/Telegram-гейтвей отложен)",
            retryable=False,
        )


_channel_gateways: dict[str, ChannelGateway] = {
    NotificationChannel.EMAIL.value: LoggingChannelGateway(NotificationChannel.EMAIL.value),
    NotificationChannel.TELEGRAM.value: LoggingChannelGateway(NotificationChannel.TELEGRAM.value),
}


def register_channel_gateway(channel: str, gateway: ChannelGateway) -> None:
    _channel_gateways[channel] = gateway


def get_channel_gateway(channel: str) -> ChannelGateway | None:
    return _channel_gateways.get(channel)


# =============================================================================
# Чтение уведомлений получателем (GET /api/notifications, .../read)
# =============================================================================


class NotificationFilters:
    def __init__(
        self,
        *,
        is_read: bool | None = None,
        priority: str | None = None,
        entity_type: str | None = None,
        event_code: str | None = None,
    ) -> None:
        self.is_read = is_read
        self.priority = priority
        self.entity_type = entity_type
        self.event_code = event_code


class NotificationReadFilters:
    def __init__(
        self,
        *,
        ids: list[uuid.UUID] | None = None,
        priority: str | None = None,
        entity_type: str | None = None,
        event_code: str | None = None,
    ) -> None:
        self.ids = ids
        self.priority = priority
        self.entity_type = entity_type
        self.event_code = event_code


class NotificationQueryService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def list_query(
        self, recipient_id: uuid.UUID, filters: NotificationFilters
    ) -> Select[tuple[Notification]]:
        stmt = select(Notification).where(Notification.recipient_id == recipient_id)
        if filters.is_read is not None:
            stmt = stmt.where(Notification.is_read == filters.is_read)
        if filters.priority is not None:
            stmt = stmt.where(Notification.priority == filters.priority)
        if filters.entity_type is not None:
            stmt = stmt.where(Notification.entity_type == filters.entity_type)
        if filters.event_code is not None:
            stmt = stmt.where(Notification.template_code == filters.event_code)
        return stmt.order_by(Notification.created_at.desc(), Notification.id.desc())

    async def in_app_templates_by_code(self, codes: set[str]) -> dict[str, NotificationTemplate]:
        if not codes:
            return {}
        rows = (
            await self._session.execute(
                select(NotificationTemplate).where(
                    NotificationTemplate.code.in_(codes),
                    NotificationTemplate.channel == NotificationChannel.IN_APP.value,
                    NotificationTemplate.is_active.is_(True),
                )
            )
        ).scalars().all()
        return {t.code: t for t in rows}

    def render_for_display(
        self, notification: Notification, template: NotificationTemplate | None
    ) -> tuple[str | None, str | None]:
        if template is None:
            return None, None
        try:
            subject = (
                render_template(template.subject_template, notification.payload)
                if template.subject_template
                else None
            )
            body = render_template(template.body_template, notification.payload)
        except jinja2.TemplateError as exc:
            logger.warning(
                "notification_render_failed",
                template_code=template.code,
                error=str(exc),
            )
            return None, None
        return subject, body

    async def mark_read(self, recipient_id: uuid.UUID, filters: NotificationReadFilters) -> int:
        stmt = select(Notification).where(
            Notification.recipient_id == recipient_id, Notification.is_read.is_(False)
        )
        if filters.ids:
            stmt = stmt.where(Notification.id.in_(filters.ids))
        if filters.priority is not None:
            stmt = stmt.where(Notification.priority == filters.priority)
        if filters.entity_type is not None:
            stmt = stmt.where(Notification.entity_type == filters.entity_type)
        if filters.event_code is not None:
            stmt = stmt.where(Notification.template_code == filters.event_code)
        rows = (await self._session.execute(stmt)).scalars().all()
        now = dt.datetime.now(dt.UTC)
        for row in rows:
            row.is_read = True
            row.read_at = now
        if rows:
            await self._session.flush()
        return len(rows)


# =============================================================================
# Настройки пользователя (/api/me/notification-prefs)
# =============================================================================


class NotificationPrefService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_user(self, user_id: uuid.UUID) -> list[UserNotificationPref]:
        return list(
            (
                await self._session.execute(
                    select(UserNotificationPref)
                    .where(UserNotificationPref.user_id == user_id)
                    .order_by(UserNotificationPref.event_code)
                )
            )
            .scalars()
            .all()
        )

    async def upsert(
        self, user_id: uuid.UUID, items: list[Any]
    ) -> list[UserNotificationPref]:
        result: list[UserNotificationPref] = []
        for item in items:
            pref = await self._session.scalar(
                select(UserNotificationPref).where(
                    UserNotificationPref.user_id == user_id,
                    UserNotificationPref.event_code == item.event_code,
                )
            )
            if pref is None:
                pref = UserNotificationPref(user_id=user_id, event_code=item.event_code)
                self._session.add(pref)
            pref.channels = list(item.channels)
            pref.is_enabled = item.is_enabled
            pref.quiet_hours_start = item.quiet_hours_start
            pref.quiet_hours_end = item.quiet_hours_end
            result.append(pref)
        await self._session.flush()
        return result


# =============================================================================
# Администрирование шаблонов (/api/admin/notification-templates)
# =============================================================================


class NotificationTemplateService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    def list_query(
        self, *, code: str | None = None, channel: str | None = None
    ) -> Select[tuple[NotificationTemplate]]:
        stmt = select(NotificationTemplate)
        if code is not None:
            stmt = stmt.where(NotificationTemplate.code == code)
        if channel is not None:
            stmt = stmt.where(NotificationTemplate.channel == channel)
        return stmt.order_by(NotificationTemplate.code, NotificationTemplate.channel)

    async def get_or_404(self, template_id: uuid.UUID) -> NotificationTemplate:
        template = await self._session.get(NotificationTemplate, template_id)
        if template is None:
            raise NotFoundError("Шаблон уведомления", template_id)
        return template

    async def create(self, payload: Any) -> NotificationTemplate:
        existing = await self._session.scalar(
            select(NotificationTemplate).where(
                NotificationTemplate.code == payload.code,
                NotificationTemplate.channel == payload.channel,
            )
        )
        if existing is not None:
            raise ValidationError(
                "Шаблон с таким кодом и каналом уже существует",
                [FieldError(field="code", reason="комбинация code+channel уже используется")],
            )
        template = NotificationTemplate(
            code=payload.code,
            channel=payload.channel,
            subject_template=payload.subject_template,
            body_template=payload.body_template,
            locale=payload.locale,
            is_active=payload.is_active,
        )
        self._session.add(template)
        await self._session.flush()
        await self._audit.record(
            AuditAction.NOTIFICATION_TEMPLATE_CREATED,
            entity_type="notification_template",
            entity_id=template.id,
            changes={
                "code": {"old": None, "new": template.code},
                "channel": {"old": None, "new": template.channel},
            },
        )
        return template

    async def update(
        self, template: NotificationTemplate, payload: Any, *, expected_version: int
    ) -> NotificationTemplate:
        if template.version != expected_version:
            raise VersionConflictError(template.version, {"code": template.code})
        data = payload.model_dump(exclude_unset=True)
        changes: dict[str, dict[str, Any]] = {}
        for key, value in data.items():
            old = getattr(template, key)
            if old == value:
                continue
            changes[key] = {"old": old, "new": value}
            setattr(template, key, value)
        if not changes:
            return template
        template.version += 1
        await self._session.flush()
        await self._audit.record(
            AuditAction.NOTIFICATION_TEMPLATE_UPDATED,
            entity_type="notification_template",
            entity_id=template.id,
            changes=changes,
        )
        return template
