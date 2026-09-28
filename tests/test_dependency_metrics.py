"""`crm_dependency_up` / `crm_queue_depth` обновляются при scrape `/metrics`, а не только в
`/health/ready`: проверки с таймаутом, кэш на несколько секунд, без наложения зависших проб.

Без БД и Redis: пробы — заглушки, значения читаются из реестра `prometheus_client`.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest
from prometheus_client import REGISTRY

from app.api import health
from app.core.dependency_metrics import DependencyMetricsRefresher, Probe
from app.core.errors import DependencyStatus
from app.core.metrics import dependency_up, queue_depth


@pytest.fixture(autouse=True)
def _clean_series() -> Iterator[None]:
    dependency_up.clear()
    queue_depth.clear()
    yield
    dependency_up.clear()
    queue_depth.clear()


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _up(name: str) -> float | None:
    return REGISTRY.get_sample_value("crm_dependency_up", {"dependency": name})


def _depth() -> float | None:
    return REGISTRY.get_sample_value("crm_queue_depth", {"queue": "arq:queue"})


def _probe(name: str, *, ok: bool = True, calls: list[str] | None = None) -> Probe:
    async def probe() -> DependencyStatus:
        if calls is not None:
            calls.append(name)
        return DependencyStatus(name=name, ok=ok)

    return probe


def _hanging_probe(name: str, release: asyncio.Event, calls: list[str]) -> Probe:
    async def probe() -> DependencyStatus:
        calls.append(name)
        await release.wait()
        return DependencyStatus(name=name, ok=True)

    return probe


async def test_refresh_sets_dependency_up_from_probe_results() -> None:
    refresher = DependencyMetricsRefresher(
        {"postgres": _probe("postgres"), "seaweedfs": _probe("seaweedfs", ok=False)}
    )

    await refresher.refresh()

    assert _up("postgres") == 1
    assert _up("seaweedfs") == 0


async def test_probe_that_exceeds_timeout_is_reported_down() -> None:
    release = asyncio.Event()
    calls: list[str] = []
    refresher = DependencyMetricsRefresher(
        {"redis": _hanging_probe("redis", release, calls), "postgres": _probe("postgres")},
        timeout=0.05,
    )

    # Зависшая проба не должна подвешивать scrape: ответ укладывается в срок с запасом.
    await asyncio.wait_for(refresher.refresh(), timeout=2)

    assert _up("redis") == 0
    assert _up("postgres") == 1
    release.set()
    await asyncio.sleep(0)


async def test_failing_probe_is_reported_down_without_raising() -> None:
    async def broken() -> DependencyStatus:
        raise RuntimeError("проба сломалась")

    refresher = DependencyMetricsRefresher({"keycloak_jwks": broken})

    await refresher.refresh()

    assert _up("keycloak_jwks") == 0


async def test_result_is_cached_for_ttl() -> None:
    clock = Clock()
    calls: list[str] = []
    refresher = DependencyMetricsRefresher(
        {"postgres": _probe("postgres", calls=calls)}, ttl=5.0, clock=clock
    )

    await refresher.refresh()
    clock.now += 4.9
    await refresher.refresh()
    assert calls == ["postgres"]

    clock.now += 0.2
    await refresher.refresh()
    assert calls == ["postgres", "postgres"]


async def test_concurrent_scrapes_share_one_run() -> None:
    calls: list[str] = []

    async def slow() -> DependencyStatus:
        calls.append("postgres")
        await asyncio.sleep(0.02)
        return DependencyStatus(name="postgres", ok=True)

    refresher = DependencyMetricsRefresher({"postgres": slow}, ttl=0.0)

    await asyncio.gather(*(refresher.refresh() for _ in range(5)))

    assert calls == ["postgres"]


async def test_hung_probe_is_not_started_again_until_it_finishes() -> None:
    """Иначе на каждый scrape при зависшем SeaweedFS уходил бы ещё один поток boto3."""
    release = asyncio.Event()
    calls: list[str] = []
    refresher = DependencyMetricsRefresher(
        {"seaweedfs": _hanging_probe("seaweedfs", release, calls)}, timeout=0.02, ttl=0.0
    )

    await refresher.refresh()
    await refresher.refresh()
    assert calls == ["seaweedfs"]
    assert _up("seaweedfs") == 0

    release.set()
    await asyncio.sleep(0.01)
    await refresher.refresh()
    assert calls == ["seaweedfs", "seaweedfs"]
    assert _up("seaweedfs") == 1


async def test_queue_depth_series_is_dropped_when_queue_probe_fails() -> None:
    queue_depth.labels(queue="arq:queue").set(7)
    refresher = DependencyMetricsRefresher({"queue": _probe("queue", ok=False)})

    await refresher.refresh()

    assert _up("queue") == 0
    assert _depth() is None


async def test_queue_depth_series_is_dropped_when_queue_probe_times_out() -> None:
    release = asyncio.Event()
    queue_depth.labels(queue="arq:queue").set(7)
    refresher = DependencyMetricsRefresher(
        {"queue": _hanging_probe("queue", release, [])}, timeout=0.02
    )

    await refresher.refresh()

    assert _up("queue") == 0
    assert _depth() is None
    release.set()
    await asyncio.sleep(0)


async def test_queue_depth_series_is_kept_when_queue_probe_succeeds() -> None:
    async def queue_probe() -> DependencyStatus:
        queue_depth.labels(queue="arq:queue").set(3)  # так делает `check_queue`
        return DependencyStatus(name="queue", ok=True, details={"depth": 3})

    refresher = DependencyMetricsRefresher({"queue": queue_probe})

    await refresher.refresh()

    assert _up("queue") == 1
    assert _depth() == 3


# --- /metrics --------------------------------------------------------------


async def test_metrics_endpoint_refreshes_dependencies_before_rendering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        health,
        "_dependency_metrics",
        DependencyMetricsRefresher(
            {"postgres": _probe("postgres"), "redis": _probe("redis", ok=False)}
        ),
    )

    response = await health.metrics()

    body = response.body.decode("utf-8")
    assert 'crm_dependency_up{dependency="postgres"} 1.0' in body
    assert 'crm_dependency_up{dependency="redis"} 0.0' in body


async def test_metrics_endpoint_still_answers_when_refresh_breaks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = AsyncMock()
    broken.refresh.side_effect = RuntimeError("проверки сломаны")
    monkeypatch.setattr(health, "_dependency_metrics", broken)

    response = await health.metrics()

    assert response.status_code == 200
    assert b"crm_" in response.body


async def test_metrics_endpoint_uses_current_check_functions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Пробы берут проверки из модуля в момент вызова, поэтому их можно подменить."""
    monkeypatch.setattr(health, "check_redis", _probe("redis", ok=False))

    probe = health._dependency_metrics._probes["redis"]

    assert (await probe()).ok is False


def test_probe_names_match_what_the_checks_report() -> None:
    """Ключ пробы — метка `dependency`; проверка при таймауте отдаёт именно его."""
    checks = {
        "postgres": health.check_database,
        "redis": health.check_redis,
        "keycloak_jwks": health.jwks_cache.check,
        "seaweedfs": health.check_storage,
        "queue": health.check_queue,
    }

    assert set(health._dependency_metrics._probes) == set(checks)
    for name, check in checks.items():
        assert f'name="{name}"' in inspect.getsource(check)
