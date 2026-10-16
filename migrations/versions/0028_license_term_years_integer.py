"""Приводит organization_licenses.license_term_years к Integer (расходится с моделью)

Побочная находка при прогоне `alembic check` для 0027 (B1-7): миграция 0026
(`0026_license_key_catalog_link.py`, спринт B3-1) создала `license_term_years` как `SMALLINT`, а
модель (`app/modules/catalog/models.py`, `OrganizationLicense.license_term_years`) объявляла поле
без явного типа — `Mapped[int | None]` без `mapped_column(...)` резолвится в `BigInteger` через
`Base.type_annotation_map` (`app/db/base.py:84`), а не в `SmallInteger`. `alembic check` падал на
этом рассинхроне для ЛЮБОЙ следующей миграции в цепочке, не только 0027 — чинится здесь, а не
правкой уже закоммиченной 0026 (та могла быть применена на других дев-копиях; миграции этого
репозитория не редактируются постфактум, см. `0015_organization_licenses.py` про тот же принцип
для CHECK-constraint'ов).

Решение — явный `Integer` в модели (как у соседнего `license_valid_year`, не `BigInteger` по
умолчанию реестра: срок лицензии в годах никогда не подойдёт к границам `BIGINT`) и `ALTER COLUMN
... TYPE integer` здесь: `SMALLINT -> INTEGER` — безопасное расширение диапазона, без потери данных
и без `USING`-преобразования (неявное приведение типов числовых колонок Postgres делает молча).

Revision ID: 0028_license_term_years_integer
Revises: 0027_report_mv_without_deleted
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_license_term_years_integer"
down_revision: str | None = "0027_report_mv_without_deleted"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "organization_licenses",
        "license_term_years",
        existing_type=sa.SmallInteger(),
        type_=sa.Integer(),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "organization_licenses",
        "license_term_years",
        existing_type=sa.Integer(),
        type_=sa.SmallInteger(),
        existing_nullable=True,
    )
