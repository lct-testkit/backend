"""Prometheus-метрики.

Спецификация требует RED-метрики (Rate, Errors, Duration), длину очереди,
время импорта, нарушения SLA и hit-rate кэша.
"""

from __future__ import annotations

import os

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, multiprocess
from prometheus_client import generate_latest as _generate_latest

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

import_duration_seconds = Histogram(
    "crm_import_duration_seconds",
    "Время выполнения импорта каталога",
    labelnames=("entity_type", "phase"),
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800),
)

import_rows_total = Counter(
    "crm_import_rows_total",
    "Обработанные строки импорта",
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


def record_cache(cache: str, *, hit: bool) -> None:
    cache_requests_total.labels(cache=cache, result="hit" if hit else "miss").inc()


def render_metrics() -> bytes:
    """Рендер метрик. Поддерживает multiprocess-режим, если задан PROMETHEUS_MULTIPROC_DIR."""
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        return _generate_latest(registry)
    return _generate_latest()
