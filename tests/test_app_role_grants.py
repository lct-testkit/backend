"""Права роли приложения `crm_app` проверяются подключением под ней самой.

Остальные тесты идут под суперпользователем (миграции и тесты делят одну учётку), а он читает и
меняет `audit_log` в обход прав и триггеров, поэтому регресс в GRANT/REVOKE остался бы незамеченным
до production, где api и worker ходят под `crm_app` (0012_audit_role_hardening). Здесь отдельное
соединение под `crm_app`: журнал аудита — только чтение и вставка, DDL и защитная функция
партиций недоступны.

Проверки без побочных эффектов: запрос с `WHERE false` проходит проверку прав до чтения строк,
поэтому и удачный, и отказанный вариант не меняют данных.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

_APP_ROLE = "crm_app"


def _app_role_url() -> str:
    """Тот же кластер и БД, что у тестов, но под ролью приложения и её паролем из настроек
    (миграция 0012 выставляет именно его)."""
    from app.core.config import get_settings

    password = get_settings().crm_app_password.get_secret_value()
    url = make_url(TEST_DATABASE_URL or "").set(username=_APP_ROLE, password=password)
    return url.render_as_string(hide_password=False)


@pytest_asyncio.fixture
async def app_role() -> AsyncIterator[AsyncConnection]:
    engine = create_async_engine(
        _app_role_url(), poolclass=NullPool, connect_args={"statement_cache_size": 0}
    )
    try:
        async with engine.connect() as connection:
            yield connection
            await connection.rollback()
    finally:
        await engine.dispose()


async def _refused(connection: AsyncConnection, sql: str) -> str:
    """Текст отказа БД; пустая строка — запрос прошёл (это и есть провал проверки)."""
    try:
        await connection.execute(text(sql))
    except DBAPIError as exc:
        await connection.rollback()
        return str(exc.orig)
    await connection.rollback()
    return ""


def _is_denied(message: str) -> bool:
    # «permission denied» — нет права; «must be owner» — операция только для владельца (DDL).
    return "permission denied" in message or "must be owner" in message


async def test_the_role_can_log_in_and_is_not_privileged(app_role: AsyncConnection) -> None:
    row = (
        await app_role.execute(
            text(
                "SELECT rolsuper, rolcreaterole, rolcreatedb, rolbypassrls "
                "FROM pg_roles WHERE rolname = current_user"
            )
        )
    ).one()
    assert (await app_role.execute(text("SELECT current_user"))).scalar_one() == _APP_ROLE
    assert not any(row), "у crm_app не должно быть ни одного привилегированного атрибута роли"


async def test_the_role_owns_nothing(app_role: AsyncConnection) -> None:
    # Владелец таблицы обходит GRANT/REVOKE и может отключить триггеры неизменяемости.
    owned = (
        await app_role.execute(
            text(
                "SELECT count(*) FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner "
                "WHERE r.rolname = current_user AND c.relkind IN ('r', 'p', 'v', 'm')"
            )
        )
    ).scalar_one()
    assert owned == 0


async def test_ordinary_tables_stay_writable(app_role: AsyncConnection) -> None:
    # Контроль от ложных срабатываний: роль работает, отказы ниже — именно про аудит и DDL.
    assert await _refused(app_role, "SELECT count(*) FROM users") == ""
    assert await _refused(app_role, "UPDATE users SET id = id WHERE false") == ""


async def test_the_audit_log_is_append_only(app_role: AsyncConnection) -> None:
    assert await _refused(app_role, "SELECT count(*) FROM audit_log") == ""
    # Проверка прав идёт до чтения строк: `WHERE false` ничего не вставит и не изменит.
    assert await _refused(app_role, "INSERT INTO audit_log (id) SELECT NULL WHERE false") == ""
    for sql in (
        "UPDATE audit_log SET action = action WHERE false",
        "DELETE FROM audit_log WHERE false",
        "TRUNCATE audit_log",
    ):
        assert _is_denied(await _refused(app_role, sql)), sql


async def test_every_audit_partition_is_append_only(app_role: AsyncConnection) -> None:
    # Новая партиция по умолчанию получает полный набор прав от `ALTER DEFAULT PRIVILEGES`;
    # `audit_log_attach_guards` обязан их отобрать (0012, 0018).
    rows = (
        await app_role.execute(
            text(
                "SELECT c.relname, "
                "  has_table_privilege(current_user, c.oid, 'UPDATE'), "
                "  has_table_privilege(current_user, c.oid, 'DELETE'), "
                "  has_table_privilege(current_user, c.oid, 'TRUNCATE') "
                "FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
                "WHERE i.inhparent = 'audit_log'::regclass"
            )
        )
    ).all()
    assert rows, "у audit_log нет партиций: миграции не применены?"
    writable = [name for name, upd, dele, trunc in rows if upd or dele or trunc]
    assert not writable, f"партиции журнала допускают изменение: {writable}"


async def test_the_role_has_no_ddl(app_role: AsyncConnection) -> None:
    for sql in (
        "CREATE TABLE x4_probe (id int)",
        "DROP TABLE users",
        "ALTER TABLE users DISABLE TRIGGER ALL",
    ):
        assert _is_denied(await _refused(app_role, sql)), sql


async def test_the_guard_function_is_not_callable_directly(app_role: AsyncConnection) -> None:
    # Прямой вызов вешал триггеры на любую таблицу и отзывал права (найдено внешним тестом).
    refused = await _refused(app_role, "SELECT audit_log_attach_guards(CAST('users' AS regclass))")
    assert _is_denied(refused), refused


async def test_the_role_calls_what_the_worker_needs(app_role: AsyncConnection) -> None:
    for signature in ("create_audit_log_partition(date)", "refresh_mv_deal_status_summary()"):
        allowed = (
            await app_role.execute(
                text("SELECT has_function_privilege(current_user, to_regprocedure(:s), 'EXECUTE')"),
                {"s": signature},
            )
        ).scalar_one()
        assert allowed, signature
