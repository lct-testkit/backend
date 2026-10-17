"""Шаблоны отчётов: восемь из раздела 4.13 и выгрузка учащихся для LMS
(`python -m app.modules.reporting.seed`).

Идемпотентно по `code` — тот же приём, что `notification.seed`/`workflow.seed`.
`query_def.kind` — ключ в `reporting.builders.REPORT_BUILDERS`; без строки в
этой таблице соответствующий builder не имеет шаблона и недостижим через API
(`ReportJobService.create` резолвит `template_code`, не `kind` напрямую).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.logging import configure_logging

# Не используется напрямую (файлов у демо-заданий нет, `file_id` остаётся
# NULL) — импорт нужен только для побочного эффекта: `report_jobs.file_id`
# ссылается на `files.id`, и при запуске этого модуля отдельным процессом
# (`python -m app.modules.reporting.seed`, как это делает `entrypoint.sh`)
# SQLAlchemy сортирует таблицы по FK перед flush и падает с
# `NoReferencedTableError`, если модуль `files.models` ни разу не был
# импортирован — его таблица тогда просто не зарегистрирована в
# `Base.metadata`. Внутри работающего приложения этого не видно: `files.
# models` уже импортирован транзитивно через `reporting.service`.
from app.modules.files import models as _files_models  # noqa: F401
from app.modules.identity.models import Role, User
from app.modules.reporting.models import ReportJob, ReportJobStatus, ReportTemplate

logger = structlog.get_logger(__name__)

# (code, name, description, kind, allowed_roles, default_params, output_formats)
_DEFAULT_TEMPLATES: list[tuple[str, str, str, str, list[str], dict[str, Any], list[str]]] = [
    (
        "deal_funnel",
        "Воронка по статусам",
        "Конверсия и среднее время между шагами воронки.",
        "deal_funnel",
        [],
        {"deal_type": "b2b"},
        ["xlsx", "xls", "pdf", "png", "json"],
    ),
    (
        "kam_summary",
        "Сводка по КАМам",
        "Сравнение КАМов: сделки в работе/выиграно/проиграно, сумма, средний цикл.",
        "kam_summary",
        ["HEAD", "ADMIN"],
        {},
        ["xlsx", "xls", "pdf", "json"],
    ),
    (
        "region_summary",
        "По регионам",
        "Сделки и выигранная сумма в разрезе регионов организаций.",
        "region_summary",
        [],
        {},
        ["xlsx", "xls", "pdf", "json"],
    ),
    (
        "loss_reasons",
        "Причины отказов",
        "Распределение проигранных сделок по справочнику причин.",
        "loss_reasons",
        [],
        {},
        ["xlsx", "xls", "pdf", "json"],
    ),
    (
        "sla_compliance",
        "Соблюдение SLA",
        "Доля сделок в норме/под угрозой/с нарушением SLA.",
        "sla_compliance",
        [],
        {},
        ["xlsx", "xls", "pdf", "json"],
    ),
    (
        "monthly_dynamics",
        "Динамика по месяцам",
        "Создано/выиграно/проиграно сделок и выигранная сумма по месяцам.",
        "monthly_dynamics",
        [],
        {"months": 12},
        ["xlsx", "xls", "pdf", "png", "json"],
    ),
    (
        "stuck_deals",
        "Зависшие сделки",
        "Сделки с нарушенным SLA — листинг, не агрегат.",
        "stuck_deals",
        [],
        {"limit": 500},
        ["xlsx", "xls", "pdf", "json"],
    ),
    (
        "deal_register",
        "Сделки по вузам и направлениям",
        "Реестр сделок за период в разрезе вуза, направления, продукта, статуса и ответственного. "
        "Строка на пару сделка-продукт (у сделки без продуктов — одна строка с пустым продуктом и "
        "направлением). Параметры: date_from/date_to, organization_ids, direction_ids, "
        "product_ids, owner_ids, columns, date_field, limit.",
        "deal_register",
        [],
        {"limit": 1000},
        ["xlsx", "xls", "pdf", "json"],
    ),
    (
        "learning_progress",
        "Прогресс обучения",
        "Прогресс учащихся по данным LMS: процент прохождения курса, оценка, даты зачисления и "
        "завершения, последняя активность, поток. Пусто и без пометки «не реализована», если "
        "источник lms не присылал данных. Параметры: date_from/date_to, organization_ids, "
        "direction_ids, product_ids, owner_ids, columns, date_field, limit.",
        "learning_progress",
        [],
        {"limit": 1000},
        ["xlsx", "xls", "pdf", "json"],
    ),
    (
        "lms_users_upload",
        "Загрузка пользователей в LMS",
        "Учащиеся оплаченных сделок физлиц в формате шаблона LMS «Загрузка пользователей»: "
        "данные контакта и профиль учащегося. Параметры: product_id, stream_number, "
        "status_codes, date_from/date_to. Только xlsx; содержит ПДн.",
        "lms_users_upload",
        ["HEAD", "ADMIN"],
        {"status_codes": ["payment_contract", "lms_enrollment"]},
        ["xlsx"],
    ),
    (
        "product_demand",
        "Рейтинг востребованности ИТ-продуктов и направлений",
        "Ранжирование продуктов (или направлений) по заявкам на обучение, числу обучающихся и "
        "параллельных потоков — индекс как среднее доле каждого показателя в выборке. "
        "Параметры: level (product/direction), top (1-100), sort_by (index/applications/"
        "learners/streams), date_from/date_to, organization_ids, direction_ids, product_ids, "
        "owner_ids.",
        "product_demand",
        [],
        {"level": "product", "top": 20, "sort_by": "index"},
        # B1-8 (график "demand") не сделан — без png, пока график не зарегистрирован.
        ["xlsx", "xls", "pdf", "json"],
    ),
    (
        "license_register",
        "Реестр лицензий",
        "Лицензии/договоры вуз-вендор-ПО: вуз, вендор, ПО, номер договора, подписание и срок "
        "действия лицензии, статус по передаче, ФИО менеджера, ответственные от вуза, "
        "комментарий. ФИО маскируются без права contact:reveal. Параметры: date_from/date_to "
        "(по дате подписания), organization_ids, vendors, transfer_statuses, valid_year, "
        "expired, columns, date_field, limit.",
        "license_register",
        [],
        {"limit": 1000},
        # B1-8 (график "license_status") не сделан — без png, пока график не зарегистрирован.
        ["xlsx", "xls", "pdf", "json"],
    ),
]


async def seed_report_templates(session: AsyncSession) -> int:
    created = 0
    for (
        code,
        name,
        description,
        kind,
        allowed_roles,
        default_params,
        output_formats,
    ) in _DEFAULT_TEMPLATES:
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


# =============================================================================
# П5 (frontend/docs/backend-issues.md #29, репозиторий lct-testkit/frontend):
# «entrypoint.sh seed засевает только воронки» — `GET /api/reports` (галерея «Мои отчёты»)
# пуста на свежей базе даже после того, как шаблоны заведены. Несколько уже "завершённых" заданий
# — не файлы (реальный xlsx/pdf сиду заводить незачем, раздел 4.13 и так не
# требует хранить их вечно — ретеншен 7 дней), просто строки истории, чтобы
# список не выглядел пустым сразу после разворачивания.
# =============================================================================

_DEMO_REPORT_JOBS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("sla_compliance", "xlsx", {}),
    ("kam_summary", "pdf", {}),
    ("region_summary", "xlsx", {}),
)

# Отдельная системная учётка — тот же приём, что `integration.seed.
# seed_integration_account` уже применяет для role=INTEGRATION: этому сиду
# нужен владелец задания (`report_jobs.requested_by` — реальный FK, NOT
# NULL), а на свежей базе, до первого входа через Keycloak, пользователей
# может не быть вовсе. `keycloak_id=None` — эта учётка не логинится, только
# владеет демо-строками истории.
_DEMO_REQUESTER_EMAIL = "demo-reports@system.local"


async def _demo_requester(session: AsyncSession) -> uuid.UUID:
    existing_admin = await session.scalar(
        select(User.id).where(User.role == Role.ADMIN.value).order_by(User.created_at).limit(1)
    )
    if existing_admin is not None:
        return existing_admin

    existing_demo = await session.scalar(select(User.id).where(User.email == _DEMO_REQUESTER_EMAIL))
    if existing_demo is not None:
        return existing_demo

    user = User(
        keycloak_id=None,
        email=_DEMO_REQUESTER_EMAIL,
        full_name="Демо-отчёты (сид)",
        role=Role.ADMIN.value,
        status="active",
    )
    session.add(user)
    await session.flush()
    return user.id


async def seed_demo_report_history(session: AsyncSession) -> int:
    """Идемпотентно по маркеру `params._seed == "demo"` — второй прогон на
    заполненной базе ничего не добавляет, тем же приёмом, что
    `seed_report_templates` проверяет `code`."""
    created = 0
    requester_id: uuid.UUID | None = None
    for template_code, fmt, extra_params in _DEMO_REPORT_JOBS:
        existing = await session.scalar(
            select(ReportJob.id).where(
                ReportJob.template_code == template_code,
                ReportJob.format == fmt,
                ReportJob.params["_seed"].astext == "demo",
            )
        )
        if existing is not None:
            continue
        if requester_id is None:
            requester_id = await _demo_requester(session)
        now = dt.datetime.now(dt.UTC)
        settings = get_settings()
        session.add(
            ReportJob(
                template_code=template_code,
                params={**extra_params, "_seed": "demo"},
                format=fmt,
                requested_by=requester_id,
                status=ReportJobStatus.COMPLETED.value,
                row_count=0,
                finished_at=now,
                expires_at=now + dt.timedelta(days=settings.reports_retention_days),
            )
        )
        created += 1
    if created:
        await session.flush()
    logger.info("demo_report_history_seeded", created=created, total=len(_DEMO_REPORT_JOBS))
    return created


async def _main() -> None:
    async with session_scope() as session:
        await seed_report_templates(session)
        await seed_demo_report_history(session)


def main() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    asyncio.run(_main())


if __name__ == "__main__":
    main()
