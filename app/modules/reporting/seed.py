"""Восемь шаблонов отчётов из раздела 4.13 (`python -m app.modules.reporting.seed`).

Идемпотентно по `code` — тот же приём, что `notification.seed`/`workflow.seed`.
`query_def.kind` — ключ в `reporting.builders.REPORT_BUILDERS`; без строки в
этой таблице соответствующий builder не имеет шаблона и недостижим через API
(`ReportJobService.create` резолвит `template_code`, не `kind` напрямую).
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.logging import configure_logging
from app.modules.reporting.models import ReportTemplate

logger = structlog.get_logger(__name__)

# (code, name, description, kind, allowed_roles, default_params, output_formats)
_DEFAULT_TEMPLATES: list[tuple[str, str, str, str, list[str], dict[str, Any], list[str]]] = [
    (
        "deal_funnel", "Воронка по статусам",
        "Конверсия и среднее время между шагами воронки (раздел 4.13).",
        "deal_funnel", [], {"deal_type": "b2b"}, ["xlsx", "pdf", "png"],
    ),
    (
        "kam_summary", "Сводка по КАМам",
        "Сравнение КАМов: сделки в работе/выиграно/проиграно, сумма, средний цикл.",
        "kam_summary", ["HEAD", "ADMIN"], {}, ["xlsx", "pdf"],
    ),
    (
        "region_summary", "По регионам",
        "Сделки и выигранная сумма в разрезе регионов организаций.",
        "region_summary", [], {}, ["xlsx", "pdf"],
    ),
    (
        "loss_reasons", "Причины отказов",
        "Распределение проигранных сделок по справочнику причин.",
        "loss_reasons", [], {}, ["xlsx", "pdf"],
    ),
    (
        "sla_compliance", "Соблюдение SLA",
        "Доля сделок в норме/под угрозой/с нарушением SLA.",
        "sla_compliance", [], {}, ["xlsx", "pdf"],
    ),
    (
        "monthly_dynamics", "Динамика по месяцам",
        "Создано/выиграно/проиграно сделок и выигранная сумма по месяцам.",
        "monthly_dynamics", [], {"months": 12}, ["xlsx", "pdf", "png"],
    ),
    (
        "stuck_deals", "Зависшие сделки",
        "Сделки с нарушенным SLA — листинг, не агрегат.",
        "stuck_deals", [], {"limit": 500}, ["xlsx", "pdf"],
    ),
    (
        "learning_progress", "Прогресс обучения",
        "Данные из LMS. Интеграция не реализована — отчёт пуст (new_spec §4.14).",
        "learning_progress", [], {}, ["xlsx", "pdf"],
    ),
]


async def seed_report_templates(session: AsyncSession) -> int:
    created = 0
    for code, name, description, kind, allowed_roles, default_params, output_formats in (
        _DEFAULT_TEMPLATES
    ):
        existing = await session.scalar(
            select(ReportTemplate.id).where(ReportTemplate.code == code)
        )
        if existing is not None:
            continue
        session.add(
            ReportTemplate(
                code=code,
                name=name,
                description=description,
                query_def={"kind": kind},
                allowed_roles=allowed_roles,
                default_params=default_params,
                output_formats=output_formats,
                is_active=True,
            )
        )
        created += 1
    if created:
        await session.flush()
    logger.info("report_templates_seeded", created=created, total=len(_DEFAULT_TEMPLATES))
    return created


async def _main() -> None:
    async with session_scope() as session:
        await seed_report_templates(session)


def main() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    asyncio.run(_main())


if __name__ == "__main__":
    main()
