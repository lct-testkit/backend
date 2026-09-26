"""SECURITY DEFINER-функции недоступны PUBLIC, а `audit_log_attach_guards` не работает с чужими
таблицами (миграция 0018).

Найдено внешним тестированием: три функции с правами владельца были доступны любой роли, и одна
из них принимала любую таблицу, вешала на неё триггеры «только чтение» и отзывала права у
`crm_app`.
"""

from __future__ import annotations

import pytest

from tests.conftest import TEST_DATABASE_URL, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

_FUNCTIONS = (
    "audit_log_attach_guards(regclass)",
    "create_audit_log_partition(date)",
    "refresh_mv_deal_status_summary()",
)


async def _public_can_execute(signature: str) -> bool:
    from sqlalchemy import text

    from app.core.db import session_scope

    async with session_scope() as session:
        # ACL функции: `NULL` — права по умолчанию (EXECUTE у PUBLIC); `grantee = 0` — это PUBLIC.
        row = (
            await session.execute(
                text(
                    "SELECT p.proacl IS NULL OR EXISTS ("
                    "  SELECT 1 FROM aclexplode(p.proacl) a"
                    "  WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE') "
                    "FROM pg_proc p WHERE p.oid = to_regprocedure(:sig)"
                ),
                {"sig": signature},
            )
        ).scalar_one()
        return bool(row)


async def _app_role_can_execute(signature: str) -> bool:
    from sqlalchemy import text

    from app.core.db import session_scope

    async with session_scope() as session:
        return bool(
            (
                await session.execute(
                    text(
                        "SELECT has_function_privilege('crm_app', to_regprocedure(:sig), 'EXECUTE')"
                    ),
                    {"sig": signature},
                )
            ).scalar_one()
        )


async def _attach_guards_to(table: str) -> str:
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError

    from app.core.db import session_scope

    try:
        async with session_scope() as session:
            await session.execute(
                text("SELECT audit_log_attach_guards(CAST(:t AS regclass))"), {"t": table}
            )
    except DBAPIError as exc:
        return str(exc.orig)
    return "ok"


@pytest.mark.parametrize("signature", _FUNCTIONS)
def test_public_has_no_execute(client, signature: str) -> None:
    assert run(client, _public_can_execute, signature) is False


@pytest.mark.parametrize(
    "signature", ["create_audit_log_partition(date)", "refresh_mv_deal_status_summary()"]
)
def test_application_role_keeps_what_it_calls(client, signature: str) -> None:
    assert run(client, _app_role_can_execute, signature) is True


def test_application_role_has_no_direct_access_to_the_guard_function(client) -> None:
    assert run(client, _app_role_can_execute, "audit_log_attach_guards(regclass)") is False


def test_guard_function_refuses_foreign_tables(client) -> None:
    result = run(client, _attach_guards_to, "users")
    assert "недопустимая таблица" in result, result


def test_guard_function_still_serves_audit_partitions(client) -> None:
    assert run(client, _attach_guards_to, "audit_log_default") == "ok"
