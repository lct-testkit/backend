"""Окружение Alembic (async).

URL подключения берётся из настроек приложения, чтобы не дублировать секреты
в alembic.ini. `compare_type` включён: изменение типа колонки должно попадать
в автогенерацию.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import get_settings
from app.db.models import target_metadata

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# `database_url` — это роль приложения (`crm_app`), у которой сознательно нет
# DDL-прав (см. 0012_audit_role_hardening). Внутри docker compose сервис
# `migrate` получает отдельный DATABASE_URL суперпользователя, поэтому там
# переменная ниже не нужна; она нужна только при локальном запуске `alembic`
# вне docker compose, где `.env` даёт один `DATABASE_URL` на всё — тот же
# приём опционального оверрайда, что и `KEYCLOAK_INTERNAL_URL`/
# `S3_PUBLIC_ENDPOINT_URL`.
config.set_main_option(
    "sqlalchemy.url",
    os.environ.get("MIGRATIONS_DATABASE_URL") or get_settings().database_url,
)


# Объекты, которые существуют только в SQL-миграциях и не описаны ORM-моделями (служебные
# индексы). Без этого списка `alembic check` в CI видел бы их как «лишние» и не мог бы служить
# гейтом на НОВЫЙ дрейф между моделями и схемой. Список не должен расти: новую таблицу
# описывайте моделью (шесть таблиц интеграций раньше были здесь и теперь описаны).
_MIGRATION_ONLY_INDEXES = frozenset(
    {
        "ix_data_erasure_requests_grace_due",
        "ix_organizations_name_trgm",
    }
)


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Что сравнивает автогенерация: без партиций audit_log и migration-only индексов."""
    if type_ == "table":
        return not name.startswith("audit_log_")
    return not (type_ == "index" and name in _MIGRATION_ONLY_INDEXES)


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={"statement_cache_size": 0},
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
