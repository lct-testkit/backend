"""Приём данных о людях: вендоры, оплаты, учащиеся LMS (отчёт внешнего тестирования)

Три файла заказчика — «Вендоры», «Данные оплат», «Загрузка пользователей» (шаблон LMS) — не
ложились в модель данных:

* **Вендоры.** `products.vendor_id` — вендор продукта (организация типа «компания»);
  `contact_products` — ответственный контакт продукта («связь контакт — продукт»);
  `contacts.contact_methods` — «Способ связи» (почта, чат в Telegram…).
* **Оплаты.** `deal_products.stream_number` — номер потока курса (раньше терялся);
  `deals.order_number` — внешний «Номер заявки» с частичным уникальным индексом: повторная
  загрузка/доставка того же заказа не создаёт вторую сделку.
* **Учащиеся.** `contact_learner_profiles` — ПДн из шаблона LMS (СНИЛС, паспорт, адрес, диплом…),
  отдельной таблицей: обычный `ContactOut` их не видит.
* **Дубли контактов.** `contacts.created_by` (создатель видит свой контакт: раньше 201, а затем 404 на
  контакт без организации и сделки); индекс по телефону; существующие email приводятся к нижнему
  регистру, телефоны — к E.164 (необратимо: исходное написание не сохраняется).
* **Импорт.** `import_row_results.effects` — что строка создала/изменила (строка вендора порождает
  организацию, продукты, контакт и связи; откат идёт по этому списку), `import_jobs.processed_rows`
  — прогресс применения, три новых значения `entity_type`.

Revision ID: 0017_people_import
Revises: 0016_identity_lifecycle
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017_people_import"
down_revision: str | None = "0016_identity_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_ENTITY_TYPE_CHECK = "entity_type IN ('organization','product','license')"
_NEW_ENTITY_TYPE_CHECK = (
    "entity_type IN ('organization','product','license','vendor_contact','payment','learner')"
)
_CLEAN_CONSTRAINT_NAME = "import_jobs_entity_type_valid"

_EDUCATION_CODES = (
    "'none','basic_general','secondary_general','secondary_vocational',"
    "'higher_bachelor','higher_specialist_master','higher_top_qualification'"
)


def _find_check_constraint(table: str, column_substring: str) -> str:
    """Имя CHECK-constraint'а по факту в БД (см. пояснение в 0015: имя из истории миграций и имя в БД
    расходятся из-за naming convention, поэтому ищем через системный каталог)."""
    bind = op.get_bind()
    return bind.execute(
        sa.text(
            "SELECT con.conname FROM pg_constraint con "
            "JOIN pg_class rel ON rel.oid = con.conrelid "
            "WHERE rel.relname = :table AND con.contype = 'c' "
            "AND pg_get_constraintdef(con.oid) LIKE :pattern"
        ),
        {"table": table, "pattern": f"%{column_substring}%"},
    ).scalar_one()


def _text_column(name: str, length: int | None = None) -> sa.Column:
    return sa.Column(name, sa.String(length) if length else sa.Text(), nullable=True)


def upgrade() -> None:
    # --- contacts ---------------------------------------------------------
    op.add_column("contacts", sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_contacts_created_by_users",
        "contacts",
        "users",
        ["created_by"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.add_column(
        "contacts",
        sa.Column(
            "contact_methods",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_index("ix_contacts_created_by", "contacts", ["created_by"])
    op.create_index("ix_contacts_phone", "contacts", ["phone"])

    # Единый вид ключей дедупликации (см. app/core/normalize.py): email — нижний регистр без
    # пробелов, телефон — E.164 для российских форматов. Международные номера и всё
    # нераспознанное остаются как были.
    op.execute(
        "UPDATE contacts SET email = lower(btrim(email)) "
        "WHERE email IS NOT NULL AND email <> lower(btrim(email))"
    )
    op.execute(
        """
        UPDATE contacts SET phone = CASE
            WHEN regexp_replace(phone, '\\D', '', 'g') ~ '^[78][0-9]{10}$'
                THEN '+7' || substr(regexp_replace(phone, '\\D', '', 'g'), 2)
            WHEN regexp_replace(phone, '\\D', '', 'g') ~ '^[0-9]{10}$'
                THEN '+7' || regexp_replace(phone, '\\D', '', 'g')
            ELSE phone
        END
        WHERE phone IS NOT NULL
        """
    )

    # --- products.vendor_id ------------------------------------------------
    op.add_column("products", sa.Column("vendor_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_products_vendor_id_organizations",
        "products",
        "organizations",
        ["vendor_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_products_vendor", "products", ["vendor_id"])

    # --- contact_products -------------------------------------------------
    op.create_table(
        "contact_products",
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(24), server_default="responsible", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("contact_id", "product_id", name="pk_contact_products"),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contacts.id"],
            name="fk_contact_products_contact_id_contacts",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name="fk_contact_products_product_id_products",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_contact_products_product", "contact_products", ["product_id"])

    # --- оплаты: поток и номер заявки --------------------------------------
    op.add_column("deal_products", sa.Column("stream_number", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "deal_products_stream_number_positive",
        "deal_products",
        "stream_number IS NULL OR stream_number > 0",
    )
    op.add_column("deals", sa.Column("order_number", sa.String(64), nullable=True))
    op.create_index(
        "uq_deals_order_number",
        "deals",
        ["order_number"],
        unique=True,
        postgresql_where=sa.text("order_number IS NOT NULL AND deleted_at IS NULL"),
    )

    # --- ПДн учащегося для шаблона LMS -----------------------------------------
    op.create_table(
        "contact_learner_profiles",
        sa.Column("contact_id", postgresql.UUID(as_uuid=True), nullable=False),
        _text_column("snils", 14),
        _text_column("passport_series", 8),
        _text_column("passport_number", 16),
        _text_column("passport_issued_by", 512),
        sa.Column("passport_issued_at", sa.Date(), nullable=True),
        _text_column("passport_dept_code", 16),
        sa.Column("sex", sa.String(1), nullable=True),
        sa.Column("birth_date", sa.Date(), nullable=True),
        _text_column("reg_region", 255),
        _text_column("reg_city", 255),
        _text_column("reg_street", 255),
        _text_column("reg_house", 64),
        _text_column("reg_apartment", 64),
        _text_column("reg_zip", 16),
        _text_column("first_name_dative", 128),
        _text_column("last_name_dative", 128),
        _text_column("middle_name_dative", 128),
        sa.Column("education", sa.String(32), nullable=True),
        _text_column("diploma_profession", 255),
        _text_column("diploma_institution", 512),
        _text_column("diploma_surname", 128),
        _text_column("diploma_number", 64),
        _text_column("diploma_series", 64),
        _text_column("diploma_reg_number", 64),
        sa.Column("diploma_issued_at", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("contact_id", name="pk_contact_learner_profiles"),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contacts.id"],
            name="fk_contact_learner_profiles_contact_id_contacts",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "sex IS NULL OR sex IN ('M','F')", name="contact_learner_profiles_sex_valid"
        ),
        sa.CheckConstraint(
            f"education IS NULL OR education IN ({_EDUCATION_CODES})",
            name="contact_learner_profiles_education_valid",
        ),
    )

    # --- импорт ------------------------------------------------------------
    op.add_column(
        "import_row_results",
        sa.Column(
            "effects", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
    )
    op.add_column(
        "import_jobs",
        sa.Column("processed_rows", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    old_name = _find_check_constraint("import_jobs", "entity_type")
    op.execute(f'ALTER TABLE import_jobs DROP CONSTRAINT "{old_name}"')
    op.create_check_constraint(_CLEAN_CONSTRAINT_NAME, "import_jobs", _NEW_ENTITY_TYPE_CHECK)


def downgrade() -> None:
    # Задания новых типов теряют смысл без таблиц, на которые ссылаются их эффекты, и не проходят
    # прежний CHECK: строки результатов уходят каскадом.
    op.execute(
        "DELETE FROM import_jobs WHERE entity_type IN ('vendor_contact','payment','learner')"
    )
    old_name = _find_check_constraint("import_jobs", "entity_type")
    op.execute(f'ALTER TABLE import_jobs DROP CONSTRAINT "{old_name}"')
    op.create_check_constraint(_CLEAN_CONSTRAINT_NAME, "import_jobs", _OLD_ENTITY_TYPE_CHECK)
    op.drop_column("import_jobs", "processed_rows")
    op.drop_column("import_row_results", "effects")

    op.drop_table("contact_learner_profiles")

    op.drop_index("uq_deals_order_number", table_name="deals")
    op.drop_column("deals", "order_number")
    stream_check = _find_check_constraint("deal_products", "stream_number")
    op.execute(f'ALTER TABLE deal_products DROP CONSTRAINT "{stream_check}"')
    op.drop_column("deal_products", "stream_number")

    op.drop_index("ix_contact_products_product", table_name="contact_products")
    op.drop_table("contact_products")

    op.drop_index("ix_products_vendor", table_name="products")
    op.drop_constraint("fk_products_vendor_id_organizations", "products", type_="foreignkey")
    op.drop_column("products", "vendor_id")

    op.drop_index("ix_contacts_phone", table_name="contacts")
    op.drop_index("ix_contacts_created_by", table_name="contacts")
    op.drop_column("contacts", "contact_methods")
    op.drop_constraint("fk_contacts_created_by_users", "contacts", type_="foreignkey")
    op.drop_column("contacts", "created_by")
