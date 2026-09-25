"""Спринт 5: локальный реестр ЕГРЮЛ, автоподстановка, импорт каталогов

Создаёт таблицы раздела 5.11 (`egrul_entries`, `registry_versions`,
`university_registry`, `org_lookup_log`) и раздела 5.10/§4.12
(`import_jobs`, `import_row_results`, `import_presets`).

Добавляет реальные внешние ключи `organizations.registry_version_id`,
`organizations.import_job_id`, `products.import_job_id` — миграция 0005
сознательно оставила их «голым» UUID, потому что `registry_versions` и
`import_jobs` тогда ещё не существовали (см. docstring
`app/modules/catalog/models.py` до этого спринта). Столбцы уже хранят
корректные значения (везде `NULL` — до этого спринта записывать в них было
нечему), поэтому FK добавляется без бэкафилла, как и в 0005 для
`deals.organization_id`/`deals.contact_id`.

`egrul_entries.search_vector` — тот же приём, что и `organizations.
search_vector` в 0005: BEFORE INSERT/UPDATE-триггер на встроенной
`tsvector_update_trigger`, а не `GENERATED ALWAYS AS`, потому что
`to_tsvector('russian', text)` не `IMMUTABLE`. Триграммный индекс по
`short_name` использует `pg_trgm`, который `0001_baseline` уже создала.

Revision ID: 0006_registry_import_sprint
Revises: 0005_catalog_sprint
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_registry_import_sprint"
down_revision: str | None = "0005_catalog_sprint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _create_registry_versions()
    _create_egrul_entries()
    _create_university_registry()
    _create_org_lookup_log()
    _create_import_jobs()
    _create_import_row_results()
    _create_import_presets()
    _add_catalog_registry_foreign_keys()
    _create_egrul_search_trigger()


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS egrul_entries_search_vector_update ON egrul_entries")

    op.drop_constraint("fk_products_import_job_id_import_jobs", "products", type_="foreignkey")
    op.drop_constraint(
        "fk_organizations_registry_version_id_registry_versions",
        "organizations",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_organizations_import_job_id_import_jobs", "organizations", type_="foreignkey"
    )

    op.drop_table("import_presets")
    op.drop_table("import_row_results")
    op.drop_table("import_jobs")
    op.drop_table("org_lookup_log")
    op.drop_table("university_registry")
    op.drop_table("egrul_entries")
    op.drop_table("registry_versions")


def _create_registry_versions() -> None:
    op.create_table(
        "registry_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(16), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entries_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("imported_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("checksum", sa.String(64), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_registry_versions"),
        sa.ForeignKeyConstraint(
            ["imported_by"],
            ["users.id"],
            name="fk_registry_versions_imported_by_users",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "source IN ('fns_egrul','rosobrnadzor','manual')",
            name="ck_registry_versions_registry_versions_source_valid",
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','completed','failed')",
            name="ck_registry_versions_registry_versions_status_valid",
        ),
    )
    op.create_index("ix_registry_versions_created_at", "registry_versions", ["created_at"])


def _create_egrul_entries() -> None:
    op.create_table(
        "egrul_entries",
        sa.Column("inn", sa.String(12), nullable=False),
        sa.Column("ogrn", sa.String(15), nullable=True),
        sa.Column("kpp", sa.String(9), nullable=True),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("short_name", sa.String(512), nullable=True),
        sa.Column("opf_code", sa.String(16), nullable=True),
        sa.Column("opf_name", sa.String(255), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("registration_date", sa.Date(), nullable=True),
        sa.Column("termination_date", sa.Date(), nullable=True),
        sa.Column("region_code", sa.String(4), nullable=True),
        sa.Column("legal_address", sa.Text(), nullable=True),
        sa.Column("address_parts", postgresql.JSONB(), nullable=True),
        sa.Column("okved_main", sa.String(16), nullable=True),
        sa.Column("okved_extra", postgresql.ARRAY(sa.String(16)), nullable=True),
        sa.Column("director_name", sa.String(255), nullable=True),
        sa.Column("director_position", sa.String(255), nullable=True),
        sa.Column("capital", sa.Numeric(18, 2), nullable=True),
        sa.Column("is_educational", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("registry_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("raw", postgresql.JSONB(), nullable=True),
        sa.Column("search_vector", postgresql.TSVECTOR(), nullable=True),
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
        sa.PrimaryKeyConstraint("inn", name="pk_egrul_entries"),
        sa.ForeignKeyConstraint(
            ["registry_version_id"],
            ["registry_versions.id"],
            name="fk_egrul_entries_registry_version_id_registry_versions",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "status IN ('active','reorganizing','liquidating','liquidated','invalid')",
            name="ck_egrul_entries_egrul_entries_status_valid",
        ),
    )
    op.create_index("ix_egrul_entries_created_at", "egrul_entries", ["created_at"])
    op.create_index(
        "ix_egrul_entries_search_vector", "egrul_entries", ["search_vector"], postgresql_using="gin"
    )
    op.execute(
        "CREATE INDEX ix_egrul_entries_short_name_trgm ON egrul_entries "
        "USING gin (short_name gin_trgm_ops)"
    )
    op.create_index("ix_egrul_entries_status", "egrul_entries", ["status"])
    op.create_index("ix_egrul_entries_region_code", "egrul_entries", ["region_code"])
    op.create_index(
        "ix_egrul_entries_educational",
        "egrul_entries",
        ["is_educational"],
        postgresql_where=sa.text("is_educational"),
    )


def _create_university_registry() -> None:
    op.create_table(
        "university_registry",
        sa.Column("inn", sa.String(12), nullable=False),
        sa.Column("license_number", sa.String(64), nullable=True),
        sa.Column("license_date", sa.Date(), nullable=True),
        sa.Column("accreditation_until", sa.Date(), nullable=True),
        sa.Column("founder_type", sa.String(64), nullable=True),
        sa.Column("forms", postgresql.ARRAY(sa.String(32)), nullable=True),
        sa.Column("directions_codes", postgresql.ARRAY(sa.String(32)), nullable=True),
        sa.Column("students_total", sa.Integer(), nullable=True),
        sa.Column("campus_count", sa.Integer(), nullable=True),
        sa.Column("registry_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("raw", postgresql.JSONB(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("inn", name="pk_university_registry"),
        sa.ForeignKeyConstraint(
            ["registry_version_id"],
            ["registry_versions.id"],
            name="fk_university_registry_registry_version_id_registry_versions",
            ondelete="SET NULL",
        ),
    )


def _create_org_lookup_log() -> None:
    op.create_table(
        "org_lookup_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("query_masked", sa.String(255), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("result_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("matched_inn", sa.String(12), nullable=True),
        sa.Column("response_ms", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_org_lookup_log"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_org_lookup_log_user_id_users", ondelete="SET NULL"
        ),
    )
    op.create_index("ix_org_lookup_log_user_created", "org_lookup_log", ["user_id", "created_at"])


def _create_import_jobs() -> None:
    op.create_table(
        "import_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_type", sa.String(16), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column(
            "mapping", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("status", sa.String(24), server_default=sa.text("'uploaded'"), nullable=False),
        sa.Column("total_rows", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("ok_rows", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("warn_rows", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_rows", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("result_file_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("initiated_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_format", sa.String(8), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "rollback_available", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name="pk_import_jobs"),
        sa.ForeignKeyConstraint(
            ["initiated_by"],
            ["users.id"],
            name="fk_import_jobs_initiated_by_users",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "entity_type IN ('organization','product')",
            name="ck_import_jobs_import_jobs_entity_type_valid",
        ),
        sa.CheckConstraint(
            "mode IN ('insert','upsert','update')", name="ck_import_jobs_import_jobs_mode_valid"
        ),
        sa.CheckConstraint(
            "status IN ('uploaded','mapped','validated','applying','completed',"
            "'completed_with_errors','rolling_back','rolled_back','failed')",
            name="ck_import_jobs_import_jobs_status_valid",
        ),
    )
    op.create_index("ix_import_jobs_created_at", "import_jobs", ["created_at"])
    op.create_index("ix_import_jobs_status", "import_jobs", ["status"])
    op.create_index("ix_import_jobs_initiated_by", "import_jobs", ["initiated_by"])


def _create_import_row_results() -> None:
    op.create_table(
        "import_row_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("import_job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("before_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column(
            "row_data", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "errors", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_import_row_results"),
        sa.ForeignKeyConstraint(
            ["import_job_id"],
            ["import_jobs.id"],
            name="fk_import_row_results_import_job_id_import_jobs",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('ok','warn','error','skipped','rolled_back','rollback_blocked')",
            name="ck_import_row_results_import_row_results_status_valid",
        ),
    )
    op.create_index("ix_import_row_results_job", "import_row_results", ["import_job_id"])
    op.create_index(
        "ix_import_row_results_job_pending",
        "import_row_results",
        ["import_job_id"],
        postgresql_where=sa.text("entity_id IS NULL AND status IN ('ok','warn')"),
    )
    op.create_index(
        "ix_import_row_results_job_applied",
        "import_row_results",
        ["import_job_id"],
        postgresql_where=sa.text("entity_id IS NOT NULL"),
    )


def _create_import_presets() -> None:
    op.create_table(
        "import_presets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("entity_type", sa.String(16), nullable=False),
        sa.Column("mapping", postgresql.JSONB(), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_import_presets"),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_import_presets_created_by_users",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_import_presets_created_at", "import_presets", ["created_at"])
    op.create_index("ix_import_presets_entity_type", "import_presets", ["entity_type"])


def _add_catalog_registry_foreign_keys() -> None:
    op.create_foreign_key(
        "fk_organizations_registry_version_id_registry_versions",
        "organizations",
        "registry_versions",
        ["registry_version_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_organizations_import_job_id_import_jobs",
        "organizations",
        "import_jobs",
        ["import_job_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_products_import_job_id_import_jobs",
        "products",
        "import_jobs",
        ["import_job_id"],
        ["id"],
        ondelete="SET NULL",
    )


def _create_egrul_search_trigger() -> None:
    op.execute(
        "CREATE TRIGGER egrul_entries_search_vector_update "
        "BEFORE INSERT OR UPDATE ON egrul_entries FOR EACH ROW EXECUTE FUNCTION "
        "tsvector_update_trigger(search_vector, 'pg_catalog.russian', full_name, short_name, inn)"
    )
