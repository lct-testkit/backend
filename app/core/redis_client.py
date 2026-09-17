"""Redis: сессии, кэш, локи, идемпотентность, rate-limit.

Схема ключей зафиксирована в разделе 16 спецификации. Если Redis недоступен,
деградация предсказуемая: чтение кэша идёт в БД, а операции с локами,
сессиями и идемпотентностью возвращают ошибку, а не рискуют двойным действием.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis.asyncio as redis

from app.core.config import get_settings
from app.core.errors import AppError, DependencyStatus, ErrorCode

_client: redis.Redis | None = None


# --- Схема ключей (раздел 16) --------------------------------------------


def key_session(sid: str) -> str:
    return f"session:{sid}"


def key_user_sessions(user_id: object) -> str:
    return f"user_sessions:{user_id}"


def key_deal_card(deal_id: object, version: int) -> str:
    return f"cache:deal:{deal_id}:v{version}"


def key_catalog(catalog_type: str, digest: str) -> str:
    return f"cache:catalog:{catalog_type}:{digest}"


def key_workflow_graph(workflow_id: object) -> str:
    return f"cache:wf:{workflow_id}"


def key_permissions(user_id: object) -> str:
    return f"cache:perm:{user_id}"


def key_recent(user_id: object) -> str:
    return f"recent:{user_id}"


def key_lock(resource: str) -> str:
    return f"lock:{resource}"


def key_idempotency(idem_key: str) -> str:
    return f"idem:{idem_key}"


def key_rate_limit(ip: str, route: str) -> str:
    return f"rl:{ip}:{route}"


def key_jwks() -> str:
    return "cache:jwks"


# --- TTL по разделу 16 ---------------------------------------------------

TTL_DEAL_CARD = 600
TTL_CATALOG = 3600
TTL_WORKFLOW_GRAPH = 3600
TTL_PERMISSIONS = 300
TTL_IDEMPOTENCY = 86400
RECENT_MAX_ITEMS = 20


def create_client() -> redis.Redis:
    settings = get_settings()
    return redis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=3,
        health_check_interval=30,
    )


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = create_client()
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


async def require_redis() -> redis.Redis:
    """Для сессий, локов и идемпотентности: лучше 503, чем двойное действие."""
    client = get_redis()
    try:
        await client.ping()
    except Exception as exc:
        raise AppError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "Хранилище сессий недоступно, повторите попытку позже",
        ) from exc
    return client


@asynccontextmanager
async def distributed_lock(
    resource: str, *, ttl: int = 30, blocking_timeout: float = 5.0
) -> AsyncIterator[bool]:
    """Короткий лок, например `lock:deal:{id}:transition` от двойного перехода."""
    client = await require_redis()
    lock = client.lock(
        key_lock(resource),
        timeout=ttl,
        blocking=True,
        blocking_timeout=blocking_timeout,
    )
    acquired = await lock.acquire()
    try:
        yield acquired
    finally:
        if acquired:
            try:
                await lock.release()
            except Exception:  # noqa: BLE001 — лок мог истечь по TTL
                pass


async def check_redis() -> DependencyStatus:
    started = time.perf_counter()
    try:
        await get_redis().ping()
        return DependencyStatus(
            name="redis", ok=True, latency_ms=round((time.perf_counter() - started) * 1000, 2)
        )
    except Exception as exc:
        return DependencyStatus(name="redis", ok=False, error=type(exc).__name__)


async def check_queue() -> DependencyStatus:
    """Длина очереди arq — часть /health/ready и метрик."""
    from app.core.metrics import queue_depth

    try:
        depth = await get_redis().zcard("arq:queue")
        queue_depth.labels(queue="arq:queue").set(depth)
        return DependencyStatus(name="queue", ok=True, details={"depth": depth})
    except Exception as exc:
        return DependencyStatus(name="queue", ok=False, error=type(exc).__name__)
