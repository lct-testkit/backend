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
integrations/outbox-events?status=dead`.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.metrics import background_tasks_total
from app.modules.integration import bitrix, lms
from app.modules.integration.models import IntegrationSource, OutboxEvent, OutboxStatus
from app.modules.integration.security import resolve_secret

logger = structlog.get_logger(__name__)

# Раздел 3.6, дословно.
_BACKOFF_SECONDS = [1, 5, 30, 300, 1800, 7200]
_MAX_ATTEMPTS = 8
_KNOWN_TARGETS = frozenset({"lms", "bitrix24"})


async def sweep_outbox_events(ctx: dict[str, Any]) -> dict[str, int]:
    settings = get_settings()
    sent = failed = dead = skipped = 0
    async with session_scope() as session:
        now = dt.datetime.now(dt.UTC)
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
                )
            )
            .scalars()
            .all()
        )

        sources = {
            row.code: row
            for row in (await session.execute(select(IntegrationSource))).scalars().all()
        }

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
                sent += 1
                continue
            if event.target not in _KNOWN_TARGETS:
                event.status = OutboxStatus.DEAD.value
                event.last_error = "unknown_target"
                skipped += 1
                continue

            source = sources.get(event.target)
            active = bool(source and source.is_active)
            if event.target == "bitrix24":
                active = active and settings.bitrix_connector_enabled

            if not active:
                event.status = OutboxStatus.DEAD.value
                event.last_error = "source_inactive"
                skipped += 1
                continue

            try:
                await _deliver(session, event, source)
                event.status = OutboxStatus.SENT.value
                event.sent_at = now
                sent += 1
            except Exception as exc:  # noqa: BLE001 — доставка не должна ронять цикл
                event.attempts += 1
                event.last_error = str(exc)[:2000]
                if event.attempts >= _MAX_ATTEMPTS:
                    event.status = OutboxStatus.DEAD.value
                    dead += 1
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
                    event.next_retry_at = now + dt.timedelta(seconds=delay)
                    failed += 1

    background_tasks_total.labels(task="sweep_outbox_events", result="success").inc()
    return {"sent": sent, "failed": failed, "dead": dead, "skipped": skipped}


async def _deliver(
    session: AsyncSession, event: OutboxEvent, source: IntegrationSource | None
) -> None:
    settings = get_settings()
    if event.target == "lms":
        base_url = (source.base_url if source else None) or settings.lms_base_url
        if not base_url:
            raise RuntimeError("lms_base_url not configured")
        client = lms.LmsClient(base_url, resolve_secret(settings.lms_auth_ref))
        await client.push_enrollment(
            {
                "deal_id": str(event.aggregate_id),
                "event_type": event.event_type,
                **(event.payload or {}),
            }
        )
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
        await bitrix.push_deal(session, client, deal_id=event.aggregate_id)
        return

    raise RuntimeError(f"no deliverer for target={event.target!r}")


async def sweep_lms_progress_pull(ctx: dict[str, Any]) -> dict[str, int]:
    """Раз в 30 минут (раздел 4.14): `GET /students/progress?updated_since=`
    с курсором в `sync_cursors`."""
    settings = get_settings()
    if not settings.lms_base_url:
        return {"pulled": 0, "skipped": 1}

    pulled = 0
    async with session_scope() as session:
        cursor = await lms.get_cursor(session, source_code="lms", resource="students_progress")
        client = lms.LmsClient(settings.lms_base_url, resolve_secret(settings.lms_auth_ref))
        try:
            rows = await client.pull_progress(updated_since=cursor)
        except Exception as exc:  # noqa: BLE001 — как sweep_registry_drift: не роняем cron
            logger.warning("lms_progress_pull_failed", error=str(exc))
            return {"pulled": 0, "error": 1}

        latest_cursor = cursor
        for row in rows:
            result = await lms.upsert_progress(session, row)
            if result is not None:
                pulled += 1
            candidate = row.get("updated_at")
            if candidate and (latest_cursor is None or str(candidate) > str(latest_cursor)):
                latest_cursor = str(candidate)

        if latest_cursor and latest_cursor != cursor:
            await lms.set_cursor(
                session, source_code="lms", resource="students_progress", value=latest_cursor
            )

    background_tasks_total.labels(task="sweep_lms_progress_pull", result="success").inc()
    return {"pulled": pulled}
