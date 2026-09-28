"""Метрики воркера arq: декоратор задач, `/metrics` воркера и порт `WORKER_METRICS_PORT`.

Без БД и Redis: метрики проверяются по реестру `prometheus_client`, сервер поднимается на
свободном порту loopback.
"""

from __future__ import annotations

import asyncio
import re
import socket
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from arq.worker import func as arq_func
from prometheus_client import REGISTRY
from pydantic import ValidationError

from app.core import metrics
from app.core.config import Settings
from app.core.metrics import (
    background_tasks_total,
    start_worker_metrics_server,
    stop_worker_metrics_server,
    track_task,
)


def _sample(name: str, **labels: str) -> float | None:
    return REGISTRY.get_sample_value(name, labels)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _scrape(port: int) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as response:
        return response.read().decode("utf-8")


# --- track_task ------------------------------------------------------------


async def test_track_task_counts_success_and_observes_duration() -> None:
    @track_task
    async def tracked_ok_task(ctx: dict[str, object]) -> dict[str, int]:
        return {"processed": 3}

    assert await tracked_ok_task({}) == {"processed": 3}

    assert _sample("crm_background_tasks_total", task="tracked_ok_task", result="success") == 1
    assert _sample("crm_background_tasks_total", task="tracked_ok_task", result="failure") is None
    assert _sample("crm_background_task_duration_seconds_count", task="tracked_ok_task") == 1


async def test_track_task_counts_failure_and_reraises() -> None:
    @track_task
    async def tracked_failing_task(ctx: dict[str, object]) -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await tracked_failing_task({})

    assert _sample("crm_background_tasks_total", task="tracked_failing_task", result="failure") == 1
    assert (
        _sample("crm_background_tasks_total", task="tracked_failing_task", result="success") is None
    )
    # Длительность неудачного запуска тоже видна: зависшая и упавшая задача — разные картины.
    assert _sample("crm_background_task_duration_seconds_count", task="tracked_failing_task") == 1


async def test_track_task_counts_cancellation_as_failure() -> None:
    started = asyncio.Event()

    @track_task
    async def tracked_cancelled_task(ctx: dict[str, object]) -> None:
        started.set()
        await asyncio.sleep(60)

    job = asyncio.ensure_future(tracked_cancelled_task({}))
    await started.wait()
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job

    assert (
        _sample("crm_background_tasks_total", task="tracked_cancelled_task", result="failure") == 1
    )


async def test_track_task_keeps_identity_that_arq_relies_on() -> None:
    async def tracked_named_task(ctx: dict[str, object]) -> None:
        return None

    wrapped = track_task(tracked_named_task)

    assert wrapped.__name__ == tracked_named_task.__name__
    assert wrapped.__qualname__ == tracked_named_task.__qualname__
    assert asyncio.iscoroutinefunction(wrapped)
    # arq регистрирует задачу под `__qualname__` и требует корутинную функцию.
    assert arq_func(wrapped).name == tracked_named_task.__qualname__


def test_every_worker_task_and_cron_job_is_tracked() -> None:
    from app.worker.main import WorkerSettings

    untracked_functions = [
        function.__name__
        for function in WorkerSettings.functions
        if not hasattr(function, "__wrapped__")
    ]
    untracked_cron = [
        job.name for job in WorkerSettings.cron_jobs if not hasattr(job.coroutine, "__wrapped__")
    ]

    assert untracked_functions == []
    assert untracked_cron == []


def test_background_tasks_total_is_only_touched_by_the_decorator() -> None:
    """Ручной `.inc()` рядом с `@track_task` считал бы каждый запуск дважды."""
    app_dir = Path(__file__).resolve().parent.parent / "app"
    offenders = sorted(
        str(path.relative_to(app_dir))
        for path in app_dir.rglob("*.py")
        if path.name != "metrics.py" and "background_tasks_total.labels" in path.read_text("utf-8")
    )

    assert offenders == []


# --- /metrics воркера ------------------------------------------------------


def test_port_zero_disables_metrics_server() -> None:
    assert start_worker_metrics_server(0) is None
    stop_worker_metrics_server(None)  # остановка «ничего» не падает


def test_metrics_server_serves_worker_metrics_until_stopped() -> None:
    background_tasks_total.labels(task="metrics_server_probe", result="success").inc()
    port = _free_port()

    server = start_worker_metrics_server(port)
    try:
        assert server is not None
        body = _scrape(port)
    finally:
        stop_worker_metrics_server(server)

    assert re.search(
        r'crm_background_tasks_total\{[^}]*task="metrics_server_probe"[^}]*\} 1\.0', body
    )
    with pytest.raises(urllib.error.URLError):
        _scrape(port)


def test_busy_port_does_not_stop_the_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    def _busy(port: int) -> None:
        raise OSError(98, "Address already in use")

    monkeypatch.setattr(metrics, "start_http_server", _busy)

    assert start_worker_metrics_server(9101) is None


@pytest.mark.parametrize("port", [-1, 65536])
def test_worker_metrics_port_must_be_a_valid_port(port: int) -> None:
    with pytest.raises(ValidationError, match="worker_metrics_port"):
        _settings(worker_metrics_port=port)


def test_worker_metrics_port_defaults_to_9101() -> None:
    assert _settings().worker_metrics_port == 9101


def _settings(**overrides: int) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        app_profile="dev",
        database_url="postgresql+asyncpg://crm_app:crm_app@db:5432/crm",
        redis_url="redis://redis:6379/0",
        keycloak_url="http://kc:8080/auth",
        keycloak_realm="crm",
        keycloak_client_id="crm-bff",
        keycloak_client_secret="secret",
        s3_endpoint_url="http://s3:8333",
        s3_access_key="a",
        s3_secret_key="b",
        signature_server_secret="test-secret",
        crm_app_password="crm_app",
        **overrides,
    )


# --- startup / shutdown воркера -------------------------------------------


@pytest.fixture
def worker_main(monkeypatch: pytest.MonkeyPatch):
    """`app.worker.main` без побочных эффектов старта: логирование, регистрация сервисов, БД."""
    from app.worker import main

    monkeypatch.setattr(main, "configure_logging", lambda **kwargs: None)
    monkeypatch.setattr(main, "register_notification_service", lambda service: None)
    monkeypatch.setattr(main, "register_outbox_service", lambda service: None)
    monkeypatch.setattr(main, "dispose_engine", AsyncMock())
    monkeypatch.setattr(main, "close_redis", AsyncMock())
    return main


def _worker_settings(port: int) -> SimpleNamespace:
    return SimpleNamespace(
        log_level="INFO", log_json=False, app_profile="dev", worker_metrics_port=port
    )


async def test_worker_startup_serves_metrics_and_shutdown_stops_them(
    worker_main, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _free_port()
    monkeypatch.setattr(worker_main, "get_settings", lambda: _worker_settings(port))
    ctx: dict[str, object] = {}

    await worker_main.startup(ctx)
    try:
        assert "crm_background_tasks_total" in _scrape(port)
    finally:
        await worker_main.shutdown(ctx)

    with pytest.raises(urllib.error.URLError):
        _scrape(port)


async def test_worker_startup_with_port_zero_starts_no_server(
    worker_main, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(worker_main, "get_settings", lambda: _worker_settings(0))
    ctx: dict[str, object] = {}

    await worker_main.startup(ctx)
    await worker_main.shutdown(ctx)

    assert ctx["metrics_server"] is None
