"""Агрессивный autovacuum на audit_chain_head — синглтон под каждым локом цепочки

Живой прогон нагрузочного теста 28.09 (после переноса `audit.record()` в конец
транзакции, см. 1317bce): `transition` уложился в SLO (p95 150мс), но `comment`,
запущенный ВТОРЫМ сразу следом, — нет (p95 840мс), хотя код `CommentService.create`
и `DealService.transition` ведут себя одинаково по отношению к локу. Разворот
порядка сценариев (comment первым, transition вторым) подтвердил: ломается тот,
кто идёт вторым, вне зависимости от того, какая это ручка — совпадает с гипотезой
про саму `audit_chain_head`, а не про код комментариев или переходов.

`audit_chain_head` (0023) — ОДНА строка (constraint `singleton`), которую обновляет
КАЖДАЯ запись аудита в системе — под нагрузкой 50 RPS это ~50 UPDATE/с в одну и ту же
строку. HOT-обновления (99.99% по `pg_stat_user_tables.n_tup_hot_upd` на проде)
держат мёртвые версии в той же паре страниц, не раздувая таблицу физически, но каждое
следующее чтение головы цепочки под локом всё равно идёт через MVCC-видимость этой
кучи версий, пока не пройдёт autovacuum. Дефолтные пороги autovacuum считаются как
`threshold + scale_factor × reltuples` — для однострочной таблицы это фактически
голый `threshold` (не 20% от миллиона, а 20% от 1), должно быть агрессивным само по
себе, но `reltuples` в `pg_class` берётся из последнего ANALYZE/VACUUM: до первого
прогона по этой таблице планировщик может решить, что порог ещё не пройден, а сам
autovacuum daemon (по умолчанию 3 параллельных воркера) в это время занят более
крупными таблицами (`deals`, `deal_comments`, `audit_log`), которые тоже растут под
той же нагрузкой — «голове цепочки» может физически не достаться воркера, пока не
разгрузятся остальные, а её собственные мёртвые версии продолжают копиться.

Фикс — не код, а настройка хранения именно для этой таблицы (безопасно, обратимо,
не трогает данные): порог по факту нулевой (10 мёртвых строк, без доли от размера
таблицы) и приоритет автовакуума выше обычного, чтобы планировщик autovacuum не
откладывал эту таблицу в пользу более крупных соседей.

Revision ID: 0024_audit_chain_head_autovacuum
Revises: 0023_audit_chain_head
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0024_audit_chain_head_autovacuum"
down_revision: str | None = "0023_audit_chain_head"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SETTINGS = (
    "autovacuum_vacuum_scale_factor = 0, "
    "autovacuum_vacuum_threshold = 10, "
    "autovacuum_analyze_scale_factor = 0, "
    "autovacuum_analyze_threshold = 10, "
    "autovacuum_vacuum_cost_delay = 0"
)


def upgrade() -> None:
    op.execute(f"ALTER TABLE audit_chain_head SET ({_SETTINGS})")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE audit_chain_head RESET ("
        "autovacuum_vacuum_scale_factor, autovacuum_vacuum_threshold, "
        "autovacuum_analyze_scale_factor, autovacuum_analyze_threshold, "
        "autovacuum_vacuum_cost_delay)"
    )
