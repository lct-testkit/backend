"""Пул arq для постановки задач прямо из API (B8-11).

До этой задачи ни один обработчик `app/` не делал `enqueue_job`: тяжёлые
отчёты и применение импорта лежали `queued`/`applying` до ближайшего тика
минутного cron воркера (`sweep_report_jobs`/`sweep_import_jobs`), то есть
«завис» на вид до 60 секунд даже для НФР7 (10 параллельных отчётов). Теперь
API кладёт задание в очередь arq сразу после коммита транзакции запроса —
до коммита воркер, даже подняв задание первым, не нашёл бы строку в БД.

Пул живёт в процессе API (отдельно от пула воркера в `app/worker/main.py`) и
закрывается в `lifespan` (`app/main.py`), как и Redis-клиент сессий. Сбой
Redis на постановке НЕ должен ронять бизнес-запрос — задание всё равно
осталось в БД со статусом `queued`/`applying`, и его подхватит cron-добор на
следующем тике; поэтому исключение здесь только логируется.
"""

from __future__ import annotations

from typing import Any

import structlog
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import run_after_commit

logger = structlog.get_logger(__name__)

_pool: ArqRedis | None = None


async def get_arq_pool() -> ArqRedis:
    """Ленивое подключение: большинство запросов API очередь не трогают вовсе, а
    открывать соединение с Redis на старте процесса ради них не нужно."""
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    return _pool


async def close_arq_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
    _pool = None


def enqueue_after_commit(
    session: AsyncSession, function: str, *args: Any, job_id: str | None = None
) -> None:
    """Откладывает `pool.enqueue_job(function, *args, _job_id=job_id)` до коммита
    транзакции запроса (`run_after_commit`). `job_id` — стабильный ключ (например
    `report:<uuid>`/`import:<uuid>:apply`), чтобы случайный повторный вызов
    (в т.ч. cron-добор того же задания) не создавал дубль в очереди arq: с тем же
    `_job_id` второй `enqueue_job` — no-op (возвращает `None`, не исключение)."""

    async def _enqueue() -> None:
        try:
            pool = await get_arq_pool()
            await pool.enqueue_job(function, *args, _job_id=job_id)
        except Exception:  # noqa: BLE001 — потеря Redis не должна ронять уже закоммиченный запрос
            logger.warning("queue_enqueue_failed", function=function, job_id=job_id, exc_info=True)

    run_after_commit(session, _enqueue)
