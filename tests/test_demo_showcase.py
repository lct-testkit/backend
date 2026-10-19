"""R-7, R-12, R-18, W-2: витрина демо-данных (`app.mocks.demo_showcase`) растягивает историю
сделок, раскладывает SLA, закрывает пару сделок основной воронки и заводит прогресс обучения.
Настоящая Postgres обязательна — см. `tests/conftest.py`. Сделки с `source='demo'` в тестах
заводятся с уникальным номером; функции витрины читаются по своим идентификаторам."""

from __future__ import annotations

import datetime as dt
import itertools
import uuid

import pytest
from sqlalchemy import select

from tests.conftest import TEST_DATABASE_URL, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

_PREFIX = "D-showcase"


async def _world() -> dict:
    from app.core.db import session_scope
    from app.modules.catalog.models import Organization, Product
    from app.modules.crm.models import Deal, DealStatusHistory
    from app.modules.identity.models import User
    from app.modules.workflow.models import Workflow, WorkflowStatus

    token = uuid.uuid4().hex[:8]
    now = dt.datetime.now(dt.UTC)
    async with session_scope() as session:
        b2b = Workflow(code=f"wf-sc-{token}", name="Витрина B2B", deal_type="b2b")
        b2c = Workflow(code=f"wf-sc2-{token}", name="Витрина B2C", deal_type="b2c")
        session.add_all([b2b, b2c])
        await session.flush()
        names = [
            ("s0", "initial"),
            ("s1", "intermediate"),
            ("s2", "intermediate"),
            ("s3", "intermediate"),
            ("won", "won"),
        ]
        b2b_statuses = [
            WorkflowStatus(workflow_id=b2b.id, code=c, name=c, type=k, sort_order=i)
            for i, (c, k) in enumerate(names)
        ]
        b2c_statuses = [
            WorkflowStatus(workflow_id=b2c.id, code=c, name=c, type=k, sort_order=i)
            for i, (c, k) in enumerate(
                [("consultation", "initial"), ("lms_enrollment", "intermediate"), ("done", "won")]
            )
        ]
        session.add_all(b2b_statuses + b2c_statuses)
        user = User(
            keycloak_id=str(uuid.uuid4()),
            email=f"sc-{token}@rt-it-school.ru",
            full_name="Витриныч В. В.",
            role="KAM",
            status="active",
            consent_version="1.0",
        )
        session.add(user)
        await session.flush()
        org = Organization(name=f"ВУЗ витрины {token}", org_type="university", owner_id=user.id)
        product = Product(code=f"sc-{token}", name="Курс витрины", kind="course")
        session.add_all([org, product])
        await session.flush()

        deals = []
        for index in range(9):
            status = b2b_statuses[1 + index % 3]
            deal = Deal(
                number=f"{_PREFIX}-{token}-{index}",
                title=f"Сделка витрины {index}",
                deal_type="b2b",
                workflow_id=b2b.id,
                status_id=status.id,
                organization_id=org.id,
                owner_id=user.id,
                source="demo",
                sla_due_at=now + dt.timedelta(hours=72),
                sla_state="ok",
                status_changed_at=now,
            )
            session.add(deal)
            await session.flush()
            previous = None
            for to_status in b2b_statuses[: 2 + index % 3]:
                session.add(
                    DealStatusHistory(
                        deal_id=deal.id,
                        from_status_id=previous,
                        to_status_id=to_status.id,
                        changed_at=now,
                        duration_in_prev=dt.timedelta(0),
                    )
                )
                previous = to_status.id
            deals.append(deal.id)
        await session.flush()
        return {
            "b2b": b2b.id,
            "won_status": b2b_statuses[-1].id,
            "deals": deals,
            "token": token,
            "user": user.id,
            "product": product.id,
            "b2c": b2c.id,
            "b2c_enroll": b2c_statuses[1].id,
        }


async def _apply(world: dict):
    from app.core.db import session_scope
    from app.mocks import demo_showcase

    async with session_scope() as session:
        return await demo_showcase._stretch_deals(
            session, dt.datetime.now(dt.UTC), main_workflow_id=world["b2b"]
        )


async def _read(world: dict):
    from app.core.db import session_scope
    from app.modules.crm.models import Deal, DealStatusHistory

    async with session_scope() as session:
        deals = (
            (await session.execute(select(Deal).where(Deal.id.in_(world["deals"])))).scalars().all()
        )
        history = (
            (
                await session.execute(
                    select(DealStatusHistory).where(DealStatusHistory.deal_id.in_(world["deals"]))
                )
            )
            .scalars()
            .all()
        )
        for item in [*deals, *history]:
            session.expunge(item)
        return deals, history


def test_history_is_spread_and_durations_are_real(client) -> None:
    world = run(client, _world)
    run(client, _apply, world)
    deals, history = run(client, _read, world)
    assert any(
        row.duration_in_prev and row.duration_in_prev > dt.timedelta(hours=6) for row in history
    )
    by_deal: dict = {}
    for row in history:
        by_deal.setdefault(row.deal_id, []).append(row)
    for deal in deals:
        rows = sorted(by_deal[deal.id], key=lambda r: r.changed_at)
        assert rows[0].duration_in_prev is None
        for earlier, later in itertools.pairwise(rows):
            assert later.changed_at > earlier.changed_at
            assert later.duration_in_prev == later.changed_at - earlier.changed_at
        assert deal.created_at < deal.status_changed_at


def test_sla_has_a_few_breached_and_a_few_warnings_and_no_mail_storm(client) -> None:
    world = run(client, _world)
    counters = run(client, _apply, world)
    deals, _ = run(client, _read, world)
    now = dt.datetime.now(dt.UTC)
    # Счётчики — по всем демо-сделкам базы (в общей тестовой базе есть и чужие): ровно 4 и 3.
    assert counters["breached"] == 4
    assert counters["warning"] == 3
    breached = [d for d in deals if d.sla_state == "breached"]
    # Просроченным сразу ставится отметка эскалации: воркер не рассылает письма по ним заново.
    assert all(d.sla_escalated_at is not None for d in breached)
    assert all(d.sla_due_at < now for d in breached)
    healthy = [d for d in deals if d.sla_state in {"ok", "warning"} and d.sla_due_at]
    assert all(d.sla_due_at > now for d in healthy)
    assert all(d.sla_escalated_at is None for d in healthy)


def test_two_deals_of_the_main_funnel_are_won(client) -> None:
    world = run(client, _world)
    counters = run(client, _apply, world)
    deals, history = run(client, _read, world)
    won = [d for d in deals if d.status_id == world["won_status"]]
    assert counters["won"] == len(won) == 2
    assert all(d.closed_at is not None for d in won)
    assert all(d.sla_due_at is None for d in won)
    won_rows = [r for r in history if r.to_status_id == world["won_status"]]
    assert len(won_rows) == 2

    # Повторный запуск не плодит новых «выигранных».
    again = run(client, _apply, world)
    assert again["won"] == 0


def test_progress_rows_for_b2c_deals_after_payment(client) -> None:
    from app.core.db import session_scope
    from app.mocks import demo_showcase
    from app.modules.catalog.models import Contact
    from app.modules.crm.models import Deal, DealProduct
    from app.modules.integration.models import LearningProgress

    world = run(client, _world)

    async def _b2c() -> uuid.UUID:
        async with session_scope() as session:
            contact = Contact(
                first_name="Анна",
                last_name=f"Учащаяся{world['token']}",
                email=f"learner-{world['token']}@example.ru",
            )
            session.add(contact)
            await session.flush()
            deal = Deal(
                number=f"{_PREFIX}-{world['token']}-b2c",
                title="Курс физлица",
                deal_type="b2c",
                workflow_id=world["b2c"],
                status_id=world["b2c_enroll"],
                contact_id=contact.id,
                owner_id=world["user"],
                source="demo",
            )
            session.add(deal)
            await session.flush()
            session.add(DealProduct(deal_id=deal.id, product_id=world["product"], quantity=1))
            return deal.id

    deal_id = run(client, _b2c)

    async def _go() -> int:
        async with session_scope() as session:
            return await demo_showcase._learning_progress(session, dt.datetime.now(dt.UTC))

    assert run(client, _go) >= 1

    async def _rows() -> list:
        async with session_scope() as session:
            return list(
                (
                    await session.execute(
                        select(LearningProgress.progress_pct).where(
                            LearningProgress.deal_id == deal_id
                        )
                    )
                ).scalars()
            )

    pct = run(client, _rows)
    assert len(pct) == 1 and 35 <= pct[0] <= 95
