"""Prometheus-метрики.

Спецификация требует RED-метрики (Rate, Errors, Duration), длину очереди,
время импорта, нарушения SLA и hit-rate кэша.

Метрики живут в памяти процесса. HTTP-метрики, `crm_cache_requests_total`, `crm_dependency_up` и
`crm_queue_depth` ведёт api (`GET /metrics`), метрики фоновых задач, SLA, отчётов и импорта — воркер
arq, у которого свой `/metrics` (`start_worker_metrics_server`, порт `WORKER_METRICS_PORT`).
"""

from __future__ import annotations

import functools
import gc
import os
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any
from wsgiref.simple_server import WSGIServer

import structlog
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    multiprocess,
    start_http_server,
)
from prometheus_client import generate_latest as _generate_latest

logger = structlog.get_logger(__name__)

# --- RED ------------------------------------------------------------------

http_requests_total = Counter(
    "crm_http_requests_total",
    "Количество HTTP-запросов",
    labelnames=("method", "route", "status"),
)

http_request_duration_seconds = Histogram(
    "crm_http_request_duration_seconds",
    "Длительность обработки HTTP-запроса",
    labelnames=("method", "route"),
    # Бакеты подобраны под бюджет p95 = 300 мс для переходов по сделке.
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1.0, 2.5, 5.0, 10.0),
)

http_errors_total = Counter(
    "crm_http_errors_total",
    "Количество ошибочных ответов по коду каталога",
    labelnames=("route", "code", "status"),
)

# --- Очередь arq ----------------------------------------------------------

queue_depth = Gauge(
    "crm_queue_depth",
    "Количество задач в очереди",
    labelnames=("queue",),
)

background_task_duration_seconds = Histogram(
    "crm_background_task_duration_seconds",
    "Длительность фоновой задачи",
    labelnames=("task",),
    buckets=(0.1, 0.5, 1, 5, 15, 60, 300, 900, 3600),
)

background_tasks_total = Counter(
    "crm_background_tasks_total",
    "Завершённые фоновые задачи",
    labelnames=("task", "result"),
)

# --- Импорт ---------------------------------------------------------------

# `phase="validate"` — вызов проверки (dry-run) целиком, `phase="apply"` — задание от `started_at`
# до `finished_at`. У отката момента старта в задании нет, поэтому его длительность не измеряется.
import_duration_seconds = Histogram(
    "crm_import_duration_seconds",
    "Время выполнения импорта каталога",
    labelnames=("entity_type", "phase"),
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800),
)

import_rows_total = Counter(
    "crm_import_rows_total",
    "Обработанные строки импорта: applied/skipped/error при применении, "
    "rolled_back/rollback_blocked при откате",
    labelnames=("entity_type", "status"),
)

# --- SLA ------------------------------------------------------------------

sla_violations_total = Counter(
    "crm_sla_violations_total",
    "Нарушения SLA по статусам воронки",
    labelnames=("workflow", "status"),
)

sla_breaching_deals = Gauge(
    "crm_sla_breaching_deals",
    "Текущее число сделок с нарушенным SLA",
    labelnames=("sla_state",),
)

# --- Кэш ------------------------------------------------------------------

cache_requests_total = Counter(
    "crm_cache_requests_total",
    "Обращения к кэшу для расчёта hit-rate",
    labelnames=("cache", "result"),
)

# --- Отчёты и подписи ----------------------------------------------------

reports_in_progress = Gauge(
    "crm_reports_in_progress",
    "Число отчётов в работе (лимит REPORTS_MAX_CONCURRENT)",
)

audit_records_total = Counter(
    "crm_audit_records_total",
    "Записи аудита",
    labelnames=("action", "result"),
)

dependency_up = Gauge(
    "crm_dependency_up",
    "Доступность внешней зависимости по данным /health/ready",
    labelnames=("dependency",),
)


# --- Процесс --------------------------------------------------------------

gc_pause_seconds = Histogram(
    "crm_gc_pause_seconds",
    "Пауза сборщика мусора Python по поколениям: событийный цикл стоит всё это время",
    labelnames=("generation",),
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0),
)

_gc_started: dict[int, float] = {}


def _observe_gc(phase: str, info: dict[str, Any]) -> None:
    generation = int(info["generation"])
    if phase == "start":
        _gc_started[generation] = time.perf_counter()
        return
    started = _gc_started.pop(generation, None)
    if started is not None:
        gc_pause_seconds.labels(generation=str(generation)).observe(time.perf_counter() - started)


def install_gc_metrics() -> None:
    """Подписывает `gc_pause_seconds` на сборки мусора; повторный вызов ничего не делает."""
    if _observe_gc not in gc.callbacks:
        gc.callbacks.append(_observe_gc)


def record_cache(cache: str, *, hit: bool) -> None:
    cache_requests_total.labels(cache=cache, result="hit" if hit else "miss").inc()


def track_task[**P, R](func: Callable[P, Awaitable[R]]) -> Callable[P, Coroutine[Any, Any, R]]:
    """Декоратор фоновой задачи arq: длительность и итог (`success`/`failure`) под её именем.

    Итог считается один раз на запуск, в том числе для досрочного возврата из задачи; отмена
    (таймаут задания, остановка воркера) — тоже `failure`. Имя и `__qualname__` сохраняются: arq
    регистрирует функцию по ним, а `WorkerSettings` и тесты сравнивают саму функцию.
    """
    task = func.__name__

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        started = time.perf_counter()
        result = "success"
        try:
            return await func(*args, **kwargs)
        except BaseException:
            result = "failure"
            raise
        finally:
            background_task_duration_seconds.labels(task=task).observe(
                time.perf_counter() - started
            )
            background_tasks_total.labels(task=task, result=result).inc()

    return wrapper


def start_worker_metrics_server(port: int) -> WSGIServer | None:
    """Поднимает `/metrics` воркера на `port`; `0` — выключено.

    Воркер не слушает HTTP, поэтому его метрики иначе никто не заберёт. Занятый порт не должен
    ронять воркер: очередь важнее метрик, поэтому ошибка только пишется в лог. Возвращает сервер
    для `stop_worker_metrics_server` или `None`, если он не запущен.
    """
    if port == 0:
        return None
    try:
        server, _thread = start_http_server(port)
    except OSError:
        logger.warning("worker_metrics_server_failed", port=port, exc_info=True)
        return None
    logger.info("worker_metrics_server_started", port=port)
    return server


def stop_worker_metrics_server(server: WSGIServer | None) -> None:
    if server is None:
        return
    server.shutdown()
    server.server_close()


def render_metrics() -> bytes:
    """Рендер метрик. Поддерживает multiprocess-режим, если задан PROMETHEUS_MULTIPROC_DIR."""
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return _generate_latest(registry)
    return _generate_latest()
