"""Спринт identity: приглашения, «четыре глаза», неизменяемость аудита

Что делает миграция:
  * `user_invites` — одноразовые приглашения (хранится только sha256 токена);
  * `admin_approvals` — подтверждение операции вторым администратором;
  * `users.must_change_password` — невыполненное обязательное действие
    Keycloak, по которому API отвергает бизнес-запросы;
  * индексы под фильтры списка пользователей (статус, триграммный поиск ФИО,
    регистронезависимая уникальность email);
  * расширение ключа идемпотентности: он хранится со скоупом актора;
  * **починку неизменяемости `audit_log`**: триггер уровня строки навешивается
    на каждую партицию, а не только на родителя, и на TRUNCATE тоже; плюс
    отдельная роль приложения с правами только INSERT/SELECT.

Revision ID: 0002_identity_sprint
Revises: 0001_baseline
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_identity_sprint"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Роль, под которой обязано работать приложение при доступе к журналу.
APP_ROLE = "crm_app"


def upgrade() -> None:
    _fix_constraint_names()
    _identity_changes()
    _invites_and_approvals()
    _idempotency_scope()
    _audit_immutability()


# Имена CHECK-ограничений, которые в 0001 получили префикс дважды
# (`ck_users_ck_users_users_role_valid`), а часть ещё и была обрезана с
# хэшем. Соглашение из `app.db.base` даёт другие имена, поэтому
# `alembic revision --autogenerate` видел бы вечный ложный диф и предлагал
# пересоздавать ограничения. Приводим к каноническим именам.
# Сопоставление идёт по определению ограничения, а не по имени: часть имён
# обрезана PostgreSQL до 63 символов с хэшем на конце, и опознать их по
# фрагменту имени невозможно.
_CONSTRAINT_RENAMES: tuple[tuple[str, str, str], ...] = (
    ("users", "role", "ck_users_users_role_valid"),
    ("users", "status", "ck_users_users_status_valid"),
    ("consents", "subject_type", "ck_consents_consent_subject_valid"),
    ("user_delegations", "ends_at", "ck_user_delegations_delegation_period_valid"),
    (
        "data_erasure_requests",
        "status",
        "ck_data_erasure_requests_erasure_status_valid",
    ),
    (
        "data_erasure_requests",
        "subject_type",
        "ck_data_erasure_requests_erasure_subject_valid",
    ),
)


def _fix_constraint_names() -> None:
    for table, definition_marker, target in _CONSTRAINT_RENAMES:
        op.execute(
            f"""
            DO $$
            DECLARE
                v_name text;
            BEGIN
                SELECT conname INTO v_name
                FROM pg_constraint
                WHERE conrelid = '{table}'::regclass
                  AND contype = 'c'
                  AND pg_get_constraintdef(oid) LIKE '%{definition_marker}%'
                  AND conname <> '{target}'
                LIMIT 1;
                IF v_name IS NOT NULL THEN
                    EXECUTE format(
                        'ALTER TABLE {table} RENAME CONSTRAINT %I TO %I', v_name, '{target}'
                    );
                END IF;
            END $$
            """
        )


def downgrade() -> None:
    _downgrade_audit_immutability()

    op.alter_column("idempotency_keys", "key", type_=sa.String(255), existing_nullable=False)

    op.drop_table("admin_approvals")
    op.drop_table("user_invites")

    op.drop_index("ix_users_status", table_name="users")
    op.drop_index("ix_users_full_name_trgm", table_name="users")
    op.drop_index("uq_users_email_lower_active", table_name="users")
    op.create_index(
        "uq_users_email_active",
        "users",
        ["email"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL AND email IS NOT NULL"),
    )
    op.drop_column("users", "must_change_password")


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------


def _identity_changes() -> None:
    op.add_column(
        "users",
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )

    # Email сравнивается регистронезависимо: «Ivanov@rt.ru» и «ivanov@rt.ru» —
    # один и тот же человек, и второй такой записи быть не должно.
    op.drop_index("uq_users_email_active", table_name="users")
    op.create_index(
        "uq_users_email_lower_active",
        "users",
        [sa.text("lower(email)")],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL AND email IS NOT NULL"),
    )
    op.create_index(
        "ix_users_status",
        "users",
        ["status"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    # Поиск по ФИО в списке пользователей — по триграммам, а не LIKE '%...%'
    # по всей таблице.
    op.execute("CREATE INDEX ix_users_full_name_trgm ON users USING gin (full_name gin_trgm_ops)")


def _invites_and_approvals() -> None:
    op.create_table(
        "user_invites",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Хранится только хэш: из дампа ссылку восстановить нельзя.
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_user_invites"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_user_invites_user_id_users", ondelete="RESTRICT"
        ),
    )
    op.create_index("uq_user_invites_token_hash", "user_invites", ["token_hash"], unique=True)
    op.create_index("ix_user_invites_user_active", "user_invites", ["user_id", "expires_at"])

    op.create_table(
        "admin_approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation", sa.String(64), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("entity_type", sa.String(64), nullable=True),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("approved_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_admin_approvals"),
        # Имена даются короткими: префикс `ck_{table}_` добавит соглашение
        # об именовании из `app.db.base`. Если написать полное имя, оно
        # получит префикс второй раз.
        sa.CheckConstraint(
            "status IN ('pending','approved','rejected','expired','consumed')",
            name="approval_status_valid",
        ),
        # Инициатор и подтверждающий обязаны различаться: в этом весь смысл
        # принципа «четырёх глаз».
        sa.CheckConstraint(
            "approved_by IS NULL OR approved_by <> requested_by",
            name="approval_four_eyes",
        ),
    )
    op.create_index("ix_admin_approvals_created_at", "admin_approvals", ["created_at"])
    op.create_index(
        "ix_admin_approvals_pending",
        "admin_approvals",
        ["operation", "request_hash", "status"],
    )


def _idempotency_scope() -> None:
    # Ключ теперь хранится как `{actor_id}:{key}`, а заголовок сам по себе
    # может занимать все 255 символов.
    op.alter_column("idempotency_keys", "key", type_=sa.String(320), existing_nullable=False)


# --------------------------------------------------------------------------
# Неизменяемость аудита
# --------------------------------------------------------------------------


def _audit_immutability() -> None:
    """Закрывает три дыры в защите журнала из миграции 0001.

    1. Триггер стоял только на родительской таблице и был `FOR EACH
       STATEMENT`. Запрос `DELETE FROM audit_log_2026_09` идёт мимо родителя
       и удалял записи беспрепятственно.
    2. TRUNCATE не был закрыт ничем, кроме `REVOKE ... FROM PUBLIC`, который
       владельца таблицы не ограничивает.
    3. У приложения были полные права владельца. Раздел 5.10 требует ровно
       `INSERT` и `SELECT`.
    """
    # Триггер на каждую партицию: и на строки, и на TRUNCATE.
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

    # Навешиваем на уже существующие партиции.
    op.execute(
        """
        DO $$
        DECLARE
            part regclass;
        BEGIN
            FOR part IN
                SELECT inhrelid::regclass
                FROM pg_inherits
                WHERE inhparent = 'audit_log'::regclass
            LOOP
                PERFORM audit_log_attach_guards(part);
            END LOOP;
        END $$
        """
    )

    # TRUNCATE родителя каскадом добирается до партиций, поэтому закрываем и его.
    op.execute(
        """
        CREATE TRIGGER trg_audit_log_truncate
        BEFORE TRUNCATE ON audit_log
        FOR EACH STATEMENT EXECUTE FUNCTION audit_log_forbid_change()
        """
    )

    # Партиции создаются функцией из 0001 — она должна сразу вешать защиту,
    # иначе следующий месяц окажется незащищённым.
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

    # Роль приложения: INSERT и SELECT на журнал, полный доступ к остальному.
    # Если роль уже существует (повторный прогон, общий кластер), просто
    # выдаём права заново.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                CREATE ROLE {APP_ROLE} NOLOGIN;
            END IF;
        END $$
        """
    )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
    )
    # И отдельно — журнал: только чтение и добавление.
    op.execute(f"REVOKE ALL ON audit_log FROM {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON audit_log TO {APP_ROLE}")
    op.execute(
        f"""
        DO $$
        DECLARE
            part regclass;
        BEGIN
            FOR part IN
                SELECT inhrelid::regclass
                FROM pg_inherits
                WHERE inhparent = 'audit_log'::regclass
            LOOP
                EXECUTE format('REVOKE ALL ON %s FROM {APP_ROLE}', part);
                EXECUTE format('GRANT SELECT, INSERT ON %s TO {APP_ROLE}', part);
            END LOOP;
        END $$
        """
    )


def _downgrade_audit_immutability() -> None:
    """Снимает защиту так, чтобы откат 0001 прошёл.

    Порядок важен: 0001 удаляет `audit_log_forbid_change()` раньше самой
    таблицы, и пока на партициях висят ссылающиеся на неё триггеры,
    `DROP FUNCTION` падает по зависимостям.
    """
    op.execute("DROP TRIGGER IF EXISTS trg_audit_log_truncate ON audit_log")
    op.execute(
        """
        DO $$
        DECLARE
            part regclass;
            v_name text;
        BEGIN
            FOR part IN
                SELECT inhrelid::regclass
                FROM pg_inherits
                WHERE inhparent = 'audit_log'::regclass
            LOOP
                v_name := replace(part::text, '.', '_');
                EXECUTE format('DROP TRIGGER IF EXISTS trg_%s_immutable ON %s', v_name, part);
                EXECUTE format('DROP TRIGGER IF EXISTS trg_%s_truncate ON %s', v_name, part);
            END LOOP;
        END $$
        """
    )
    # Возвращаем функцию создания партиций к версии из 0001: она не должна
    # ссылаться на удаляемый ниже хелпер.
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
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute("DROP FUNCTION IF EXISTS audit_log_attach_guards(regclass)")

    # Права роли снимаем, саму роль не трогаем: она может быть общей для
    # кластера, а её удаление сломало бы соседние базы.
    op.execute(
        f"""
        DO $$
        DECLARE
            part regclass;
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
                FOR part IN
                    SELECT inhrelid::regclass
                    FROM pg_inherits
                    WHERE inhparent = 'audit_log'::regclass
                LOOP
                    EXECUTE format('REVOKE ALL ON %s FROM {APP_ROLE}', part);
                END LOOP;
                REVOKE ALL ON audit_log FROM {APP_ROLE};
                REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {APP_ROLE};
                REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {APP_ROLE};
                REVOKE USAGE ON SCHEMA public FROM {APP_ROLE};
                ALTER DEFAULT PRIVILEGES IN SCHEMA public
                    REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {APP_ROLE};
            END IF;
        END $$
        """
    )
