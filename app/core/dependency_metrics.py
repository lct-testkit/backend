"""Обновление `crm_dependency_up` и `crm_queue_depth` в момент scrape.

Раньше оба ряда выставлял только обработчик `/health/ready`: реплика, к которой никто не
обращался, не отдавала их вовсе, а после сбоя зависимости значение оставалось прежним до
следующего запроса. Теперь `/metrics` сам прогоняет проверки, но так, чтобы scrape не превращался
в нагрузку и не зависал:

* каждая проверка ограничена по времени (`timeout`, по умолчанию 1 с) — зависшая зависимость
  считается недоступной, а не подвешивает ответ;
* результат кэшируется на `ttl` секунд (по умолчанию 5), одновременные scrape ждут один прогон;
* проверка, не уложившаяся в срок, не отменяется и не запускается заново, пока не завершится:
  `check_storage` уходит в поток с boto3 (таймауты клиента — минуты), и по потоку на каждый scrape
  быстро исчерпали бы общий пул.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping

from app.core.errors import DependencyStatus
from app.core.metrics import dependency_up, queue_depth

Probe = Callable[[], Awaitable[DependencyStatus]]

#: Проверка, от которой зависит `crm_queue_depth`: при её сбое глубину очереди не знаем.
QUEUE_PROBE = "queue"


class DependencyMetricsRefresher:
    def __init__(
        self,
        probes: Mapping[str, Probe],
        *,
        timeout: float = 1.0,
        ttl: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._probes = dict(probes)
        self._timeout = timeout
        self._ttl = ttl
        self._clock = clock
        self._refreshed_at: float | None = None
        self._refresh: asyncio.Task[None] | None = None
        self._inflight: dict[str, asyncio.Task[DependencyStatus]] = {}

    async def refresh(self) -> None:
        """Обновляет метрики, если кэш старше `ttl`; параллельные вызовы делят один прогон."""
        if self._refresh is None or self._refresh.done():
            if self._refreshed_at is not None and self._clock() - self._refreshed_at < self._ttl:
                return
            self._refresh = asyncio.ensure_future(self._run())
        # shield: отмена одного scrape (клиент оборвал соединение) не должна прерывать прогон,
        # которого ждут остальные.
        await asyncio.shield(self._refresh)

    async def _run(self) -> None:
        statuses = await asyncio.gather(
            *(self._probe(name, probe) for name, probe in self._probes.items())
        )
        for status in statuses:
            dependency_up.labels(dependency=status.name).set(1 if status.ok else 0)
            if status.name == QUEUE_PROBE and not status.ok:
                # Нет ответа Redis — нет и глубины очереди; лучше пустой ряд, чем застывшее число.
                queue_depth.clear()
        self._refreshed_at = self._clock()

    async def _probe(self, name: str, probe: Probe) -> DependencyStatus:
        task = self._inflight.get(name)
        if task is None or task.done():
            task = asyncio.ensure_future(probe())
            # Исключение, которое никто не дождался (проверка вышла за срок), не должно
            # превращаться в «Task exception was never retrieved».
            task.add_done_callback(_consume_exception)
            self._inflight[name] = task
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=self._timeout)
        except TimeoutError:
            return DependencyStatus(name=name, ok=False, error="TimeoutError")
        except Exception as exc:  # noqa: BLE001 — сама проверка обычно ловит всё и возвращает статус
            return DependencyStatus(name=name, ok=False, error=type(exc).__name__)


def _consume_exception(task: asyncio.Task[DependencyStatus]) -> None:
    if not task.cancelled():
        task.exception()
