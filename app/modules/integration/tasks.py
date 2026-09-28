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
import uuid
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.metrics import track_task
from app.modules.admin.models import FeatureFlag
from app.modules.integration import bitrix, lms
from app.modules.integration.models import IntegrationSource, OutboxEvent, OutboxStatus
from app.modules.integration.security import (
    check_outbound_url_resolved,
    resolve_secret,
    same_host,
)

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
    """Раз в 30 минут (раздел 4.14): `GET /students/progress?updated_since=`
    с курсором в `sync_cursors`."""
    settings = get_settings()
    if not settings.lms_base_url:
        return {"pulled": 0, "skipped": 1}

    async with session_scope() as session:
        cursor = await lms.get_cursor(session, source_code="lms", resource="students_progress")
        client = lms.LmsClient(settings.lms_base_url, resolve_secret(settings.lms_auth_ref))
        try:
            rows = await client.pull_progress(updated_since=cursor)
        except Exception as exc:  # noqa: BLE001 — как sweep_registry_drift: не роняем cron
            logger.warning("lms_progress_pull_failed", error=str(exc))
            return {"pulled": 0, "error": 1}

        # Каждая строка — в своём SAVEPOINT: одна битая строка не откатывает остальные, а курсор не
        # уходит дальше строк, которые применить не удалось (их следующий тик получит снова).
        rows = rows if isinstance(rows, list) else []
        batch = await lms.apply_progress_rows(session, rows)
        latest_cursor = lms.advance_cursor(cursor, rows, batch)
        if batch.failed:
            logger.warning(
                "lms_progress_pull_partial",
                failed=batch.failed,
                cursor_held=latest_cursor == cursor,
            )

        if latest_cursor and latest_cursor != cursor:
            await lms.set_cursor(
                session, source_code="lms", resource="students_progress", value=latest_cursor
            )

    return {"pulled": batch.applied, "skipped": batch.skipped, "failed": batch.failed}
