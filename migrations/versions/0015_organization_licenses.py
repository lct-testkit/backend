"""П3 (rtk_requiriments.md разд. 4, Треб.1): лицензии/договоры вуз↔вендор↔ПО

Новая таблица `organization_licenses` — каталог из 10 полей кейса (Название
ВУЗа, Вендор, ПО, Номер договора, Подписание лицензии, Срок действия
лицензии (год), Статус по передаче, ФИО Менеджера, Ответственные от ВУЗа,
Комментарий), загружаемый через общий импортёр (`imports.service`,
`entity_type='license'`) — см. докстринг `catalog.models.
OrganizationLicense` про то, почему `product_name`/`manager_full_name`/
`responsible_contacts` текстовые, а не ссылки, и почему natural key импорта
— `contract_number`, а не составной ключ.

Расширяет CHECK `import_jobs.entity_type` третьим значением `'license'` —
только это меняется в уже существующей таблице, остальное — новая таблица,
FK на неё смотрят, ничто существующее не ссылается на неё сегодня.

Revision ID: 0015_organization_licenses
Revises: 0014_organization_erasure
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_organization_licenses"
down_revision: str | None = "0014_organization_erasure"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_ENTITY_TYPE_CHECK = "entity_type IN ('organization','product')"
_NEW_ENTITY_TYPE_CHECK = "entity_type IN ('organization','product','license')"
# Реальное имя constraint'а в БД, как оно легло миграцией 0006, —
# `ck_import_jobs_ck_import_jobs_import_jobs_entity_type_valid`: `op.create_
# table` пропускает уже переданное имя через naming convention ещё раз, а
# автор 0006 передал туда имя, уже включавшее префикс `ck_<table>_` (та же
# история у `ck_products_ck_products_products_format_valid` и других
# constraint'ов из 0005/0006/0009 — задвоение проверено вживую на тестовой
# БД этим агентом, трогать остальные не входит в объём П3). Полагаться на
# этот буквальный литерал в `DROP CONSTRAINT` рискованно — имя ищем
# динамически (`_find_check_constraint`) по таблице и колонке, а не по
# заранее угаданной строке: тогда `upgrade()`/`downgrade()` остаются
# рабочими независимо от того, какое ровно имя constraint носит на текущий
# момент (в том числе после ручного downgrade — см. отчёт по П3, там же
# история находки). Новое имя создаём голым через `op.create_check_
# constraint` (тот же приём, что уже правильно применяет 0014 для
# `erasure_subject_valid`) — naming convention обернёт его ровно один раз,
# итоговое имя в БД совпадёт с тем, что дала бы модель (`imports.models.
# ImportJob`, `name="import_jobs_entity_type_valid"`) при свежем `create_
# all()`, без задвоения.
_CLEAN_CONSTRAINT_NAME = "import_jobs_entity_type_valid"


def _find_check_constraint(table: str, column_substring: str) -> str:
    """Имя CHECK-constraint'а на `table`, чьё условие упоминает
    `column_substring`, по факту в БД — не по тому, что "должно быть" по
    истории миграций (см. комментарий у `_CLEAN_CONSTRAINT_NAME`). Через
    `pg_constraint`/`pg_get_constraintdef`, а не `information_schema.
    table_constraints`: та отдавала задвоенные строки на этой же БД при
    проверке (виден один и тот же constraint дважды) — системный каталог
    Postgres однозначен."""
    bind = op.get_bind()
    name = bind.execute(
        sa.text(
            "SELECT con.conname FROM pg_constraint con "
            "JOIN pg_class rel ON rel.oid = con.conrelid "
            "WHERE rel.relname = :table AND con.contype = 'c' "
            "AND pg_get_constraintdef(con.oid) LIKE :pattern"
        ),
        {"table": table, "pattern": f"%{column_substring}%"},
    ).scalar_one()
    return name


def upgrade() -> None:
    op.create_table(
        "organization_licenses",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vendor", sa.String(255), nullable=False),
        sa.Column("product_name", sa.String(255), nullable=False),
        sa.Column("contract_number", sa.String(128), nullable=False),
        sa.Column("license_signed_at", sa.Date(), nullable=True),
        sa.Column("license_valid_year", sa.Integer(), nullable=True),
        sa.Column("transfer_status", sa.String(24), nullable=True),
        sa.Column("manager_full_name", sa.String(255), nullable=True),
        sa.Column("responsible_contacts", sa.Text(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("import_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
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
        sa.PrimaryKeyConstraint("id", name="pk_organization_licenses"),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_organization_licenses_organization_id_organizations",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["import_job_id"],
            ["import_jobs.id"],
            name="fk_organization_licenses_import_job_id_import_jobs",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "transfer_status IS NULL OR transfer_status IN "
            "('not_started','in_progress','transferred','declined')",
            name="organization_licenses_transfer_status_valid",
        ),
    )
    op.create_index("ix_organization_licenses_created_at", "organization_licenses", ["created_at"])
    op.create_index(
        "ix_organization_licenses_organization", "organization_licenses", ["organization_id"]
    )
    op.create_index(
        "ix_organization_licenses_contract_number",
        "organization_licenses",
        ["contract_number"],
    )

    # `op.drop_constraint(name, ..., type_="check")` пропускает `name` через
    # ту же naming convention (проверено вживую: отдать сюда уже реальное
    # имя — получить попытку удалить несуществующее, ещё раз обёрнутое и
    # обрезанное до 63 байт с хэш-суффиксом). Raw SQL по динамически
    # найденному имени — способ адресовать constraint, который есть в БД
    # прямо сейчас, без повторной обработки и без угадывания истории.
    old_name = _find_check_constraint("import_jobs", "entity_type")
    op.execute(f'ALTER TABLE import_jobs DROP CONSTRAINT "{old_name}"')
    op.create_check_constraint(_CLEAN_CONSTRAINT_NAME, "import_jobs", _NEW_ENTITY_TYPE_CHECK)


def downgrade() -> None:
    old_name = _find_check_constraint("import_jobs", "entity_type")
    op.execute(f'ALTER TABLE import_jobs DROP CONSTRAINT "{old_name}"')
    op.create_check_constraint(_CLEAN_CONSTRAINT_NAME, "import_jobs", _OLD_ENTITY_TYPE_CHECK)

    op.drop_table("organization_licenses")
