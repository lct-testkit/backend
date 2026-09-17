"""Подключение к PostgreSQL (async SQLAlchemy 2.0).

Сессия живёт в рамках запроса. Бизнес-изменение, история, аудит и
`outbox_events` должны попадать в одну транзакцию (раздел 1), поэтому
коммит делается один раз на уровне зависимости, а не внутри сервисов.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings
from app.core.errors import DependencyStatus

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


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
        connect_args={"statement_cache_size": 0, "server_settings": {"jit": "off"}},
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
            raise


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
            raise


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
