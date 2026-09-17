"""Воркер и планировщик на arq.

На каркасе здесь только служебные задачи: подготовка партиций аудита,
чистка просроченных идемпотентных ключей и обновление метрики длины очереди.
Прикладные задачи (импорт, отчёты, доставка outbox, напоминания по подписям)
добавляются в своих спринтах.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import structlog
from arq import cron
from arq.connections import RedisSettings
from sqlalchemy import delete, text

from app.core.config import get_settings
from app.core.db import dispose_engine, session_scope
from app.core.logging import configure_logging
from app.core.metrics import background_tasks_total
from app.core.redis_client import close_redis
from app.modules.admin.models import IdempotencyKey

logger = structlog.get_logger(__name__)


async def ensure_audit_partitions(ctx: dict[str, Any]) -> dict[str, int]:
    """Создаёт партиции audit_log на ближайшие месяцы.

    Запускается заранее: если партиции не окажется, вставка уйдёт в DEFAULT,
    а это со временем превращается в узкое место.
    """
    created = 0
    async with session_scope() as session:
        for offset in range(0, 4):
            await session.execute(
                text(
                    "SELECT create_audit_log_partition("
                    "(date_trunc('month', now()) + make_interval(months => :offset))::date)"
                ),
                {"offset": offset},
            )
            created += 1
    background_tasks_total.labels(task="ensure_audit_partitions", result="success").inc()
    logger.info("audit_partitions_ensured", months=created)
    return {"months": created}


async def purge_expired_idempotency_keys(ctx: dict[str, Any]) -> dict[str, int]:
    """Удаляет идемпотентные ключи с истёкшим сроком (TTL 24 часа)."""
    async with session_scope() as session:
        result = await session.execute(
            delete(IdempotencyKey).where(IdempotencyKey.expires_at < dt.datetime.now(dt.UTC))
        )
    removed = result.rowcount or 0
    background_tasks_total.labels(task="purge_expired_idempotency_keys", result="success").inc()
    logger.info("idempotency_keys_purged", removed=removed)
    return {"removed": removed}


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    logger.info("worker_starting", profile=settings.app_profile)


async def shutdown(ctx: dict[str, Any]) -> None:
    await dispose_engine()
    await close_redis()
    logger.info("worker_stopped")


def _redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(get_settings().redis_url)


class WorkerSettings:
    """Загружается командой `arq app.worker.main.WorkerSettings`."""

    functions = [ensure_audit_partitions, purge_expired_idempotency_keys]
    cron_jobs = [
        # Раз в сутки: партиции аудита на будущее.
        cron(ensure_audit_partitions, hour=3, minute=0),
        # Раз в час: чистка просроченных идемпотентных ключей.
        cron(purge_expired_idempotency_keys, minute=15),
    ]
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 10
    job_timeout = 3600
    keep_result = 3600
    # Воркер не слушает HTTP, поэтому его healthcheck читает heartbeat в Redis
    # командой `arq ... --check`. Интервал по умолчанию — час, при нём мёртвый
    # воркер выглядел бы живым слишком долго.
    health_check_interval = 30

    @property
    def redis_settings(self) -> RedisSettings:  # pragma: no cover - читается arq
        return _redis_settings()


# arq читает атрибут класса, а не property, поэтому задаём его явно.
WorkerSettings.redis_settings = _redis_settings()  # type: ignore[assignment]
