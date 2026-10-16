"""B3-1: составной ключ лицензии (вуз, вендор, ПО, номер договора) и связь с каталогом

ТЗ §4 п.1 просит каталог «вуз – вендор – ПО», загружаемый из xls/xlsx.
Единственным естественным ключом строки был `contract_number`: один договор
на несколько единиц ПО молча терял строки при импорте (BUG-9, старый
`imports.service._apply_license_row` искал запись по номеру договора и
перезаписывал её), а файл без номеров договоров нельзя было загрузить
вовсе — `contract_number` был `NOT NULL`.

Эта миграция:

1. Делает `organization_licenses.contract_number` необязательным.
2. Добавляет `vendor_id`/`product_id` (необязательные ссылки на каталог —
   заполняет их `catalog.license_matching.LicenseMatcher`, задача B3-4, и
   ручной ввод через API B3-6) и `vendor_key`/`product_key`/`contract_key` —
   составной естественный ключ строки (`catalog.license_keys.LicenseKey`).
3. Backfill'ит ключи для уже существующих строк. Дубли одного ключа (тот же
   вуз, вендор, ПО и номер договора) получают суффикс `~2`, `~3`, … у
   `contract_key` — данные не удаляются, только разводится ключ, чтобы на
   них лёг уникальный индекс `uq_org_licenses_key`.
4. Пытается привязать `vendor_id`/`product_id` существующих строк к
   каталогу: `vendor_id` — если ровно одна организация `org_type='company'`
   имеет тот же `company_key` названия/короткого названия; `product_id` —
   если ровно один продукт `kind='software'` (его же вводит эта миграция,
   см. ниже) имеет тот же ключ названия. Неоднозначные и ненайденные
   остаются `NULL` — их свяжет первая ручная правка через API или следующий
   запуск импорта.
5. Заодно вводит `products.kind` (`course`/`software`, задача B3-7, в одной
   миграции с B3-1 по контракту задачи — обе колонки нужны `catalog.
   license_matching` сразу): бэкфилл `software` там, где уже стоит
   `vendor_id` (вендорский импорт и задачи-лицензии пишут ПО именно туда).

Нормализация имени/номера скопирована из `app.core.normalize.company_key` и
`app.modules.catalog.license_keys.contract_key` прямо в этот файл (а не
импортирована): правка той логики в будущем не должна тихо поменять
поведение уже применённой миграции. Паритет с текущим кодом проверяет
`tests/test_license_keys_migration_parity.py`.

Revision ID: 0026_license_key_catalog_link
Revises: 0025_import_job_last_error

Примечание к имени ревизии: `alembic_version.version_num` — `varchar(32)`
(видно по факту предыдущих ревизий, например `0024_audit_chain_head_
autovacuum` — ровно 32 символа), поэтому слаг короче, чем в задаче B3-1
документа спринта (`0026_b3_license_key_and_catalog_link`, 36 символов —
не влезает и ловится только при реальном `alembic upgrade`, не при обычных
тестах). Содержание ревизии от этого не меняется.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0026_license_key_catalog_link"
down_revision: str | None = "0025_import_job_last_error"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# --- Копия нормализации (см. докстринг выше, зачем копия, а не импорт) ----

_SPACES = re.compile(r"\s+")
_QUOTE_CHARS = "«»\"“”„'`"
_CONTRACT_PREFIX = re.compile(r"^(?:№|no\.?|n\.?)\s*")
_CONTRACT_SEPARATOR_SPACES = re.compile(r"\s*([/-])\s*")


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = _SPACES.sub(" ", unicodedata.normalize("NFKC", str(value))).strip()
    return text or None


def _company_key(value: object) -> str:
    text = _clean_text(value) or ""
    text = text.casefold().replace("ё", "е")
    for quote in _QUOTE_CHARS:
        text = text.replace(quote, " ")
    text = text.replace("—", "-").replace("–", "-")
    return _SPACES.sub(" ", text).strip(" .,;:-")


def _contract_key(value: object) -> str:
    text = _company_key(value)
    if not text:
        return ""
    text = _CONTRACT_PREFIX.sub("", text)
    text = _CONTRACT_SEPARATOR_SPACES.sub(r"\1", text)
    return text


def _find_check_constraint(table: str, column_substring: str) -> str | None:
    """См. `0015_organization_licenses._find_check_constraint` — то же
    приём, адресуем CHECK по факту в БД, а не по угаданному имени."""
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            "SELECT con.conname FROM pg_constraint con "
            "JOIN pg_class rel ON rel.oid = con.conrelid "
            "WHERE rel.relname = :table AND con.contype = 'c' "
            "AND pg_get_constraintdef(con.oid) LIKE :pattern"
        ),
        {"table": table, "pattern": f"%{column_substring}%"},
    ).first()
    return row[0] if row else None


def upgrade() -> None:
    bind = op.get_bind()

    # --- B3-7: products.kind (нужен раньше, чем связываем product_id ниже) ---
    op.add_column(
        "products",
        sa.Column("kind", sa.String(16), nullable=False, server_default="course"),
    )
    op.create_check_constraint("products_kind_valid", "products", "kind IN ('course','software')")
    op.execute("UPDATE products SET kind = 'software' WHERE vendor_id IS NOT NULL")

    # --- B3-1: новые колонки organization_licenses ---
    op.add_column(
        "organization_licenses",
        sa.Column("vendor_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "organization_licenses",
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "organization_licenses",
        sa.Column("vendor_key", sa.String(255), nullable=False, server_default=""),
    )
    op.add_column(
        "organization_licenses",
        sa.Column("product_key", sa.String(255), nullable=False, server_default=""),
    )
    op.add_column(
        "organization_licenses",
        sa.Column("contract_key", sa.String(128), nullable=False, server_default=""),
    )
    op.add_column("organization_licenses", sa.Column("is_signed", sa.Boolean(), nullable=True))
    op.add_column(
        "organization_licenses", sa.Column("license_term_years", sa.SmallInteger(), nullable=True)
    )
    op.add_column(
        "organization_licenses", sa.Column("license_valid_until", sa.Date(), nullable=True)
    )
    op.add_column(
        "organization_licenses",
        sa.Column("is_perpetual", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.alter_column("organization_licenses", "contract_number", nullable=True)

    op.create_foreign_key(
        "fk_organization_licenses_vendor_id_organizations",
        "organization_licenses",
        "organizations",
        ["vendor_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_organization_licenses_product_id_products",
        "organization_licenses",
        "products",
        ["product_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # --- Backfill ключей для существующих строк + разведение дублей ---
    rows = bind.execute(
        sa.text(
            "SELECT id, organization_id, vendor, product_name, contract_number "
            "FROM organization_licenses WHERE deleted_at IS NULL ORDER BY created_at, id"
        )
    ).fetchall()

    seen: dict[tuple[object, str, str, str], int] = {}
    updates: list[dict[str, object]] = []
    for row in rows:
        vendor_key = _company_key(row.vendor)
        product_key = _company_key(row.product_name)
        contract_key = _contract_key(row.contract_number)
        base = (row.organization_id, vendor_key, product_key, contract_key)
        count = seen.get(base, 0)
        seen[base] = count + 1
        if count > 0:
            # Дубль ключа в уже сохранённых данных (например, старый импорт
            # до B3-1 слил бы их — после миграции они различимы): данные не
            # трогаем, разводим только служебный contract_key суффиксом.
            contract_key = f"{contract_key}~{count + 1}"
        updates.append(
            {
                "id": row.id,
                "vendor_key": vendor_key,
                "product_key": product_key,
                "contract_key": contract_key,
            }
        )

    for item in updates:
        bind.execute(
            sa.text(
                "UPDATE organization_licenses SET vendor_key = :vendor_key, "
                "product_key = :product_key, contract_key = :contract_key WHERE id = :id"
            ),
            item,
        )

    # --- Привязка к каталогу: только однозначные совпадения ---
    if updates:
        companies = bind.execute(
            sa.text("SELECT id, name, short_name FROM organizations WHERE org_type = 'company'")
        ).fetchall()
        vendor_by_key: dict[str, list[object]] = {}
        for company in companies:
            for label in (company.name, company.short_name):
                key = _company_key(label)
                if key:
                    vendor_by_key.setdefault(key, [])
                    if company.id not in vendor_by_key[key]:
                        vendor_by_key[key].append(company.id)

        software = bind.execute(
            sa.text("SELECT id, name FROM products WHERE kind = 'software'")
        ).fetchall()
        product_by_key: dict[str, list[object]] = {}
        for product in software:
            key = _company_key(product.name)
            if key:
                product_by_key.setdefault(key, []).append(product.id)

        license_rows = bind.execute(
            sa.text("SELECT id, vendor_key, product_key FROM organization_licenses")
        ).fetchall()
        for lic in license_rows:
            vendor_candidates = vendor_by_key.get(lic.vendor_key, [])
            product_candidates = product_by_key.get(lic.product_key, [])
            vendor_id = vendor_candidates[0] if len(vendor_candidates) == 1 else None
            product_id = product_candidates[0] if len(product_candidates) == 1 else None
            if vendor_id is not None or product_id is not None:
                bind.execute(
                    sa.text(
                        "UPDATE organization_licenses SET vendor_id = :vendor_id, "
                        "product_id = :product_id WHERE id = :id"
                    ),
                    {"vendor_id": vendor_id, "product_id": product_id, "id": lic.id},
                )

    op.create_index("ix_org_licenses_vendor", "organization_licenses", ["vendor_id"])
    op.create_index("ix_org_licenses_product", "organization_licenses", ["product_id"])
    op.create_index(
        "uq_org_licenses_key",
        "organization_licenses",
        ["organization_id", "vendor_key", "product_key", "contract_key"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_org_licenses_key", table_name="organization_licenses")
    op.drop_index("ix_org_licenses_product", table_name="organization_licenses")
    op.drop_index("ix_org_licenses_vendor", table_name="organization_licenses")
    op.drop_constraint(
        "fk_organization_licenses_product_id_products",
        "organization_licenses",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_organization_licenses_vendor_id_organizations",
        "organization_licenses",
        type_="foreignkey",
    )

    # Строки с пустым номером получают служебный номер перед тем, как
    # вернуть NOT NULL — тот же приём, что предлагает задача (откат должен
    # быть рабочим на "грязных" данных, не только на пустой таблице).
    op.execute(
        "UPDATE organization_licenses SET contract_number = 'б/н-' || left(id::text, 8) "
        "WHERE contract_number IS NULL"
    )
    op.alter_column("organization_licenses", "contract_number", nullable=False)

    op.drop_column("organization_licenses", "is_perpetual")
    op.drop_column("organization_licenses", "license_valid_until")
    op.drop_column("organization_licenses", "license_term_years")
    op.drop_column("organization_licenses", "is_signed")
    op.drop_column("organization_licenses", "contract_key")
    op.drop_column("organization_licenses", "product_key")
    op.drop_column("organization_licenses", "vendor_key")
    op.drop_column("organization_licenses", "product_id")
    op.drop_column("organization_licenses", "vendor_id")

    kind_check = _find_check_constraint("products", "kind")
    if kind_check:
        op.execute(f'ALTER TABLE products DROP CONSTRAINT "{kind_check}"')
    op.drop_column("products", "kind")
