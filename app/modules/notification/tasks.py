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
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import structlog
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.metrics import background_tasks_total
from app.modules.notification.models import DeliveryStatus, NotificationDelivery
from app.modules.notification.service import ChannelDeliveryError, get_channel_gateway

logger = structlog.get_logger(__name__)


async def dispatch_pending_notifications(ctx: dict[str, Any]) -> dict[str, int]:
    settings = get_settings()
    sent = failed = skipped = 0

    async with session_scope() as session:
        deliveries = (
            (
                await session.execute(
                    select(NotificationDelivery)
                    .where(NotificationDelivery.status == DeliveryStatus.PENDING.value)
                    .order_by(NotificationDelivery.created_at)
                    .limit(settings.notification_dispatch_batch_size)
                )
            )
            .scalars()
            .all()
        )

        for delivery in deliveries:
            gateway = get_channel_gateway(delivery.channel)
            delivery.attempt += 1
            if gateway is None:
                delivery.status = DeliveryStatus.SKIPPED.value
                delivery.error = f"Нет зарегистрированного шлюза для канала {delivery.channel!r}"
                skipped += 1
                continue
            try:
                await gateway.send(
                    address_masked=delivery.address_masked, subject=None, body=""
                )
            except ChannelDeliveryError as exc:
                if exc.retryable and delivery.attempt < settings.notification_max_delivery_attempts:
                    delivery.error = str(exc)
                    # Остаётся `pending` — заберётся следующим тиком.
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
                sent += 1

    background_tasks_total.labels(task="dispatch_pending_notifications", result="success").inc()
    if deliveries:
        logger.info(
            "notifications_dispatched", sent=sent, failed=failed, skipped=skipped,
            total=len(deliveries),
        )
    return {"sent": sent, "failed": failed, "skipped": skipped}
