"""Шаблоны отчётов: `_DEFAULT_TEMPLATES` — восемь видов из раздела 4.13, выгрузка учащихся для
LMS, `deal_register`, три вида спринта B1 (`product_demand`, B1-9; `learning_progress`,
B1-10 — настоящая реализация вместо честной заглушки; `license_register`, B1-11) и
`signing_periods` (B7-9) (`python -m app.modules.reporting.seed`).

Идемпотентно по `code` — тот же приём, что `notification.seed`/`workflow.seed`.
`query_def.kind` — ключ в `reporting.builders.REPORT_BUILDERS`; без строки в
этой таблице соответствующий builder не имеет шаблона и недостижим через API
(`ReportJobService.create` резолвит `template_code`, не `kind` напрямую). Число видов —
`len(_DEFAULT_TEMPLATES)` (тест `TestSeedTemplates.test_one_template_per_builder`,
`tests/test_reporting.py`), не повторяется здесь как отдельная цифра, чтобы не расходиться.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import structlog
from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult, Result
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.logging import configure_logging

# Не используется напрямую (из той же причины, что и раньше: `report_jobs.file_id` ссылается на
# `files.id`, и при запуске этого модуля отдельным процессом (`python -m app.modules.reporting.
# seed`, как делает `entrypoint.sh`) SQLAlchemy сортирует таблицы по FK перед flush и падает с
# `NoReferencedTableError`, если модуль `files.models` ни разу не был импортирован — его таблица
# тогда просто не зарегистрирована в `Base.metadata`. Внутри работающего приложения этого не
# видно: `files.models` уже импортирован транзитивно через `reporting.service`.
from app.modules.files import models as _files_models  # noqa: F401
from app.modules.identity.models import User, UserStatus
from app.modules.integration.seed import seed_integration_account
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
        # B1-8: график корзин прогресса зарегистрирован (`ReportSpec.chart="progress"`).
        ["xlsx", "xls", "pdf", "png", "json"],
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
        # B1-8: график трёх панелей зарегистрирован (`ReportSpec.chart="demand"`).
        ["xlsx", "xls", "pdf", "png", "json"],
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
        # B1-8: график двух панелей зарегистрирован (`ReportSpec.chart="license_status"`).
        ["xlsx", "xls", "pdf", "png", "json"],
    ),
    (
        "signing_periods",
        "Подписание по периодам",
        "Документы на подпись (signature_documents) помесячно: число документов, доля "
        "подписанных, истекшие и отклонённые, медиана часов от первой отправки подписанту до "
        "завершения (только у подписанных). Без ПДн — только счётчики и доли. Операционный "
        "отчёт по всей подсистеме подписания, не по чьим-то личным сделкам. "
        "Параметры: months (по умолчанию 12), doc_type, date_from/date_to.",
        "signing_periods",
        ["HEAD", "ADMIN", "AUDITOR"],
        {"months": 12},
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
# пуста на свежей базе даже после того, как шаблоны заведены.
# =============================================================================
#
# B1-14: прежняя версия этой функции заводила три записи `completed` БЕЗ файла (`file_id IS
# NULL`, `row_count=0`) — "Мои отчёты" показывали историю, но кнопка «Скачать» отвечала 410
# `CRM-1603` («срок хранения истёк»), хотя срок и не начинался: файла не было вообще. На
# демонстрации жюри это выглядит как поломка, не как «история есть». Настоящие файлы заводит
# отдельный этап деплоя (`deploy/seed/reports.mjs`, не входит в этот backend-репозиторий —
# вызывает `POST /api/reports` от демо-пользователей по-настоящему, со вставкой в S3 и
# `REPORT_EXPORTED` в аудите); здесь остаётся только миграция ДАННЫХ — убрать то, что эта же
# функция когда-то создала без файла, не трогая ничего, что создал кто-то другой (у
# `report_jobs` нет FK на этот сид, поэтому удаление строго по маркеру `_seed=demo` и
# `file_id IS NULL`, а не “все записи demo”: запись `_seed=demo`, у которой уже есть файл после
# прогона `reports.mjs`, остаётся как есть).


def _rows_affected(result: Result[Any]) -> int:
    """Число строк, затронутых DELETE/UPDATE. `AsyncSession.execute` типизирован как
    `Result[Any]`, а у DML это `CursorResult` с `rowcount`. Те же `result.rowcount or 0` в
    `worker/main.py`, `crm/service.py`, `signing/service.py` обходятся без приведения лишь
    потому, что эти модули в списке `ignore_errors` mypy (`pyproject.toml`); здесь модуль
    проверяется строго."""
    return cast("CursorResult[Any]", result).rowcount or 0


async def seed_demo_report_history(session: AsyncSession) -> int:
    """Идемпотентная уборка, не создание: удаляет пустышки `completed` без файла, заведённые
    прежней версией этой функции (маркер `params._seed == "demo"`). Повторный запуск на базе,
    где пустышек уже нет (убраны этим же вызовом раньше, либо настоящие файлы уже заведены
    `deploy/seed/reports.mjs`), не находит ни одной строки и возвращает 0 — то и есть
    идемпотентность, не отдельная проверка "уже убрано"."""
    stmt = delete(ReportJob).where(
        ReportJob.params["_seed"].astext == "demo",
        ReportJob.file_id.is_(None),
        ReportJob.status == ReportJobStatus.COMPLETED.value,
    )
    removed = _rows_affected(await session.execute(stmt))
    if removed:
        await session.flush()
    logger.info("demo_report_history_stubs_removed", removed=removed)
    return removed


#: I16 (B4-13), шаг 5: e-mail фантома, который прежняя версия `_demo_requester` (до B1-14,
#: см. докстрин выше) заводила без `keycloak_id` на стендах без единого реального ADMIN.
_PHANTOM_ADMIN_EMAIL = "demo-reports@system.local"


async def remediate_phantom_admin(session: AsyncSession) -> int:
    """I16 (B4-13), шаг 5: лечение уже развёрнутых стендов. На чистой базе, где такой
    строки никогда не было, — no-op (возвращает 0). Текущая версия `_demo_requester`
    (B1-14) больше не создаёт фантома, поэтому второй прогон после первого успешного —
    тоже no-op: `report_jobs.requested_by` уже перенесены, менять в них нечего, а
    `status='terminated'` присвоить второй раз безвредно."""
    phantom = await session.scalar(
        select(User).where(
            User.email == _PHANTOM_ADMIN_EMAIL,
            User.keycloak_id.is_(None),
            User.role == "ADMIN",
        )
    )
    if phantom is None:
        return 0
    service_account = await seed_integration_account(session)
    moved = await session.execute(
        update(ReportJob)
        .where(ReportJob.requested_by == phantom.id)
        .values(requested_by=service_account.id)
    )
    phantom.status = UserStatus.TERMINATED.value
    await session.flush()
    count = _rows_affected(moved)
    logger.info(
        "demo_phantom_admin_remediated", phantom_id=str(phantom.id), report_jobs_moved=count
    )
    return count


async def _main() -> None:
    async with session_scope() as session:
        await seed_report_templates(session)
        await seed_demo_report_history(session)
        await remediate_phantom_admin(session)


def main() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    asyncio.run(_main())


if __name__ == "__main__":
    main()
