"""Пересобирает mv_deal_status_summary без мягко удалённых сделок (B1-7, BUG-2)

`backend-sprints.md`, спринт B1, задача B1-7. Воронка по статусам идёт двумя путями:
ADMIN без фильтров читает `mv_deal_status_summary` (быстрый путь, обновляется раз в 5 минут,
`reporting.tasks.refresh_report_materialized_views`), все остальные случаи (любой фильтр, роль
KAM/HEAD) считаются построчно (`reporting.builders.build_deal_funnel`). Представление (0009) и
построчный путь расходились в одном: подзапрос `cur` (сколько сделок сейчас в статусе) уже
фильтровал `deleted_at IS NULL`, а подзапрос `hist` (сколько всего прошло через статус, среднее
время) — нет. Сделку можно удалить мягко откатом импорта оплат
(`app/modules/imports/handlers.py`), после чего колонка «Всего прошло» у ADMIN без фильтров
расходилась с тем же отчётом с любым фильтром (который всегда шёл построчно и `deleted_at`
учитывал). Паритет закрепляется тестом (`tests/test_reporting_numbers.py`), который сравнивает
оба пути друг с другом на одних данных.

Заодно (B1-7, п.2 задачи) представление получает колонку `reached_count` — число различных сделок,
у которых наибольший `sort_order` среди статусов типа `initial`/`intermediate`/`won` не меньше
`sort_order` данного шага («дошли до шага N и дальше»). Конверсия шага у builder'а теперь считается
как `reached(i) / reached(i-1)`, а не `entered(i) / entered(i-1)` — это не может дать больше 100%
при пропуске необязательного шага (раньше могло: `entered` считает КАЖДОЕ попадание в статус, а не
«дошла ли сделка до него»). Построчный путь вычисляет тот же `reached_count` отдельным запросом
(`builders._reached_counts`), формула идентична — паритет-тест проверяет именно это.

`refreshed_at` — момент построения представления (`now()` на момент `REFRESH`), используется как
`ReportDataset.data_as_of` (B1-7/B1-6): «Данные на HH:MM (обновляются раз в 5 минут)» в шапке файла.

`DROP`/`CREATE MATERIALIZED VIEW` держит эксклюзивную блокировку на время пересборки (на базе в
сотни тысяч переходов — доли секунды, не секунды: план содержит только агрегаты по индексированным
FK). `refresh_mv_deal_status_summary()` (0013) обращается к представлению по имени и продолжает
работать без изменений после пересоздания. Downgrade возвращает определение из 0009 (без
`reached_count`/`refreshed_at`) — код после `alembic downgrade -1` читает эти колонки через билдер
новой версии кода, поэтому откат миграции предполагается вместе с откатом коммита B1-7.

Revision ID: 0027_report_mv_without_deleted
Revises: 0026_license_key_catalog_link
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0027_report_mv_without_deleted"
down_revision: str | None = "0026_license_key_catalog_link"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APP_ROLE = "crm_app"


def upgrade() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS mv_deal_status_summary")
    op.execute(
        """
        CREATE MATERIALIZED VIEW mv_deal_status_summary AS
        WITH per_deal AS (
            SELECT h.deal_id, ws.workflow_id, max(ws.sort_order) AS max_order
            FROM deal_status_history h
            JOIN deals d ON d.id = h.deal_id AND d.deleted_at IS NULL
            JOIN workflow_statuses ws ON ws.id = h.to_status_id
                 AND ws.type IN ('initial', 'intermediate', 'won')
            GROUP BY h.deal_id, ws.workflow_id
        )
        SELECT
            ws.id AS status_id,
            ws.workflow_id AS workflow_id,
            COALESCE(cur.currently_in_count, 0) AS currently_in_count,
            COALESCE(hist.entered_count, 0) AS entered_count,
            hist.avg_duration_in_prev AS avg_duration_in_prev,
            CASE WHEN ws.type IN ('initial', 'intermediate', 'won') THEN
                (SELECT count(*) FROM per_deal p
                 WHERE p.workflow_id = ws.workflow_id AND p.max_order >= ws.sort_order)
            END AS reached_count,
            now() AS refreshed_at
        FROM workflow_statuses ws
        LEFT JOIN (
            SELECT status_id, count(*) AS currently_in_count
            FROM deals
            WHERE deleted_at IS NULL
            GROUP BY status_id
        ) cur ON cur.status_id = ws.id
        LEFT JOIN (
            SELECT
                h.to_status_id,
                count(DISTINCT h.deal_id) AS entered_count,
                avg(h.duration_in_prev) AS avg_duration_in_prev
            FROM deal_status_history h
            JOIN deals d ON d.id = h.deal_id AND d.deleted_at IS NULL
            GROUP BY h.to_status_id
        ) hist ON hist.to_status_id = ws.id
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX ix_mv_deal_status_summary_status ON mv_deal_status_summary (status_id)"
    )
    # Явный GRANT вдобавок к `ALTER DEFAULT PRIVILEGES` (0002): `DROP`/`CREATE` создаёт новый
    # объект, страховка на случай, если дефолтные привилегии на материализованные представления
    # на конкретной версии Postgres/окружении не применились молча.
    op.execute(f"GRANT SELECT ON mv_deal_status_summary TO {_APP_ROLE}")


def downgrade() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS mv_deal_status_summary")
    op.execute(
        """
        CREATE MATERIALIZED VIEW mv_deal_status_summary AS
        SELECT
            ws.id AS status_id,
            ws.workflow_id AS workflow_id,
            COALESCE(cur.currently_in_count, 0) AS currently_in_count,
            COALESCE(hist.entered_count, 0) AS entered_count,
            hist.avg_duration_in_prev AS avg_duration_in_prev
        FROM workflow_statuses ws
        LEFT JOIN (
            SELECT status_id, count(*) AS currently_in_count
            FROM deals
            WHERE deleted_at IS NULL
            GROUP BY status_id
        ) cur ON cur.status_id = ws.id
        LEFT JOIN (
            SELECT
                to_status_id,
                count(DISTINCT deal_id) AS entered_count,
                avg(duration_in_prev) AS avg_duration_in_prev
            FROM deal_status_history
            GROUP BY to_status_id
        ) hist ON hist.to_status_id = ws.id
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX ix_mv_deal_status_summary_status ON mv_deal_status_summary (status_id)"
    )
    op.execute(f"GRANT SELECT ON mv_deal_status_summary TO {_APP_ROLE}")
