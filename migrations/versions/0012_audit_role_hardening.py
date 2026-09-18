"""Приводит роль crm_app к реальной изоляции привилегий (аудит безопасности)

Контекст: 0002_identity_sprint создала `crm_app` с `NOLOGIN` и точечными
правами на `audit_log` (только SELECT/INSERT), рассчитывая, что приложение
будет подключаться под этой ролью, а не под `crm` — суперпользователем
кластера (`POSTGRES_USER` в docker-compose.yml/.env.example). Но роль с
`NOLOGIN` в принципе не может обслуживать подключение, и ни один компонент
(`app/core/db.py`, `deploy/entrypoint.sh`, `migrations/env.py`) не переключал
сессию на неё через `SET ROLE`. Итог: api/worker/migrate все подключались как
`crm`, и REVOKE/GRANT из 0002 были мёртвым кодом — суперпользователь читает и
меняет `audit_log` в обход триггера неизменяемости (`ALTER TABLE audit_log
DISABLE TRIGGER ALL` или прямой UPDATE/DELETE от имени владельца).

Эта миграция:
  1. даёт `crm_app` LOGIN и пароль (`Settings.crm_app_password`, тот же
     `.env`/окружение контейнера, что и остальные секреты — не хранится в
     БД и не хардкодится здесь);
  2. переопределяет `audit_log_attach_guards()`/`create_audit_log_partition()`
     как `SECURITY DEFINER` с фиксированным `search_path`. Без этого сам
     переход на пункт 1 сломал бы суточную cron-задачу
     `ensure_audit_partitions` (`app/worker/main.py`): она станет вызываться
     от имени `crm_app`, а создание партиции — это `CREATE TABLE ...
     PARTITION OF`, требующее CREATE на схему `public`, которого у `crm_app`
     нет и не должно появиться;
  3. заставляет `audit_log_attach_guards()` не только перевешивать триггер,
     но и переиздавать REVOKE/GRANT на саму партицию. Без этого новая
     партиция получила бы от `ALTER DEFAULT PRIVILEGES` (уже выданного 0002
     в схеме `public`) полный набор SELECT/INSERT/UPDATE/DELETE — защита
     пропадала бы для любого месяца, партиция которого создаётся уже после
     этой миграции.

`crm` остаётся ролью для `alembic upgrade head` (нужен DDL — CREATE ROLE,
CREATE TABLE/FUNCTION, GRANT). docker-compose.yml даёт сервису `migrate`
отдельный DATABASE_URL суперпользователя; api/worker получают DATABASE_URL
с `crm_app`.

Revision ID: 0012_audit_role_hardening
Revises: 0011_admin_sprint
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from app.core.config import get_settings

revision: str = "0012_audit_role_hardening"
down_revision: str | None = "0011_admin_sprint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "crm_app"


def upgrade() -> None:
    _grant_login()
    _harden_partition_functions()


def downgrade() -> None:
    _unharden_partition_functions()
    op.execute(f"ALTER ROLE {APP_ROLE} WITH NOLOGIN PASSWORD NULL")


def _grant_login() -> None:
    # Пароль подставляется в SQL-литерал: `ALTER ROLE ... PASSWORD` не умеет
    # bind-параметры. Значение приходит из `.env`/окружения контейнера
    # (доверенный оператор), не из пользовательского ввода — но кавычки
    # экранируем всё равно, а не полагаемся на это допущение.
    password = get_settings().crm_app_password.get_secret_value()
    escaped = password.replace("'", "''")
    op.execute(f"ALTER ROLE {APP_ROLE} WITH LOGIN PASSWORD '{escaped}'")


def _harden_partition_functions() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION audit_log_attach_guards(p_table regclass)
        RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        -- pg_catalog не перечисляем: он и так неявно ищется первым для
        -- разрешения имён (документированное поведение Postgres), а если
        -- поставить его явно первым в search_path, тем самым он становится
        -- и целевой схемой для неквалифицированного CREATE TABLE — ровно
        -- это и произошло при первой версии миграции: `CREATE TABLE
        -- audit_log_2027_10 PARTITION OF ...` пыталось создать таблицу в
        -- pg_catalog и падало с "System catalog modifications are
        -- currently disallowed".
        SET search_path = public, pg_temp
        AS $$
        DECLARE
            v_name text := replace(p_table::text, '.', '_');
        BEGIN
            EXECUTE format(
                'DROP TRIGGER IF EXISTS trg_%s_immutable ON %s', v_name, p_table
            );
            EXECUTE format(
                'CREATE TRIGGER trg_%s_immutable BEFORE UPDATE OR DELETE ON %s
                 FOR EACH ROW EXECUTE FUNCTION audit_log_forbid_change()',
                v_name, p_table
            );
            EXECUTE format(
                'DROP TRIGGER IF EXISTS trg_%s_truncate ON %s', v_name, p_table
            );
            EXECUTE format(
                'CREATE TRIGGER trg_%s_truncate BEFORE TRUNCATE ON %s
                 FOR EACH STATEMENT EXECUTE FUNCTION audit_log_forbid_change()',
                v_name, p_table
            );
            -- Партиция создаётся уже после того, как ALTER DEFAULT PRIVILEGES
            -- (0002) выдал {APP_ROLE} полный DML на новые таблицы схемы —
            -- без явного REVOKE каждая новая партиция получала бы
            -- UPDATE/DELETE, и весь смысл этой миграции терялся бы для
            -- любого месяца, партиция которого создана позже неё.
            EXECUTE format('REVOKE ALL ON %s FROM {APP_ROLE}', p_table);
            EXECUTE format('GRANT SELECT, INSERT ON %s TO {APP_ROLE}', p_table);
        END;
        $$
        """
    )

    # SECURITY DEFINER: вызывается из `ensure_audit_partitions`
    # (app/worker/main.py) уже от имени crm_app, а `CREATE TABLE ...
    # PARTITION OF` требует CREATE на схему public, которого у crm_app нет.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION create_audit_log_partition(p_month date)
        RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        DECLARE
            v_start date := date_trunc('month', p_month)::date;
            v_end   date := (date_trunc('month', p_month) + interval '1 month')::date;
            v_name  text := 'audit_log_' || to_char(v_start, 'YYYY_MM');
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = v_name) THEN
                EXECUTE format(
                    'CREATE TABLE %I PARTITION OF audit_log FOR VALUES FROM (%L) TO (%L)',
                    v_name, v_start, v_end
                );
            END IF;
            PERFORM audit_log_attach_guards(v_name::regclass);
        END;
        $$
        """
    )


def _unharden_partition_functions() -> None:
    # Возврат к версии 0002 (SECURITY INVOKER, без переиздачи прав) —
    # симметрично паре _audit_immutability/_downgrade_audit_immutability там.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_log_attach_guards(p_table regclass)
        RETURNS void AS $$
        DECLARE
            v_name text := replace(p_table::text, '.', '_');
        BEGIN
            EXECUTE format(
                'DROP TRIGGER IF EXISTS trg_%s_immutable ON %s', v_name, p_table
            );
            EXECUTE format(
                'CREATE TRIGGER trg_%s_immutable BEFORE UPDATE OR DELETE ON %s
                 FOR EACH ROW EXECUTE FUNCTION audit_log_forbid_change()',
                v_name, p_table
            );
            EXECUTE format(
                'DROP TRIGGER IF EXISTS trg_%s_truncate ON %s', v_name, p_table
            );
            EXECUTE format(
                'CREATE TRIGGER trg_%s_truncate BEFORE TRUNCATE ON %s
                 FOR EACH STATEMENT EXECUTE FUNCTION audit_log_forbid_change()',
                v_name, p_table
            );
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION create_audit_log_partition(p_month date)
        RETURNS void AS $$
        DECLARE
            v_start date := date_trunc('month', p_month)::date;
            v_end   date := (date_trunc('month', p_month) + interval '1 month')::date;
            v_name  text := 'audit_log_' || to_char(v_start, 'YYYY_MM');
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = v_name) THEN
                EXECUTE format(
                    'CREATE TABLE %I PARTITION OF audit_log FOR VALUES FROM (%L) TO (%L)',
                    v_name, v_start, v_end
                );
            END IF;
            PERFORM audit_log_attach_guards(v_name::regclass);
        END;
        $$ LANGUAGE plpgsql
        """
    )
