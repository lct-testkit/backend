"""Построители наборов данных отчётов (new_spec §4.13).

Восемь видов — ровно то, что перечисляет раздел 4.13: «воронка по статусам
(конверсия и время между шагами), сводка по КАМам, по регионам/вузам,
причины отказов, соблюдение SLA, динамика по месяцам, прогресс обучения (из
LMS), «зависшие сделки»».

Каждый builder получает открытую сессию, принципала и параметры и возвращает
`ReportDataset` — плоскую таблицу, которую `reporting.rendering` превращает в
xlsx/pdf/png. Скоуп строк — тот же `deal_scope_clause`, что уже применяет
`crm.service.DealService.list_query` (раздел 3.2/4.13: «отчёт не выгружает
то, что не видит запрашивающий») — КАМ получает тот же отчёт, что и HEAD, но
данные внутри ограничены его же сделками.

`REPORT_ESTIMATORS` — необязательная быстрая `COUNT`-оценка числа строк ДО
построения полного датасета; нужна только `reporting.service.ReportJobService`
для решения sync/async (раздел 4.13: «синхронно отдаём только лёгкие отчёты»,
<1000 строк). Для агрегатных видов оценки нет: число строк структурно мало
(статусы/регионы/КАМы/месяцы/причины отказов — заведомо не больше нескольких
десятков), поэтому они всегда идут по лёгкому пути. Только `stuck_deals` —
листинг по отдельным сделкам, где строк может быть много — имеет настоящую
оценку.

П1 (rtk_requiriments.md разд. 4, ФТ.1/ФТ.4): «фильтрация... за выбранный
период по выбранным вузам, ИТ-направлениям, ИТ-продуктам, ответственным».
`ReportFilters`/`_parse_report_filters` разбирают шесть общих ключей
`params` — `date_from`, `date_to`, `organization_ids`, `direction_ids`,
`product_ids`, `owner_ids`, все опциональны (отсутствие = без фильтра,
старые вызовы без этих ключей ведут себя как раньше). `_apply_deal_filters`/
`_apply_entity_filters` — общие хелперы применения, каждый builder сам решает,
по какой дате считать «период» (по умолчанию `Deal.created_at`) и какие из
пяти фильтров ему структурно осмысленны — решение описано в комментарии
рядом с телом builder'а. `learning_progress` не участвует (честная заглушка,
данных нет структурно).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import FieldError, ValidationError
from app.core.permissions import DealScope, deal_scope_for
from app.core.security import Principal
from app.modules.catalog.models import LossReason, Organization, Product, Region
from app.modules.crm.models import Deal, DealProduct, DealStatusHistory
from app.modules.crm.service import deal_scope_clause
from app.modules.identity.models import User
from app.modules.workflow.models import TERMINAL_TYPES, StatusType, Workflow, WorkflowStatus


@dataclass(slots=True)
class ReportDataset:
    title: str
    columns: list[str]
    rows: list[list[Any]]
    note: str | None = None
    generated_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))


ReportBuilder = Callable[[AsyncSession, Principal, dict[str, Any]], Awaitable[ReportDataset]]
ReportEstimator = Callable[[AsyncSession, Principal, dict[str, Any]], Awaitable[int]]


def _parse_uuid(params: dict[str, Any], key: str) -> uuid.UUID | None:
    raw = params.get(key)
    if raw is None or raw == "":
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError as exc:
        raise ValidationError(
            f"Параметр {key!r} должен быть UUID",
            [FieldError(field=key, reason="некорректный UUID")],
        ) from exc


def _parse_int(
    params: dict[str, Any], key: str, default: int, *, minimum: int, maximum: int
) -> int:
    raw = params.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"Параметр {key!r} должен быть целым числом",
            [FieldError(field=key, reason="ожидается integer")],
        ) from exc
    return max(minimum, min(maximum, value))


def _parse_date(params: dict[str, Any], key: str) -> dt.date | None:
    raw = params.get(key)
    if raw is None or raw == "":
        return None
    if isinstance(raw, dt.datetime):
        return raw.date()
    if isinstance(raw, dt.date):
        return raw
    try:
        return dt.date.fromisoformat(str(raw))
    except ValueError as exc:
        raise ValidationError(
            f"Параметр {key!r} должен быть датой в формате YYYY-MM-DD",
            [FieldError(field=key, reason="ожидается дата ISO-8601")],
        ) from exc


def _parse_date_range(params: dict[str, Any]) -> tuple[dt.date | None, dt.date | None]:
    """Раздел 4/ФТ.1: «фильтрация... за выбранный период». Оба конца
    включительны — `date_to` расширяется до конца дня при применении к
    timestamptz-колонке (см. `_apply_deal_filters`/`_day_start`)."""
    date_from = _parse_date(params, "date_from")
    date_to = _parse_date(params, "date_to")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ValidationError(
            "Параметр 'date_from' не может быть позже 'date_to'",
            [FieldError(field="date_from", reason="date_from позже date_to")],
        )
    return date_from, date_to


def _parse_uuid_list(params: dict[str, Any], key: str) -> list[uuid.UUID] | None:
    """Список ID фильтра (организации/направления/продукты/ответственные —
    раздел 4/ФТ.1 и ФТ.4). Принимает JSON-массив (обычный путь: `params`
    приходит из тела `POST /reports`) и, на всякий случай, CSV-строку —
    тем же приёмом прощения формата, что уже применяет `validate_field`
    для чисел с запятой вместо точки."""
    raw = params.get(key)
    if raw is None or raw == "" or raw == []:
        return None
    if isinstance(raw, str):
        items: list[Any] = [item.strip() for item in raw.split(",") if item.strip()]
    elif isinstance(raw, list | tuple):
        items = list(raw)
    else:
        raise ValidationError(
            f"Параметр {key!r} должен быть списком UUID",
            [FieldError(field=key, reason="ожидается массив UUID")],
        )
    if not items:
        return None
    try:
        parsed = [uuid.UUID(str(item)) for item in items]
    except ValueError as exc:
        raise ValidationError(
            f"Параметр {key!r} должен быть списком UUID",
            [FieldError(field=key, reason="некорректный UUID в списке")],
        ) from exc
    return parsed


@dataclass(slots=True)
class ReportFilters:
    """П1 (rtk_requiriments.md разд. 4, ФТ.1/ФТ.4): период + фильтры по
    вузам/направлениям/продуктам/ответственным, общие для builder'ов этого
    модуля. Каждое поле — опционально, отсутствие означает «без фильтра» —
    старые вызовы (например, дашборды, уже сохранившие `config.params` без
    этих ключей) продолжают работать без изменений."""

    date_from: dt.date | None = None
    date_to: dt.date | None = None
    organization_ids: list[uuid.UUID] | None = None
    direction_ids: list[uuid.UUID] | None = None
    product_ids: list[uuid.UUID] | None = None
    owner_ids: list[uuid.UUID] | None = None

    def has_any(self) -> bool:
        return any(
            (
                self.date_from,
                self.date_to,
                self.organization_ids,
                self.direction_ids,
                self.product_ids,
                self.owner_ids,
            )
        )


def _parse_report_filters(params: dict[str, Any]) -> ReportFilters:
    date_from, date_to = _parse_date_range(params)
    return ReportFilters(
        date_from=date_from,
        date_to=date_to,
        organization_ids=_parse_uuid_list(params, "organization_ids"),
        direction_ids=_parse_uuid_list(params, "direction_ids"),
        product_ids=_parse_uuid_list(params, "product_ids"),
        owner_ids=_parse_uuid_list(params, "owner_ids"),
    )


def _day_start(value: dt.date) -> dt.datetime:
    return dt.datetime.combine(value, dt.time.min, tzinfo=dt.UTC)


def _apply_entity_filters(stmt: Any, filters: ReportFilters) -> Any:
    """Организации/направления/продукты/ответственные — без периода. Отдельно
    от `_apply_deal_filters`, потому что `build_monthly_dynamics` применяет
    период к двум разным колонкам (`created_at`/`closed_at`) самостоятельно,
    а сущностные фильтры у обеих серий одни и те же."""
    if filters.organization_ids:
        stmt = stmt.where(Deal.organization_id.in_(filters.organization_ids))
    if filters.owner_ids:
        stmt = stmt.where(Deal.owner_id.in_(filters.owner_ids))
    if filters.product_ids:
        stmt = stmt.where(
            Deal.id.in_(
                select(DealProduct.deal_id).where(
                    DealProduct.product_id.in_(filters.product_ids)
                )
            )
        )
    if filters.direction_ids:
        stmt = stmt.where(
            Deal.id.in_(
                select(DealProduct.deal_id)
                .join(Product, Product.id == DealProduct.product_id)
                .where(Product.direction_id.in_(filters.direction_ids))
            )
        )
    return stmt


def _apply_deal_filters(
    stmt: Any, filters: ReportFilters, *, date_column: Any = None
) -> Any:
    """Период + фильтры по сущностям для запросов, у которых `Deal` — базовая
    таблица (`select(...).select_from(Deal)` либо просто `select(Deal...)`).

    `date_column` — по какой дате считать «период»; по умолчанию
    `Deal.created_at` (когда заведена сделка). Отдельные builder'ы передают
    более уместную колонку своим принципалам (например `build_loss_reasons`
    — `Deal.closed_at`, момент, когда причина отказа стала известна) —
    выбор для каждого builder'а описан в его собственном комментарии."""
    column = date_column if date_column is not None else Deal.created_at
    if filters.date_from is not None:
        stmt = stmt.where(column >= _day_start(filters.date_from))
    if filters.date_to is not None:
        stmt = stmt.where(column < _day_start(filters.date_to) + dt.timedelta(days=1))
    return _apply_entity_filters(stmt, filters)


async def _scoped_deal_ids(
    session: AsyncSession, principal: Principal, filters: ReportFilters | None = None
):
    """Подзапрос ID сделок в скоупе принципала — переиспользуется билдерами,
    которым нужно фильтровать `deal_status_history`/другие журналы, у
    которых нет собственного owner_id. `filters`, когда передан, сужает тот
    же набор сделок по периоду/сущностям (раздел 4/ФТ.1) — те же условия,
    что применяются к самой `Deal` в этом же builder'е."""
    clause = await deal_scope_clause(session, principal)
    stmt = select(Deal.id).where(Deal.deleted_at.is_(None))
    if clause is not None:
        stmt = stmt.where(clause)
    if filters is not None:
        stmt = _apply_deal_filters(stmt, filters)
    return stmt


async def _resolve_workflow_id(
    session: AsyncSession, params: dict[str, Any]
) -> uuid.UUID:
    workflow_id = _parse_uuid(params, "workflow_id")
    if workflow_id is not None:
        return workflow_id
    deal_type = str(params.get("deal_type") or "b2b")
    resolved = await session.scalar(
        select(Workflow.id).where(
            Workflow.deal_type == deal_type,
            Workflow.is_default.is_(True),
            Workflow.state == "published",
        )
    )
    if resolved is None:
        raise ValidationError(
            "Не удалось определить воронку: укажите workflow_id явно",
            [FieldError(field="workflow_id", reason="нет опубликованной воронки по умолчанию")],
        )
    return resolved


# =============================================================================
# 1. Воронка по статусам (конверсия + время между шагами)
# =============================================================================


async def build_deal_funnel(
    session: AsyncSession, principal: Principal, params: dict[str, Any]
) -> ReportDataset:
    workflow_id = await _resolve_workflow_id(session, params)
    filters = _parse_report_filters(params)
    statuses = (
        (
            await session.execute(
                select(WorkflowStatus)
                .where(WorkflowStatus.workflow_id == workflow_id)
                .order_by(WorkflowStatus.sort_order)
            )
        )
        .scalars()
        .all()
    )
    columns = [
        "Статус", "Сейчас в статусе", "Всего прошло", "Конверсия шага, %", "Среднее время, дн.",
    ]
    if not statuses:
        return ReportDataset(title="Воронка по статусам", columns=columns, rows=[])

    note: str | None = None
    if deal_scope_for(principal.role) is DealScope.ALL and not filters.has_any():
        # Быстрый путь: материализованное представление (раздел 3.4/4.13),
        # обновляется `reporting.tasks.refresh_report_materialized_views`
        # каждые 5 минут. Скоуп ALL — единственный случай, когда это честно:
        # представление не знает о KAM/HEAD-ограничениях по построению — и,
        # по той же причине, не знает о фильтрах П1 (период/вуз/направление/
        # продукт/ответственный): снимок агрегирован по всем сделкам сразу.
        # Как только хотя бы один фильтр задан, считаем построчно (ветка
        # `else`), даже для ADMIN.
        mv_rows = (
            await session.execute(
                text(
                    "SELECT ws.id, ws.name, ws.sort_order, "
                    "COALESCE(mv.currently_in_count, 0), COALESCE(mv.entered_count, 0), "
                    "mv.avg_duration_in_prev "
                    "FROM workflow_statuses ws "
                    "LEFT JOIN mv_deal_status_summary mv ON mv.status_id = ws.id "
                    "WHERE ws.workflow_id = :workflow_id "
                    "ORDER BY ws.sort_order"
                ),
                {"workflow_id": workflow_id},
            )
        ).all()
        stats = {row[0]: (row[3], row[4], row[5]) for row in mv_rows}
        note = "Источник: материализованное представление (обновляется раз в 5 минут)"
    else:
        clause = await deal_scope_clause(session, principal)
        current_stmt = (
            select(Deal.status_id, func.count(Deal.id))
            .where(Deal.workflow_id == workflow_id, Deal.deleted_at.is_(None))
            .group_by(Deal.status_id)
        )
        if clause is not None:
            current_stmt = current_stmt.where(clause)
        current_stmt = _apply_deal_filters(current_stmt, filters)
        current_rows = (await session.execute(current_stmt)).all()
        current_map = {row[0]: row[1] for row in current_rows}

        status_ids = [s.id for s in statuses]
        hist_rows = (
            await session.execute(
                select(
                    DealStatusHistory.to_status_id,
                    func.count(func.distinct(DealStatusHistory.deal_id)),
                    func.avg(DealStatusHistory.duration_in_prev),
                )
                .where(
                    DealStatusHistory.to_status_id.in_(status_ids),
                    DealStatusHistory.deal_id.in_(
                        await _scoped_deal_ids(session, principal, filters)
                    ),
                )
                .group_by(DealStatusHistory.to_status_id)
            )
        ).all()
        hist_map = {row[0]: (row[1], row[2]) for row in hist_rows}
        stats = {
            s.id: (current_map.get(s.id, 0), *hist_map.get(s.id, (0, None))) for s in statuses
        }

    rows: list[list[Any]] = []
    prev_entered: int | None = None
    for status in statuses:
        currently_in, entered, avg_dur = stats.get(status.id, (0, 0, None))
        entered = entered or 0
        if prev_entered is None:
            conversion = 100.0 if entered else None
        elif prev_entered == 0:
            conversion = None
        else:
            conversion = round(entered / prev_entered * 100, 1)
        avg_days = round(avg_dur.total_seconds() / 86400, 1) if avg_dur is not None else None
        rows.append([status.name, currently_in, entered, conversion, avg_days])
        prev_entered = entered

    return ReportDataset(title="Воронка по статусам", columns=columns, rows=rows, note=note)


# =============================================================================
# 2. Сводка по КАМам
# =============================================================================


async def build_kam_summary(
    session: AsyncSession, principal: Principal, params: dict[str, Any]
) -> ReportDataset:
    filters = _parse_report_filters(params)
    clause = await deal_scope_clause(session, principal)
    name_expr = func.coalesce(User.display_name, User.full_name)
    terminal = [t.value for t in TERMINAL_TYPES]
    stmt = (
        select(
            name_expr,
            func.count(Deal.id).filter(WorkflowStatus.type.notin_(terminal)),
            func.count(Deal.id).filter(WorkflowStatus.type == StatusType.WON.value),
            func.count(Deal.id).filter(WorkflowStatus.type == StatusType.LOST.value),
            func.coalesce(
                func.sum(Deal.amount).filter(WorkflowStatus.type == StatusType.WON.value), 0
            ),
            func.avg(Deal.closed_at - Deal.created_at).filter(Deal.closed_at.is_not(None)),
        )
        .select_from(Deal)
        .join(User, User.id == Deal.owner_id)
        .join(WorkflowStatus, WorkflowStatus.id == Deal.status_id)
        .where(Deal.deleted_at.is_(None))
        .group_by(User.id, name_expr)
        .order_by(name_expr)
    )
    if clause is not None:
        stmt = stmt.where(clause)
    stmt = _apply_deal_filters(stmt, filters)
    result = (await session.execute(stmt)).all()

    rows = [
        [
            name,
            open_count,
            won_count,
            lost_count,
            float(won_amount) if won_amount else 0.0,
            round(avg_cycle.total_seconds() / 86400, 1) if avg_cycle is not None else None,
        ]
        for name, open_count, won_count, lost_count, won_amount, avg_cycle in result
    ]
    return ReportDataset(
        title="Сводка по КАМам",
        columns=["КАМ", "В работе", "Выиграно", "Проиграно", "Сумма выигранных", "Ср. цикл, дн."],
        rows=rows,
    )


# =============================================================================
# 3. По регионам/вузам
# =============================================================================


async def build_region_summary(
    session: AsyncSession, principal: Principal, params: dict[str, Any]
) -> ReportDataset:
    filters = _parse_report_filters(params)
    clause = await deal_scope_clause(session, principal)
    region_name = func.coalesce(Region.name, "Без региона")
    stmt = (
        select(
            region_name,
            func.count(Deal.id),
            func.coalesce(
                func.sum(Deal.amount).filter(WorkflowStatus.type == StatusType.WON.value), 0
            ),
        )
        .select_from(Deal)
        .join(WorkflowStatus, WorkflowStatus.id == Deal.status_id)
        .outerjoin(Organization, Organization.id == Deal.organization_id)
        .outerjoin(Region, Region.id == Organization.region_id)
        .where(Deal.deleted_at.is_(None))
        .group_by(region_name)
        .order_by(func.count(Deal.id).desc())
    )
    if clause is not None:
        stmt = stmt.where(clause)
    stmt = _apply_deal_filters(stmt, filters)
    result = (await session.execute(stmt)).all()
    rows = [[name, count, float(amount) if amount else 0.0] for name, count, amount in result]
    return ReportDataset(
        title="По регионам",
        columns=["Регион", "Сделок", "Сумма выигранных"],
        rows=rows,
    )


# =============================================================================
# 4. Причины отказов
# =============================================================================


async def build_loss_reasons(
    session: AsyncSession, principal: Principal, params: dict[str, Any]
) -> ReportDataset:
    # Период считается по `closed_at` (когда сделка фактически проиграна),
    # а не `created_at`: «причины отказов за период» естественнее читать как
    # «сколько отказов случилось в этом периоде», а не «сколько отказов дали
    # сделки, заведённые в этом периоде» (могли быть заведены сильно раньше).
    filters = _parse_report_filters(params)
    clause = await deal_scope_clause(session, principal)
    total_stmt = select(func.count(Deal.id)).where(
        Deal.deleted_at.is_(None), Deal.loss_reason_id.is_not(None)
    )
    if clause is not None:
        total_stmt = total_stmt.where(clause)
    total_stmt = _apply_deal_filters(total_stmt, filters, date_column=Deal.closed_at)
    total = (await session.execute(total_stmt)).scalar_one() or 0

    stmt = (
        select(LossReason.name, LossReason.category, func.count(Deal.id))
        .select_from(Deal)
        .join(LossReason, LossReason.id == Deal.loss_reason_id)
        .where(Deal.deleted_at.is_(None))
        .group_by(LossReason.id, LossReason.name, LossReason.category)
        .order_by(func.count(Deal.id).desc())
    )
    if clause is not None:
        stmt = stmt.where(clause)
    stmt = _apply_deal_filters(stmt, filters, date_column=Deal.closed_at)
    result = (await session.execute(stmt)).all()
    rows = [
        [name, category, count, round(count / total * 100, 1) if total else 0.0]
        for name, category, count in result
    ]
    return ReportDataset(
        title="Причины отказов",
        columns=["Причина", "Категория", "Сделок", "Доля, %"],
        rows=rows,
    )


# =============================================================================
# 5. Соблюдение SLA
# =============================================================================


async def build_sla_compliance(
    session: AsyncSession, principal: Principal, params: dict[str, Any]
) -> ReportDataset:
    filters = _parse_report_filters(params)
    clause = await deal_scope_clause(session, principal)
    stmt = (
        select(Deal.sla_state, func.count(Deal.id))
        .where(Deal.deleted_at.is_(None))
        .group_by(Deal.sla_state)
    )
    if clause is not None:
        stmt = stmt.where(clause)
    stmt = _apply_deal_filters(stmt, filters)
    result = dict((await session.execute(stmt)).all())
    total = sum(result.values())
    labels = {
        "ok": "В норме", "warning": "Под угрозой", "breached": "Нарушен", "paused": "На паузе",
    }
    rows = []
    for state in ("ok", "warning", "breached", "paused"):
        count = result.get(state, 0)
        pct = round(count / total * 100, 1) if total else 0.0
        rows.append([labels.get(state, state), count, pct])
    return ReportDataset(
        title="Соблюдение SLA", columns=["Состояние", "Сделок", "Доля, %"], rows=rows
    )


# =============================================================================
# 6. Динамика по месяцам
# =============================================================================


async def build_monthly_dynamics(
    session: AsyncSession, principal: Principal, params: dict[str, Any]
) -> ReportDataset:
    months = _parse_int(params, "months", default=12, minimum=1, maximum=36)
    filters = _parse_report_filters(params)
    clause = await deal_scope_clause(session, principal)
    # `date_from`/`date_to` (П1), когда заданы, замещают «месяцев назад»
    # целиком — явный период точнее эвристики по количеству месяцев.
    # Список организаций сюда тоже добавлен (в отличие от примера в задаче
    # «может не подходить структурно») — механически это тот же `.where()`,
    # что у остальных builder'ов, план заведомо не запрещал добавлять его
    # там, где не ломает структуру запроса.
    since = (
        _day_start(filters.date_from)
        if filters.date_from is not None
        else dt.datetime.now(dt.UTC) - dt.timedelta(days=31 * months)
    )
    until = (
        _day_start(filters.date_to) + dt.timedelta(days=1)
        if filters.date_to is not None
        else None
    )

    # `func.date_trunc("month", ...)` строится один раз в переменную и
    # переиспользуется в SELECT и GROUP BY: два отдельных вызова с одним и
    # тем же строковым литералом дают ДВА разных bind-параметра ($1 и $3) —
    # Postgres не может на этапе планирования доказать, что они совпадут в
    # рантайме, и падает `GroupingError`. Живая проверка против настоящего
    # Postgres поймала это сразу (offline/юнит-тесты этого не видят —
    # SQLite-подобной заглушки тут нет, см. docstring `tests/test_reporting.py`).
    created_month = func.date_trunc("month", Deal.created_at)
    created_stmt = (
        select(created_month, func.count(Deal.id))
        .where(Deal.deleted_at.is_(None), Deal.created_at >= since)
        .group_by(created_month)
    )
    if until is not None:
        created_stmt = created_stmt.where(Deal.created_at < until)
    closed_month = func.date_trunc("month", Deal.closed_at)
    closed_stmt = (
        select(
            closed_month,
            func.count(Deal.id).filter(WorkflowStatus.type == StatusType.WON.value),
            func.count(Deal.id).filter(WorkflowStatus.type == StatusType.LOST.value),
            func.coalesce(
                func.sum(Deal.amount).filter(WorkflowStatus.type == StatusType.WON.value), 0
            ),
        )
        .select_from(Deal)
        .join(WorkflowStatus, WorkflowStatus.id == Deal.status_id)
        .where(Deal.deleted_at.is_(None), Deal.closed_at.is_not(None), Deal.closed_at >= since)
        .group_by(closed_month)
    )
    if until is not None:
        closed_stmt = closed_stmt.where(Deal.closed_at < until)
    if clause is not None:
        created_stmt = created_stmt.where(clause)
        closed_stmt = closed_stmt.where(clause)
    created_stmt = _apply_entity_filters(created_stmt, filters)
    closed_stmt = _apply_entity_filters(closed_stmt, filters)

    created_map = {row[0].date(): row[1] for row in (await session.execute(created_stmt)).all()}
    closed_map = {
        row[0].date(): (row[1], row[2], float(row[3]) if row[3] else 0.0)
        for row in (await session.execute(closed_stmt)).all()
    }

    all_months = sorted(set(created_map) | set(closed_map))
    rows = [
        [
            month.isoformat()[:7],
            created_map.get(month, 0),
            *closed_map.get(month, (0, 0, 0.0)),
        ]
        for month in all_months
    ]
    return ReportDataset(
        title="Динамика по месяцам",
        columns=["Месяц", "Создано", "Выиграно", "Проиграно", "Сумма выигранных"],
        rows=rows,
    )


# =============================================================================
# 7. Зависшие сделки
# =============================================================================


async def _stuck_deals_query(session: AsyncSession, principal: Principal, params: dict[str, Any]):
    filters = _parse_report_filters(params)
    clause = await deal_scope_clause(session, principal)
    stmt = (
        select(
            Deal.number,
            Deal.title,
            func.coalesce(User.display_name, User.full_name),
            WorkflowStatus.name,
            Organization.name,
            Deal.sla_due_at,
        )
        .select_from(Deal)
        .join(User, User.id == Deal.owner_id)
        .join(WorkflowStatus, WorkflowStatus.id == Deal.status_id)
        .outerjoin(Organization, Organization.id == Deal.organization_id)
        .where(Deal.deleted_at.is_(None), Deal.sla_state == "breached")
        .order_by(Deal.sla_due_at.asc())
    )
    if clause is not None:
        stmt = stmt.where(clause)
    stmt = _apply_deal_filters(stmt, filters)
    return stmt


async def estimate_stuck_deals(
    session: AsyncSession, principal: Principal, params: dict[str, Any]
) -> int:
    # `select(func.count()).select_from(subquery)`, а не `with_only_columns`:
    # `COUNT(*)` без аргументов не ссылается ни на один столбец исходного
    # запроса, и `with_only_columns` в этом случае не гарантированно сохранит
    # явные JOIN'ы — подзапрос убирает эту двусмысленность полностью.
    inner = await _stuck_deals_query(session, principal, params)
    count_stmt = select(func.count()).select_from(inner.subquery())
    return (await session.execute(count_stmt)).scalar_one() or 0


async def build_stuck_deals(
    session: AsyncSession, principal: Principal, params: dict[str, Any]
) -> ReportDataset:
    limit = _parse_int(params, "limit", default=500, minimum=1, maximum=5000)
    stmt = (await _stuck_deals_query(session, principal, params)).limit(limit)
    result = (await session.execute(stmt)).all()
    now = dt.datetime.now(dt.UTC)
    rows = [
        [
            number,
            title,
            owner_name,
            status_name,
            org_name or "—",
            round((now - sla_due_at).total_seconds() / 86400, 1) if sla_due_at else None,
        ]
        for number, title, owner_name, status_name, org_name, sla_due_at in result
    ]
    return ReportDataset(
        title="Зависшие сделки",
        columns=["Номер", "Название", "Ответственный", "Статус", "Организация", "Дней просрочки"],
        rows=rows,
    )


# =============================================================================
# 8. Прогресс обучения (LMS) — честная заглушка
# =============================================================================


async def build_learning_progress(
    session: AsyncSession, principal: Principal, params: dict[str, Any]
) -> ReportDataset:
    """LMS-интеграция (new_spec §4.14) не реализована ни в одном спринте —
    таблица `learning_progress` не создана, тянуть данные неоткуда. Честная
    пустая выдача с пояснением, а не выдуманные цифры — тот же принцип, что
    `registry.tasks` уже применил к источнику `rosobrnadzor` (спринт 5) и
    `notification.service` — к отсутствующему SMTP/SMS-шлюзу (спринт 6/7)."""
    return ReportDataset(
        title="Прогресс обучения",
        columns=["Студент", "Продукт", "Прогресс, %", "Оценка", "Завершено"],
        rows=[],
        note=(
            "Интеграция с LMS не реализована (new_spec §4.14): источник данных "
            "отсутствует, отчёт всегда пуст до появления модуля integration."
        ),
    )


# =============================================================================
# Реестр
# =============================================================================

REPORT_BUILDERS: dict[str, ReportBuilder] = {
    "deal_funnel": build_deal_funnel,
    "kam_summary": build_kam_summary,
    "region_summary": build_region_summary,
    "loss_reasons": build_loss_reasons,
    "sla_compliance": build_sla_compliance,
    "monthly_dynamics": build_monthly_dynamics,
    "stuck_deals": build_stuck_deals,
    "learning_progress": build_learning_progress,
}

#: Отсутствие ключа = отчёт всегда лёгкий (см. докстринг модуля).
REPORT_ESTIMATORS: dict[str, ReportEstimator] = {
    "stuck_deals": estimate_stuck_deals,
}
