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
from app.modules.crm.tasks import sweep_sla_breaches
from app.modules.imports.tasks import sweep_import_jobs
from app.modules.registry.tasks import sweep_registry_drift, sweep_registry_imports
from app.modules.signing.tasks import sweep_signature_deadlines, sweep_signature_otp_cleanup
from app.modules.workflow.tasks import sweep_status_mapping_jobs

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

    functions = [
        ensure_audit_partitions,
        purge_expired_idempotency_keys,
        sweep_status_mapping_jobs,
        sweep_sla_breaches,
        sweep_registry_imports,
        sweep_registry_drift,
        sweep_import_jobs,
        sweep_signature_deadlines,
        sweep_signature_otp_cleanup,
    ]
    cron_jobs = [
        # Раз в сутки: партиции аудита на будущее.
        cron(ensure_audit_partitions, hour=3, minute=0),
        # Раз в час: чистка просроченных идемпотентных ключей.
        cron(purge_expired_idempotency_keys, minute=15),
        # Раз в минуту: продолжение переноса сделок при архивировании статуса
        # воронки (раздел 6.5). Задач обычно ноль — партиция уникального
        # индекса `uq_status_mapping_jobs_active` держит не больше одной
        # незавершённой задачи на статус.
        cron(sweep_status_mapping_jobs, minute=set(range(60))),
        # Раз в 15 минут: эскалация SLA (new_spec §4.10).
        cron(sweep_sla_breaches, minute={0, 15, 30, 45}),
        # Раз в минуту: подхватывает `pending`-версию реестра ЕГРЮЛ, если
        # админ только что запустил импорт (dop.md §11.3).
        cron(sweep_registry_imports, minute=set(range(60))),
        # Раз в сутки: сверка реквизитов организаций с локальным реестром
        # (dop.md §11.7 — интервал внутри самой задачи, `registry_drift_interval_days`).
        cron(sweep_registry_drift, hour=4, minute=30),
        # Раз в минуту: батчи применения/отката импорта каталогов (раздел 4.12).
        cron(sweep_import_jobs, minute=set(range(60))),
        # Раз в 15 минут: просроченные запросы на подпись (spec.txt §15
        # `signature.expire_deadlines`).
        cron(sweep_signature_deadlines, minute={0, 15, 30, 45}),
        # Раз в сутки: чистка OTP-кодов старше 30 дней (`signature.clean_otp`).
        cron(sweep_signature_otp_cleanup, hour=4, minute=45),
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
