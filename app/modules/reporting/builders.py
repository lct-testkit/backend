"""Построители наборов данных отчётов (new_spec §4.13, B1-9/B1-10/B1-11).

Восемь видов раздела 4.13: «воронка по статусам (конверсия и время между
шагами), сводка по КАМам, по регионам/вузам, причины отказов, соблюдение
SLA, динамика по месяцам, прогресс обучения (из LMS), «зависшие сделки»».

Пять видов сверх раздела 4.13 — спринт B1 (`backend-sprints.md`):
`deal_register` — буквальное прочтение rtk_requiriments.md разд. 4
(Функц.4/§4.2): «отчёт за период в разрезе вуз/направление/продукт/статус/
ответственный». Ни один из восьми видов new_spec не даёт эти пять граней
вместе как колонки одной таблицы (`stuck_deals` ближе всего, но без
направления/продукта и только по просроченным); `deal_register` — построчный
реестр сделок (при нескольких продуктах в сделке — по строке на пару
сделка-продукт, у сделки без продуктов — одна строка с пустым продуктом и
направлением), со всеми шестью общими фильтрами `_parse_report_filters`
включая период. `lms_users_upload` — не отчёт из раздела 4.13, а файл для
внешней системы (см. ниже). `product_demand` (B1-9) — рейтинг
востребованности продуктов/направлений по заявкам/обучающимся/потокам,
собственная агрегатная формула (`compute_demand_index`). `license_register`
(B1-11) — реестр лицензий вуз-вендор-ПО (ТЗ §4 п.1) поверх
`catalog.service.organization_scope_clause`, не `deal_scope_clause`:
лицензия привязана к вузу, не к сделке.

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
(статусы/регионы/КАМы/месяцы/причины отказов, B1-9: продукты/направления —
заведомо не больше нескольких десятков), поэтому они всегда идут по лёгкому
пути. Построчные листинги — `stuck_deals`, `deal_register`, `learning_progress`
(B1-10), `license_register` (B1-11) — где строк может быть много, имеют
настоящую оценку.

П1 (rtk_requiriments.md разд. 4, ФТ.1/ФТ.4): «фильтрация... за выбранный
период по выбранным вузам, ИТ-направлениям, ИТ-продуктам, ответственным».
`ReportFilters`/`_parse_report_filters` разбирают шесть общих ключей
`params` — `date_from`, `date_to`, `organization_ids`, `direction_ids`,
`product_ids`, `owner_ids`, все опциональны (отсутствие = без фильтра,
старые вызовы без этих ключей ведут себя как раньше). `_apply_deal_filters`/
`_apply_entity_filters` — общие хелперы применения, каждый builder сам решает,
по какой дате считать «период» (по умолчанию `Deal.created_at`) и какие из
пяти фильтров ему структурно осмысленны — решение описано в комментарии
рядом с телом builder'а. `learning_progress` (B1-10) применяет те же пять фильтров своей
собственной реализацией (`_learning_progress_query`), не этими общими хелперами — его базовая
таблица `LearningProgress`, не `Deal`, и продукт/направление фильтруются по курсу LMS
(`LearningProgress.product_id`), а не по всем строкам сделки.

`lms_users_upload`: учащиеся оплаченных сделок физлиц в формате шаблона LMS «Загрузка
пользователей». В нём СНИЛС, паспорт и адрес целиком, поэтому он выходит только файлом xlsx
через очередь отчётов (аудит `REPORT_EXPORTED`), а не JSON-ом через `GET /reports/{id}/data`
(`FILE_ONLY_REPORTS`).
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
import zoneinfo
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import FieldError, ValidationError
from app.core.masking import mask_contacts_text, mask_name
from app.core.permissions import DealScope, Permission, deal_scope_for, has_permission
from app.core.security import Principal
from app.modules.catalog import learner
from app.modules.catalog.license_values import TRANSFER_STATUS_LABELS
from app.modules.catalog.models import (
    Contact,
    ContactLearnerProfile,
    Direction,
    LicenseTransferStatus,
    LossReason,
    Organization,
    OrganizationLicense,
    Product,
    Region,
)
from app.modules.catalog.service import organization_scope_clause
from app.modules.crm.models import Deal, DealProduct, DealStatusHistory
from app.modules.crm.service import deal_scope_clause
from app.modules.identity.models import User
from app.modules.integration.models import LearningProgress
from app.modules.workflow.models import StatusType, Workflow, WorkflowStatus


@dataclass(slots=True)
class ReportDataset:
    title: str
    columns: list[str]
    rows: list[list[Any]]
    note: str | None = None
    generated_at: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))
    # B1-2: параллельные `columns` списки для рендера (B1-6) — ключ колонки каталога (`None`, если
    # у вида отчёта нет каталога колонок) и её тип значения. `None` целиком (а не список `None`) —
    # признак «у этого builder'а колонки не из каталога», используется только рендером B1-6.
    keys: list[str | None] | None = None
    kinds: list[str] | None = None
    # B1-6/B1-7: необязательные поля качества файлов — усечение по `limit` и момент среза данных
    # (материализованное представление). Отсутствие `total_rows`/`truncated` означает «усечения не
    # было, оценивать незачем» (агрегаты заведомо малы).
    total_rows: int | None = None
    truncated: bool = False
    data_as_of: dt.datetime | None = None
    # B1-8/B1-11: данные для графика, которые не идут в табличные `rows` целиком или зависят от
    # исходного набора, а не от выбранных пользователем `columns` (`learning_progress.
    # progress_pct` может быть не выбран, но график корзин прогресса всё равно должен его
    # видеть; `license_register` считает график по статусу/году до применения `columns`).
    # Builder-специфичный словарь, читает только соответствующая функция `reporting.charts`.
    extra: dict[str, Any] | None = None


ReportBuilder = Callable[[AsyncSession, Principal, dict[str, Any]], Awaitable[ReportDataset]]
ReportEstimator = Callable[[AsyncSession, Principal, dict[str, Any]], Awaitable[int]]


@dataclass(frozen=True, slots=True)
class ColumnDef:
    """Описание одной колонки каталога вида отчёта (B1-2). `kind` — подсказка рендеру (B1-6), как
    форматировать значение (деньги/процент/дни/дата/дата-время), а не влияет на сам запрос."""

    key: str
    title: str
    kind: Literal["text", "int", "money", "pct", "days", "date", "datetime"] = "text"
    default: bool = True


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


#: B1-3: `date_field` — по какой дате считать «период» (раздел 4/ФТ.1/ФТ.4). Колонка `Deal`,
#: которую выбирает `filters.date_field`; `updated_at` — последнее изменение карточки сделки
#: (`TimestampMixin`, комментарии в карточке не считаются).
_DATE_FIELD_COLUMNS: dict[str, Any] = {
    "created_at": Deal.created_at,
    "closed_at": Deal.closed_at,
    "updated_at": Deal.updated_at,
}
DEFAULT_TIMEZONE_NAME = "Europe/Moscow"


def _default_timezone() -> dt.tzinfo:
    return zoneinfo.ZoneInfo(DEFAULT_TIMEZONE_NAME)


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
    # B1-1 (BUG-1): поддерево `direction_ids`, если оно было раскрыто `_load_filters` — выбор
    # родительского направления тогда включает дочерние. `None` у builder'ов, которые продолжают
    # звать `_parse_report_filters` напрямую (без БД) — `_apply_entity_filters` в этом случае
    # падает назад на точное совпадение `direction_ids`, как было до этой задачи.
    direction_tree_ids: list[uuid.UUID] | None = None
    # B1-3: колонка периода (`created_at`/`closed_at`/`updated_at`) — проверяется против
    # `ReportSpec.date_fields` в `reporting.service.validate_params`, здесь просто хранится.
    date_field: str = "created_at"
    # B1-3: пояс пользователя для границ суток периода и `date_trunc` месяца (B1-7). По умолчанию
    # Europe/Moscow — тот же запасной пояс, что `crm.service._business_calendar`.
    tz: dt.tzinfo = field(default_factory=_default_timezone)

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
    date_field = params.get("date_field")
    return ReportFilters(
        date_from=date_from,
        date_to=date_to,
        organization_ids=_parse_uuid_list(params, "organization_ids"),
        direction_ids=_parse_uuid_list(params, "direction_ids"),
        product_ids=_parse_uuid_list(params, "product_ids"),
        owner_ids=_parse_uuid_list(params, "owner_ids"),
        date_field=str(date_field) if date_field else "created_at",
    )


def _day_start(value: dt.date, tz: dt.tzinfo) -> dt.datetime:
    return dt.datetime.combine(value, dt.time.min, tzinfo=tz)


async def _expand_direction_tree(
    session: AsyncSession, direction_ids: list[uuid.UUID]
) -> list[uuid.UUID]:
    """B1-1 (BUG-1): `direction_ids` -> само значение плюс все дочерние направления рекурсивно
    (выбор родителя — «Программирование» — должен включать «Python», «Java» и т.п., раздел
    5.3/demo-дерево `deploy/seed/catalogs.mjs`). `UNION`, не `UNION ALL`: испорченные данные с
    циклом `parent_id` не должны зациклить запрос (повтор id схлопывается)."""
    base = select(Direction.id).where(Direction.id.in_(direction_ids))
    tree = base.cte("direction_tree", recursive=True)
    tree = tree.union(select(Direction.id).join(tree, Direction.parent_id == tree.c.id))
    rows = (await session.execute(select(tree.c.id))).scalars().all()
    return list(rows)


async def _resolve_user_timezone(session: AsyncSession, principal: Principal) -> dt.tzinfo:
    """B1-3: пояс пользователя, одним запросом (`User.timezone`, по умолчанию Europe/Moscow —
    `identity.models.User`). Пустое или неизвестное (`zoneinfo` не знает такой пояс) значение —
    тот же запасной пояс, тем же приёмом, что `crm.service._business_calendar`."""
    raw = await session.scalar(select(User.timezone).where(User.id == principal.user_id))
    if not raw:
        return _default_timezone()
    try:
        return zoneinfo.ZoneInfo(raw)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return _default_timezone()


async def resolve_user_timezone_name(session: AsyncSession, principal: Principal) -> str:
    """B1-5/B1-6: публичная обёртка над `_resolve_user_timezone` — IANA-имя пояса строкой, для
    шапки файла и конверта JSON (`rendering.render_json`), которым сам `ReportFilters` не нужен,
    только имя пояса. Отдельный маленький запрос в `ReportJobService.generate`, а не протаскивание
    через `ReportDataset`: пояс у всех builder'ов этого модуля — один и тот же принцип (`User.
    timezone` с откатом на Europe/Moscow), трогать сигнатуру каждого из них ради одного поля
    конверта было бы лишним churn."""
    tz = await _resolve_user_timezone(session, principal)
    return getattr(tz, "key", DEFAULT_TIMEZONE_NAME)


async def _load_filters(
    session: AsyncSession, principal: Principal, params: dict[str, Any]
) -> ReportFilters:
    """B1-1/B1-3: как `_parse_report_filters`, но раскрывает `direction_ids` в поддерево и
    подставляет настоящий пояс пользователя — обоим нужна сессия, поэтому async и отдельная
    функция, а не правка `_parse_report_filters`, которую используют офлайновые тесты формата
    параметров."""
    filters = _parse_report_filters(params)
    if filters.direction_ids:
        filters.direction_tree_ids = await _expand_direction_tree(session, filters.direction_ids)
    filters.tz = await _resolve_user_timezone(session, principal)
    return filters


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
                select(DealProduct.deal_id).where(DealProduct.product_id.in_(filters.product_ids))
            )
        )
    direction_ids = filters.direction_tree_ids or filters.direction_ids
    if direction_ids:
        stmt = stmt.where(
            Deal.id.in_(
                select(DealProduct.deal_id)
                .join(Product, Product.id == DealProduct.product_id)
                .where(Product.direction_id.in_(direction_ids))
            )
        )
    return stmt


def _apply_deal_filters(stmt: Any, filters: ReportFilters, *, date_column: Any = None) -> Any:
    """Период + фильтры по сущностям для запросов, у которых `Deal` — базовая
    таблица (`select(...).select_from(Deal)` либо просто `select(Deal...)`).

    `date_column`, когда передан явно, перекрывает `filters.date_field` — так
    `build_loss_reasons` закрепляет `Deal.closed_at` (момент, когда причина
    отказа стала известна) независимо от выбора пользователя, потому что его
    `ReportSpec.date_fields` не даёт этот параметр выбрать (B1-2/B1-3). Без
    явной колонки период считается по `_DATE_FIELD_COLUMNS[filters.date_field]`
    (по умолчанию `created_at`), а границы суток — в поясе `filters.tz`
    (B1-3: `Europe/Moscow`, если у пользователя не задан свой)."""
    column = (
        date_column
        if date_column is not None
        else _DATE_FIELD_COLUMNS.get(filters.date_field, Deal.created_at)
    )
    if filters.date_from is not None:
        stmt = stmt.where(column >= _day_start(filters.date_from, filters.tz))
    if filters.date_to is not None:
        stmt = stmt.where(column < _day_start(filters.date_to, filters.tz) + dt.timedelta(days=1))
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


async def _resolve_workflow_id(session: AsyncSession, params: dict[str, Any]) -> uuid.UUID:
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


#: Статусы, которые считаются «прогрессом» воронки — `lost`/`parked` не входят: сделка не
#: «доходит» до отказа или заморозки, она туда ветвится. `reached_count` (B1-7, BUG-3/4)
#: определён только для этих трёх типов — см. миграцию `0027_report_mv_without_deleted`.
_MAIN_STATUS_TYPES = (
    StatusType.INITIAL.value,
    StatusType.INTERMEDIATE.value,
    StatusType.WON.value,
)


async def _reached_counts(
    session: AsyncSession,
    principal: Principal,
    filters: ReportFilters,
    workflow_id: uuid.UUID,
    main_statuses: list[tuple[uuid.UUID, int]],
) -> dict[uuid.UUID, int]:
    """B1-7 (BUG-3): для каждого основного статуса (initial/intermediate/won) — число РАЗЛИЧНЫХ
    сделок, у которых наибольший `sort_order` среди посещённых основных статусов не меньше
    `sort_order` этого статуса, то есть «дошли до шага N и дальше». В отличие от `entered_count`
    (сколько раз сделка ВХОДИЛА в статус — возвраты назад и повторные переходы считаются
    отдельно), это даёт конверсию, которая не может превысить 100% при пропуске необязательного
    шага: `reached` монотонно не возрастает с номером шага по построению.

    `main_statuses` — пары `(status_id, sort_order)` только основных статусов, уже отфильтрованных
    вызывающим кодом (другие типы сюда не передаются — у них reached не определён)."""
    if not main_statuses:
        return {}
    per_deal = (
        select(
            DealStatusHistory.deal_id,
            func.max(WorkflowStatus.sort_order).label("max_order"),
        )
        .select_from(DealStatusHistory)
        .join(WorkflowStatus, WorkflowStatus.id == DealStatusHistory.to_status_id)
        .where(
            WorkflowStatus.workflow_id == workflow_id,
            WorkflowStatus.type.in_(_MAIN_STATUS_TYPES),
            DealStatusHistory.deal_id.in_(await _scoped_deal_ids(session, principal, filters)),
        )
        .group_by(DealStatusHistory.deal_id)
        .subquery()
    )
    counts_stmt = select(per_deal.c.max_order, func.count()).group_by(per_deal.c.max_order)
    by_order: dict[int, int] = dict((await session.execute(counts_stmt)).all())  # type: ignore[arg-type]
    return {
        status_id: sum(count for order, count in by_order.items() if order >= sort_order)
        for status_id, sort_order in main_statuses
    }


async def build_deal_funnel(
    session: AsyncSession, principal: Principal, params: dict[str, Any]
) -> ReportDataset:
    """Воронка по статусам (B1-7, BUG-3/4): «Конверсия шага, %» считается от `reached` (дошли
    до шага и дальше), не от `entered` (вошли хоть раз) — иначе необязательный шаг, который
    пропускает часть сделок, давал конверсию следующего шага выше 100% (раздел 1: шаг 5
    задания). Новые колонки «Дошли до шага» и «От старта, %» — в конец, первые пять заголовков
    не меняются (контракт с дашбордом, `widgets.ts:18-24`)."""
    workflow_id = await _resolve_workflow_id(session, params)
    filters = await _load_filters(session, principal, params)
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
        "Статус",
        "Сейчас в статусе",
        "Всего прошло",
        "Конверсия шага, %",
        "Среднее время, дн.",
        "Дошли до шага",
        "От старта, %",
    ]
    if not statuses:
        return ReportDataset(title="Воронка по статусам", columns=columns, rows=[])

    note: str | None = None
    data_as_of: dt.datetime | None = None
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
                    "mv.avg_duration_in_prev, mv.reached_count, mv.refreshed_at "
                    "FROM workflow_statuses ws "
                    "LEFT JOIN mv_deal_status_summary mv ON mv.status_id = ws.id "
                    "WHERE ws.workflow_id = :workflow_id "
                    "ORDER BY ws.sort_order"
                ),
                {"workflow_id": workflow_id},
            )
        ).all()
        stats = {row[0]: (row[3], row[4], row[5], row[6]) for row in mv_rows}
        as_of_values = [row[7] for row in mv_rows if row[7] is not None]
        data_as_of = max(as_of_values) if as_of_values else None
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
        reached_map = await _reached_counts(
            session,
            principal,
            filters,
            workflow_id,
            [(s.id, s.sort_order) for s in statuses if s.type in _MAIN_STATUS_TYPES],
        )
        stats = {
            s.id: (
                current_map.get(s.id, 0),
                *hist_map.get(s.id, (0, None)),
                reached_map.get(s.id),
            )
            for s in statuses
        }
        data_as_of = dt.datetime.now(dt.UTC)

    rows: list[list[Any]] = []
    prev_reached: int | None = None
    reached_first: int | None = None
    for status in statuses:
        currently_in, entered, avg_dur, reached = stats.get(status.id, (0, 0, None, None))
        entered = entered or 0
        # B1-7: архивный статус без единого перехода и без сделок сейчас — шум, не строка
        # воронки (статус мог архивироваться до того, как через него прошла хоть одна сделка).
        if status.is_archived and currently_in == 0 and entered == 0:
            continue
        avg_days = round(avg_dur.total_seconds() / 86400, 1) if avg_dur is not None else None

        if status.type in _MAIN_STATUS_TYPES:
            if reached_first is None:
                reached_first = reached or 0
            if prev_reached is None:
                conversion = 100.0 if reached else None
            elif prev_reached == 0:
                conversion = None
            else:
                conversion = round((reached or 0) / prev_reached * 100, 1)
            prev_reached = reached or 0
            reached_out: int | None = prev_reached
            from_start = round(prev_reached / reached_first * 100, 1) if reached_first else None
        else:
            # lost/parked: не часть цепочки конверсии (ветвление, не прогресс), поэтому
            # «Конверсия шага» и «Дошли до шага» для них не определены (`None`); «От старта, %»
            # всё же считается — по числу сделок, попавших именно в этот статус (`entered`), от
            # того же знаменателя, что и у основных шагов.
            conversion = None
            reached_out = None
            from_start = round(entered / reached_first * 100, 1) if reached_first else None

        rows.append(
            [status.name, currently_in, entered, conversion, avg_days, reached_out, from_start]
        )

    return ReportDataset(
        title="Воронка по статусам",
        columns=columns,
        rows=rows,
        note=note,
        data_as_of=data_as_of,
    )


# =============================================================================
# 2. Сводка по КАМам
# =============================================================================


async def build_kam_summary(
    session: AsyncSession, principal: Principal, params: dict[str, Any]
) -> ReportDataset:
    """B1-7 (BUG-5): «В работе» раньше считала всё, что не `won`/`lost` (`TERMINAL_TYPES` — по
    замыслу «закрыта», `parked` туда намеренно не входит, см. докстринг `workflow.models`), то
    есть включала и замороженные сделки. Теперь «В работе» — только `initial`/`intermediate`,
    замороженные — отдельная колонка в конце (контракт первых пяти заголовков не меняется)."""
    filters = await _load_filters(session, principal, params)
    clause = await deal_scope_clause(session, principal)
    name_expr = func.coalesce(User.display_name, User.full_name)
    in_progress_types = [StatusType.INITIAL.value, StatusType.INTERMEDIATE.value]
    stmt = (
        select(
            name_expr,
            func.count(Deal.id).filter(WorkflowStatus.type.in_(in_progress_types)),
            func.count(Deal.id).filter(WorkflowStatus.type == StatusType.WON.value),
            func.count(Deal.id).filter(WorkflowStatus.type == StatusType.LOST.value),
            func.coalesce(
                func.sum(Deal.amount).filter(WorkflowStatus.type == StatusType.WON.value), 0
            ),
            func.avg(Deal.closed_at - Deal.created_at).filter(Deal.closed_at.is_not(None)),
            func.count(Deal.id).filter(WorkflowStatus.type == StatusType.PARKED.value),
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
            frozen_count,
        ]
        for (
            name,
            open_count,
            won_count,
            lost_count,
            won_amount,
            avg_cycle,
            frozen_count,
        ) in result
    ]
    return ReportDataset(
        title="Сводка по КАМам",
        columns=[
            "КАМ",
            "В работе",
            "Выиграно",
            "Проиграно",
            "Сумма выигранных",
            "Ср. цикл, дн.",
            "Заморожено",
        ],
        rows=rows,
    )


# =============================================================================
# 3. По регионам/вузам
# =============================================================================


async def build_region_summary(
    session: AsyncSession, principal: Principal, params: dict[str, Any]
) -> ReportDataset:
    filters = await _load_filters(session, principal, params)
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
    # `date_column=Deal.closed_at` ниже закрепляет это жёстко: `ReportSpec.date_fields` этого вида
    # отчёта — только `("closed_at",)` (B1-3), `date_field` параметром не выбирается.
    filters = await _load_filters(session, principal, params)
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
    """B1-7 (BUG-4): `apply_sla` (`crm.service`) обнуляет `sla_state` до `ok` при закрытии
    сделки — раньше это завышало долю «В норме» на число закрытых сделок, у которых SLA
    фактически уже не действует. Открытые и закрытые считаются раздельно: доли — только по
    открытым (`closed_at IS NULL`), число закрытых в выборке — в `note`, без них самих в
    знаменателе."""
    filters = await _load_filters(session, principal, params)
    clause = await deal_scope_clause(session, principal)
    open_stmt = (
        select(Deal.sla_state, func.count(Deal.id))
        .where(Deal.deleted_at.is_(None), Deal.closed_at.is_(None))
        .group_by(Deal.sla_state)
    )
    closed_stmt = select(func.count(Deal.id)).where(
        Deal.deleted_at.is_(None), Deal.closed_at.is_not(None)
    )
    if clause is not None:
        open_stmt = open_stmt.where(clause)
        closed_stmt = closed_stmt.where(clause)
    open_stmt = _apply_deal_filters(open_stmt, filters)
    closed_stmt = _apply_deal_filters(closed_stmt, filters)
    result: dict[str, int] = {row[0]: row[1] for row in (await session.execute(open_stmt)).all()}
    closed_count = (await session.execute(closed_stmt)).scalar_one() or 0
    total = sum(result.values())
    labels = {
        "ok": "В норме",
        "warning": "Под угрозой",
        "breached": "Нарушен",
        "paused": "Остановлен (сделка заморожена)",
    }
    rows = []
    for state in ("ok", "warning", "breached", "paused"):
        count = result.get(state, 0)
        pct = round(count / total * 100, 1) if total else 0.0
        rows.append([labels.get(state, state), count, pct])
    return ReportDataset(
        title="Соблюдение SLA",
        columns=["Состояние", "Сделок", "Доля, %"],
        rows=rows,
        note=f"Закрытых сделок в выборке: {closed_count}, SLA к ним не применяется",
    )


# =============================================================================
# 6. Динамика по месяцам
# =============================================================================


def _now() -> dt.datetime:
    """Текущий момент (UTC, aware) — отдельная функция, а не прямой `dt.datetime.now(dt.UTC)` в
    теле builder'а, чтобы тесты (B1-7) могли зафиксировать «сегодня» через
    `monkeypatch.setattr(builders, "_now", ...)` и не зависеть от даты прогона."""
    return dt.datetime.now(dt.UTC)


def _month_floor(value: dt.date) -> dt.date:
    return value.replace(day=1)


def _add_months(value: dt.date, delta: int) -> dt.date:
    total = value.month - 1 + delta
    year = value.year + total // 12
    month = total % 12 + 1
    return dt.date(year, month, 1)


def _month_range(start: dt.date, end: dt.date) -> list[dt.date]:
    """Все месяцы окна `[start, end]` включительно, по первому числу — так месяц без единой
    сделки всё равно попадает в ряд с нулём (B1-7, BUG-3), а не просто выпадает из строк."""
    months: list[dt.date] = []
    cur = _month_floor(start)
    last = _month_floor(end)
    while cur <= last:
        months.append(cur)
        cur = _add_months(cur, 1)
    return months


def _month_trunc_local(column: Any, tz_name: str) -> Any:
    """`date_trunc('month', колонка)` в поясе пользователя, не сессии БД (B1-3/B1-7):
    `timezone(zone, timestamptz)` в Postgres сначала переводит момент в стенные часы указанного
    пояса (и превращает его в `timestamp` без пояса), а `date_trunc` уже режет по месяцу этих
    стенных часов. Без этого перевода «1 февраля 00:30 по Москве» (это 31 января 21:30 UTC)
    попадало бы в январскую строку."""
    return func.date_trunc("month", func.timezone(tz_name, column))


async def build_monthly_dynamics(
    session: AsyncSession, principal: Principal, params: dict[str, Any]
) -> ReportDataset:
    """Динамика по месяцам (B1-7, BUG-3): ряд строится в Python от первого до последнего месяца
    окна и заполняется нулями за пропущенные месяцы — раньше месяц без единой сделки просто не
    попадал в строки, а график (`charts.py`, B1-8) рисовал точки на равных расстояниях по
    индексу, из-за чего пропуск не был виден на глаз. `date_field` у этого вида отчёта не
    выбирается (`ReportSpec.date_fields == ()`, B1-2/B1-3): у динамики уже две самостоятельные
    серии по двум датам (создано/закрыто одновременно), выбор одной даты не имеет смысла."""
    months = _parse_int(params, "months", default=12, minimum=1, maximum=36)
    filters = await _load_filters(session, principal, params)
    clause = await deal_scope_clause(session, principal)
    tz = filters.tz
    tz_name = getattr(tz, "key", DEFAULT_TIMEZONE_NAME)
    now_local = _now().astimezone(tz).date()

    # `date_from`/`date_to` (П1), когда заданы, замещают «месяцев назад» целиком — явный период
    # точнее эвристики по количеству месяцев. Список организаций и остальные сущностные фильтры
    # применяются тем же `_apply_entity_filters`, что и у остальных builder'ов.
    window_start = (
        filters.date_from
        if filters.date_from is not None
        else _add_months(_month_floor(now_local), -(months - 1))
    )
    window_end = filters.date_to if filters.date_to is not None else now_local
    since = _day_start(window_start, tz)
    until = (
        _day_start(filters.date_to, tz) + dt.timedelta(days=1)
        if filters.date_to is not None
        else None
    )

    # Выражение месяца строится один раз в переменную и переиспользуется в SELECT и GROUP BY: два
    # отдельных вызова с одним и тем же строковым литералом дают ДВА разных bind-параметра —
    # Postgres не может на этапе планирования доказать, что они совпадут в рантайме, и падает
    # `GroupingError`. Живая проверка против настоящего Postgres поймала это сразу (офлайновые
    # тесты этого не видят — SQLite-подобной заглушки тут нет, см. docstring
    # `tests/test_reporting.py`).
    created_month = _month_trunc_local(Deal.created_at, tz_name)
    created_stmt = (
        select(created_month, func.count(Deal.id))
        .where(Deal.deleted_at.is_(None), Deal.created_at >= since)
        .group_by(created_month)
    )
    if until is not None:
        created_stmt = created_stmt.where(Deal.created_at < until)
    closed_month = _month_trunc_local(Deal.closed_at, tz_name)
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

    all_months = _month_range(window_start, window_end)
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
        note=(
            f"Создано считается по дате создания, выиграно и проиграно — по дате закрытия; "
            f"месяц — в поясе {tz_name}. Пропущенные месяцы показаны с нулём, а не выпадают из "
            "строк."
        ),
    )


# =============================================================================
# 7. Зависшие сделки
# =============================================================================


async def _stuck_deals_query(session: AsyncSession, principal: Principal, params: dict[str, Any]):
    filters = await _load_filters(session, principal, params)
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
        # B1-7 (BUG-4), на всякий случай: `apply_sla` обнуляет `sla_state` при закрытии, так что
        # закрытая сделка в норме не должна остаться "breached" — но явное условие защищает от
        # любого исторического/краевого случая, когда это почему-то не так.
        .where(Deal.deleted_at.is_(None), Deal.sla_state == "breached", Deal.closed_at.is_(None))
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

    # B1-6: то же усечение, что у `deal_register` — второй запрос (`estimate_stuck_deals`)
    # только когда выдача упёрлась в `limit`.
    note: str | None = None
    total_rows: int | None = None
    truncated = False
    if len(rows) == limit:
        total_rows = await estimate_stuck_deals(session, principal, params)
        if total_rows > limit:
            truncated = True
            note = _truncation_note(limit, total_rows)

    return ReportDataset(
        title="Зависшие сделки",
        columns=["Номер", "Название", "Ответственный", "Статус", "Организация", "Дней просрочки"],
        rows=rows,
        note=note,
        keys=["number", "title", "owner", "status", "organization", "days_overdue"],
        kinds=["text", "text", "text", "text", "text", "days"],
        total_rows=total_rows,
        truncated=truncated,
    )


# =============================================================================
# 8а. Реестр сделок: вуз + направление + продукт + статус + ответственный
# =============================================================================

#: Каталог колонок `deal_register` (B1-2). Порядок в файле равен порядку в параметре `columns`;
#: без него действует набор по умолчанию — первые десять (`default=True`), в этом же порядке:
#: ровно нынешние заголовки, которые ищет фронтенд (`widgets.ts:18-24`) — не переименовываются и
#: не переставляются, новые шесть добавлены только в конец каталога.
REGISTER_COLUMNS: tuple[ColumnDef, ...] = (
    ColumnDef("organization", "Вуз"),
    ColumnDef("direction", "Направление"),
    ColumnDef("product", "Продукт"),
    ColumnDef("status", "Статус"),
    ColumnDef("owner", "Ответственный"),
    ColumnDef("number", "Номер"),
    ColumnDef("title", "Название"),
    ColumnDef("amount", "Сумма, ₽", kind="money"),
    ColumnDef("created_at", "Создана", kind="datetime"),
    ColumnDef("closed_at", "Закрыта", kind="datetime"),
    ColumnDef("region", "Регион", default=False),
    ColumnDef("quantity", "Кол-во", kind="int", default=False),
    ColumnDef("stream_number", "Поток", kind="int", default=False),
    ColumnDef("product_total", "Сумма по продукту, ₽", kind="money", default=False),
    ColumnDef("students_planned", "Студентов (план)", kind="int", default=False),
    ColumnDef("priority", "Приоритет", default=False),
)
_REGISTER_COLUMNS_BY_KEY: dict[str, ColumnDef] = {c.key: c for c in REGISTER_COLUMNS}
#: Колонки продукта (B1-1): их выбор требует зерна «product» — строка на пару сделка-продукт.
_REGISTER_PRODUCT_KEYS = frozenset({"product", "quantity", "stream_number", "product_total"})
#: Колонки-названия, у которых отсутствующее значение показывается как «—», не пустой ячейкой —
#: тот же приём, что уже был у `organization`/`direction`/`product` до каталога колонок.
_REGISTER_DASH_KEYS = frozenset({"organization", "direction", "product", "region"})
_REGISTER_MONEY_KEYS = frozenset({"amount", "product_total"})


def register_default_columns() -> list[str]:
    return [c.key for c in REGISTER_COLUMNS if c.default]


def _register_grain(columns: list[str]) -> Literal["deal", "direction", "product"]:
    """B1-1/B1-2: зерно строки реестра зависит от выбранных колонок. Есть колонка продукта —
    строка на пару сделка-продукт (как было до каталога колонок). Нет колонки продукта, но есть
    «направление» — строка на пару сделка-направление (`SELECT DISTINCT`: несколько продуктов
    одного направления у одной сделки дают одну строку). Иначе — строка на сделку, без join'ов к
    продуктам вовсе."""
    if any(key in _REGISTER_PRODUCT_KEYS for key in columns):
        return "product"
    if "direction" in columns:
        return "direction"
    return "deal"


def _parse_register_columns(params: dict[str, Any]) -> list[str]:
    """Разбор `columns` реестра сделок. Строже, чем остальные `_parse_*` этого модуля: ошибка
    содержит список допустимых ключей (B1-2, «422 со списком допустимых»), а не просто жалуется
    на тип — колонок 16, угадывать их пользователю не из чего."""
    raw = params.get("columns")
    if raw is None or raw == []:
        return register_default_columns()
    if not isinstance(raw, list):
        raise ValidationError(
            "Параметр 'columns' должен быть списком ключей колонок",
            [FieldError(field="columns", reason="ожидается массив строк")],
        )
    valid_keys = sorted(_REGISTER_COLUMNS_BY_KEY)
    seen: set[str] = set()
    result: list[str] = []
    for key in raw:
        if key not in _REGISTER_COLUMNS_BY_KEY:
            raise ValidationError(
                f"Параметр 'columns' содержит неизвестный ключ: {key!r}",
                [
                    FieldError(
                        field="columns",
                        reason=f"неизвестный ключ: {key}; допустимо: {', '.join(valid_keys)}",
                    )
                ],
            )
        if key in seen:
            raise ValidationError(
                f"Параметр 'columns' содержит повтор ключа: {key!r}",
                [FieldError(field="columns", reason=f"повтор ключа: {key}")],
            )
        seen.add(key)
        result.append(key)
    return result


async def _deal_register_query(
    session: AsyncSession, principal: Principal, params: dict[str, Any], *, columns: list[str]
):
    filters = await _load_filters(session, principal, params)
    clause = await deal_scope_clause(session, principal)
    direction_ids = filters.direction_tree_ids or filters.direction_ids
    grain = _register_grain(columns)
    needs_region = "region" in columns
    owner_name = func.coalesce(User.display_name, User.full_name)

    if grain == "product":
        # BUG-1 (часть 2): сумма сделки повторялась на каждой её строке-продукте, поэтому сумма по
        # колонке в Excel была завышена ровно во столько раз, сколько у сделки продуктов. Окно
        # считается уже после WHERE/JOIN (обычная семантика Postgres), поэтому при фильтре по
        # продукту сумма достаётся первой ОТОБРАННОЙ строке, а не первой по каталогу.
        amount_expr = case(
            (
                func.row_number().over(
                    partition_by=Deal.id, order_by=(Product.name, DealProduct.id)
                )
                == 1,
                Deal.amount,
            ),
            else_=None,
        )
        product_total_expr = func.coalesce(
            DealProduct.total,
            DealProduct.price * DealProduct.quantity * (1 - DealProduct.discount_pct / 100),
        )
        exprs: dict[str, Any] = {
            "organization": Organization.name,
            "direction": Direction.name,
            "product": Product.name,
            "status": WorkflowStatus.name,
            "owner": owner_name,
            "number": Deal.number,
            "title": Deal.title,
            "amount": amount_expr,
            "created_at": Deal.created_at,
            "closed_at": Deal.closed_at,
            "quantity": DealProduct.quantity,
            "stream_number": DealProduct.stream_number,
            "product_total": product_total_expr,
            "students_planned": Deal.students_planned,
            "priority": Deal.priority,
        }
        if needs_region:
            exprs["region"] = Region.name
        stmt = (
            select(*[exprs[key] for key in columns])
            .select_from(Deal)
            .join(User, User.id == Deal.owner_id)
            .join(WorkflowStatus, WorkflowStatus.id == Deal.status_id)
            .outerjoin(Organization, Organization.id == Deal.organization_id)
            .outerjoin(DealProduct, DealProduct.deal_id == Deal.id)
            .outerjoin(Product, Product.id == DealProduct.product_id)
            .outerjoin(Direction, Direction.id == Product.direction_id)
            .where(Deal.deleted_at.is_(None))
            .order_by(Deal.created_at.desc(), Deal.number)
        )
        if needs_region:
            stmt = stmt.outerjoin(Region, Region.id == Organization.region_id)
        if clause is not None:
            stmt = stmt.where(clause)
        stmt = _apply_deal_filters(stmt, filters)
        # BUG-1 (часть 1): `_apply_deal_filters` выше уже ограничил, какие СДЕЛКИ попадают в отчёт
        # (сделка остаётся, если хотя бы один её продукт подходит под фильтр) — но без этих двух
        # условий в выдаче сделки с продуктами P и Q при фильтре "только P" всё равно была бы и
        # строка с Q, потому что сама строка берётся из LEFT JOIN без ограничения на продукт/
        # направление. Здесь ограничиваются уже СТРОКИ.
        if filters.product_ids:
            stmt = stmt.where(Product.id.in_(filters.product_ids))
        if direction_ids:
            stmt = stmt.where(Direction.id.in_(direction_ids))
        return stmt

    if grain == "direction":
        # B1-2: строка на пару сделка-направление — несколько продуктов одного направления у
        # одной сделки должны дать одну строку, не одну на каждый продукт. `DealProduct`/
        # `Product` здесь только путь до `Direction`, в итоговой проекции их нет.
        pairs = (
            select(Deal.id.label("deal_id"), Direction.id.label("direction_id"))
            .select_from(Deal)
            .outerjoin(DealProduct, DealProduct.deal_id == Deal.id)
            .outerjoin(Product, Product.id == DealProduct.product_id)
            .outerjoin(Direction, Direction.id == Product.direction_id)
            .where(Deal.deleted_at.is_(None))
        )
        if clause is not None:
            pairs = pairs.where(clause)
        pairs = _apply_deal_filters(pairs, filters)
        # Тот же приём BUG-1 (часть 1), что и в зерне «product»: ограничивает уже строки
        # сделка-направление, не только то, какие сделки прошли сущностный фильтр.
        if filters.product_ids:
            pairs = pairs.where(Product.id.in_(filters.product_ids))
        if direction_ids:
            pairs = pairs.where(Direction.id.in_(direction_ids))
        pairs_cte = pairs.distinct().cte("register_direction_pairs")

        # Сумма сделки — один раз (BUG-1, часть 2), но порядок окна считается по уже СХЛОПНУТЫМ
        # парам `pairs_cte`, а не по исходным строкам-продуктам: иначе `SELECT DISTINCT` после
        # оконной функции развалил бы одну пару на две строки там, где сумма стоит на одной из
        # двух исходных строк-продуктов одного направления, а на другой пустует.
        amount_expr = case(
            (
                func.row_number().over(
                    partition_by=pairs_cte.c.deal_id, order_by=pairs_cte.c.direction_id
                )
                == 1,
                Deal.amount,
            ),
            else_=None,
        )
        exprs = {
            "organization": Organization.name,
            "direction": Direction.name,
            "status": WorkflowStatus.name,
            "owner": owner_name,
            "number": Deal.number,
            "title": Deal.title,
            "amount": amount_expr,
            "created_at": Deal.created_at,
            "closed_at": Deal.closed_at,
            "students_planned": Deal.students_planned,
            "priority": Deal.priority,
        }
        if needs_region:
            exprs["region"] = Region.name
        stmt = (
            select(*[exprs[key] for key in columns])
            .select_from(pairs_cte)
            .join(Deal, Deal.id == pairs_cte.c.deal_id)
            .join(User, User.id == Deal.owner_id)
            .join(WorkflowStatus, WorkflowStatus.id == Deal.status_id)
            .outerjoin(Organization, Organization.id == Deal.organization_id)
            .outerjoin(Direction, Direction.id == pairs_cte.c.direction_id)
            .order_by(Deal.created_at.desc(), Deal.number)
        )
        if needs_region:
            stmt = stmt.outerjoin(Region, Region.id == Organization.region_id)
        return stmt

    # grain == "deal": строка на сделку, без join'ов к продуктам/направлениям вовсе. Фильтры по
    # продукту/направлению всё равно ограничивают, какие СДЕЛКИ попадают (через
    # `_apply_deal_filters` -> `_apply_entity_filters`, подзапросы по `deal_products`) — просто
    # показывать сам продукт/направление нечем, колонка не выбрана.
    exprs = {
        "organization": Organization.name,
        "status": WorkflowStatus.name,
        "owner": owner_name,
        "number": Deal.number,
        "title": Deal.title,
        "amount": Deal.amount,
        "created_at": Deal.created_at,
        "closed_at": Deal.closed_at,
        "students_planned": Deal.students_planned,
        "priority": Deal.priority,
    }
    if needs_region:
        exprs["region"] = Region.name
    stmt = (
        select(*[exprs[key] for key in columns])
        .select_from(Deal)
        .join(User, User.id == Deal.owner_id)
        .join(WorkflowStatus, WorkflowStatus.id == Deal.status_id)
        .outerjoin(Organization, Organization.id == Deal.organization_id)
        .where(Deal.deleted_at.is_(None))
        .order_by(Deal.created_at.desc(), Deal.number)
    )
    if needs_region:
        stmt = stmt.outerjoin(Region, Region.id == Organization.region_id)
    if clause is not None:
        stmt = stmt.where(clause)
    stmt = _apply_deal_filters(stmt, filters)
    return stmt


async def estimate_deal_register(
    session: AsyncSession, principal: Principal, params: dict[str, Any]
) -> int:
    columns = _parse_register_columns(params)
    inner = await _deal_register_query(session, principal, params, columns=columns)
    count_stmt = select(func.count()).select_from(inner.subquery())
    return (await session.execute(count_stmt)).scalar_one() or 0


def _truncation_note(shown: int, total: int) -> str:
    """B1-6: единая фраза об усечении по `limit` — её же текст ищут тесты сквозной проверки
    (B1-13), поэтому формулировка здесь единственная, а не копия в каждом builder'е."""
    return f"Показано {shown} из {total} строк: отчёт усечён по параметру limit"


async def build_deal_register(
    session: AsyncSession, principal: Principal, params: dict[str, Any]
) -> ReportDataset:
    columns = _parse_register_columns(params)
    limit = _parse_int(params, "limit", default=1000, minimum=1, maximum=5000)
    stmt = (await _deal_register_query(session, principal, params, columns=columns)).limit(limit)
    result = (await session.execute(stmt)).all()

    rows: list[list[Any]] = []
    for raw_row in result:
        mapped_row: list[Any] = []
        for key, value in zip(columns, raw_row, strict=True):
            if key in _REGISTER_DASH_KEYS:
                mapped_row.append(value or "—")
            elif key in _REGISTER_MONEY_KEYS:
                mapped_row.append(float(value) if value is not None else None)
            else:
                mapped_row.append(value)
        rows.append(mapped_row)

    notes: list[str] = []
    if "amount" in columns:
        notes.append("Сумма сделки указана на первой строке сделки")
    # B1-6: усечение молчаливым быть не должно — но второй запрос (`estimate_deal_register`,
    # тот же COUNT, что уже делает `ReportJobService` для решения sync/async) нужен только
    # когда выдача упёрлась в `limit`: меньше строк, чем лимит, однозначно значит «это все
    # строки», оценивать незачем.
    total_rows: int | None = None
    truncated = False
    if len(rows) == limit:
        total_rows = await estimate_deal_register(session, principal, params)
        if total_rows > limit:
            truncated = True
            notes.append(_truncation_note(limit, total_rows))

    return ReportDataset(
        title="Сделки по вузам и направлениям",
        columns=[_REGISTER_COLUMNS_BY_KEY[key].title for key in columns],
        rows=rows,
        note="; ".join(notes) or None,
        keys=list(columns),
        kinds=[_REGISTER_COLUMNS_BY_KEY[key].kind for key in columns],
        total_rows=total_rows,
        truncated=truncated,
    )


# =============================================================================
# 8. Прогресс обучения (LMS)
# =============================================================================

#: Каталог колонок `learning_progress` (B1-10). Первые пять — на тех же местах, что были у
#: заглушки (фронтенд уже знает эти заголовки); следующие шесть добавлены этой задачей. Порядок
#: в файле — порядок в этом тюпле, как и у `REGISTER_COLUMNS` (B1-2).
LEARNING_PROGRESS_COLUMNS: tuple[ColumnDef, ...] = (
    ColumnDef("student", "Студент"),
    ColumnDef("product", "Продукт"),
    ColumnDef("progress_pct", "Прогресс, %", kind="int"),
    # `score` — оценка по шкале курса (0–5 или 0–100 в зависимости от LMS), не процент; формат
    # ячейки "0.0" (`kind="pct"`) переиспользован как ближайший готовый однодесятичный числовой
    # формат (`rendering._XLSX_NUMBER_FORMATS`/`rendering_xls._XlsStyles`) — отдельный `kind` под
    # одну колонку не завели ради этого недостаточного повода.
    ColumnDef("score", "Оценка", kind="pct"),
    ColumnDef("completed_at", "Завершено", kind="datetime"),
    ColumnDef("organization", "Вуз"),
    ColumnDef("direction", "Направление", default=False),
    ColumnDef("stream_number", "Поток", kind="int"),
    ColumnDef("enrolled_at", "Зачислен", kind="datetime", default=False),
    ColumnDef("last_activity_at", "Последняя активность", kind="datetime"),
    ColumnDef("deal_number", "Сделка", default=False),
)
LEARNING_PROGRESS_COLUMNS_BY_KEY: dict[str, ColumnDef] = {
    c.key: c for c in LEARNING_PROGRESS_COLUMNS
}
#: `date_field` этого вида (B1-3/B1-10) — по умолчанию период считается по активности, не по
#: заведению сделки (у заглушки `ReportSpec.date_fields` был `("created_at",)`, настоящая
#: реализация переопределяет это в `spec.py` вместе с данной задачей).
_LEARNING_PROGRESS_DATE_COLUMNS: dict[str, Any] = {
    "last_activity_at": LearningProgress.last_activity_at,
    "enrolled_at": LearningProgress.enrolled_at,
    "completed_at": LearningProgress.completed_at,
}


def _parse_progress_columns(params: dict[str, Any]) -> list[str]:
    """Как `_parse_register_columns` (B1-2), но против каталога `learning_progress` — те же
    правила: неизвестный ключ/повтор — 422 со списком допустимых, пусто/отсутствует — набор по
    умолчанию. `reporting.spec.validate_params` уже проверяет это до вызова builder'а из
    `POST /api/reports`, здесь — защита при вызове builder'а напрямую (тесты, `/data`)."""
    raw = params.get("columns")
    if raw is None or raw == []:
        return [c.key for c in LEARNING_PROGRESS_COLUMNS if c.default]
    if not isinstance(raw, list):
        raise ValidationError(
            "Параметр 'columns' должен быть списком ключей колонок",
            [FieldError(field="columns", reason="ожидается массив строк")],
        )
    valid_keys = sorted(LEARNING_PROGRESS_COLUMNS_BY_KEY)
    seen: set[str] = set()
    result: list[str] = []
    for key in raw:
        if key not in LEARNING_PROGRESS_COLUMNS_BY_KEY:
            raise ValidationError(
                f"Параметр 'columns' содержит неизвестный ключ: {key!r}",
                [
                    FieldError(
                        field="columns",
                        reason=f"неизвестный ключ: {key}; допустимо: {', '.join(valid_keys)}",
                    )
                ],
            )
        if key in seen:
            raise ValidationError(
                f"Параметр 'columns' содержит повтор ключа: {key!r}",
                [FieldError(field="columns", reason=f"повтор ключа: {key}")],
            )
        seen.add(key)
        result.append(key)
    return result


async def _learning_progress_query(
    session: AsyncSession, principal: Principal, params: dict[str, Any]
):
    """От `LearningProgress`, не от `Deal` (в отличие от остальных builder'ов этого модуля) —
    отчёт о прогрессе, не о сделках; скоуп всё равно идёт через `deal_scope_clause` (строка видна
    тому, кому видна её сделка). `INNER JOIN Deal` — строки с `deal_id IS NULL` структурно не
    бывают (`integration.lms.upsert_progress` не пишет прогресс без сделки) и не показываются,
    если встретятся (см. докстринг `build_learning_progress`, риск №1 задачи B1-10)."""
    filters = await _load_filters(session, principal, params)
    clause = await deal_scope_clause(session, principal)
    date_column = _LEARNING_PROGRESS_DATE_COLUMNS.get(
        filters.date_field, LearningProgress.last_activity_at
    )
    # Поток — `min(DealProduct.stream_number)` по паре (сделка, продукт курса), не произвольный
    # первый продукт сделки (та же проблема, что `lms.py:211-221` решает для `product_id`, здесь
    # не решается — только читается результат): коррелированный подзапрос, не JOIN, иначе сделка
    # с несколькими позициями того же продукта размножила бы строку прогресса.
    stream_subquery = (
        select(func.min(DealProduct.stream_number))
        .where(
            DealProduct.deal_id == Deal.id,
            DealProduct.product_id == LearningProgress.product_id,
        )
        .scalar_subquery()
    )

    stmt = (
        select(
            Contact.last_name,
            Contact.first_name,
            Contact.middle_name,
            Product.name,
            LearningProgress.progress_pct,
            LearningProgress.score,
            LearningProgress.completed_at,
            Organization.name,
            Direction.name,
            stream_subquery,
            LearningProgress.enrolled_at,
            LearningProgress.last_activity_at,
            Deal.number,
        )
        .select_from(LearningProgress)
        .join(Deal, Deal.id == LearningProgress.deal_id)
        .outerjoin(Contact, Contact.id == LearningProgress.contact_id)
        .outerjoin(Organization, Organization.id == Deal.organization_id)
        .outerjoin(Product, Product.id == LearningProgress.product_id)
        .outerjoin(Direction, Direction.id == Product.direction_id)
        .where(Deal.deleted_at.is_(None))
        .order_by(LearningProgress.last_activity_at.desc().nulls_last())
    )
    if clause is not None:
        stmt = stmt.where(clause)
    if filters.organization_ids:
        stmt = stmt.where(Deal.organization_id.in_(filters.organization_ids))
    if filters.owner_ids:
        stmt = stmt.where(Deal.owner_id.in_(filters.owner_ids))
    # B1-10: по `LearningProgress.product_id`/направлению ЭТОГО продукта, не по всем строкам
    # сделки (`_apply_entity_filters` фильтрует по продуктам сделки целиком — не подходит: курс
    # LMS мог быть ровно одной позицией из нескольких в сделке).
    if filters.product_ids:
        stmt = stmt.where(LearningProgress.product_id.in_(filters.product_ids))
    direction_ids = filters.direction_tree_ids or filters.direction_ids
    if direction_ids:
        stmt = stmt.where(Product.direction_id.in_(direction_ids))
    if filters.date_from is not None:
        stmt = stmt.where(date_column >= _day_start(filters.date_from, filters.tz))
    if filters.date_to is not None:
        stmt = stmt.where(
            date_column < _day_start(filters.date_to, filters.tz) + dt.timedelta(days=1)
        )
    return stmt


async def estimate_learning_progress(
    session: AsyncSession, principal: Principal, params: dict[str, Any]
) -> int:
    inner = await _learning_progress_query(session, principal, params)
    count_stmt = select(func.count()).select_from(inner.subquery())
    return (await session.execute(count_stmt)).scalar_one() or 0


async def build_learning_progress(
    session: AsyncSession, principal: Principal, params: dict[str, Any]
) -> ReportDataset:
    """B1-10: прогресс обучения из LMS. Раньше честная заглушка — ни таблица, ни приём данных не
    существовали структурно ни в одном прошедшем спринте (см. историю этого докстринга в git).
    Таблица `learning_progress` и обработчик `integration.lms.upsert_progress` с тех пор
    появились (push `POST /api/v1/integrations/lms/progress` и 30-минутный pull), отчёт просто не
    читал их — карточка в каталоге отчётов всегда показывала пустой результат с текстом
    «интеграция не реализована» даже при работающем источнике."""
    columns = _parse_progress_columns(params)
    limit = _parse_int(params, "limit", default=1000, minimum=1, maximum=5000)
    stmt = (await _learning_progress_query(session, principal, params)).limit(limit)
    result = (await session.execute(stmt)).all()

    rows: list[list[Any]] = []
    progress_values: list[int] = []
    completed_count = 0
    for (
        last_name,
        first_name,
        middle_name,
        product_name,
        progress_pct,
        score,
        completed_at,
        organization_name,
        direction_name,
        stream_number,
        enrolled_at,
        last_activity_at,
        deal_number,
    ) in result:
        # «У B2B-сделки без контакта — "—"» (B1-10, п.2): учащийся физлицо (`Contact`), не сама
        # сделка — у b2b-сделки с вузом-организацией контакта может не быть вовсе.
        if last_name:
            student = mask_name(f"{last_name} {first_name} {middle_name or ''}".strip())
        else:
            student = "—"
        full_row = {
            "student": student,
            "product": product_name or "—",
            "progress_pct": progress_pct,
            "score": float(score) if score is not None else None,
            "completed_at": completed_at,
            "organization": organization_name or "—",
            "direction": direction_name or "—",
            "stream_number": stream_number,
            "enrolled_at": enrolled_at,
            "last_activity_at": last_activity_at,
            "deal_number": deal_number,
        }
        rows.append([full_row[key] for key in columns])
        if progress_pct is not None:
            progress_values.append(progress_pct)
        if completed_at is not None:
            completed_count += 1

    notes: list[str] = []
    if rows:
        avg_progress = round(sum(progress_values) / len(progress_values)) if progress_values else 0
        completed_pct = round(completed_count / len(rows) * 100)
        notes.append(
            f"Записей LMS: {len(rows)}, средний прогресс {avg_progress}%, "
            f"завершили {completed_count} ({completed_pct}%)"
        )
    else:
        # Пустой результат — у ЭТОГО запроса (фильтры/скоуп), не у таблицы целиком: другой
        # пользователь или период мог бы увидеть строки. «Источник не настроен» показывается
        # только когда данных нет вообще ни у кого (B1-10, п.4).
        any_rows_exist = await session.scalar(select(func.count(LearningProgress.id))) or 0
        if any_rows_exist == 0:
            notes.append(
                "Данные о прогрессе из LMS не поступали: проверьте источник lms в разделе "
                "интеграций и LMS_BASE_URL"
            )

    total_rows: int | None = None
    truncated = False
    if len(rows) == limit:
        total_rows = await estimate_learning_progress(session, principal, params)
        if total_rows > limit:
            truncated = True
            notes.append(_truncation_note(limit, total_rows))

    return ReportDataset(
        title="Прогресс обучения",
        columns=[LEARNING_PROGRESS_COLUMNS_BY_KEY[key].title for key in columns],
        rows=rows,
        note="; ".join(notes) or None,
        keys=list(columns),
        kinds=[LEARNING_PROGRESS_COLUMNS_BY_KEY[key].kind for key in columns],
        total_rows=total_rows,
        truncated=truncated,
        # B1-8: график корзин прогресса (`reporting.charts.build_progress_figure`) должен
        # видеть `progress_pct` каждой строки независимо от того, выбрана ли эта колонка в
        # `columns` — иначе ограничение B1-2 на показ колонок молча ограничило бы и график.
        extra={"progress_values": progress_values},
    )


# =============================================================================
# 9. Выгрузка учащихся в шаблон LMS «Загрузка пользователей»
# =============================================================================

LMS_USERS_UPLOAD = "lms_users_upload"

# Этапы воронки физлиц, на которых человек уже оплатил обучение и ему нужна учётная запись в LMS:
# «Оплата и договор оферты» и «Зачисление в LMS».
LMS_DEFAULT_STATUS_CODES = ("payment_contract", "lms_enrollment")

# Виды отчёта с ПДн целиком (СНИЛС, паспорт, адрес): отдаются только файлом с записью аудита
# `REPORT_EXPORTED`, а не JSON-ом через `GET /reports/{id}/data`, у которого аудита нет.
FILE_ONLY_REPORTS: frozenset[str] = frozenset({LMS_USERS_UPLOAD})

_INT32_MAX = 2_147_483_647
_NON_DIGITS = re.compile(r"\D+")


def _parse_positive_int(params: dict[str, Any], key: str) -> int | None:
    """Целое от 1 (номер потока). В отличие от `_parse_int`, не «подрезает» значение до границ:
    поток `-5` — ошибка запроса, а не поток №1."""
    raw = params.get(key)
    if raw is None or raw == "":
        return None
    try:
        # `True` для `int()` — единица, но номером потока не является.
        if isinstance(raw, bool):
            raise ValueError
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            f"Параметр {key!r} должен быть целым числом от 1",
            [FieldError(field=key, reason="ожидается целое число от 1")],
        ) from exc
    if not 1 <= value <= _INT32_MAX:
        raise ValidationError(
            f"Параметр {key!r} должен быть целым числом от 1",
            [FieldError(field=key, reason="ожидается целое число от 1")],
        )
    return value


def _parse_status_codes(params: dict[str, Any]) -> list[str]:
    """Коды статусов воронки: JSON-массив либо CSV-строка (как `_parse_uuid_list`). Пусто — статусы
    по умолчанию; неизвестный код не ошибка, просто ни одна сделка в нём не найдётся."""
    raw = params.get("status_codes")
    if raw is None or raw == "" or raw == []:
        return list(LMS_DEFAULT_STATUS_CODES)
    if isinstance(raw, str):
        items: list[Any] = raw.split(",")
    elif isinstance(raw, list | tuple):
        items = list(raw)
    else:
        raise ValidationError(
            "Параметр 'status_codes' должен быть списком кодов статусов",
            [FieldError(field="status_codes", reason="ожидается массив строк")],
        )
    if not all(isinstance(item, str) for item in items):
        raise ValidationError(
            "Параметр 'status_codes' должен быть списком кодов статусов",
            [FieldError(field="status_codes", reason="ожидается массив строк")],
        )
    codes = list(dict.fromkeys(item.strip() for item in items if item.strip()))
    if any(len(code) > 64 for code in codes):
        raise ValidationError(
            "Код статуса не длиннее 64 символов",
            [FieldError(field="status_codes", reason="слишком длинный код статуса")],
        )
    return codes or list(LMS_DEFAULT_STATUS_CODES)


def _phone_number(phone: str | None) -> int | None:
    """`+79990234365` -> `79990234365`: в шаблоне LMS телефон — число без плюса. E.164 — до 15 цифр,
    целое такой длины Excel хранит без потери точности."""
    digits = _NON_DIGITS.sub("", phone or "")
    return int(digits) if digits else None


def _lms_profile_cell(target: str, value: Any) -> Any:
    """Значение профиля в виде, который ждёт шаблон: СНИЛС `112-233-445 95`, пол `М`/`Ж`,
    образование — текст из выпадающего списка «Лист2». Даты остаются датами (формат ячейки задаёт
    рендер), остальное — текст."""
    if value is None or value == "":
        return None
    if target == "snils":
        return learner.format_snils(value)
    if target == "sex":
        return learner.SEX_LABELS.get(value)
    if target == "education":
        return learner.EDUCATION_LABELS.get(value)
    return value


def _lms_users_query(clause: Any, params: dict[str, Any]) -> Any:
    """Контакты B2C-сделок в скоупе принципала: по одной строке на человека, сколько бы сделок у
    него ни подходило под фильтры. Профиль присоединяется слева: у оплатившего, чьи данные ещё не
    собраны, в шаблоне заполнены только имя, телефон и email."""
    date_from, date_to = _parse_date_range(params)
    product_id = _parse_uuid(params, "product_id")
    stream_number = _parse_positive_int(params, "stream_number")
    status_codes = _parse_status_codes(params)

    deals = (
        select(Deal.contact_id)
        .join(WorkflowStatus, WorkflowStatus.id == Deal.status_id)
        .where(
            Deal.deleted_at.is_(None),
            Deal.deal_type == "b2c",
            Deal.contact_id.is_not(None),
            WorkflowStatus.code.in_(status_codes),
        )
    )
    if clause is not None:
        deals = deals.where(clause)
    if date_from is not None:
        deals = deals.where(Deal.created_at >= _day_start(date_from, dt.UTC))
    if date_to is not None:
        deals = deals.where(Deal.created_at < _day_start(date_to, dt.UTC) + dt.timedelta(days=1))
    if product_id is not None or stream_number is not None:
        # Продукт и поток — об одной и той же строке сделки: «курс А, поток 2», а не «курс А» у
        # одной позиции и «поток 2» у другой.
        lines = select(DealProduct.id).where(DealProduct.deal_id == Deal.id)
        if product_id is not None:
            lines = lines.where(DealProduct.product_id == product_id)
        if stream_number is not None:
            lines = lines.where(DealProduct.stream_number == stream_number)
        deals = deals.where(lines.exists())

    return (
        select(Contact, ContactLearnerProfile)
        .outerjoin(ContactLearnerProfile, ContactLearnerProfile.contact_id == Contact.id)
        .where(
            Contact.id.in_(deals),
            Contact.deleted_at.is_(None),
            Contact.is_anonymized.is_(False),
        )
        .order_by(Contact.last_name, Contact.first_name, Contact.id)
    )


async def build_lms_users_upload(
    session: AsyncSession, principal: Principal, params: dict[str, Any]
) -> ReportDataset:
    """Строки шаблона LMS: 30 колонок `catalog.learner.LMS_USER_COLUMNS` в порядке шаблона.

    Параметры: `product_id` и `stream_number` (курс и поток), `status_codes` (по умолчанию
    «Оплата и договор оферты» и «Зачисление в LMS»), `date_from`/`date_to` (по дате создания
    сделки, включительно). Значения отдаются в типах шаблона — телефон числом, даты датами, СНИЛС и
    паспорт текстом, — а как их положить в ячейки, решает `rendering.render_lms_users_xlsx`."""
    clause = await deal_scope_clause(session, principal)
    stmt = _lms_users_query(clause, params)

    rows: list[list[Any]] = []
    for contact, profile in (await session.execute(stmt)).all():
        values: dict[str, Any] = {
            "last_name": contact.last_name,
            "first_name": contact.first_name,
            "middle_name": contact.middle_name,
            "phone": _phone_number(contact.phone),
            "email": contact.email,
        }
        if profile is not None:
            for target in learner.PROFILE_TARGETS:
                values[target] = _lms_profile_cell(target, getattr(profile, target))
        rows.append([values.get(target) for target, _header in learner.LMS_USER_COLUMNS])

    return ReportDataset(
        title="Загрузка пользователей",
        columns=[header for _target, header in learner.LMS_USER_COLUMNS],
        rows=rows,
    )


# =============================================================================
# 10. Рейтинг востребованности ИТ-продуктов и направлений
# =============================================================================

#: Нулевой UUID — ключ группировки «без направления» (B1-9): `GROUP BY COALESCE(direction_id,
#: …)` не может группировать по `NULL` как по обычному значению (все NULL в Postgres различны
#: для `DISTINCT`/`GROUP BY` с точки зрения формальной семантики сравнения, но `GROUP BY` на
#: практике схлопывает их в одну группу — в отличие от уникальных индексов эта группа одна,
#: поэтому константа здесь не для корректности группировки, а чтобы ключом группы был конкретный
#: сравнимый `UUID`, а не `None`, с которым неудобно работать в Python после `fetchall()`).
_NO_DIRECTION_KEY = uuid.UUID(int=0)
_NO_DIRECTION_LABEL = "Без направления"

_PRODUCT_DEMAND_SORT_KEYS: frozenset[str] = frozenset(
    {"index", "applications", "learners", "streams"}
)
_DEMAND_METRIC_LABELS: dict[str, str] = {
    "applications": "Заявки",
    "learners": "Обучающиеся",
    "streams": "Потоки",
}


def _assign_ranks(rows: list[dict[str, Any]], key: str) -> None:
    """RANK по `key` (B1-9) — равные значения делят место, следующее за ничьей идёт через
    столько позиций, сколько строк в ничьей (1,2,2,4, не 1,2,2,3 — та же семантика, что SQL
    `RANK()`). Мутирует `rows`, записывая/перезаписывая `"rank"`."""
    order = sorted({row[key] for row in rows}, reverse=True)
    rank_by_value = {value: position + 1 for position, value in enumerate(order)}
    for row in rows:
        row["rank"] = rank_by_value[row[key]]


def compute_demand_index(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Индекс востребованности (B1-9: раздел «Цель» ТЗ — ранжирование по заявкам на обучение,
    числу обучающихся и параллельных потоков). Решение B задачи (сравнение вариантов формулы
    ранга в документе спринта): среднее арифметическое долей позиции в сумме каждого из трёх
    показателей по всей выборке — равные веса, формула видна в файле, без произвольных
    коэффициентов. Показатель, чья сумма по выборке равна нулю (например, ни у одной позиции не
    указан номер потока), не входит в среднее — 0/0 было бы неопределённостью, а не нулём, и
    молча искажало бы доли остальных показателей. Входные `rows` не мутируются — возвращаются
    новые словари с добавленными `index`/`rank`, отсортированные по индексу по убыванию (ничья —
    одно место), и список метрик, исключённых из среднего (для `note`). Не трогает БД/сессию,
    тестируется отдельно от SQL."""
    metrics = ("applications", "learners", "streams")
    totals = {metric: sum(row[metric] for row in rows) for metric in metrics}
    active_metrics = [metric for metric in metrics if totals[metric] > 0]
    excluded_metrics = [metric for metric in metrics if metric not in active_metrics]

    enriched: list[dict[str, Any]] = []
    for row in rows:
        shares = [row[metric] / totals[metric] for metric in active_metrics]
        index = round(sum(shares) / len(shares) * 100, 1) if shares else 0.0
        enriched.append({**row, "index": index})

    enriched.sort(key=lambda r: r["index"], reverse=True)
    _assign_ranks(enriched, "index")
    return enriched, excluded_metrics


async def _demand_rows(
    session: AsyncSession, principal: Principal, params: dict[str, Any], *, level: str
) -> list[dict[str, Any]]:
    """Строки-кандидаты до ранжирования (B1-9) — от `DealProduct` (строка сделки), не от `Deal`:
    востребованность считается по продукту/направлению, который Deal.id с несколькими позициями
    дал бы несколько строк-кандидатов (по одной на свой продукт), каждая со своим вкладом в
    заявки/обучающихся/потоки СВОЕГО продукта — тот же принцип зерна, что `deal_register`
    (B1-1/B1-2) применяет к зерну "product". Фильтры по продукту/направлению ограничивают
    строки ЭТОГО запроса напрямую (`Product.id`/`Product.direction_id`, уже присоединённые),
    не через `_apply_entity_filters` (та ограничивала бы, какие СДЕЛКИ попадают, а не какие
    строки продукта/направления показаны — разница ровно того рода, что была багом BUG-1)."""
    filters = await _load_filters(session, principal, params)
    clause = await deal_scope_clause(session, principal)
    date_column = _DATE_FIELD_COLUMNS.get(filters.date_field, Deal.created_at)
    direction_ids = filters.direction_tree_ids or filters.direction_ids

    applications = func.count(func.distinct(DealProduct.deal_id))
    won = func.count(func.distinct(DealProduct.deal_id)).filter(
        WorkflowStatus.type == StatusType.WON.value
    )
    learners = func.coalesce(
        func.sum(DealProduct.quantity).filter(WorkflowStatus.type != StatusType.LOST.value), 0
    )

    def _apply_filters(stmt: Any) -> Any:
        stmt = stmt.where(Deal.deleted_at.is_(None))
        if clause is not None:
            stmt = stmt.where(clause)
        if filters.organization_ids:
            stmt = stmt.where(Deal.organization_id.in_(filters.organization_ids))
        if filters.owner_ids:
            stmt = stmt.where(Deal.owner_id.in_(filters.owner_ids))
        if filters.date_from is not None:
            stmt = stmt.where(date_column >= _day_start(filters.date_from, filters.tz))
        if filters.date_to is not None:
            stmt = stmt.where(
                date_column < _day_start(filters.date_to, filters.tz) + dt.timedelta(days=1)
            )
        if filters.product_ids:
            stmt = stmt.where(Product.id.in_(filters.product_ids))
        if direction_ids:
            stmt = stmt.where(Product.direction_id.in_(direction_ids))
        return stmt

    base = (
        select()
        .select_from(DealProduct)
        .join(Deal, Deal.id == DealProduct.deal_id)
        .join(WorkflowStatus, WorkflowStatus.id == Deal.status_id)
        .join(Product, Product.id == DealProduct.product_id)
        .outerjoin(Direction, Direction.id == Product.direction_id)
    )

    if level == "product":
        streams = func.count(func.distinct(DealProduct.stream_number)).filter(
            WorkflowStatus.type != StatusType.LOST.value,
            DealProduct.stream_number.is_not(None),
        )
        stmt = _apply_filters(
            base.with_only_columns(
                Product.id, Product.name, Direction.name, applications, won, learners, streams
            ).group_by(Product.id, Product.name, Direction.name)
        )
        rows = (await session.execute(stmt)).all()
        return [
            {
                "key": str(product_id),
                "title": product_name,
                "direction": direction_name or "—",
                "applications": applications_v,
                "won": won_v,
                "learners": int(learners_v),
                "streams": streams_v,
            }
            for (
                product_id,
                product_name,
                direction_name,
                applications_v,
                won_v,
                learners_v,
                streams_v,
            ) in rows
        ]

    # level == "direction": потоки — различные пары (продукт, поток), не просто различные номера
    # потока (два разных продукта могли случайно получить один и тот же номер).
    direction_key = func.coalesce(Product.direction_id, _NO_DIRECTION_KEY)
    streams = func.count(
        func.distinct(func.concat(DealProduct.product_id, ":", DealProduct.stream_number))
    ).filter(WorkflowStatus.type != StatusType.LOST.value, DealProduct.stream_number.is_not(None))
    product_count = func.count(func.distinct(Product.id))
    stmt = _apply_filters(
        base.with_only_columns(
            direction_key, Direction.name, product_count, applications, won, learners, streams
        ).group_by(direction_key, Direction.name)
    )
    rows = (await session.execute(stmt)).all()
    return [
        {
            "key": str(direction_key_v),
            "title": direction_name or _NO_DIRECTION_LABEL,
            "product_count": product_count_v,
            "applications": applications_v,
            "won": won_v,
            "learners": int(learners_v),
            "streams": streams_v,
        }
        for (
            direction_key_v,
            direction_name,
            product_count_v,
            applications_v,
            won_v,
            learners_v,
            streams_v,
        ) in rows
    ]


def _demand_note(
    *, totals: dict[str, int], excluded_metrics: list[str], shown: int, total_entities: int
) -> str:
    parts = [
        "Всего: заявок {applications}, обучающихся {learners}, потоков {streams}".format(**totals)
    ]
    if excluded_metrics:
        labels = ", ".join(_DEMAND_METRIC_LABELS[metric] for metric in excluded_metrics)
        parts.append(f"{labels} не учтены")
    if shown < total_entities:
        parts.append(f"Показано {shown} из {total_entities}")
    return "; ".join(parts)


async def build_product_demand(
    session: AsyncSession, principal: Principal, params: dict[str, Any]
) -> ReportDataset:
    """B1-9 (раздел «Цель» ТЗ): «Ранжирование происходит на основе востребованности программы,
    которое становится очевидным по статистическим данным: заявки на обучение, количество
    обучающихся, количество параллельных потоков» — отчёта под этот смысл системы не было ни у
    одного builder'а раздела 4.13."""
    level = params.get("level") or "product"
    if level not in ("product", "direction"):
        raise ValidationError(
            "Параметр 'level' должен быть 'product' или 'direction'",
            [FieldError(field="level", reason="допустимо: product, direction")],
        )
    top = _parse_int(params, "top", default=20, minimum=1, maximum=100)
    sort_by = params.get("sort_by") or "index"
    if sort_by not in _PRODUCT_DEMAND_SORT_KEYS:
        raise ValidationError(
            f"Параметр 'sort_by' должен быть одним из: "
            f"{', '.join(sorted(_PRODUCT_DEMAND_SORT_KEYS))}",
            [FieldError(field="sort_by", reason="недопустимое значение")],
        )

    raw_rows = await _demand_rows(session, principal, params, level=level)
    ranked, excluded_metrics = compute_demand_index(raw_rows)
    if sort_by != "index":
        ranked.sort(key=lambda r: r[sort_by], reverse=True)
        _assign_ranks(ranked, sort_by)

    total_entities = len(ranked)
    shown_rows = ranked[:top]
    totals = {
        metric: sum(r[metric] for r in ranked) for metric in ("applications", "learners", "streams")
    }
    note = _demand_note(
        totals=totals,
        excluded_metrics=excluded_metrics,
        shown=len(shown_rows),
        total_entities=total_entities,
    )

    if level == "product":
        columns = [
            "Место",
            "Продукт",
            "Направление",
            "Заявок",
            "Выиграно",
            "Обучающихся",
            "Потоков",
            "Индекс, %",
        ]
        rows = [
            [
                r["rank"],
                r["title"],
                r["direction"],
                r["applications"],
                r["won"],
                r["learners"],
                r["streams"],
                r["index"],
            ]
            for r in shown_rows
        ]
    else:
        columns = [
            "Место",
            "Направление",
            "Продуктов",
            "Заявок",
            "Выиграно",
            "Обучающихся",
            "Потоков",
            "Индекс, %",
        ]
        rows = [
            [
                r["rank"],
                r["title"],
                r["product_count"],
                r["applications"],
                r["won"],
                r["learners"],
                r["streams"],
                r["index"],
            ]
            for r in shown_rows
        ]

    kinds = (
        ["int", "text", "int", "int", "int", "int", "int", "pct"]
        if level == "direction"
        else ["int", "text", "text", "int", "int", "int", "int", "pct"]
    )
    return ReportDataset(
        title="Рейтинг востребованности ИТ-продуктов и направлений",
        columns=columns,
        rows=rows,
        note=note or None,
        kinds=kinds,
    )


# =============================================================================
# 11. Реестр лицензий
# =============================================================================

#: Каталог колонок `license_register` (B1-11) — все десять полей ТЗ §4 п.1 по умолчанию, в
#: порядке таблицы задачи; «Срок» (не по умолчанию) — производная колонка, расчёт в builder'е.
LICENSE_REGISTER_COLUMNS: tuple[ColumnDef, ...] = (
    ColumnDef("organization", "Вуз"),
    ColumnDef("vendor", "Вендор"),
    ColumnDef("product_name", "ПО"),
    ColumnDef("contract_number", "Номер договора"),
    ColumnDef("is_signed", "Подписание лицензии"),
    ColumnDef("license_valid_year", "Срок действия, год", kind="int"),
    ColumnDef("transfer_status", "Статус по передаче"),
    ColumnDef("manager_full_name", "ФИО менеджера"),
    ColumnDef("responsible_contacts", "Ответственные от вуза"),
    ColumnDef("comment", "Комментарий"),
    ColumnDef("validity", "Срок", default=False),
)
LICENSE_REGISTER_COLUMNS_BY_KEY: dict[str, ColumnDef] = {c.key: c for c in LICENSE_REGISTER_COLUMNS}
_LICENSE_TRANSFER_STATUS_VALUES: frozenset[str] = frozenset(s.value for s in LicenseTransferStatus)


def _parse_license_columns(params: dict[str, Any]) -> list[str]:
    """Как `_parse_register_columns`/`_parse_progress_columns` — тот же приём 422 со списком
    допустимых ключей, против каталога `license_register`."""
    raw = params.get("columns")
    if raw is None or raw == []:
        return [c.key for c in LICENSE_REGISTER_COLUMNS if c.default]
    if not isinstance(raw, list):
        raise ValidationError(
            "Параметр 'columns' должен быть списком ключей колонок",
            [FieldError(field="columns", reason="ожидается массив строк")],
        )
    valid_keys = sorted(LICENSE_REGISTER_COLUMNS_BY_KEY)
    seen: set[str] = set()
    result: list[str] = []
    for key in raw:
        if key not in LICENSE_REGISTER_COLUMNS_BY_KEY:
            raise ValidationError(
                f"Параметр 'columns' содержит неизвестный ключ: {key!r}",
                [
                    FieldError(
                        field="columns",
                        reason=f"неизвестный ключ: {key}; допустимо: {', '.join(valid_keys)}",
                    )
                ],
            )
        if key in seen:
            raise ValidationError(
                f"Параметр 'columns' содержит повтор ключа: {key!r}",
                [FieldError(field="columns", reason=f"повтор ключа: {key}")],
            )
        seen.add(key)
        result.append(key)
    return result


def _parse_string_list(params: dict[str, Any], key: str) -> list[str] | None:
    """Как `_parse_uuid_list`, но без разбора в UUID — список названий вендоров (B1-11), где
    значение само текст, не идентификатор."""
    raw = params.get(key)
    if raw is None or raw == "" or raw == []:
        return None
    if isinstance(raw, str):
        items = [item.strip() for item in raw.split(",") if item.strip()]
    elif isinstance(raw, list | tuple):
        items = [str(item) for item in raw]
    else:
        raise ValidationError(
            f"Параметр {key!r} должен быть списком строк",
            [FieldError(field=key, reason="ожидается массив строк")],
        )
    return items or None


def _parse_transfer_statuses(params: dict[str, Any]) -> list[str] | None:
    raw = _parse_string_list(params, "transfer_statuses")
    if raw is None:
        return None
    unknown = [code for code in raw if code not in _LICENSE_TRANSFER_STATUS_VALUES]
    if unknown:
        raise ValidationError(
            f"Параметр 'transfer_statuses' содержит неизвестный код: {unknown[0]!r}",
            [
                FieldError(
                    field="transfer_statuses",
                    reason=f"допустимо: {', '.join(sorted(_LICENSE_TRANSFER_STATUS_VALUES))}",
                )
            ],
        )
    return raw


def _license_validity_label(valid_year: int | None, *, current_year: int) -> str:
    """«Срок» (B1-11, доп. колонка): по `license_valid_year` и текущему году в поясе
    пользователя — сам по себе год ничего не говорит о статусе лицензии без сравнения с «сейчас»."""
    if valid_year is None:
        return "—"
    if valid_year < current_year:
        return "Истёк"
    if valid_year == current_year:
        return "Истекает в этом году"
    return "Действует"


async def estimate_license_register(
    session: AsyncSession, principal: Principal, params: dict[str, Any]
) -> int:
    inner, _tz = await _license_register_query(session, principal, params)
    count_stmt = select(func.count()).select_from(inner.subquery())
    return (await session.execute(count_stmt)).scalar_one() or 0


async def _license_register_query(
    session: AsyncSession, principal: Principal, params: dict[str, Any]
) -> tuple[Any, dt.tzinfo]:
    """Скоуп — `organization_scope_clause` (B3, `catalog.service`), та же проверка прав, что уже
    применяет `OrganizationLicenseService.list_query` к API списка лицензий. Общая выборка не
    вынесена в отдельную публичную функцию каталога (полный план задачи предлагал
    `catalog.service.license_rows_query`) — в этом черновике отчёт строит свой запрос
    самостоятельно, тем же приёмом, что и остальные builder'ы этого модуля; расхождение в
    поведении исключено проверкой `tests/test_reporting_licenses.py` против живого скоупа."""
    clause = await organization_scope_clause(session, principal)
    tz = await _resolve_user_timezone(session, principal)
    date_from, date_to = _parse_date_range(params)
    date_field = params.get("date_field") or "license_signed_at"
    organization_ids = _parse_uuid_list(params, "organization_ids")
    vendors = _parse_string_list(params, "vendors")
    transfer_statuses = _parse_transfer_statuses(params)
    valid_year = _parse_positive_int(params, "valid_year")
    expired = bool(params.get("expired", False))

    stmt = (
        select(OrganizationLicense, Organization.name, Organization.short_name)
        .select_from(OrganizationLicense)
        .join(Organization, Organization.id == OrganizationLicense.organization_id)
        .where(OrganizationLicense.deleted_at.is_(None))
        .order_by(Organization.name, OrganizationLicense.created_at.desc())
    )
    if clause is not None:
        stmt = stmt.where(clause)
    if organization_ids:
        stmt = stmt.where(OrganizationLicense.organization_id.in_(organization_ids))
    if vendors:
        stmt = stmt.where(OrganizationLicense.vendor.in_(vendors))
    if transfer_statuses:
        stmt = stmt.where(OrganizationLicense.transfer_status.in_(transfer_statuses))
    if valid_year is not None:
        stmt = stmt.where(OrganizationLicense.license_valid_year == valid_year)
    if expired:
        current_year = dt.datetime.now(tz).year
        stmt = stmt.where(OrganizationLicense.license_valid_year < current_year)
    # `license_signed_at` — `Date`, без часового пояса (день подписания, не момент времени);
    # `created_at` — `DateTime(timezone=True)`, границы суток считаются в поясе пользователя
    # (тот же приём, что `_apply_deal_filters`/`_day_start`, B1-3).
    if date_field == "created_at":
        if date_from is not None:
            stmt = stmt.where(OrganizationLicense.created_at >= _day_start(date_from, tz))
        if date_to is not None:
            stmt = stmt.where(
                OrganizationLicense.created_at < _day_start(date_to, tz) + dt.timedelta(days=1)
            )
    else:
        if date_from is not None:
            stmt = stmt.where(OrganizationLicense.license_signed_at >= date_from)
        if date_to is not None:
            stmt = stmt.where(OrganizationLicense.license_signed_at <= date_to)
    return stmt, tz


async def build_license_register(
    session: AsyncSession, principal: Principal, params: dict[str, Any]
) -> ReportDataset:
    """B1-11 (ТЗ §4 п.1): реестр из десяти полей каталога вуз-вендор-ПО. ФИО менеджера и
    ответственные от вуза маскируются без права `contact:reveal` — тот же принцип, что
    `OrganizationLicenseOut.from_model` уже применяет к API списка лицензий (B3-6)."""
    columns = _parse_license_columns(params)
    limit = _parse_int(params, "limit", default=1000, minimum=1, maximum=5000)
    reveal = has_permission(principal.role, Permission.CONTACT_REVEAL)

    stmt, tz = await _license_register_query(session, principal, params)
    result = (await session.execute(stmt.limit(limit))).all()
    current_year = dt.datetime.now(tz).year

    rows: list[list[Any]] = []
    # B1-8/B1-11: график `license_status` (`reporting.charts.build_license_status_figure`)
    # считает по статусу передачи и году окончания независимо от `columns` — строится здесь, до
    # выбора колонок, тем же приёмом, что `learning_progress.extra["progress_values"]`.
    status_counts: dict[str, int] = {}
    year_counts: dict[str, int] = {}
    for license_, organization_name, organization_short_name in result:
        manager_name = license_.manager_full_name
        responsible = license_.responsible_contacts
        if not reveal:
            manager_name = mask_name(manager_name)
            responsible = mask_contacts_text(responsible)
        status_label = (
            TRANSFER_STATUS_LABELS.get(license_.transfer_status, license_.transfer_status)
            if license_.transfer_status
            else "—"
        )
        status_counts[status_label] = status_counts.get(status_label, 0) + 1
        year_label = (
            str(license_.license_valid_year) if license_.license_valid_year else "Без срока"
        )
        year_counts[year_label] = year_counts.get(year_label, 0) + 1
        full_row = {
            # `Organization.name` — то же поле, что показывают остальные отчёты этого модуля
            # (deal_register и т.п.), не `short_name` (B3-6 вводит его только для API списка
            # лицензий, где короче удобнее в таблице интерфейса).
            "organization": organization_name or organization_short_name or "—",
            "vendor": license_.vendor,
            "product_name": license_.product_name,
            "contract_number": license_.contract_number or "",
            "is_signed": (
                "—" if license_.is_signed is None else ("Да" if license_.is_signed else "Нет")
            ),
            "license_valid_year": license_.license_valid_year,
            "transfer_status": status_label,
            "manager_full_name": manager_name or "",
            "responsible_contacts": responsible or "",
            "comment": license_.comment or "",
            "validity": _license_validity_label(
                license_.license_valid_year, current_year=current_year
            ),
        }
        rows.append([full_row[key] for key in columns])

    total_rows: int | None = None
    truncated = False
    note: str | None = None
    if len(rows) == limit:
        total_rows = await estimate_license_register(session, principal, params)
        if total_rows > limit:
            truncated = True
            note = _truncation_note(limit, total_rows)

    return ReportDataset(
        title="Реестр лицензий",
        columns=[LICENSE_REGISTER_COLUMNS_BY_KEY[key].title for key in columns],
        rows=rows,
        note=note,
        keys=list(columns),
        kinds=[LICENSE_REGISTER_COLUMNS_BY_KEY[key].kind for key in columns],
        total_rows=total_rows,
        truncated=truncated,
        extra={"by_status": status_counts, "by_year": year_counts},
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
    "deal_register": build_deal_register,
    "learning_progress": build_learning_progress,
    LMS_USERS_UPLOAD: build_lms_users_upload,
    "product_demand": build_product_demand,
    "license_register": build_license_register,
}

#: Отсутствие ключа = отчёт всегда лёгкий (см. докстринг модуля).
REPORT_ESTIMATORS: dict[str, ReportEstimator] = {
    "stuck_deals": estimate_stuck_deals,
    "deal_register": estimate_deal_register,
    # B1-10: тоже построчный листинг (не агрегат) — число записей LMS не ограничено структурно.
    "learning_progress": estimate_learning_progress,
    # B1-11: листинг лицензий, тоже потенциально много строк у крупного заказчика.
    "license_register": estimate_license_register,
}
