"""Витрина демо-данных, которую нельзя положить через API (QA 20.10: R-7, R-12, R-18, W-2).

Сид через API (`frontend/tools/seed-demo.mjs`, копия в `deploy/seed`) создаёт сделки «сейчас»: у
всех один и тот же момент перехода, поэтому на демо:

* «Воронка по статусам» — среднее время в статусе 0, «Успешно закрыта» 0 (R-12);
* «Подписание по периодам» — медиана часов до подписания 0, «Сводка по КАМам» — цикл 0 дней (R-18);
* SLA — либо «все в норме», либо (при коротких сроках сида) «просрочено 34» (W-2);
* «Прогресс обучения» — пусто, потому что LMS на демо ничего не присылала (R-7).

Этот модуль растягивает историю демо-сделок во времени, переносит две сделки основной воронки в
«Успешно закрыта», раскладывает SLA (4 просрочены, 3 «истекает», остальное в норме), растягивает
время подписания документов и заводит строки прогресса обучения у физлиц на LMS-этапах.

Запуск — под суперпользователем БД (история переходов неизменяема для роли приложения):

    docker compose run --rm --no-deps --entrypoint python migrate -m app.mocks.demo_showcase

Только профиль `demo`; трогает ТОЛЬКО сделки с `source='demo'` (их заводит сид). Повторный запуск
ничего не меняет: признак — настройка `demo.showcase.v1` (флаг `--force` применяет заново).
Рассылок не вызывает: состояния SLA выставляются сразу такими, какими их посчитал бы воркер, а у
просроченных сразу стоит отметка эскалации.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import sys
import uuid
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.logging import configure_logging
from app.modules.admin.models import SystemSetting
from app.modules.catalog.models import Product
from app.modules.crm.models import Deal, DealProduct, DealStatusHistory
from app.modules.integration.lms import upsert_progress
from app.modules.signing.models import SignatureDocument, SignatureRequest
from app.modules.workflow.models import StatusType, Workflow, WorkflowStatus

logger = structlog.get_logger(__name__)

MARKER = "demo.showcase.v1"
#: Сколько сделок основной B2B-воронки переводим в «Успешно закрыта».
WON_TARGET = 2
#: Сколько открытых сделок получают просрочку SLA / предупреждение.
BREACHED_COUNT = 4
WARNING_COUNT = 3


def _h(*parts: object) -> int:
    digest = hashlib.sha1("|".join(str(p) for p in parts).encode(), usedforsecurity=False)
    return int.from_bytes(digest.digest()[:6], "big")


def _unit(*parts: object) -> float:
    """Детерминированное число из [0, 1)."""
    return (_h(*parts) % 10_000) / 10_000


def _sla_state(elapsed: dt.timedelta, total: dt.timedelta) -> str:
    fraction = elapsed / total
    if fraction >= 1.0:
        return "breached"
    if fraction >= 0.75:
        return "warning"
    return "ok"


async def _stretch_deals(
    session: Any, now: dt.datetime, *, main_workflow_id: uuid.UUID | None = None
) -> dict[str, int]:
    deals = list(
        (
            await session.execute(
                select(Deal)
                .where(Deal.source == "demo", Deal.deleted_at.is_(None))
                .order_by(Deal.number)
            )
        )
        .scalars()
        .all()
    )
    statuses: dict[uuid.UUID, WorkflowStatus] = {
        s.id: s for s in (await session.execute(select(WorkflowStatus))).scalars().all()
    }
    default_b2b = main_workflow_id or await session.scalar(
        select(Workflow.id).where(
            Workflow.deal_type == "b2b",
            Workflow.is_default.is_(True),
            Workflow.state == "published",
        )
    )
    counters = {"deals": 0, "won": 0, "breached": 0, "warning": 0}

    def _open(deal: Deal) -> bool:
        return deal.closed_at is None and statuses[deal.status_id].type != StatusType.PARKED.value

    # --- две сделки основной воронки закрываем успешно ------------------------------------
    won_status = None
    if default_b2b is not None:
        won_status = next(
            (
                s
                for s in statuses.values()
                if s.workflow_id == default_b2b and s.type == StatusType.WON.value
            ),
            None,
        )
    won_ids: set[uuid.UUID] = set()
    already_won = sum(
        1
        for d in deals
        if d.workflow_id == default_b2b and statuses[d.status_id].type == StatusType.WON.value
    )
    if won_status is not None and already_won < WON_TARGET:
        candidates = [
            d
            for d in deals
            if d.workflow_id == default_b2b
            and _open(d)
            and statuses[d.status_id].type == StatusType.INTERMEDIATE.value
        ]
        candidates.sort(key=lambda d: (-statuses[d.status_id].sort_order, d.number))
        for deal in candidates[: WON_TARGET - already_won]:
            won_ids.add(deal.id)

    # --- кто просрочен / под угрозой ------------------------------------------------------
    timed = [d for d in deals if _open(d) and d.sla_due_at is not None and d.id not in won_ids]
    timed.sort(key=lambda d: _h(d.number, "sla"))
    breached_ids = {d.id for d in timed[:BREACHED_COUNT]}
    warning_ids = {d.id for d in timed[BREACHED_COUNT : BREACHED_COUNT + WARNING_COUNT]}

    for deal in deals:
        history = list(
            (
                await session.execute(
                    select(DealStatusHistory)
                    .where(DealStatusHistory.deal_id == deal.id)
                    .order_by(DealStatusHistory.changed_at, DealStatusHistory.id)
                )
            )
            .scalars()
            .all()
        )
        if deal.id in won_ids and won_status is not None:
            history.append(
                DealStatusHistory(
                    deal_id=deal.id,
                    from_status_id=deal.status_id,
                    to_status_id=won_status.id,
                    changed_by=deal.owner_id,
                    reason="manual",
                    comment="Договор подписан, программа запущена",
                    changed_at=now,
                )
            )
            session.add(history[-1])
            deal.status_id = won_status.id
            deal.closed_at = now
            deal.sla_due_at = None
            deal.sla_state = "ok"
            deal.sla_escalated_at = None
            counters["won"] += 1

        status = statuses[deal.status_id]
        closed = deal.closed_at is not None
        total = (
            deal.sla_due_at - deal.status_changed_at
            if deal.sla_due_at is not None and not closed
            else None
        )
        if total is not None and total <= dt.timedelta(0):
            total = None

        # Сколько сделка уже находится в текущем статусе.
        if closed:
            last_gap = dt.timedelta(days=1 + _unit(deal.number, "gap") * 9)
        elif total is not None and deal.id in breached_ids:
            last_gap = total * (1.3 + _unit(deal.number, "b") * 0.6)
        elif total is not None and deal.id in warning_ids:
            last_gap = total * (0.78 + _unit(deal.number, "w") * 0.15)
        elif total is not None:
            last_gap = total * (0.05 + _unit(deal.number, "o") * 0.55)
        else:
            last_gap = dt.timedelta(days=0.5 + _unit(deal.number, "gap") * 6)

        steps = max(len(history) - 1, 0)
        age_days = (
            (28 + _unit(deal.number, "age") * 50)
            if closed
            else (12 + _unit(deal.number, "age") * 55)
        )
        last_at = now - last_gap
        first_at = min(now - dt.timedelta(days=age_days), last_at - dt.timedelta(days=0.4 * steps))
        span = last_at - first_at

        if history:
            weights = [1 + (_h(deal.number, i) % 5) for i in range(steps)]
            total_weight = sum(weights) or 1
            moment = first_at
            previous: dt.datetime | None = None
            for index, row in enumerate(history):
                if index > 0:
                    moment = moment + span * (weights[index - 1] / total_weight)
                row.changed_at = last_at if index == len(history) - 1 else moment
                row.duration_in_prev = None if previous is None else row.changed_at - previous
                previous = row.changed_at
            deal.created_at = history[0].changed_at - dt.timedelta(minutes=5)
        else:
            deal.created_at = first_at

        deal.status_changed_at = last_at
        if closed:
            deal.closed_at = last_at
        elif total is not None:
            deal.sla_due_at = last_at + total
            elapsed = now - last_at
            state = _sla_state(elapsed, total)
            deal.sla_state = state
            deal.sla_escalated_at = now if state == "breached" else None
            if state in counters:
                counters[state] += 1
        elif status.type == StatusType.PARKED.value:
            deal.sla_state = "paused"
        counters["deals"] += 1

    await session.flush()
    return counters


async def _stretch_signatures(session: Any, now: dt.datetime) -> int:
    documents = list(
        (
            await session.execute(
                select(SignatureDocument)
                .where(SignatureDocument.completed_at.is_not(None))
                .order_by(SignatureDocument.created_at, SignatureDocument.id)
            )
        )
        .scalars()
        .all()
    )
    changed = 0
    for document in documents:
        requests = list(
            (
                await session.execute(
                    select(SignatureRequest).where(SignatureRequest.document_id == document.id)
                )
            )
            .scalars()
            .all()
        )
        if not requests:
            continue
        hours = 4 + _unit(document.id, "sign") * 44
        completed = min(document.completed_at, now - dt.timedelta(hours=1))
        sent = completed - dt.timedelta(hours=hours)
        for request in requests:
            request.sent_at = sent
            if request.viewed_at is not None:
                request.viewed_at = sent + dt.timedelta(hours=min(hours / 3, 6))
        document.created_at = sent - dt.timedelta(minutes=20)
        document.completed_at = completed
        changed += 1
    await session.flush()
    return changed


_STAGE_PROGRESS = {
    "payment_contract": (0, False),
    "lms_enrollment": (35, False),
    "training_completed": (100, True),
    "won": (100, True),
}


async def _learning_progress(session: Any, now: dt.datetime) -> int:
    """Строки «Прогресс обучения» у физлиц на этапах после оплаты. Строка — тем же разбором, что
    приём от LMS (`upsert_progress`), поэтому форма данных настоящая."""
    rows = (
        await session.execute(
            select(Deal, WorkflowStatus.code)
            .join(WorkflowStatus, WorkflowStatus.id == Deal.status_id)
            .where(
                Deal.source == "demo",
                Deal.deal_type == "b2c",
                Deal.deleted_at.is_(None),
                Deal.contact_id.is_not(None),
            )
            .order_by(Deal.number)
        )
    ).all()
    written = 0
    for deal, status_code in rows:
        if status_code not in _STAGE_PROGRESS:
            continue
        product = (
            await session.execute(
                select(Product)
                .join(DealProduct, DealProduct.product_id == Product.id)
                .where(DealProduct.deal_id == deal.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if product is None:
            continue
        base, finished = _STAGE_PROGRESS[status_code]
        pct = 100 if finished else min(95, base + _h(deal.number, "pct") % 40)
        enrolled = now - dt.timedelta(days=14 + _h(deal.number, "enr") % 40)
        row: dict[str, Any] = {
            "deal_id": str(deal.id),
            "external_course_id": product.code,
            "progress_pct": pct,
            "enrolled_at": enrolled.isoformat(),
            "last_activity_at": (now - dt.timedelta(days=_h(deal.number, "act") % 6)).isoformat(),
        }
        if finished:
            row["score"] = str(Decimal(72 + _h(deal.number, "score") % 27))
            row["completed_at"] = (
                now - dt.timedelta(days=1 + _h(deal.number, "end") % 9)
            ).isoformat()
        if await upsert_progress(session, row) is not None:
            written += 1
    await session.flush()
    return written


async def apply(*, force: bool = False) -> dict[str, int]:
    settings = get_settings()
    if settings.app_profile != "demo":
        raise SystemExit(
            f"demo_showcase: профиль {settings.app_profile!r}; данные витрины только для demo"
        )
    now = dt.datetime.now(dt.UTC)
    async with session_scope() as session:
        done = await session.get(SystemSetting, MARKER)
        if done is not None and not force:
            logger.info("demo_showcase_already_applied", at=done.value.get("at"))
            return {"skipped": 1}
        result = await _stretch_deals(session, now)
        result["signatures"] = await _stretch_signatures(session, now)
        result["progress"] = await _learning_progress(session, now)
        value = {"at": now.isoformat(), **result}
        if done is None:
            session.add(
                SystemSetting(
                    key=MARKER,
                    value=value,
                    description="Витрина демо-данных применена (app.mocks.demo_showcase)",
                )
            )
        else:
            done.value = value
        await session.flush()
        count = await session.scalar(select(func.count(Deal.id)).where(Deal.source == "demo"))
        logger.info("demo_showcase_applied", demo_deals=count, **result)
        return result


def main() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    result = asyncio.run(apply(force="--force" in sys.argv[1:]))
    print("demo_showcase:", result)  # noqa: T201 — CLI


if __name__ == "__main__":
    main()
