"""Разрешить subject_type='organization' на data_erasure_requests (dop.md §11.8)

Данные ИП (`organizations.org_type='individual_entrepreneur'`) — ПДн физлица,
а не сведения о юрлице (new_spec §7.2 сам заводит этот `org_type`, но до этой
миграции запрос на удаление такого субъекта был структурно невозможен:
`erasure_subject_valid` разрешал только `'user'`/`'contact'`). Компании/вузы
этим субъектом не становятся — `catalog.service.OrganizationService`
на уровне сервиса отказывает в запросе для любого `org_type`, отличного от
`individual_entrepreneur`; на уровне БД для этого нет отдельного
ограничения, потому что «какие именно организации» — не то, что должно
проверяться CHECK-constraint'ом на `data_erasure_requests` (эта таблица не
ссылается на `organizations` по FK, ровно как не ссылается на `users`/
`contacts` — см. докстринг `DataErasureRequest`).

Revision ID: 0014_organization_erasure
Revises: 0013_mv_refresh_definer
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014_organization_erasure"
down_revision: str | None = "0013_mv_refresh_definer"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_CHECK = "subject_type IN ('user','contact')"
_NEW_CHECK = "subject_type IN ('user','contact','organization')"


def upgrade() -> None:
    op.drop_constraint(
        "erasure_subject_valid", "data_erasure_requests", type_="check"
    )
    op.create_check_constraint(
        "erasure_subject_valid", "data_erasure_requests", _NEW_CHECK
    )


def downgrade() -> None:
    op.drop_constraint(
        "erasure_subject_valid", "data_erasure_requests", type_="check"
    )
    op.create_check_constraint(
        "erasure_subject_valid", "data_erasure_requests", _OLD_CHECK
    )
