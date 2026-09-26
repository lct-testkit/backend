"""Подключение к PostgreSQL (async SQLAlchemy 2.0).

Сессия живёт в рамках запроса. Бизнес-изменение, история, аудит и
`outbox_events` должны попадать в одну транзакцию (раздел 1), поэтому
коммит делается один раз на уровне зависимости, а не внутри сервисов.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings
from app.core.errors import DependencyStatus

logger = structlog.get_logger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None

# Ключ `session.info` со списком действий, отложенных до коммита (`run_after_commit`).
_AFTER_COMMIT_KEY = "after_commit"
# То же для откатов (`run_after_rollback`).
_AFTER_ROLLBACK_KEY = "after_rollback"


def _server_settings() -> dict[str, str]:
    """Параметры сессии Postgres, которые asyncpg выставляет при открытии каждого соединения."""
    settings = get_settings()
    server_settings = {"jit": "off"}
    # Транзакция, «повисшая» открытой (зависший внешний вызов, забытый коммит), держит свои
    # замки, в том числе advisory-лок цепочки аудита, и останавливает запись у всей системы.
    # Сервер сам рвёт такое соединение по истечении срока; 0 отключает предел. Настройка
    # действует именно на простой между командами: долгий запрос ею не прерывается.
    idle_seconds = max(0, settings.db_idle_in_transaction_timeout_seconds)
    server_settings["idle_in_transaction_session_timeout"] = str(idle_seconds * 1000)
    return server_settings


def create_engine() -> AsyncEngine:
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
        # Пул стейтментов asyncpg плохо дружит с pgbouncer в transaction-режиме.
        connect_args={"statement_cache_size": 0, "server_settings": _server_settings()},
    )


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = create_engine()
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None


def run_after_commit(session: AsyncSession, action: Callable[[], Awaitable[None]]) -> None:
    """Откладывает `action` до успешного коммита транзакции запроса.

    Запись во внешнее хранилище (кэш Redis) до коммита оставляет «призрака», если
    запрос потом откатится: кэш указывает на строку, которой в БД нет. Действие
    выполняется после коммита и пропускается, если запрос завершился исключением.
    Его сбой транзакцию не отменяет — она уже зафиксирована.
    """
    session.info.setdefault(_AFTER_COMMIT_KEY, []).append(action)


def run_after_rollback(session: AsyncSession, action: Callable[[], Awaitable[None]]) -> None:
    """Откладывает `action` до отката транзакции запроса — зеркало `run_after_commit`.

    Нужно тому, что запрос успел записать во внешнее хранилище ДО результата и что при неудаче
    должно исчезнуть: метка «запрос с этим Idempotency-Key выполняется» в Redis. Без снятия
    упавший запрос блокировал повтор с тем же ключом на сутки (409 «ещё обрабатывается»).
    """
    session.info.setdefault(_AFTER_ROLLBACK_KEY, []).append(action)


async def _run_after_commit_actions(session: AsyncSession) -> None:
    session.info.pop(_AFTER_ROLLBACK_KEY, None)  # запрос удался — откатывать нечего
    for action in session.info.pop(_AFTER_COMMIT_KEY, []):
        try:
            await action()
        except Exception:  # noqa: BLE001 — коммит уже состоялся
            logger.warning("after_commit_action_failed", exc_info=True)


async def _run_after_rollback_actions(session: AsyncSession) -> None:
    session.info.pop(_AFTER_COMMIT_KEY, None)  # запрос упал — отложенное до коммита не нужно
    for action in session.info.pop(_AFTER_ROLLBACK_KEY, []):
        try:
            await action()
        except Exception:  # noqa: BLE001 — исходная ошибка запроса важнее
            logger.warning("after_rollback_action_failed", exc_info=True)


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """FastAPI-зависимость: одна транзакция на запрос.

    Коммит выполняется только при успешном ответе. Любое исключение
    откатывает и бизнес-изменение, и запись аудита — это требование
    атомарности из раздела 1.
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            if session.in_transaction():
                await session.commit()
        except Exception:
            await session.rollback()
            await _run_after_rollback_actions(session)
            raise
        await _run_after_commit_actions(session)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Транзакция для фоновых задач и CLI, где нет FastAPI-зависимостей."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            if session.in_transaction():
                await session.commit()
        except Exception:
            await session.rollback()
            await _run_after_rollback_actions(session)
            raise
        await _run_after_commit_actions(session)


async def check_database() -> DependencyStatus:
    started = time.perf_counter()
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return DependencyStatus(
            name="postgres", ok=True, latency_ms=round((time.perf_counter() - started) * 1000, 2)
        )
    except Exception as exc:
        return DependencyStatus(name="postgres", ok=False, error=type(exc).__name__)
