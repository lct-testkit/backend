"""Чинит REFRESH mv_deal_status_summary под crm_app (следствие 0012)

`app/modules/reporting/tasks.py:refresh_report_materialized_views` (cron,
раз в 5 минут) выполняет `REFRESH MATERIALIZED VIEW CONCURRENTLY
mv_deal_status_summary` напрямую. Пока воркер подключался как суперпользователь
`crm`, это работало без вопросов. После 0012 (api/worker реально подключаются
как `crm_app`) задача стала падать: `REFRESH MATERIALIZED VIEW` — не
GRANT-ируемая привилегия в PostgreSQL, её может выполнить только владелец
представления или суперпользователь, а `mv_deal_status_summary` (миграция
0009) принадлежит `crm`. Обнаружено сразу после 0012 живой проверкой (в
логах воркера — `InsufficientPrivilegeError: must be owner of materialized
view mv_deal_status_summary`), не рассуждением заранее.

Решение то же, что и для партиций audit_log в 0012: узкая `SECURITY DEFINER`
функция вместо выдачи `crm_app` владения представлением (владение дало бы
заодно право `DROP`/`ALTER`, а нужен только REFRESH).

Revision ID: 0013_mv_refresh_definer
Revises: 0012_audit_role_hardening
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_mv_refresh_definer"
down_revision: str | None = "0012_audit_role_hardening"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION refresh_mv_deal_status_summary()
        RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
        BEGIN
            REFRESH MATERIALIZED VIEW CONCURRENTLY mv_deal_status_summary;
        END;
        $$
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS refresh_mv_deal_status_summary()")
