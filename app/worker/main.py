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
from app.core.metrics import start_worker_metrics_server, stop_worker_metrics_server, track_task
from app.core.redis_client import close_redis
from app.modules.admin.models import IdempotencyKey
from app.modules.admin.tasks import sweep_retention
from app.modules.crm.tasks import sweep_sla_breaches
from app.modules.files.antivirus import build_scanner
from app.modules.files.service import register_antivirus_scanner
from app.modules.identity.tasks import (
    sweep_erasure_requests,
    sweep_incident_deadlines,
    sweep_keycloak_events,
    sweep_user_lifecycle,
)
from app.modules.imports.tasks import run_import_job, sweep_import_jobs
from app.modules.integration.security import resolve_secret
from app.modules.integration.service import RealOutboxService, register_outbox_service
from app.modules.integration.tasks import (
    sweep_cms_leads_pull,
    sweep_lms_progress_pull,
    sweep_outbox_events,
    sweep_webhook_deliveries,
)
from app.modules.notification.service import RealNotificationService, register_notification_service
from app.modules.notification.tasks import dispatch_pending_notifications
from app.modules.registry.tasks import sweep_registry_drift, sweep_registry_imports
from app.modules.reporting.tasks import (
    expire_report_files,
    refresh_report_materialized_views,
    render_report_job,
    sweep_report_jobs,
)
from app.modules.signing.tasks import (
    sweep_signature_deadlines,
    sweep_signature_orphans,
    sweep_signature_reminders,
)
from app.modules.workflow.tasks import sweep_status_mapping_jobs

logger = structlog.get_logger(__name__)


@track_task
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
    logger.info("audit_partitions_ensured", months=created)
    return {"months": created}


@track_task
async def purge_expired_idempotency_keys(ctx: dict[str, Any]) -> dict[str, int]:
    """Удаляет идемпотентные ключи с истёкшим сроком (TTL 24 часа)."""
    async with session_scope() as session:
        result = await session.execute(
            delete(IdempotencyKey).where(IdempotencyKey.expires_at < dt.datetime.now(dt.UTC))
        )
    removed = result.rowcount or 0
    logger.info("idempotency_keys_purged", removed=removed)
    return {"removed": removed}


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    logger.info("worker_starting", profile=settings.app_profile)
    # Отдельный процесс от API — регистрация в `app/main.py` сюда не долетает,
    # `_service` в `notification/service.py` живёт per-процесс.
    register_notification_service(RealNotificationService())
    # То же самое для outbox: доставка (sweep_outbox_events) живёт в этом
    # процессе, публикация (crm/signing) — в `api`. Оба обязаны
    # зарегистрировать реализацию независимо.
    register_outbox_service(RealOutboxService())
    # B6-4: тот же приём — регистрация реального сканера per-процесс. Воркер пока не
    # сканирует сам (sweep_quarantined_files не реализован в этом эпике), но будущий код
    # воркера, который позовёт `get_antivirus_scanner()`, не должен попасть на заглушку
    # молча только потому, что забыли продублировать регистрацию.
    clamav_scanner = build_scanner(settings)
    if clamav_scanner is not None:
        register_antivirus_scanner(clamav_scanner)
    # Метрики фоновых задач считаются в этом процессе, api их не видит: отдаём сами.
    ctx["metrics_server"] = start_worker_metrics_server(settings.worker_metrics_port)

    # B5-1: `*_AUTH_REF` указывает ИМЯ переменной с токеном, а не сам токен — если
    # такой переменной нет в окружении (забыли добавить в `x-api-env` compose/Helm,
    # см. риски B5-1), `resolve_secret` молча вернёт `None`, и запрос к источнику
    # уйдёт без `Authorization`. Старт не блокируем: без токена моки тоже работают.
    for base_url, auth_ref, source_code in (
        (settings.cms_base_url, settings.cms_auth_ref, "cms"),
        (settings.lms_base_url, settings.lms_auth_ref, "lms"),
    ):
        if base_url and auth_ref and not resolve_secret(auth_ref):
            logger.warning(
                "integration_auth_token_missing",
                source=source_code,
                auth_ref=auth_ref,
                detail=f"{auth_ref} не задан в окружении — запросы уйдут без Authorization",
            )


async def shutdown(ctx: dict[str, Any]) -> None:
    stop_worker_metrics_server(ctx.get("metrics_server"))
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
        sweep_retention,
        sweep_status_mapping_jobs,
        sweep_sla_breaches,
        sweep_registry_imports,
        sweep_registry_drift,
        sweep_import_jobs,
        # B8-11: не cron — ставятся из API сразу после коммита (`enqueue_after_commit`).
        run_import_job,
        render_report_job,
        sweep_signature_deadlines,
        sweep_signature_orphans,
        sweep_signature_reminders,
        dispatch_pending_notifications,
        sweep_report_jobs,
        expire_report_files,
        refresh_report_materialized_views,
        sweep_outbox_events,
        sweep_lms_progress_pull,
        sweep_cms_leads_pull,
        sweep_webhook_deliveries,
        sweep_erasure_requests,
        sweep_user_lifecycle,
        sweep_keycloak_events,
        sweep_incident_deadlines,
    ]
    cron_jobs = [
        # Раз в сутки, до партиций аудита: очистка по сроку хранения (B6-6, 152-ФЗ ст. 5 ч. 7).
        cron(sweep_retention, hour=2, minute=30),
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
        # Раз в сутки: объекты бакета signatures под signed/ без строки files (B7-6).
        cron(sweep_signature_orphans, hour=4, minute=50),
        # Раз в 30 минут: напоминания подписантам о приближающемся дедлайне (B7-1).
        cron(sweep_signature_reminders, minute={5, 35}),
        # Раз в минуту: доставка уведомлений во внешние каналы
        # (`notifications.dispatch`, spec.txt §6.11/строка 890).
        cron(dispatch_pending_notifications, minute=set(range(60))),
        # Раз в минуту: тяжёлые отчёты из очереди (раздел 4.13, семафор —
        # `reports_max_concurrent`).
        cron(sweep_report_jobs, minute=set(range(60))),
        # Раз в сутки: удаление файлов готовых отчётов старше
        # `reports_retention_days` (раздел 4.13).
        cron(expire_report_files, hour=5, minute=0),
        # Раз в 5 минут: обновление материализованного представления для
        # быстрого (ADMIN-скоуп) пути отчёта «Воронка по статусам»
        # (раздел 3.4: «Кэш агрегатов в Redis на 5 минут»).
        cron(
            refresh_report_materialized_views,
            minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
        ),
        # Раз в минуту: доставка outbox-событий (раздел 3.6, backoff
        # 1s/5s/30s/5m/30m/2h — минутный тик приближает «доставить как можно
        # быстрее» для первой, самой короткой ступени).
        cron(sweep_outbox_events, minute=set(range(60))),
        # B5-6: тик раз в минуту, реальный интервал задаёт integration_sources.
        # pull_interval_seconds источника lms (по умолчанию 1800 с = раз в 30 минут,
        # раздел 4.14, дословно) — тот же приём, что sweep_cms_leads_pull ниже.
        cron(sweep_lms_progress_pull, minute=set(range(60))),
        # Раз в минуту: реальный интервал задаёт integration_sources.pull_interval_seconds
        # (B5-2), тик, пришедшийся раньше срока, сам себя пропускает.
        cron(sweep_cms_leads_pull, minute=set(range(60))),
        # Раз в минуту: доставка webhook-подписок (B5-9), тот же приём, что sweep_outbox_events
        # выше — backoff 1с/5с/30с/5м/30м/2ч, dead-letter после 8 попыток.
        cron(sweep_webhook_deliveries, minute=set(range(60))),
        # Раз в 15 минут: исполнение удаления/обезличивания по истечении
        # отсрочки режима A (new_spec §4.8.4 шаг 4-5, 30 дней по умолчанию) —
        # та же периодичность, что и SLA-скан: отсрочка считается днями, чаще
        # проверять незачем, реже — заметная задержка исполнения обязательства
        # по 152-ФЗ.
        cron(sweep_erasure_requests, minute={0, 15, 30, 45}),
        # Раз в 5 минут: снятие блокировки по `auto_unblock_at` и `INVITE_EXPIRED`
        # (приглашение не принято за 30 дней) — new_spec §4.5, §4.1.
        cron(sweep_user_lifecycle, minute=set(range(0, 60, 5))),
        # Раз в минуту: опрос Admin API Keycloak на LOGIN_ERROR -> LOGIN_FAILED/
        # BRUTE_FORCE_LOCK (B6-3, группа АУД). Нет-оп, пока KEYCLOAK_EVENTS_POLL_ENABLED=false
        # (дефолт, до включения событий в realm — спринт D1).
        cron(sweep_keycloak_events, minute=set(range(60))),
        # Раз в 15 минут (B6-12): напоминания о сроке уведомления РКН (24/72 ч, 152-ФЗ
        # ст. 21 ч. 3.1) для подтверждённых инцидентов. Периодичность зашита здесь: `arq` не
        # читает `Settings` в объявлении cron-слотов.
        cron(sweep_incident_deadlines, minute={0, 15, 30, 45}),
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
