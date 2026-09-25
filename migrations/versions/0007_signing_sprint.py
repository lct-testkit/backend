"""Спринт 6: ПЭП (простая электронная подпись)

Создаёт таблицы раздела 5.12 (`edm_agreements`, `signature_templates`,
`signature_documents`, `signature_requests`, `signature_otp_codes`,
`signatures`) и добавляет реальные внешние ключи на месте «голых» UUID,
оставленных предыдущими спринтами в ожидании этого модуля:
`consents.signature_id`, `data_erasure_requests.act_signature_id`,
`data_erasure_requests.act_file_id`, `deals.active_signature_document_id`
(тот же приём, что 0006 применила к `organizations.registry_version_id`).

**Порядок создания важен.** Полный граф FK между `contacts` (спринт 4),
`consents` (спринт 1), `signatures` и `signature_requests` (этот спринт)
содержит цикл: `contacts.consent_id → consents.id`, `consents.signature_id →
signatures.id`, `signatures.request_id → signature_requests.id`,
`signature_requests.signer_contact_id → contacts.id`. Postgres не запрещает
циклические FK между таблицами (запрещены только самоссылающиеся строки при
одновременной вставке), поэтому таблицы создаются в порядке, где каждая
ссылается только на уже существующие: `signature_documents` раньше
`signature_requests`, та раньше `signatures`, а обратная ссылка
`consents.signature_id` добавляется отдельным `ALTER TABLE` в самом конце —
`consents` существует с 0001, `signatures` появляется только здесь.

**`signatures` неизменяема не только по конвенции.** `REVOKE UPDATE, DELETE`
для роли приложения (тот же приём, что `deal_status_history` в
`0004_deals_sprint`) здесь пришлось бы применять только к `DELETE`: dop.md
§10.7 требует один легитимный путь изменения — пометку `is_disputed` при
компрометации ключа. Поэтому основную защиту несёт `BEFORE UPDATE OR DELETE`
триггер (`FOR EACH ROW`, не `FOR EACH STATEMENT`, как у `audit_log` в
`0001_baseline`, — нужно сравнить конкретные старое/новое значения): он
безусловно запрещает `DELETE` и разрешает `UPDATE`, только если ни одно поле,
кроме `is_disputed`, не изменилось. `RealSigningService.mark_disputed_since`
(`app/modules/signing/service.py`) делает обычный `UPDATE ... SET
is_disputed = true` — триггер пропустит именно его и завернёт любой другой.

Revision ID: 0007_signing_sprint
Revises: 0006_registry_import_sprint
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_signing_sprint"
down_revision: str | None = "0006_registry_import_sprint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "crm_app"


def upgrade() -> None:
    _create_signature_templates()
    _create_edm_agreements()
    _create_signature_documents()
    _create_signature_requests()
    _create_signatures()
    _create_signature_otp_codes()
    _add_cross_module_foreign_keys()
    _lock_down_signatures()


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_signatures_immutable ON signatures")
    op.execute("DROP FUNCTION IF EXISTS signatures_forbid_change()")

    op.drop_constraint(
        "fk_deals_active_signature_document_id_signature_documents", "deals", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_data_erasure_requests_act_file_id_files", "data_erasure_requests", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_data_erasure_requests_act_signature_id_signatures",
        "data_erasure_requests",
        type_="foreignkey",
    )
    op.drop_constraint("fk_consents_signature_id_signatures", "consents", type_="foreignkey")

    op.drop_table("signature_otp_codes")
    op.drop_table("signatures")
    op.drop_table("signature_requests")
    op.drop_table("signature_documents")
    op.drop_table("edm_agreements")
    op.drop_table("signature_templates")


def _create_signature_templates() -> None:
    op.create_table(
        "signature_templates",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("doc_type", sa.String(16), nullable=False),
        sa.Column("body_template", sa.Text(), nullable=False),
        sa.Column(
            "required_signer_roles",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "default_deadline_days", sa.Integer(), server_default=sa.text("7"), nullable=False
        ),
        sa.Column("output_format", sa.String(8), server_default=sa.text("'pdf'"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_signature_templates"),
        sa.UniqueConstraint("code", name="uq_signature_templates_code"),
        sa.CheckConstraint(
            "doc_type IN ('kp','act','consent','erasure_act','offer','custom')",
            name="ck_signature_templates_signature_templates_doc_type_valid",
        ),
        sa.CheckConstraint(
            "output_format IN ('pdf')",
            name="ck_signature_templates_signature_templates_output_format_valid",
        ),
    )
    op.create_index("ix_signature_templates_created_at", "signature_templates", ["created_at"])


def _create_edm_agreements() -> None:
    op.create_table(
        "edm_agreements",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("party_type", sa.String(16), nullable=False),
        sa.Column("party_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agreement_number", sa.String(64), nullable=True),
        sa.Column("agreement_file_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("conclusion_method", sa.String(24), nullable=False),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("status", sa.String(16), server_default=sa.text("'active'"), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.Text(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_edm_agreements"),
        sa.ForeignKeyConstraint(
            ["agreement_file_id"],
            ["files.id"],
            name="fk_edm_agreements_agreement_file_id_files",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_edm_agreements_created_by_users",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "party_type IN ('organization','contact','user')",
            name="ck_edm_agreements_edm_agreements_party_type_valid",
        ),
        sa.CheckConstraint(
            "conclusion_method IN ('paper','ukep','offer_acceptance','employment')",
            name="ck_edm_agreements_edm_agreements_conclusion_method_valid",
        ),
        sa.CheckConstraint(
            "status IN ('active','expired','revoked')",
            name="ck_edm_agreements_edm_agreements_status_valid",
        ),
        sa.CheckConstraint(
            "status <> 'revoked' OR revoked_at IS NOT NULL",
            name="ck_edm_agreements_edm_agreements_revoked_at_present",
        ),
    )
    op.create_index("ix_edm_agreements_created_at", "edm_agreements", ["created_at"])
    op.create_index(
        "ix_edm_agreements_party", "edm_agreements", ["party_type", "party_id", "status"]
    )


def _create_signature_documents() -> None:
    op.create_table(
        "signature_documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("doc_type", sa.String(16), nullable=False),
        sa.Column("template_code", sa.String(64), nullable=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("signed_file_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("protocol_file_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("manifest", postgresql.JSONB(), nullable=True),
        sa.Column(
            "signing_order", sa.String(16), server_default=sa.text("'sequential'"), nullable=False
        ),
        sa.Column("status", sa.String(24), server_default=sa.text("'draft'"), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("void_reason", sa.Text(), nullable=True),
        sa.Column("voided_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("on_rejected", sa.String(64), nullable=True),
        sa.Column("on_expired", sa.String(32), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_signature_documents"),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_signature_documents_created_by_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["files.id"],
            name="fk_signature_documents_file_id_files",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["signed_file_id"],
            ["files.id"],
            name="fk_signature_documents_signed_file_id_files",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["protocol_file_id"],
            ["files.id"],
            name="fk_signature_documents_protocol_file_id_files",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "doc_type IN ('kp','act','consent','erasure_act','offer','custom')",
            name="ck_signature_documents_signature_documents_doc_type_valid",
        ),
        sa.CheckConstraint(
            "signing_order IN ('sequential','parallel')",
            name="ck_signature_documents_signature_documents_signing_order_valid",
        ),
        sa.CheckConstraint(
            "status IN ('draft','pending','partially_signed','signed','rejected',"
            "'expired','void','blocked_no_agreement')",
            name="ck_signature_documents_signature_documents_status_valid",
        ),
        sa.CheckConstraint(
            "status <> 'void' OR void_reason IS NOT NULL",
            name="ck_signature_documents_signature_documents_void_reason",
        ),
        sa.CheckConstraint(
            "on_expired IS NULL OR on_expired IN ('notify_initiator','void','previous_status')",
            name="ck_signature_documents_signature_documents_on_expired_valid",
        ),
    )
    op.create_index("ix_signature_documents_created_at", "signature_documents", ["created_at"])
    op.create_index(
        "ix_signature_documents_entity", "signature_documents", ["entity_type", "entity_id"]
    )
    op.create_index(
        "ix_signature_documents_status_deadline",
        "signature_documents",
        ["status", "deadline_at"],
        postgresql_where=sa.text("status IN ('pending','partially_signed')"),
    )


def _create_signature_requests() -> None:
    op.create_table(
        "signature_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("signer_type", sa.String(16), nullable=False),
        sa.Column("signer_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("signer_contact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("signer_role_code", sa.String(32), nullable=True),
        sa.Column("signer_name_snapshot", sa.String(255), nullable=False),
        sa.Column("signer_identifier_masked", sa.String(64), nullable=True),
        sa.Column("sign_order", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("access_token_hash", sa.String(64), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("viewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column("edm_agreement_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_signature_requests"),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["signature_documents.id"],
            name="fk_signature_requests_document_id_signature_documents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["signer_user_id"],
            ["users.id"],
            name="fk_signature_requests_signer_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["signer_contact_id"],
            ["contacts.id"],
            name="fk_signature_requests_signer_contact_id_contacts",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["edm_agreement_id"],
            ["edm_agreements.id"],
            name="fk_signature_requests_edm_agreement_id_edm_agreements",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "signer_type IN ('internal','external')",
            name="ck_signature_requests_signature_requests_signer_type_valid",
        ),
        sa.CheckConstraint(
            "status IN ('pending','sent','viewed','signed','rejected','expired','locked','void')",
            name="ck_signature_requests_signature_requests_status_valid",
        ),
        sa.CheckConstraint(
            "(signer_type = 'internal' AND signer_user_id IS NOT NULL) OR "
            "(signer_type = 'external' AND signer_contact_id IS NOT NULL)",
            name="ck_signature_requests_signature_requests_signer_ref_matches_type",
        ),
    )
    op.create_index(
        "ix_signature_requests_document", "signature_requests", ["document_id", "sign_order"]
    )
    op.create_index(
        "ix_signature_requests_user", "signature_requests", ["signer_user_id", "status"]
    )
    op.create_index(
        "ix_signature_requests_contact", "signature_requests", ["signer_contact_id", "status"]
    )
    op.create_index(
        "uq_signature_requests_token_hash",
        "signature_requests",
        ["access_token_hash"],
        unique=True,
        postgresql_where=sa.text("access_token_hash IS NOT NULL"),
    )


def _create_signatures() -> None:
    op.create_table(
        "signatures",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("method", sa.String(16), nullable=False),
        sa.Column("signer_display", sa.String(255), nullable=False),
        sa.Column("signature_value", sa.Text(), nullable=False),
        sa.Column(
            "algorithm", sa.String(32), server_default=sa.text("'HMAC-SHA256'"), nullable=False
        ),
        sa.Column("key_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("time_source", sa.String(64), nullable=True),
        sa.Column("time_drift_ms", sa.Integer(), nullable=True),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("is_disputed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("prev_hash", sa.String(64), nullable=True),
        sa.Column("hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_signatures"),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["signature_requests.id"],
            name="fk_signatures_request_id_signature_requests",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["signature_documents.id"],
            name="fk_signatures_document_id_signature_documents",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "method IN ('pep_otp','pep_session','unep','ukep')",
            name="ck_signatures_signatures_method_valid",
        ),
    )
    op.create_index("ix_signatures_created_at", "signatures", ["created_at"])
    op.create_index("ix_signatures_document", "signatures", ["document_id", "created_at"])
    op.create_index("ix_signatures_request", "signatures", ["request_id"])


def _create_signature_otp_codes() -> None:
    op.create_table(
        "signature_otp_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code_hash", sa.String(72), nullable=False),
        sa.Column("salt", sa.String(32), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("sent_to_masked", sa.String(64), nullable=False),
        sa.Column("attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.SmallInteger(), server_default=sa.text("3"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_signature_otp_codes"),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["signature_requests.id"],
            name="fk_signature_otp_codes_request_id_signature_requests",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "channel IN ('sms','email','telegram')",
            name="ck_signature_otp_codes_signature_otp_channel_valid",
        ),
    )
    op.create_index(
        "ix_signature_otp_codes_request", "signature_otp_codes", ["request_id", "created_at"]
    )


def _add_cross_module_foreign_keys() -> None:
    """`consents`/`data_erasure_requests`/`deals` держали эти столбцы «голым»
    UUID с тех спринтов, где появились сами таблицы, — `signatures`/
    `signature_documents` тогда не существовали (см. docstring модуля)."""
    op.create_foreign_key(
        "fk_consents_signature_id_signatures",
        "consents",
        "signatures",
        ["signature_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_data_erasure_requests_act_signature_id_signatures",
        "data_erasure_requests",
        "signatures",
        ["act_signature_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_data_erasure_requests_act_file_id_files",
        "data_erasure_requests",
        "files",
        ["act_file_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_deals_active_signature_document_id_signature_documents",
        "deals",
        "signature_documents",
        ["active_signature_document_id"],
        ["id"],
        ondelete="SET NULL",
    )


def _lock_down_signatures() -> None:
    """dop.md §10.9: «INSERT/SELECT, никакого UPDATE/DELETE (исключение —
    is_disputed через отдельную процедуру)». `DELETE` не имеет ни одного
    легитимного пути — блокируется и на уровне GRANT, и триггером. `UPDATE`
    легитимен ровно для одного поля, поэтому GRANT его не трогает (иначе
    `RealSigningService.mark_disputed_since` не смог бы работать под ролью
    приложения) — единственный барьер здесь триггер, который построчно
    сравнивает старую и новую запись без поля `is_disputed`."""
    op.execute(f"REVOKE DELETE ON signatures FROM {APP_ROLE}")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION signatures_forbid_change()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            old_row signatures;
            new_row signatures;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'signatures is append-only: DELETE is not allowed';
            END IF;
            old_row := OLD;
            new_row := NEW;
            old_row.is_disputed := false;
            new_row.is_disputed := false;
            IF old_row IS DISTINCT FROM new_row THEN
                RAISE EXCEPTION
                    'signatures is append-only: only is_disputed may change';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_signatures_immutable "
        "BEFORE UPDATE OR DELETE ON signatures "
        "FOR EACH ROW EXECUTE FUNCTION signatures_forbid_change()"
    )
