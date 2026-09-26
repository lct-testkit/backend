"""SECURITY DEFINER-функции: убрать EXECUTE у PUBLIC (отчёт внешнего тестирования)

Три функции миграций 0002/0012/0013 объявлены `SECURITY DEFINER` (исполняются от имени владельца —
суперпользователя миграций), но `EXECUTE` в Postgres по умолчанию выдан PUBLIC — то есть любой роли,
включая `crm_app`, а через SQL-инъекцию или компрометацию приложения — кому угодно:

* `audit_log_attach_guards(regclass)` принимала ЛЮБУЮ таблицу: вешала на неё триггеры «запрет
  UPDATE/DELETE/TRUNCATE» и отзывала у `crm_app` все права (`REVOKE ALL … FROM crm_app`) — то есть
  одним вызовом можно было отключить приложению, например, `users` или `deals`;
* `create_audit_log_partition(date)` создавала таблицы от имени владельца;
* `refresh_mv_deal_status_summary()` — обновление материализованного представления.

Теперь EXECUTE только у владельца и у `crm_app` там, где его зовёт приложение
(`create_audit_log_partition` — воркер, `refresh_mv_deal_status_summary` — отчёты).
`audit_log_attach_guards` вызывается только из `create_audit_log_partition` (тоже `SECURITY DEFINER`,
то есть от владельца) — приложению прав на неё не нужно вовсе. Кроме того, она теперь отказывается
работать с чем угодно, кроме таблиц `audit_log*`.

Revision ID: 0018_revoke_definer_public
Revises: 0017_people_import
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0018_revoke_definer_public"
down_revision: str | None = "0017_people_import"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "crm_app"

# Тело — как в 0012 (`_harden_partition_functions`), плюс проверка имени таблицы.
_ATTACH_GUARDS = f"""
CREATE OR REPLACE FUNCTION audit_log_attach_guards(p_table regclass)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_name text := replace(p_table::text, '.', '_');
BEGIN
    IF p_table::text !~ '^(public\\.)?audit_log(_[0-9]{{4}}_[0-9]{{2}}|_default)?$' THEN
        RAISE EXCEPTION 'audit_log_attach_guards: недопустимая таблица %', p_table;
    END IF;
    EXECUTE format('DROP TRIGGER IF EXISTS trg_%s_immutable ON %s', v_name, p_table);
    EXECUTE format(
        'CREATE TRIGGER trg_%s_immutable BEFORE UPDATE OR DELETE ON %s
         FOR EACH ROW EXECUTE FUNCTION audit_log_forbid_change()',
        v_name, p_table
    );
    EXECUTE format('DROP TRIGGER IF EXISTS trg_%s_truncate ON %s', v_name, p_table);
    EXECUTE format(
        'CREATE TRIGGER trg_%s_truncate BEFORE TRUNCATE ON %s
         FOR EACH STATEMENT EXECUTE FUNCTION audit_log_forbid_change()',
        v_name, p_table
    );
    EXECUTE format('REVOKE ALL ON %s FROM {APP_ROLE}', p_table);
    EXECUTE format('GRANT SELECT, INSERT ON %s TO {APP_ROLE}', p_table);
END;
$$
"""


def upgrade() -> None:
    op.execute(_ATTACH_GUARDS)
    op.execute("REVOKE EXECUTE ON FUNCTION audit_log_attach_guards(regclass) FROM PUBLIC")
    op.execute("REVOKE EXECUTE ON FUNCTION create_audit_log_partition(date) FROM PUBLIC")
    op.execute("REVOKE EXECUTE ON FUNCTION refresh_mv_deal_status_summary() FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION create_audit_log_partition(date) TO {APP_ROLE}")
    op.execute(f"GRANT EXECUTE ON FUNCTION refresh_mv_deal_status_summary() TO {APP_ROLE}")


def downgrade() -> None:
    # Возвращаем прежние права (EXECUTE у PUBLIC). Проверку имени таблицы в теле
    # `audit_log_attach_guards` оставляем: она безвредна для штатных вызовов.
    op.execute("GRANT EXECUTE ON FUNCTION audit_log_attach_guards(regclass) TO PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION create_audit_log_partition(date) TO PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION refresh_mv_deal_status_summary() TO PUBLIC")
