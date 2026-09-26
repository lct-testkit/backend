"""Доставка уведомлений во внешние каналы (spec.txt §6.11: «Доставка
выполняется через очередь», строка 890: `notifications.dispatch`).

`RealNotificationService.notify_user` заводит `notification_deliveries` в
статусе `pending` для `email`/`telegram` синхронно с бизнес-операцией
(`in_app` уже `sent` — запись и есть доставка). Эта задача — единственное
место, которое реально дёргает `ChannelGateway.send`, чтобы медленный или
недоступный внешний шлюз не задерживал HTTP-ответ вызывающей ручки.

В этом контуре оба внешних шлюза — честная заглушка `LoggingChannelGateway`
(см. `service.py`), поэтому на практике каждый `pending` немедленно
становится `skipped`. Ретрай-цикл (`attempt`/`max_attempts`) при этом уже
полностью рабочий: реальный SMTP/SMS-клиент подключается через
`register_channel_gateway` без изменений здесь.

Что делает тик, кроме вызова шлюза:

* берёт доставки под `FOR UPDATE SKIP LOCKED` — пересекающиеся тики не отправляют одно и то же
  письмо дважды;
* критичные и важные уведомления идут раньше обычных, отложенные тихими часами — в конец;
* повтор после сбоя не на следующем тике, а по расписанию 30 с / 2 мин / 10 мин / 30 мин от
  создания доставки (отдельной колонки «следующая попытка» нет — возраст записи и число
  попыток её заменяют);
* тихие часы получателя откладывают отправку, а не отменяют её;
* тема и текст рендерятся из шаблона канала (`notification_templates`), настоящий адрес берётся у
  получателя в момент отправки — в записи доставки лежит только маска.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import structlog
from sqlalchemy import case, func, literal_column, or_, select

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.metrics import background_tasks_total
from app.modules.identity.models import User
from app.modules.notification.models import (
    DeliveryStatus,
    Notification,
    NotificationDelivery,
    NotificationTemplate,
    UserNotificationPref,
)
from app.modules.notification.service import (
    ChannelDeliveryError,
    _in_quiet_hours,
    get_channel_gateway,
    render_template,
)

logger = structlog.get_logger(__name__)

_QUIET_NOTE = "Тихие часы получателя: отправка отложена"
_EMAIL = "email"

# Через сколько секунд после создания доставки допустима очередная попытка, если прошлые
# (их число — `attempt`) не удались: 1 -> 30 с, 2 -> 2 мин, 3 -> 10 мин, дальше — 30 мин.
_RETRY_DELAY = case(
    (NotificationDelivery.attempt <= 1, 30),
    (NotificationDelivery.attempt == 2, 120),
    (NotificationDelivery.attempt == 3, 600),
    else_=1800,
)
_PRIORITY_RANK = case(
    (Notification.priority == "critical", 0),
    (Notification.priority == "high", 1),
    else_=2,
)


async def dispatch_pending_notifications(ctx: dict[str, Any]) -> dict[str, int]:
    settings = get_settings()
    sent = failed = skipped = deferred = 0

    async with session_scope() as session:
        due = or_(
            NotificationDelivery.attempt == 0,
            NotificationDelivery.created_at
            <= func.now() - _RETRY_DELAY * literal_column("interval '1 second'"),
        )
        deliveries = (
            (
                await session.execute(
                    select(NotificationDelivery)
                    .outerjoin(
                        Notification, Notification.id == NotificationDelivery.notification_id
                    )
                    .where(NotificationDelivery.status == DeliveryStatus.PENDING.value, due)
                    # Отложенные тихими часами — в конец, чтобы не занимать всю пачку.
                    .order_by(
                        NotificationDelivery.error.is_not(None),
                        _PRIORITY_RANK,
                        NotificationDelivery.created_at,
                    )
                    .limit(settings.notification_dispatch_batch_size)
                    # `OF`: внешнее соединение с уведомлением не блокируется, блокируются только
                    # доставки. Взятые другим тиком пропускаются.
                    .with_for_update(of=NotificationDelivery, skip_locked=True)
                )
            )
            .scalars()
            .all()
        )

        for delivery in deliveries:
            notification = (
                await session.get(Notification, delivery.notification_id)
                if delivery.notification_id is not None
                else None
            )
            if notification is None:
                # Уведомление удалено (обезличивание): доставлять нечего.
                delivery.status = DeliveryStatus.SKIPPED.value
                delivery.error = "Уведомление удалено"
                skipped += 1
                continue
            recipient = await session.get(User, notification.recipient_id)
            if recipient is None:
                delivery.status = DeliveryStatus.SKIPPED.value
                delivery.error = "Получатель не найден"
                skipped += 1
                continue

            pref = await session.scalar(
                select(UserNotificationPref).where(
                    UserNotificationPref.user_id == recipient.id,
                    UserNotificationPref.event_code == notification.template_code,
                )
            )
            if pref is not None and _in_quiet_hours(pref, recipient.timezone):
                # Окно открыто: не отправляем и не тратим попытку — запись дождётся его конца.
                delivery.error = _QUIET_NOTE
                deferred += 1
                continue

            gateway = get_channel_gateway(delivery.channel)
            delivery.attempt += 1
            if gateway is None:
                delivery.status = DeliveryStatus.SKIPPED.value
                delivery.error = f"Нет зарегистрированного шлюза для канала {delivery.channel!r}"
                skipped += 1
                continue

            template = await session.scalar(
                select(NotificationTemplate).where(
                    NotificationTemplate.code == notification.template_code,
                    NotificationTemplate.channel == delivery.channel,
                    NotificationTemplate.is_active.is_(True),
                )
            )
            if template is None:
                delivery.status = DeliveryStatus.SKIPPED.value
                delivery.error = "Нет активного шаблона канала"
                skipped += 1
                continue
            try:
                subject = (
                    render_template(template.subject_template, notification.payload)
                    if template.subject_template
                    else None
                )
                body = render_template(template.body_template, notification.payload)
            except Exception as exc:  # noqa: BLE001 — шаблон правит админ; повтор не поможет
                delivery.status = DeliveryStatus.SKIPPED.value
                delivery.error = f"Шаблон не отрисовался: {type(exc).__name__}"
                skipped += 1
                continue
            address = recipient.email if delivery.channel == _EMAIL else None
            if not address:
                # У Telegram нет ни поля для chat id, ни адреса вовсе: отправлять некуда.
                delivery.status = DeliveryStatus.SKIPPED.value
                delivery.error = f"У получателя нет адреса для канала {delivery.channel!r}"
                skipped += 1
                continue

            try:
                await gateway.send(
                    address_masked=delivery.address_masked,
                    subject=subject,
                    body=body,
                    address=address,
                )
            except ChannelDeliveryError as exc:
                if exc.retryable and delivery.attempt < settings.notification_max_delivery_attempts:
                    delivery.error = str(exc)
                    # Остаётся `pending` — повтор по расписанию (`_RETRY_DELAY`).
                    continue
                delivery.status = (
                    DeliveryStatus.FAILED.value if exc.retryable else DeliveryStatus.SKIPPED.value
                )
                delivery.error = str(exc)
                if delivery.status == DeliveryStatus.FAILED.value:
                    failed += 1
                else:
                    skipped += 1
            except Exception as exc:  # noqa: BLE001 — сбой шлюза не должен ронять воркер
                if delivery.attempt < settings.notification_max_delivery_attempts:
                    delivery.error = f"{type(exc).__name__}: {exc}"
                    continue
                delivery.status = DeliveryStatus.FAILED.value
                delivery.error = f"{type(exc).__name__}: {exc}"
                failed += 1
            else:
                delivery.status = DeliveryStatus.SENT.value
                delivery.sent_at = dt.datetime.now(dt.UTC)
                delivery.error = None
                sent += 1

    background_tasks_total.labels(task="dispatch_pending_notifications", result="success").inc()
    if deliveries:
        logger.info(
            "notifications_dispatched",
            sent=sent,
            failed=failed,
            skipped=skipped,
            deferred=deferred,
            total=len(deliveries),
        )
    return {"sent": sent, "failed": failed, "skipped": skipped, "deferred": deferred}
