"""Фоновые задачи интеграций: доставка outbox (раздел 3.6/4.14) и pull LMS.

Backoff — раздел 3.6, дословно: `1s, 5s, 30s, 5m, 30m, 2h`, `dead` после 8
неудач. Индекс `(status, next_retry_at) WHERE status IN ('pending','failed')`
(`integration.models.OutboxEvent`) держит выборку дешёвой при любом объёме
очереди — тот же приём, что `sweep_report_jobs`/`sweep_import_jobs` уже
применяют к своим очередям. Алерт админу на dead-letter (раздел 3.6) сведён
к структурному логу уровня `error`, а не к вызову `notification` — интеграции
завязываться на модуль уведомлений изнутри «горячего» цикла доставки не
стали (тот же довод, по которому `RealOutboxService.publish()` сам не делает
HTTP-вызовов): любая задержка/исключение в notify не должна блокировать
обработку остальной очереди. Отслеживается через `GET /api/admin/
integrations/outbox-events?status=dead`, вернуть событие в очередь после
устранения причины — `POST .../outbox-events/{id}/retry`.

Доставка в Bitrix24 включена, только когда совпали три выключателя:
`BITRIX_CONNECTOR_ENABLED` (окружение), `integration_sources.is_active`
(«Настройка → Интеграции») и флаг функции `bitrix_connector`
(`feature_flags`, «Настройка → Флаги»). Выключенный источник или коннектор —
`dead` с `last_error='source_inactive'`, выключенный флаг — `dead` с
`last_error='feature_flag_disabled'`; в обоих случаях событие остаётся в
таблице и повторяется вручную. Флаг читается так же, как в `admin.router`
(строка `feature_flags` по коду), `rollout` здесь не применяется: коннектор
включается целиком. Флаги заводятся вручную — нет строки, нечего выключать, и
доставку решают остальные два условия.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from typing import Any

import httpx
import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.metrics import track_task, webhook_deliveries_total
from app.modules.admin.models import FeatureFlag
from app.modules.admin.setting_crypto import decrypt_value
from app.modules.integration import bitrix, lms
from app.modules.integration.cms_pull import CmsPullService
from app.modules.integration.http import TIMEOUT as _HTTP_TIMEOUT
from app.modules.integration.models import (
    IntegrationSource,
    OutboxEvent,
    OutboxStatus,
    WebhookDelivery,
    WebhookDeliveryStatus,
    WebhookSubscription,
)
from app.modules.integration.routing import Routing
from app.modules.integration.security import (
    check_outbound_url_resolved,
    compute_signature,
    resolve_secret,
    same_host,
)
from app.modules.integration.webhooks import invalidate_cache_after_commit

logger = structlog.get_logger(__name__)

# Раздел 3.6, дословно.
_BACKOFF_SECONDS = [1, 5, 30, 300, 1800, 7200]
_MAX_ATTEMPTS = 8
# Сколько событие считается «в работе» у захватившего его тика. Дольше любой доставки (таймауты
# httpx — секунды); упавший воркер возвращает событие в очередь по истечении аренды.
_LEASE_SECONDS = 600
_KNOWN_TARGETS = frozenset({"lms", "bitrix24"})
# Код флага функции в `feature_flags` — третий выключатель доставки в Bitrix24.
_BITRIX_FLAG_CODE = "bitrix_connector"


@track_task
async def sweep_outbox_events(ctx: dict[str, Any]) -> dict[str, int]:
    """Два шага, а не одна транзакция на всю пачку.

    Раньше все события пачки (до 200) обрабатывались в одной транзакции без блокировок, а HTTP-вызов
    шёл внутри неё: два пересекающихся тика (cron раз в минуту) брали одни и те же события и
    заводили дубли сделок Bitrix24 и зачислений LMS, а сбой после `crm.item.add` терял и запись
    о доставке.

    1. **Захват** (короткая транзакция): `FOR UPDATE SKIP LOCKED` — события, взятые другим тиком,
       пропускаются. Захваченное событие получает «аренду»: `next_retry_at` сдвигается вперёд на
       `_LEASE_SECONDS`, а счётчик попыток растёт СРАЗУ — падение воркера посреди доставки не
       обнуляет попытки, и событие не вернётся в очередь раньше конца аренды. Новый статус
       «в работе» не нужен: аренда записана в поле, по которому очередь и так выбирается (CHECK на
       статус изменить нельзя без миграции).
    2. **Доставка** каждого события в своей транзакции: сеть — без общих блокировок; успех
       фиксируется вместе с записью о соответствии (`external_refs`), неудача — отдельной
       транзакцией с backoff. Стабильный ключ вызова (id события) уходит внешней системе, чтобы
       повтор не создавал второй объект.
    """
    settings = get_settings()
    counters = {"sent": 0, "failed": 0, "dead": 0, "skipped": 0}
    claimed = await _claim_events(settings, counters)
    for event_id, lease in claimed:
        await _deliver_event(event_id, lease, counters)
    return counters


async def _claim_events(
    settings: Any, counters: dict[str, int]
) -> list[tuple[uuid.UUID, dt.datetime]]:
    async with session_scope() as session:
        now = dt.datetime.now(dt.UTC)
        lease = now + dt.timedelta(seconds=_LEASE_SECONDS)
        rows = (
            (
                await session.execute(
                    select(OutboxEvent)
                    .where(
                        OutboxEvent.status.in_(
                            [OutboxStatus.PENDING.value, OutboxStatus.FAILED.value]
                        ),
                        (OutboxEvent.next_retry_at.is_(None)) | (OutboxEvent.next_retry_at <= now),
                    )
                    .order_by(OutboxEvent.created_at)
                    .limit(200)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )

        sources = {
            row.code: row
            for row in (await session.execute(select(IntegrationSource))).scalars().all()
        }
        bitrix_flag = (
            await session.execute(
                select(FeatureFlag.is_enabled).where(FeatureFlag.code == _BITRIX_FLAG_CODE)
            )
        ).scalar_one_or_none()

        claimed: list[tuple[uuid.UUID, dt.datetime]] = []
        for event in rows:
            if event.target is None:
                # Не у каждого outbox-события есть внешний получатель — например,
                # `signing.service`'s `DOCUMENT_SIGNED` публикуется всегда
                # (раздел 3.6), но Bitrix-синхронизация конкретно для событий
                # подписания в этом спринте не реализована (см. docstring
                # `integration.bitrix` — он понимает только сделки). Такое
                # событие не «не доставлено», доставлять было нечего.
                event.status = OutboxStatus.SENT.value
                event.sent_at = now
                counters["sent"] += 1
                continue
            if event.target not in _KNOWN_TARGETS:
                event.status = OutboxStatus.DEAD.value
                event.last_error = "unknown_target"
                counters["skipped"] += 1
                continue

            source = sources.get(event.target)
            active = bool(source and source.is_active)
            if event.target == "bitrix24":
                active = active and settings.bitrix_connector_enabled

            if not active:
                event.status = OutboxStatus.DEAD.value
                event.last_error = "source_inactive"
                counters["skipped"] += 1
                continue

            # Строки флага нет (`None`) — доставку решают остальные два условия.
            if event.target == "bitrix24" and bitrix_flag is False:
                event.status = OutboxStatus.DEAD.value
                event.last_error = "feature_flag_disabled"
                counters["skipped"] += 1
                continue

            event.attempts += 1
            event.next_retry_at = lease
            claimed.append((event.id, lease))
        return claimed


async def _deliver_event(event_id: uuid.UUID, lease: dt.datetime, counters: dict[str, int]) -> None:
    failure: str | None = None
    async with session_scope() as session:
        event = (
            await session.execute(
                select(OutboxEvent).where(OutboxEvent.id == event_id).with_for_update()
            )
        ).scalar_one_or_none()
        # Аренда истекла и событие взял другой тик (или его вернули вручную): не трогаем.
        if (
            event is None
            or event.status not in (OutboxStatus.PENDING.value, OutboxStatus.FAILED.value)
            or event.next_retry_at != lease
        ):
            return
        source = (
            await session.execute(
                select(IntegrationSource).where(IntegrationSource.code == event.target)
            )
        ).scalar_one_or_none()
        try:
            await _deliver(session, event, source)
        except Exception as exc:  # noqa: BLE001 — доставка не должна ронять цикл
            # Частичные записи доставки (например, `external_refs`) не должны пережить ошибку.
            await session.rollback()
            failure = str(exc)[:2000]
        else:
            event.status = OutboxStatus.SENT.value
            event.sent_at = dt.datetime.now(dt.UTC)
            event.next_retry_at = None
            event.last_error = None
            counters["sent"] += 1
            return

    async with session_scope() as session:
        event = (
            await session.execute(
                select(OutboxEvent).where(OutboxEvent.id == event_id).with_for_update()
            )
        ).scalar_one_or_none()
        if event is None or event.next_retry_at != lease:
            return
        # Попытка уже посчитана при захвате.
        event.last_error = failure
        if event.attempts >= _MAX_ATTEMPTS:
            event.status = OutboxStatus.DEAD.value
            event.next_retry_at = None
            counters["dead"] += 1
            logger.error(
                "outbox_event_dead_letter",
                event_id=str(event.id),
                event_type=event.event_type,
                target=event.target,
                attempts=event.attempts,
            )
        else:
            event.status = OutboxStatus.FAILED.value
            delay = _BACKOFF_SECONDS[min(event.attempts - 1, len(_BACKOFF_SECONDS) - 1)]
            event.next_retry_at = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=delay)
            counters["failed"] += 1


async def _deliver(
    session: AsyncSession, event: OutboxEvent, source: IntegrationSource | None
) -> None:
    settings = get_settings()
    if event.target == "lms":
        base_url = (source.base_url if source else None) or settings.lms_base_url
        if not base_url:
            raise RuntimeError("lms_base_url not configured")
        # Адрес мог смениться админским PATCH или само имя хоста — разрешаться иначе: токен LMS
        # уходит только на проверенный адрес, и только на хост, который задан окружением.
        try:
            base_url = await check_outbound_url_resolved(base_url, strict=settings.is_prod)
        except ValueError as exc:
            raise RuntimeError(f"lms base_url rejected: {exc}") from None
        if settings.lms_base_url and not same_host(base_url, settings.lms_base_url):
            raise RuntimeError("lms base_url host differs from LMS_BASE_URL: token not sent")
        client = lms.LmsClient(base_url, resolve_secret(settings.lms_auth_ref))
        payload: dict[str, Any] = {
            "deal_id": str(event.aggregate_id),
            "event_type": event.event_type,
        }
        if event.aggregate_type == "deal" and event.event_type in lms.ENROLLMENT_EVENTS:
            # Действие DSL `integration_event` публикует событие с пустой нагрузкой: без
            # обогащения зачисление не несло бы учащегося. Собирается здесь, а не при
            # публикации, — так уходят актуальные данные, а не снимок на момент перехода.
            payload.update(await lms.build_enrollment_details(session, event.aggregate_id))
        # Явная нагрузка события — последней: она сильнее собранных данных.
        payload.update(event.payload or {})
        await client.push_enrollment(payload, idempotency_key=str(event.id))
        return

    if event.target == "bitrix24":
        # Секрет — весь URL входящего вебхука целиком (раздел «Аутентификация»
        # докстринга `integration.bitrix`), не пара base_url+токен.
        webhook_url = resolve_secret(source.credentials_ref if source else None) or resolve_secret(
            settings.bitrix_webhook_url_ref
        )
        if not webhook_url:
            raise RuntimeError("bitrix webhook url not configured")
        client = bitrix.BitrixClient(webhook_url)
        # Не первая попытка: предыдущая могла создать сделку в портале и потерять ответ.
        retry = event.attempts > 1 or bool(event.last_error)
        await bitrix.push_deal(session, client, deal_id=event.aggregate_id, retry=retry)
        return

    raise RuntimeError(f"no deliverer for target={event.target!r}")


@track_task
async def sweep_lms_progress_pull(ctx: dict[str, Any]) -> dict[str, int]:
    """Раз в минуту проверяет `pull_interval_seconds` источника `lms` (по умолчанию 1800 с —
    «раз в 30 минут», раздел 4.14, дословно; миграция 0027). `GET /students/progress?
    updated_since=&cursor=&limit=` с курсором в `sync_cursors`, постранично (B5-6/BUG-16).

    B5-6: раньше хватало непустого `LMS_BASE_URL`, теперь нужны ещё `is_active` и
    `pull_enabled` источника — ЭТО ИЗМЕНЕНИЕ ПОВЕДЕНИЯ (см. релиз-заметки B5-13.1): источник,
    у которого раньше опрос шёл «по факту адреса», нужно явно включить в настройках.

    B5-5: «Забрать сейчас» — `pull_request` проверяется первым делом и запись очищается до
    запуска (аренда теряется при падении воркера — это ок, кнопку нажмут снова); такой запуск
    идёт сразу, минуя `pull_enabled` и интервал, с `updated_since` из запроса, если он был."""
    settings = get_settings()
    if not settings.lms_base_url:
        logger.info("lms_progress_pull_skipped", reason="base_url_empty")
        return {"pulled": 0, "skipped": 1}

    async with session_scope() as session:
        source = await session.scalar(
            select(IntegrationSource).where(IntegrationSource.code == "lms")
        )
        if source is None or not source.is_active:
            return {"pulled": 0, "skipped": 1}

        trigger = "cron"
        updated_since: str | None = None
        if source.pull_request:
            trigger = "manual"
            updated_since = source.pull_request.get("updated_since")
            source.pull_request = None
        elif not source.pull_enabled:
            return {"pulled": 0, "skipped": 1}
        elif source.last_sync_at is not None:
            elapsed = (dt.datetime.now(dt.UTC) - source.last_sync_at).total_seconds()
            if elapsed < source.pull_interval_seconds:
                return {"pulled": 0, "skipped": 1}

        persisted_cursor = await lms.get_cursor(
            session, source_code="lms", resource="students_progress"
        )
        cursor = updated_since if updated_since is not None else persisted_cursor
        base_url = source.base_url or settings.lms_base_url
        client = lms.LmsClient(base_url, resolve_secret(settings.lms_auth_ref))
        try:
            rows = await client.pull_progress(
                updated_since=cursor,
                max_pages=settings.integration_pull_max_pages,
                page_size=settings.integration_pull_page_size,
            )
        except Exception as exc:  # noqa: BLE001 — как sweep_registry_drift: не роняем cron
            logger.warning("lms_progress_pull_failed", error=str(exc))
            now = dt.datetime.now(dt.UTC)
            source.last_error = str(exc)
            source.last_run = {
                "trigger": trigger,
                "finished_at": now.isoformat(),
                "error": str(exc),
            }
            return {"pulled": 0, "error": 1}

        # Каждая строка — в своём SAVEPOINT: одна битая строка не откатывает остальные, а курсор не
        # уходит дальше строк, которые применить не удалось (их следующий тик получит снова).
        rows = rows if isinstance(rows, list) else []
        # B5-8: правила движения сделки по прогрессу — из маршрутизации источника lms (B5-4).
        routing = Routing.model_validate(source.routing) if source.routing else None
        progress_rules = routing.progress_rules if routing is not None else []
        # B5-7: сопоставление кода курса мока с кодом продукта каталога (демо-профиль).
        course_code_map = (source.config or {}).get("course_code_map")
        batch = await lms.apply_progress_rows(
            session, rows, progress_rules=progress_rules, course_code_map=course_code_map
        )
        latest_cursor = lms.advance_cursor(cursor, rows, batch)
        if batch.failed:
            logger.warning(
                "lms_progress_pull_partial",
                failed=batch.failed,
                cursor_held=latest_cursor == cursor,
            )

        # B5-5: курсор не откатывается назад — `updated_since` ручного запроса может быть раньше
        # уже сохранённого курсора (подбор упавших строк), итог — максимум старого и нового.
        if (
            latest_cursor is not None
            and latest_cursor != persisted_cursor
            and (persisted_cursor is None or latest_cursor > persisted_cursor)
        ):
            await lms.set_cursor(
                session, source_code="lms", resource="students_progress", value=latest_cursor
            )

        now = dt.datetime.now(dt.UTC)
        source.last_sync_at = now
        source.last_error = None
        source.last_run = {
            "trigger": trigger,
            "finished_at": now.isoformat(),
            "fetched": len(rows),
            "applied": batch.applied,
            "skipped": batch.skipped,
            "failed": batch.failed,
            # B5-8: сколько строк прогресса подвинули сделку по правилам routing.progress_rules.
            "moved": batch.moved,
            "move_failed": batch.move_failed,
        }

    return {"pulled": batch.applied, "skipped": batch.skipped, "failed": batch.failed}


@track_task
async def sweep_cms_leads_pull(ctx: dict[str, Any]) -> dict[str, int]:
    """B5-2 (Функц.5 ТЗ): забор лидов с сайта по расписанию. Тик раз в минуту
    (`worker/main.py`), реальный интервал задаёт `integration_sources.
    pull_interval_seconds` — тик, пришедшийся раньше срока, молча пропускается.

    B5-5: «Забрать сейчас» — `pull_request` проверяется первым делом; если он есть, запуск идёт
    сразу (минуя `pull_enabled` и интервал — `force=True`), а запись очищается до запуска
    (аренда теряется при падении воркера — это ок, кнопку нажмут снова)."""
    trigger = "cron"
    updated_since: str | None = None
    force = False
    async with session_scope() as session:
        source = await session.scalar(
            select(IntegrationSource).where(IntegrationSource.code == "cms")
        )
        if source is None or not source.is_active:
            return {"status": "skipped"}
        if source.pull_request:
            trigger = "manual"
            updated_since = source.pull_request.get("updated_since")
            force = True
            source.pull_request = None
        elif not source.pull_enabled:
            return {"status": "skipped"}
        elif source.last_sync_at is not None:
            elapsed = (dt.datetime.now(dt.UTC) - source.last_sync_at).total_seconds()
            if elapsed < source.pull_interval_seconds:
                return {"status": "too_soon"}

    result = await CmsPullService().run(trigger, updated_since=updated_since, force=force)
    return {
        "status": result.status,
        "fetched": result.fetched,
        "created": result.created,
        "duplicates": result.duplicates,
        "failed": result.failed,
    }


# =============================================================================
# B5-9: доставка webhook-подписок
#
# Та же форма, что `sweep_outbox_events` выше: захват (короткая транзакция, `FOR UPDATE SKIP
# LOCKED`, аренда) → доставка (своя транзакция на строку, сеть без общих блокировок) → запись
# backoff/dead-letter отдельной транзакцией. `410` выключает подписку немедленно; 20 `dead`
# подряд (`WebhookSubscription.consecutive_failures`, сбрасывается любой успешной доставкой)
# выключают её тоже — нерабочий адрес не должен копить доставки вечно. Очистка старых записей —
# в конце того же тика, не отдельным cron: `webhook_deliveries` растёт пропорционально числу
# подписок и событий, а не стоит заводить для этого ещё одну задачу.
# =============================================================================

_WEBHOOK_BACKOFF_SECONDS = [1, 5, 30, 300, 1800, 7200]
_WEBHOOK_MAX_ATTEMPTS = 8
_WEBHOOK_LEASE_SECONDS = 600
_WEBHOOK_BATCH_SIZE = 100
_WEBHOOK_DISABLE_AFTER_DEAD = 20
_WEBHOOK_SENT_RETENTION_DAYS = 7
_WEBHOOK_DEAD_RETENTION_DAYS = 30


def _disable_webhook_subscription(subscription: WebhookSubscription, *, reason: str) -> None:
    subscription.is_active = False
    subscription.disabled_reason = reason
    logger.error(
        "webhook_subscription_disabled", subscription_id=str(subscription.id), reason=reason
    )


async def _send_webhook(subscription: WebhookSubscription, delivery: WebhookDelivery) -> int:
    """Отправляет доставку и возвращает код ответа; сетевые ошибки уходят исключением
    `httpx.HTTPError` — вызывающий решает backoff/dead-letter. Адрес никогда не попадает в
    исключение наружу этой функции (см. `_deliver_webhook`: используется только тип ошибки)."""
    settings = get_settings()
    url = await check_outbound_url_resolved(subscription.url, strict=settings.is_prod)
    secret = decrypt_value(subscription.secret_encrypted)
    body = json.dumps(
        {
            "id": str(delivery.id),
            "event": delivery.event_type,
            "occurred_at": delivery.created_at.isoformat(),
            "data": delivery.payload or {},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    timestamp = str(int(dt.datetime.now(dt.UTC).timestamp()))
    signature = compute_signature(secret, timestamp.encode("ascii") + b"." + body)
    headers = {
        "Content-Type": "application/json",
        "X-Signature": f"sha256={signature}",
        "X-Timestamp": timestamp,
        "X-CRM-Event": delivery.event_type,
        "X-CRM-Delivery": str(delivery.id),
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        response = await client.post(url, content=body, headers=headers, follow_redirects=False)
    return response.status_code


@track_task
async def sweep_webhook_deliveries(ctx: dict[str, Any]) -> dict[str, int]:
    counters = {"sent": 0, "failed": 0, "dead": 0}
    claimed = await _claim_webhook_deliveries()
    for delivery_id, lease in claimed:
        await _deliver_webhook(delivery_id, lease, counters)
    await _cleanup_webhook_deliveries()
    return counters


async def _claim_webhook_deliveries() -> list[tuple[uuid.UUID, dt.datetime]]:
    async with session_scope() as session:
        now = dt.datetime.now(dt.UTC)
        lease = now + dt.timedelta(seconds=_WEBHOOK_LEASE_SECONDS)
        rows = (
            (
                await session.execute(
                    select(WebhookDelivery)
                    .where(
                        WebhookDelivery.status.in_(
                            [
                                WebhookDeliveryStatus.PENDING.value,
                                WebhookDeliveryStatus.FAILED.value,
                            ]
                        ),
                        (WebhookDelivery.next_retry_at.is_(None))
                        | (WebhookDelivery.next_retry_at <= now),
                    )
                    .order_by(WebhookDelivery.created_at)
                    .limit(_WEBHOOK_BATCH_SIZE)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        claimed: list[tuple[uuid.UUID, dt.datetime]] = []
        for delivery in rows:
            delivery.attempts += 1
            delivery.next_retry_at = lease
            claimed.append((delivery.id, lease))
        return claimed


async def _deliver_webhook(
    delivery_id: uuid.UUID, lease: dt.datetime, counters: dict[str, int]
) -> None:
    failure: str | None = None
    async with session_scope() as session:
        delivery = (
            await session.execute(
                select(WebhookDelivery).where(WebhookDelivery.id == delivery_id).with_for_update()
            )
        ).scalar_one_or_none()
        if (
            delivery is None
            or delivery.status
            not in (WebhookDeliveryStatus.PENDING.value, WebhookDeliveryStatus.FAILED.value)
            or delivery.next_retry_at != lease
        ):
            return
        subscription = await session.get(WebhookSubscription, delivery.subscription_id)
        if subscription is None or not subscription.is_active:
            # Подписку сняли/выключили между постановкой доставки и её разбором: не копим
            # попытки на мёртвый адрес, но это не провал подписки — просто нечего доставлять.
            delivery.status = WebhookDeliveryStatus.DEAD.value
            delivery.last_error = "subscription_inactive"
            delivery.next_retry_at = None
            counters["dead"] += 1
            webhook_deliveries_total.labels(result="dead").inc()
            return
        try:
            status_code = await _send_webhook(subscription, delivery)
        except Exception as exc:  # noqa: BLE001 — доставка не должна ронять цикл
            await session.rollback()
            failure = f"request_failed:{type(exc).__name__}"
        else:
            delivery.last_status_code = status_code
            if 200 <= status_code < 300:
                delivery.status = WebhookDeliveryStatus.SENT.value
                delivery.sent_at = dt.datetime.now(dt.UTC)
                delivery.next_retry_at = None
                delivery.last_error = None
                subscription.consecutive_failures = 0
                counters["sent"] += 1
                webhook_deliveries_total.labels(result="sent").inc()
                return
            if status_code == 410:
                delivery.status = WebhookDeliveryStatus.DEAD.value
                delivery.next_retry_at = None
                delivery.last_error = "http_410"
                _disable_webhook_subscription(subscription, reason="gone_410")
                invalidate_cache_after_commit(session)
                counters["dead"] += 1
                webhook_deliveries_total.labels(result="dead").inc()
                return
            failure = f"http_{status_code}"

    async with session_scope() as session:
        delivery = (
            await session.execute(
                select(WebhookDelivery).where(WebhookDelivery.id == delivery_id).with_for_update()
            )
        ).scalar_one_or_none()
        if delivery is None or delivery.next_retry_at != lease:
            return
        # Попытка уже посчитана при захвате.
        delivery.last_error = failure
        if delivery.attempts >= _WEBHOOK_MAX_ATTEMPTS:
            delivery.status = WebhookDeliveryStatus.DEAD.value
            delivery.next_retry_at = None
            counters["dead"] += 1
            webhook_deliveries_total.labels(result="dead").inc()
            logger.error(
                "webhook_delivery_dead_letter",
                delivery_id=str(delivery.id),
                subscription_id=str(delivery.subscription_id),
                event_type=delivery.event_type,
                attempts=delivery.attempts,
            )
            subscription = await session.get(WebhookSubscription, delivery.subscription_id)
            if subscription is not None and subscription.is_active:
                subscription.consecutive_failures += 1
                if subscription.consecutive_failures >= _WEBHOOK_DISABLE_AFTER_DEAD:
                    _disable_webhook_subscription(subscription, reason="too_many_failures")
                    invalidate_cache_after_commit(session)
        else:
            delivery.status = WebhookDeliveryStatus.FAILED.value
            delay = _WEBHOOK_BACKOFF_SECONDS[
                min(delivery.attempts - 1, len(_WEBHOOK_BACKOFF_SECONDS) - 1)
            ]
            delivery.next_retry_at = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=delay)
            counters["failed"] += 1
            webhook_deliveries_total.labels(result="failed").inc()


async def _cleanup_webhook_deliveries() -> None:
    """Раздел «очистка»: `sent` старше 7 дней и `dead` старше 30 дней — тот же порядок величин,
    что ретеншн других очередей доставки (`reports_retention_days`, `retention_otp_days`)."""
    now = dt.datetime.now(dt.UTC)
    async with session_scope() as session:
        await session.execute(
            delete(WebhookDelivery).where(
                WebhookDelivery.status == WebhookDeliveryStatus.SENT.value,
                WebhookDelivery.sent_at.is_not(None),
                WebhookDelivery.sent_at < now - dt.timedelta(days=_WEBHOOK_SENT_RETENTION_DAYS),
            )
        )
        await session.execute(
            delete(WebhookDelivery).where(
                WebhookDelivery.status == WebhookDeliveryStatus.DEAD.value,
                WebhookDelivery.created_at < now - dt.timedelta(days=_WEBHOOK_DEAD_RETENTION_DAYS),
            )
        )
