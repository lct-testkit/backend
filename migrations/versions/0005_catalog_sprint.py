"""Спринт 4: каталог, организации, контакты, файлы

Создаёт таблицы разделов 5.2/5.3/5.6: regions, organizations,
organization_branches, contacts, contact_channels, directions, products,
loss_reasons, holidays, custom_field_defs, files, attachments.

Добавляет внешние ключи, которых не было у `deals.organization_id`,
`deals.contact_id`, `deals.loss_reason_id` и `deal_products.product_id` —
миграция 0004 сознательно оставила их «голым» UUID, потому что каталог ещё
не существовал (см. docstring `app/modules/crm/models.py` до этого спринта).
Столбцы уже хранят корректные значения, ничего, кроме ограничения, менять
не нужно.

`organizations.search_vector` заполняется не приложением, а BEFORE
INSERT/UPDATE-триггером на встроенной (без расширений) функции
`tsvector_update_trigger` — `to_tsvector('russian', text)` не `IMMUTABLE`,
поэтому `GENERATED ALWAYS AS` для такой колонки Postgres не разрешает (см.
docstring `app/modules/catalog/models.py`). Триграммный индекс по `name`
использует `pg_trgm`, который `0001_baseline` уже создал именно для этого
(комментарий в той миграции: «нужен для триграммного поиска по названиям
организаций»).

`regions` заполняется списком субъектов РФ прямо в этой миграции: это
статичный справочник без собственных ручек записи (`catalog.router` отдаёт
только `GET /api/regions`), и organizations.region_id должен на что-то
ссылаться уже на старте, а не только после отдельного административного
импорта.

Revision ID: 0005_catalog_sprint
Revises: 0004_deals_sprint
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.ids import uuid7

revision: str = "0005_catalog_sprint"
down_revision: str | None = "0004_deals_sprint"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    _create_regions()
    _create_organizations()
    _create_organization_branches()
    _create_directions()
    _create_contacts()
    _create_contact_channels()
    _create_products()
    _create_loss_reasons()
    _create_holidays()
    _create_custom_field_defs()
    _create_files()
    _create_attachments()

    _add_deal_catalog_foreign_keys()
    _create_organization_search_trigger()
    _seed_regions()


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS organizations_search_vector_update ON organizations")

    op.drop_constraint("fk_deal_products_product_id_products", "deal_products", type_="foreignkey")
    op.drop_constraint("fk_deals_loss_reason_id_loss_reasons", "deals", type_="foreignkey")
    op.drop_constraint("fk_deals_contact_id_contacts", "deals", type_="foreignkey")
    op.drop_constraint("fk_deals_organization_id_organizations", "deals", type_="foreignkey")

    op.drop_table("attachments")
    op.drop_table("files")
    op.drop_table("custom_field_defs")
    op.drop_table("holidays")
    op.drop_table("loss_reasons")
    op.drop_table("products")
    op.drop_table("contact_channels")
    op.drop_table("contacts")
    op.drop_table("directions")
    op.drop_table("organization_branches")
    op.drop_table("organizations")
    op.drop_table("regions")


def _create_regions() -> None:
    op.create_table(
        "regions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("federal_district", sa.String(64), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=True),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name="pk_regions"),
        sa.UniqueConstraint("code", name="uq_regions_code"),
    )
    op.create_index("ix_regions_created_at", "regions", ["created_at"])


def _create_organizations() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("short_name", sa.String(255), nullable=True),
        sa.Column("org_type", sa.String(32), nullable=False),
        sa.Column("inn", sa.String(12), nullable=True),
        sa.Column("kpp", sa.String(9), nullable=True),
        sa.Column("ogrn", sa.String(15), nullable=True),
        sa.Column("legal_address", sa.Text(), nullable=True),
        sa.Column("actual_address", sa.Text(), nullable=True),
        sa.Column("region_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("website", sa.String(255), nullable=True),
        sa.Column("main_phone", sa.String(32), nullable=True),
        sa.Column("main_email", sa.String(255), nullable=True),
        sa.Column("students_count", sa.Integer(), nullable=True),
        sa.Column(
            "external_ids",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source", sa.String(32), nullable=True),
        sa.Column("import_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("search_vector", postgresql.TSVECTOR(), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "custom_fields",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("verified_source", sa.String(32), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("registry_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("registry_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("registry_status", sa.String(16), nullable=True),
        sa.Column("registry_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("requisites_drift", postgresql.JSONB(), nullable=True),
        sa.Column(
            "manual_overrides",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("is_accredited", sa.Boolean(), nullable=True),
        sa.Column("accreditation_until", sa.Date(), nullable=True),
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
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_organizations"),
        sa.ForeignKeyConstraint(
            ["region_id"],
            ["regions.id"],
            name="fk_organizations_region_id_regions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name="fk_organizations_owner_id_users", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name="fk_organizations_created_by_users",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "org_type IN ('university','college','company','individual_entrepreneur')",
            name="ck_organizations_organizations_org_type_valid",
        ),
        sa.CheckConstraint(
            "registry_status IS NULL OR registry_status IN "
            "('active','reorganizing','liquidating','liquidated','invalid')",
            name="ck_organizations_organizations_registry_status_valid",
        ),
    )
    op.create_index("ix_organizations_created_at", "organizations", ["created_at"])
    op.create_index("ix_organizations_owner", "organizations", ["owner_id"])
    op.create_index("ix_organizations_region", "organizations", ["region_id"])
    op.create_index(
        "uq_organizations_inn_active",
        "organizations",
        ["inn"],
        unique=True,
        postgresql_where=sa.text("inn IS NOT NULL AND deleted_at IS NULL"),
    )
    op.create_index(
        "ix_organizations_search_vector", "organizations", ["search_vector"], postgresql_using="gin"
    )
    op.execute(
        "CREATE INDEX ix_organizations_name_trgm ON organizations USING gin (name gin_trgm_ops)"
    )


def _create_organization_branches() -> None:
    op.create_table(
        "organization_branches",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("address", sa.Text(), nullable=True),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_organization_branches"),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_organization_branches_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["organization_branches.id"],
            name="fk_organization_branches_parent_id_organization_branches",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_organization_branches_created_at", "organization_branches", ["created_at"])
    op.create_index("ix_organization_branches_org", "organization_branches", ["organization_id"])


def _create_directions() -> None:
    op.create_table(
        "directions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
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
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_directions"),
        sa.UniqueConstraint("code", name="uq_directions_code"),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["directions.id"],
            name="fk_directions_parent_id_directions",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_directions_created_at", "directions", ["created_at"])


def _create_contacts() -> None:
    op.create_table(
        "contacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("first_name", sa.String(128), nullable=False),
        sa.Column("last_name", sa.String(128), nullable=False),
        sa.Column("middle_name", sa.String(128), nullable=True),
        sa.Column("position", sa.String(255), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("phone", sa.String(32), nullable=True),
        sa.Column(
            "is_decision_maker", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("is_anonymized", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("anonymized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source", sa.String(32), nullable=True),
        sa.Column(
            "external_ids",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
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
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_contacts"),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_contacts_organization_id_organizations",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["consent_id"],
            ["consents.id"],
            name="fk_contacts_consent_id_consents",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_contacts_created_at", "contacts", ["created_at"])
    op.create_index("ix_contacts_organization", "contacts", ["organization_id"])
    op.create_index("ix_contacts_email", "contacts", ["email"])


def _create_contact_channels() -> None:
    op.create_table(
        "contact_channels",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("value", sa.String(255), nullable=False),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("is_verified", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_contact_channels"),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contacts.id"],
            name="fk_contact_channels_contact_id_contacts",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "type IN ('telegram','whatsapp','phone_extra','email_extra')",
            name="ck_contact_channels_contact_channels_type_valid",
        ),
    )
    op.create_index("ix_contact_channels_contact", "contact_channels", ["contact_id"])


def _create_products() -> None:
    op.create_table(
        "products",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("direction_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("duration_hours", sa.Integer(), nullable=True),
        sa.Column("format", sa.String(16), nullable=True),
        sa.Column("base_price", sa.Numeric(14, 2), nullable=True),
        sa.Column("currency", sa.String(3), server_default="RUB", nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column("import_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "custom_fields",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
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
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_products"),
        sa.UniqueConstraint("code", name="uq_products_code"),
        sa.ForeignKeyConstraint(
            ["direction_id"],
            ["directions.id"],
            name="fk_products_direction_id_directions",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "format IS NULL OR format IN ('online','offline','blended')",
            name="ck_products_products_format_valid",
        ),
    )
    op.create_index("ix_products_created_at", "products", ["created_at"])
    op.create_index("ix_products_direction", "products", ["direction_id"])


def _create_loss_reasons() -> None:
    op.create_table(
        "loss_reasons",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("category", sa.String(16), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
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
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_loss_reasons"),
        sa.UniqueConstraint("code", name="uq_loss_reasons_code"),
        sa.CheckConstraint(
            "category IN ('price','timing','competitor','no_need','no_budget','no_contact','other')",
            name="ck_loss_reasons_loss_reasons_category_valid",
        ),
    )
    op.create_index("ix_loss_reasons_created_at", "loss_reasons", ["created_at"])


def _create_holidays() -> None:
    op.create_table(
        "holidays",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("is_working_day", sa.Boolean(), server_default=sa.text("false"), nullable=False),
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
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_holidays"),
        sa.UniqueConstraint("date", name="uq_holidays_date"),
    )
    op.create_index("ix_holidays_created_at", "holidays", ["created_at"])


def _create_custom_field_defs() -> None:
    op.create_table(
        "custom_field_defs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_type", sa.String(16), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("label", sa.String(255), nullable=False),
        sa.Column("field_type", sa.String(16), nullable=False),
        sa.Column("options", postgresql.JSONB(), nullable=True),
        sa.Column("is_required", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("validation", postgresql.JSONB(), nullable=True),
        sa.Column("workflow_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
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
        sa.Column("version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_custom_field_defs"),
        sa.UniqueConstraint("entity_type", "code", name="uq_custom_field_defs_entity_code"),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflows.id"],
            name="fk_custom_field_defs_workflow_id_workflows",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "entity_type IN ('deal','organization','contact','product')",
            name="ck_custom_field_defs_custom_field_defs_entity_type_valid",
        ),
        sa.CheckConstraint(
            "field_type IN ('string','number','date','bool','select','multiselect','file')",
            name="ck_custom_field_defs_custom_field_defs_field_type_valid",
        ),
    )
    op.create_index("ix_custom_field_defs_created_at", "custom_field_defs", ["created_at"])


def _create_files() -> None:
    op.create_table(
        "files",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("bucket", sa.String(128), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("refcount", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("scan_result", sa.String(32), nullable=True),
        sa.Column("scanned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("contains_pd", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("uploaded_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_files"),
        sa.ForeignKeyConstraint(
            ["uploaded_by"], ["users.id"], name="fk_files_uploaded_by_users", ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "status IN ('pending','ready','infected','quarantined','deleted')",
            name="ck_files_files_status_valid",
        ),
    )
    op.create_index("ix_files_created_at", "files", ["created_at"])
    op.create_index("ix_files_sha256", "files", ["sha256"])
    op.create_index("ix_files_uploaded_by", "files", ["uploaded_by"])


def _create_attachments() -> None:
    op.create_table(
        "attachments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category", sa.String(24), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("uploaded_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_attachments"),
        sa.ForeignKeyConstraint(
            ["file_id"], ["files.id"], name="fk_attachments_file_id_files", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by"],
            ["users.id"],
            name="fk_attachments_uploaded_by_users",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "category IN ('contract','presentation','act','license','report',"
            "'signature_container','other')",
            name="ck_attachments_attachments_category_valid",
        ),
    )
    op.create_index("ix_attachments_created_at", "attachments", ["created_at"])
    op.create_index("ix_attachments_file", "attachments", ["file_id"])
    op.create_index("ix_attachments_entity", "attachments", ["entity_type", "entity_id"])


def _add_deal_catalog_foreign_keys() -> None:
    op.create_foreign_key(
        "fk_deals_organization_id_organizations",
        "deals",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_deals_contact_id_contacts",
        "deals",
        "contacts",
        ["contact_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_deals_loss_reason_id_loss_reasons",
        "deals",
        "loss_reasons",
        ["loss_reason_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_deal_products_product_id_products",
        "deal_products",
        "products",
        ["product_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def _create_organization_search_trigger() -> None:
    op.execute(
        "CREATE TRIGGER organizations_search_vector_update "
        "BEFORE INSERT OR UPDATE OF name, short_name, inn ON organizations "
        "FOR EACH ROW EXECUTE FUNCTION "
        "tsvector_update_trigger(search_vector, 'pg_catalog.russian', name, short_name, inn)"
    )


# Код, полное и краткое название, федеральный округ, часовой пояс (UTC-смещение
# как в IANA tz database) — раздел 5.2. Источник: перечень субъектов РФ ФНС/
# Росстата на 2024 год, без временно оккупированных территорий Украины —
# это база «в закрытом контуре ИТ Школы Ростелекома», не политическое заявление,
# а фактическое административно-территориальное деление, признаваемое сторонами
# контракта на момент разработки.
_REGIONS: tuple[tuple[str, str, str, str], ...] = (
    ("77", "Москва", "Центральный", "Europe/Moscow"),
    ("78", "Санкт-Петербург", "Северо-Западный", "Europe/Moscow"),
    ("01", "Республика Адыгея", "Южный", "Europe/Moscow"),
    ("02", "Республика Башкортостан", "Приволжский", "Asia/Yekaterinburg"),
    ("03", "Республика Бурятия", "Сибирский", "Asia/Irkutsk"),
    ("04", "Республика Алтай", "Сибирский", "Asia/Barnaul"),
    ("05", "Республика Дагестан", "Северо-Кавказский", "Europe/Moscow"),
    ("06", "Республика Ингушетия", "Северо-Кавказский", "Europe/Moscow"),
    ("07", "Кабардино-Балкарская Республика", "Северо-Кавказский", "Europe/Moscow"),
    ("08", "Республика Калмыкия", "Южный", "Europe/Moscow"),
    ("09", "Карачаево-Черкесская Республика", "Северо-Кавказский", "Europe/Moscow"),
    ("10", "Республика Карелия", "Северо-Западный", "Europe/Moscow"),
    ("11", "Республика Коми", "Северо-Западный", "Europe/Moscow"),
    ("12", "Республика Марий Эл", "Приволжский", "Europe/Moscow"),
    ("13", "Республика Мордовия", "Приволжский", "Europe/Moscow"),
    ("14", "Республика Саха (Якутия)", "Дальневосточный", "Asia/Yakutsk"),
    ("15", "Республика Северная Осетия — Алания", "Северо-Кавказский", "Europe/Moscow"),
    ("16", "Республика Татарстан", "Приволжский", "Europe/Moscow"),
    ("17", "Республика Тыва", "Сибирский", "Asia/Krasnoyarsk"),
    ("18", "Удмуртская Республика", "Приволжский", "Europe/Samara"),
    ("19", "Республика Хакасия", "Сибирский", "Asia/Krasnoyarsk"),
    ("20", "Чеченская Республика", "Северо-Кавказский", "Europe/Moscow"),
    ("21", "Чувашская Республика", "Приволжский", "Europe/Moscow"),
    ("22", "Алтайский край", "Сибирский", "Asia/Barnaul"),
    ("23", "Краснодарский край", "Южный", "Europe/Moscow"),
    ("24", "Красноярский край", "Сибирский", "Asia/Krasnoyarsk"),
    ("25", "Приморский край", "Дальневосточный", "Asia/Vladivostok"),
    ("26", "Ставропольский край", "Северо-Кавказский", "Europe/Moscow"),
    ("27", "Хабаровский край", "Дальневосточный", "Asia/Vladivostok"),
    ("28", "Амурская область", "Дальневосточный", "Asia/Yakutsk"),
    ("29", "Архангельская область", "Северо-Западный", "Europe/Moscow"),
    ("30", "Астраханская область", "Южный", "Europe/Moscow"),
    ("31", "Белгородская область", "Центральный", "Europe/Moscow"),
    ("32", "Брянская область", "Центральный", "Europe/Moscow"),
    ("33", "Владимирская область", "Центральный", "Europe/Moscow"),
    ("34", "Волгоградская область", "Южный", "Europe/Volgograd"),
    ("35", "Вологодская область", "Северо-Западный", "Europe/Moscow"),
    ("36", "Воронежская область", "Центральный", "Europe/Moscow"),
    ("37", "Ивановская область", "Центральный", "Europe/Moscow"),
    ("38", "Иркутская область", "Сибирский", "Asia/Irkutsk"),
    ("39", "Калининградская область", "Северо-Западный", "Europe/Kaliningrad"),
    ("40", "Калужская область", "Центральный", "Europe/Moscow"),
    ("41", "Камчатский край", "Дальневосточный", "Asia/Kamchatka"),
    ("42", "Кемеровская область", "Сибирский", "Asia/Novokuznetsk"),
    ("43", "Кировская область", "Приволжский", "Europe/Kirov"),
    ("44", "Костромская область", "Центральный", "Europe/Moscow"),
    ("45", "Курганская область", "Уральский", "Asia/Yekaterinburg"),
    ("46", "Курская область", "Центральный", "Europe/Moscow"),
    ("47", "Ленинградская область", "Северо-Западный", "Europe/Moscow"),
    ("48", "Липецкая область", "Центральный", "Europe/Moscow"),
    ("49", "Магаданская область", "Дальневосточный", "Asia/Magadan"),
    ("50", "Московская область", "Центральный", "Europe/Moscow"),
    ("51", "Мурманская область", "Северо-Западный", "Europe/Moscow"),
    ("52", "Нижегородская область", "Приволжский", "Europe/Moscow"),
    ("53", "Новгородская область", "Северо-Западный", "Europe/Moscow"),
    ("54", "Новосибирская область", "Сибирский", "Asia/Novosibirsk"),
    ("55", "Омская область", "Сибирский", "Asia/Omsk"),
    ("56", "Оренбургская область", "Приволжский", "Asia/Yekaterinburg"),
    ("57", "Орловская область", "Центральный", "Europe/Moscow"),
    ("58", "Пензенская область", "Приволжский", "Europe/Moscow"),
    ("59", "Пермский край", "Приволжский", "Asia/Yekaterinburg"),
    ("60", "Псковская область", "Северо-Западный", "Europe/Moscow"),
    ("61", "Ростовская область", "Южный", "Europe/Moscow"),
    ("62", "Рязанская область", "Центральный", "Europe/Moscow"),
    ("63", "Самарская область", "Приволжский", "Europe/Samara"),
    ("64", "Саратовская область", "Приволжский", "Europe/Saratov"),
    ("65", "Сахалинская область", "Дальневосточный", "Asia/Sakhalin"),
    ("66", "Свердловская область", "Уральский", "Asia/Yekaterinburg"),
    ("67", "Смоленская область", "Центральный", "Europe/Moscow"),
    ("68", "Тамбовская область", "Центральный", "Europe/Moscow"),
    ("69", "Тверская область", "Центральный", "Europe/Moscow"),
    ("70", "Томская область", "Сибирский", "Asia/Tomsk"),
    ("71", "Тульская область", "Центральный", "Europe/Moscow"),
    ("72", "Тюменская область", "Уральский", "Asia/Yekaterinburg"),
    ("73", "Ульяновская область", "Приволжский", "Europe/Ulyanovsk"),
    ("74", "Челябинская область", "Уральский", "Asia/Yekaterinburg"),
    ("75", "Забайкальский край", "Дальневосточный", "Asia/Chita"),
    ("76", "Ярославская область", "Центральный", "Europe/Moscow"),
    ("79", "Еврейская автономная область", "Дальневосточный", "Asia/Vladivostok"),
    ("83", "Ненецкий автономный округ", "Северо-Западный", "Europe/Moscow"),
    ("86", "Ханты-Мансийский автономный округ — Югра", "Уральский", "Asia/Yekaterinburg"),
    ("87", "Чукотский автономный округ", "Дальневосточный", "Asia/Anadyr"),
    ("89", "Ямало-Ненецкий автономный округ", "Уральский", "Asia/Yekaterinburg"),
    ("91", "Республика Крым", "Южный", "Europe/Simferopol"),
    ("92", "Севастополь", "Южный", "Europe/Simferopol"),
)


def _seed_regions() -> None:
    regions = sa.table(
        "regions",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("code", sa.String),
        sa.column("name", sa.String),
        sa.column("federal_district", sa.String),
        sa.column("timezone", sa.String),
    )
    # UUIDv7 генерируется в Python тем же генератором, что и остальные
    # первичные ключи приложения (`app.core.ids.uuid7`) — не серверной
    # функцией: `gen_random_uuid()` даёт UUIDv4 и требует pgcrypto, которого
    # `0001_baseline` не создавала.
    op.bulk_insert(
        regions,
        [
            {
                "id": uuid7(),
                "code": code,
                "name": name,
                "federal_district": district,
                "timezone": tz,
            }
            for code, name, district, tz in _REGIONS
        ],
    )
