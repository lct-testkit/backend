"""Каркас: identity, аудит и системные таблицы

Создаёт таблицы разделов 5.1 и 5.10 спецификации:
users, teams, user_delegations, consents, security_events,
data_erasure_requests, feature_flags, system_settings, idempotency_keys
и партиционированный по месяцам audit_log.

Revision ID: 0001_baseline
Revises: None
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # pg_trgm нужен для триграммного поиска по названиям организаций,
    # unaccent — для поиска без учёта диакритики. Заводим сразу.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent")

    _create_identity_tables()
    _create_system_tables()
    _create_audit_log()


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_audit_log_immutable ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_forbid_change()")
    op.execute("DROP FUNCTION IF EXISTS create_audit_log_partition(date)")
    op.execute("DROP TABLE IF EXISTS audit_log CASCADE")

    op.drop_table("idempotency_keys")
    op.drop_table("system_settings")
    op.drop_table("feature_flags")

    op.drop_table("data_erasure_requests")
    op.drop_table("security_events")
    op.drop_table("consents")
    op.drop_table("user_delegations")
    op.drop_table("users")
    op.drop_table("teams")


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------


def _create_identity_tables() -> None:
    op.create_table(
        "teams",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("head_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("region_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_teams"),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["teams.id"], name="fk_teams_parent_id_teams", ondelete="RESTRICT"
        ),
    )
    op.create_index("ix_teams_parent_id", "teams", ["parent_id"])
    op.create_index("ix_teams_created_at", "teams", ["created_at"])

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("keycloak_id", sa.String(64), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=True),
        sa.Column("phone", sa.String(32), nullable=True),
        sa.Column("position", sa.String(255), nullable=True),
        sa.Column("role", sa.String(32), server_default="KAM", nullable=False),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("manager_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(16), server_default="invited", nullable=False),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("avatar_file_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("locale", sa.String(8), server_default="ru", nullable=False),
        sa.Column("timezone", sa.String(64), server_default="Europe/Moscow", nullable=False),
        sa.Column("perm_epoch", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consent_version", sa.String(32), nullable=True),
        sa.Column("invited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("anonymized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.ForeignKeyConstraint(
            ["team_id"], ["teams.id"], name="fk_users_team_id_teams", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["manager_id"], ["users.id"], name="fk_users_manager_id_users", ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "role IN ('KAM','HEAD','ADMIN','AUDITOR','INTEGRATION')",
            name="ck_users_users_role_valid",
        ),
        sa.CheckConstraint(
            "status IN ('invited','active','blocked','terminated','anonymized')",
            name="ck_users_users_status_valid",
        ),
    )
    # Уникальность только среди живых записей: soft delete не должен
    # блокировать повторное создание пользователя с тем же email.
    op.create_index(
        "uq_users_email_active",
        "users",
        ["email"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL AND email IS NOT NULL"),
    )
    op.create_index(
        "uq_users_keycloak_id_active",
        "users",
        ["keycloak_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL AND keycloak_id IS NOT NULL"),
    )
    op.create_index("ix_users_team_role", "users", ["team_id", "role"])
    op.create_index("ix_users_team_id", "users", ["team_id"])
    op.create_index("ix_users_manager_id", "users", ["manager_id"])
    op.create_index("ix_users_created_at", "users", ["created_at"])

    op.create_table(
        "user_delegations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("from_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("to_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_user_delegations"),
        sa.ForeignKeyConstraint(
            ["from_user_id"], ["users.id"], name="fk_user_delegations_from_user_id_users", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["to_user_id"], ["users.id"], name="fk_user_delegations_to_user_id_users", ondelete="RESTRICT"
        ),
        sa.CheckConstraint("ends_at > starts_at", name="ck_user_delegations_delegation_period_valid"),
    )
    op.create_index("ix_user_delegations_from_user_id", "user_delegations", ["from_user_id"])
    op.create_index(
        "ix_user_delegations_to_user_period",
        "user_delegations",
        ["to_user_id", "starts_at", "ends_at"],
    )

    op.create_table(
        "consents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject_type", sa.String(16), nullable=False),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("policy_version", sa.String(32), nullable=False),
        sa.Column("policy_text_hash", sa.String(64), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("signature_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_consents"),
        sa.CheckConstraint(
            "subject_type IN ('user','contact')", name="ck_consents_consent_subject_valid"
        ),
    )
    op.create_index(
        "ix_consents_subject", "consents", ["subject_type", "subject_id", "accepted_at"]
    )

    op.create_table(
        "security_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("severity", sa.String(16), server_default="info", nullable=False),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_security_events"),
    )
    op.create_index("ix_security_events_created_at", "security_events", ["created_at"])
    op.create_index(
        "ix_security_events_type_created", "security_events", ["event_type", "created_at"]
    )
    op.create_index(
        "ix_security_events_user_created", "security_events", ["user_id", "created_at"]
    )

    op.create_table(
        "data_erasure_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject_type", sa.String(16), nullable=False),
        sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("legal_basis", sa.String(255), nullable=True),
        sa.Column("requested_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("blockers", postgresql.JSONB(), nullable=True),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("act_file_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("act_signature_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_data_erasure_requests"),
        sa.CheckConstraint(
            "status IN ('pending','blocked','approved','rejected','completed')",
            name="ck_data_erasure_requests_erasure_status_valid",
        ),
        sa.CheckConstraint(
            "subject_type IN ('user','contact')",
            name="ck_data_erasure_requests_erasure_subject_valid",
        ),
    )
    op.create_index("ix_data_erasure_requests_created_at", "data_erasure_requests", ["created_at"])
    op.create_index(
        "ix_data_erasure_requests_subject",
        "data_erasure_requests",
        ["subject_type", "subject_id"],
    )


# --------------------------------------------------------------------------
# Системные таблицы
# --------------------------------------------------------------------------


def _create_system_tables() -> None:
    op.create_table(
        "feature_flags",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("rollout", sa.SmallInteger(), server_default=sa.text("100"), nullable=False),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_feature_flags"),
        sa.UniqueConstraint("code", name="uq_feature_flags_code"),
    )
    op.create_index("ix_feature_flags_created_at", "feature_flags", ["created_at"])

    op.create_table(
        "system_settings",
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("value", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_secret", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("key", name="pk_system_settings"),
    )
    op.create_index("ix_system_settings_created_at", "system_settings", ["created_at"])

    op.create_table(
        "idempotency_keys",
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("request_method", sa.String(10), nullable=False),
        sa.Column("request_path", sa.String(512), nullable=False),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key", name="pk_idempotency_keys"),
    )
    # По expires_at фоновая задача чистит просроченные ключи.
    op.create_index("ix_idempotency_keys_expires_at", "idempotency_keys", ["expires_at"])


# --------------------------------------------------------------------------
# Аудит
# --------------------------------------------------------------------------


def _create_audit_log() -> None:
    # Партиционирование по месяцам: журнал растёт быстрее всех остальных таблиц,
    # а старые партиции удобно отправлять в архив целиком.
    op.execute(
        """
        CREATE TABLE audit_log (
            id              uuid        NOT NULL,
            created_at      timestamptz NOT NULL DEFAULT now(),
            actor_id        uuid,
            actor_role      varchar(32),
            impersonated_by uuid,
            action          varchar(64) NOT NULL,
            entity_type     varchar(64),
            entity_id       uuid,
            changes         jsonb,
            result          varchar(16) NOT NULL,
            ip              inet,
            user_agent      text,
            request_id      varchar(64),
            prev_hash       varchar(64),
            hash            varchar(64) NOT NULL,
            CONSTRAINT pk_audit_log PRIMARY KEY (id, created_at),
            CONSTRAINT ck_audit_log_result_valid
                CHECK (result IN ('success','denied','error'))
        ) PARTITION BY RANGE (created_at)
        """
    )

    # actor_id намеренно без внешнего ключа: запись аудита обязана переживать
    # обезличивание и удаление пользователя.
    op.execute("CREATE INDEX ix_audit_log_actor_created ON audit_log (actor_id, created_at)")
    op.execute(
        "CREATE INDEX ix_audit_log_entity ON audit_log (entity_type, entity_id, created_at)"
    )
    op.execute("CREATE INDEX ix_audit_log_action_created ON audit_log (action, created_at)")
    op.execute("CREATE INDEX ix_audit_log_request_id ON audit_log (request_id)")
    op.execute("CREATE INDEX ix_audit_log_created_at ON audit_log (created_at)")

    # Хелпер создания месячной партиции. Вызывается планировщиком заранее.
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

    # Партиции на текущий месяц и год вперёд плюс DEFAULT, чтобы вставка
    # никогда не падала из-за отсутствия секции.
    op.execute(
        """
        DO $$
        DECLARE
            i int;
        BEGIN
            FOR i IN -1..12 LOOP
                PERFORM create_audit_log_partition(
                    (date_trunc('month', now()) + (i || ' month')::interval)::date
                );
            END LOOP;
        END $$
        """
    )
    op.execute("CREATE TABLE audit_log_default PARTITION OF audit_log DEFAULT")

    # Неизменяемость журнала. Одних GRANT недостаточно: владелец таблицы
    # обошёл бы их, а триггер действует на всех, включая суперпользователя.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_log_forbid_change()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'audit_log is append-only: % is not allowed', TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    # TRUNCATE нельзя перечислять в одном триггере с UPDATE/DELETE,
    # поэтому триггеров два.
    op.execute(
        """
        CREATE TRIGGER trg_audit_log_immutable
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH STATEMENT EXECUTE FUNCTION audit_log_forbid_change()
        """
    )
    op.execute("REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM PUBLIC")
