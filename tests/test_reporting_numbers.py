"""B1-7: объективность цифр воронки/SLA/КАМ. Настоящая Postgres обязательна (материализованное
представление, `date_trunc`/`timezone`) — см. докстринг `tests/conftest.py`. Каждый тест заводит
свой изолированный мир (воронка/вуз/пользователи с уникальным токеном в коде/почте), поэтому общая
БД с данными других тестов не мешает — тот же приём, что и в `tests/test_reporting_register.py`/
`tests/test_reporting_period.py`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from app.modules.reporting.builders import (
    build_deal_funnel,
    build_kam_summary,
    build_sla_compliance,
)
from tests.conftest import TEST_DATABASE_URL, run
from tests.test_reporting_register import _principal

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


@dataclass(slots=True)
class FunnelWorld:
    admin_id: uuid.UUID = field(default_factory=uuid.uuid4)
    workflow_id: uuid.UUID = field(default_factory=uuid.uuid4)
    status_ids: dict[str, uuid.UUID] = field(default_factory=dict)


async def _make_funnel_world() -> FunnelWorld:
    """Воронка из пяти статусов, 10 сделок, одна удалена мягко (B1-7, BUG-2/3):

    * S0 «Выявление» (initial), S1 «Переговоры» (intermediate), S2 «КП» (intermediate,
      необязательный шаг — часть сделок его пропускает), S3 «Выиграна» (won), S4 «Отказ» (lost).
    * D1, D2 — прошли через S2, выиграны (S0->S1->S2->S3).
    * D3..D6 — ПРОПУСТИЛИ S2, выиграны напрямую (S0->S1->S3). Старый расчёт конверсии
      (`entered(i) / entered(i-1)`) давал на этом шаге больше 100%: в S2 вошло 2 сделки
      (D1, D2), а в S3 (следующий шаг) — 6 (D1-D6), 6/2 = 300%.
    * D7, D8 — сейчас в S2 (ещё не выиграны и не пропущены).
    * D9 — отказ после переговоров (S0->S1->S4).
    * D10 — только S0, удалена мягко: не должна попасть ни в один путь после миграции
      `0042_report_mv_without_deleted`.
    """
    from app.core.db import session_scope
    from app.modules.catalog.models import Organization
    from app.modules.crm.models import Deal, DealStatusHistory
    from app.modules.identity.models import User
    from app.modules.workflow.models import Workflow, WorkflowStatus

    token = uuid.uuid4().hex[:10]
    world = FunnelWorld()

    async with session_scope() as session:
        workflow = Workflow(code=f"wf-numbers-{token}", name="Воронка чисел", deal_type="b2b")
        session.add(workflow)
        await session.flush()
        world.workflow_id = workflow.id

        statuses = [
            WorkflowStatus(
                workflow_id=workflow.id, code=code, name=name, type=kind, sort_order=order
            )
            for order, (code, name, kind) in enumerate(
                [
                    ("s0", "Выявление", "initial"),
                    ("s1", "Переговоры", "intermediate"),
                    ("s2", "КП", "intermediate"),
                    ("s3", "Выиграна", "won"),
                    ("s4", "Отказ", "lost"),
                ]
            )
        ]
        session.add_all(statuses)
        await session.flush()
        world.status_ids = {s.code: s.id for s in statuses}
        s0, s1, s2, s3, s4 = (s.id for s in statuses)

        admin = User(
            keycloak_id=str(uuid.uuid4()),
            email=f"admin-numbers-{token}@rt-it-school.ru",
            full_name="Админов А. А.",
            role="ADMIN",
            status="active",
            consent_version="1.0",
        )
        session.add(admin)
        await session.flush()
        world.admin_id = admin.id

        org = Organization(name=f"ВУЗ чисел {token}", org_type="university", owner_id=admin.id)
        session.add(org)
        await session.flush()

        def _deal(index: int, status_id: uuid.UUID) -> Deal:
            return Deal(
                number=f"D-numbers-{token}-{index}",
                title=f"Сделка {index}",
                deal_type="b2b",
                workflow_id=workflow.id,
                status_id=status_id,
                organization_id=org.id,
                owner_id=admin.id,
                amount=1000,
            )

        # (deal_index, path статусов в порядке прохождения)
        paths: list[tuple[int, list[uuid.UUID]]] = [
            (1, [s0, s1, s2, s3]),
            (2, [s0, s1, s2, s3]),
            (3, [s0, s1, s3]),
            (4, [s0, s1, s3]),
            (5, [s0, s1, s3]),
            (6, [s0, s1, s3]),
            (7, [s0, s1, s2]),
            (8, [s0, s1, s2]),
            (9, [s0, s1, s4]),
            (10, [s0]),
        ]
        deals = {}
        for index, path in paths:
            deal = _deal(index, path[-1])
            session.add(deal)
            await session.flush()
            deals[index] = deal
            from_status: uuid.UUID | None = None
            for to_status in path:
                session.add(
                    DealStatusHistory(
                        deal_id=deal.id, from_status_id=from_status, to_status_id=to_status
                    )
                )
                from_status = to_status
            if path[-1] in (s3, s4):
                deal.closed_at = dt.datetime.now(dt.UTC)
            await session.flush()

        deals[10].deleted_at = dt.datetime.now(dt.UTC)
        await session.flush()

    return world


async def _refresh_mv() -> None:
    from sqlalchemy import text

    from app.core.db import session_scope

    async with session_scope() as session:
        await session.execute(text("SELECT refresh_mv_deal_status_summary()"))


async def _funnel(world: FunnelWorld, params: dict) -> list[list]:
    from app.core.db import session_scope

    principal = _principal(world.admin_id, "ADMIN")
    async with session_scope() as session:
        dataset = await build_deal_funnel(
            session, principal, {"workflow_id": str(world.workflow_id), **params}
        )
        return dataset.rows


class TestFunnelParityBetweenMvAndRowByRowPaths:
    def test_admin_fast_path_matches_slow_path_after_refresh(self, client) -> None:
        world = run(client, _make_funnel_world)
        run(client, _refresh_mv)
        fast = run(client, _funnel, world, {})
        slow = run(client, _funnel, world, {"date_from": "2000-01-01"})
        assert fast == slow


class TestConversionNeverExceedsHundredPercentOnOptionalStepSkip:
    def test_reached_based_conversion_stays_within_bounds(self, client) -> None:
        world = run(client, _make_funnel_world)
        rows = run(client, _funnel, world, {"date_from": "2000-01-01"})
        by_name = {row[0]: row for row in rows}

        # 9 сделок (без D10, удалённой) дошли хотя бы до S0/S1.
        assert by_name["Выявление"][5] == 9  # «Дошли до шага»
        assert by_name["Переговоры"][5] == 9
        # КП: дошли 8 (D1-D8), т.к. D3-D6 "дошли и дальше" (reached >= order КП), хоть и
        # пропустили сам статус; D9 (отказ после переговоров) до КП не дошла.
        assert by_name["КП"][5] == 8
        # Выиграна: дошли 6 (D1-D6).
        assert by_name["Выиграна"][5] == 6

        for row in rows:
            conversion = row[3]
            if conversion is not None:
                assert conversion <= 100.0, row

        # Старый расчёт (entered/entered) на этом переходе давал 300% (6 вошедших в "Выиграна"
        # делить на 2 вошедших в "КП") — явная регрессия, которую покрывает именно этот тест.
        kp_conversion = by_name["КП"][3]
        won_conversion = by_name["Выиграна"][3]
        assert kp_conversion is not None and kp_conversion <= 100.0
        assert won_conversion is not None and won_conversion <= 100.0

    def test_lost_status_has_no_conversion_or_reached(self, client) -> None:
        world = run(client, _make_funnel_world)
        rows = run(client, _funnel, world, {"date_from": "2000-01-01"})
        lost_row = next(row for row in rows if row[0] == "Отказ")
        assert lost_row[3] is None  # Конверсия шага
        assert lost_row[5] is None  # Дошли до шага
        assert lost_row[6] is not None  # От старта, % — всё же посчитан


@dataclass(slots=True)
class SlaWorld:
    user_id: uuid.UUID = field(default_factory=uuid.uuid4)
    team_id: uuid.UUID | None = None


async def _make_sla_world() -> SlaWorld:
    from app.core.db import session_scope
    from app.modules.catalog.models import Organization
    from app.modules.crm.models import Deal
    from app.modules.identity.models import User
    from app.modules.workflow.models import Workflow, WorkflowStatus

    token = uuid.uuid4().hex[:10]
    world = SlaWorld()

    async with session_scope() as session:
        workflow = Workflow(code=f"wf-sla-{token}", name="Воронка SLA", deal_type="b2b")
        session.add(workflow)
        await session.flush()
        statuses = {
            name: WorkflowStatus(
                workflow_id=workflow.id, code=name, name=name, type=kind, sort_order=order
            )
            for order, (name, kind) in enumerate(
                [("open", "initial"), ("won", "won"), ("parked", "parked")]
            )
        }
        session.add_all(statuses.values())
        await session.flush()

        user = User(
            keycloak_id=str(uuid.uuid4()),
            email=f"sla-{token}@rt-it-school.ru",
            full_name="SLA С. С.",
            role="KAM",
            status="active",
            consent_version="1.0",
        )
        session.add(user)
        await session.flush()
        world.user_id = user.id

        org = Organization(name=f"ВУЗ SLA {token}", org_type="university", owner_id=user.id)
        session.add(org)
        await session.flush()

        def _deal(number: str, status, sla_state: str, closed: bool) -> Deal:
            deal = Deal(
                number=f"D-sla-{token}-{number}",
                title=f"Сделка {number}",
                deal_type="b2b",
                workflow_id=workflow.id,
                status_id=status.id,
                organization_id=org.id,
                owner_id=user.id,
                amount=500,
                sla_state=sla_state,
            )
            if closed:
                deal.closed_at = dt.datetime.now(dt.UTC)
            return deal

        session.add_all(
            [
                _deal("open-ok", statuses["open"], "ok", closed=False),
                _deal("open-warn", statuses["open"], "warning", closed=False),
                _deal("open-breached", statuses["open"], "breached", closed=False),
                _deal("parked", statuses["parked"], "paused", closed=False),
                _deal("closed-1", statuses["won"], "ok", closed=True),
                _deal("closed-2", statuses["won"], "ok", closed=True),
            ]
        )
        await session.flush()

    return world


class TestSlaComplianceExcludesClosedDeals:
    def test_closed_deals_are_not_counted_as_ok(self, client) -> None:
        from app.core.db import session_scope

        world = run(client, _make_sla_world)
        principal = _principal(world.user_id, "KAM")

        async def _report():
            async with session_scope() as session:
                return await build_sla_compliance(session, principal, {})

        dataset = run(client, _report)
        by_state = {row[0]: row for row in dataset.rows}
        # «В норме» = 1 (только открытая open-ok): два ЗАКРЫТЫХ "ok" (closed-1, closed-2) не
        # должны сюда попасть — иначе было бы 3, это и есть BUG-4.
        assert by_state["В норме"][1] == 1
        assert by_state["Под угрозой"][1] == 1
        assert by_state["Нарушен"][1] == 1
        assert by_state["Остановлен (сделка заморожена)"][1] == 1
        total_open = sum(row[1] for row in dataset.rows)
        assert total_open == 4
        assert dataset.note is not None
        assert "2" in dataset.note


@dataclass(slots=True)
class KamWorld:
    user_id: uuid.UUID = field(default_factory=uuid.uuid4)


async def _make_kam_world() -> KamWorld:
    from app.core.db import session_scope
    from app.modules.catalog.models import Organization
    from app.modules.crm.models import Deal
    from app.modules.identity.models import User
    from app.modules.workflow.models import Workflow, WorkflowStatus

    token = uuid.uuid4().hex[:10]
    world = KamWorld()

    async with session_scope() as session:
        workflow = Workflow(code=f"wf-kam-{token}", name="Воронка КАМа", deal_type="b2b")
        session.add(workflow)
        await session.flush()
        statuses = {
            name: WorkflowStatus(
                workflow_id=workflow.id, code=name, name=name, type=kind, sort_order=order
            )
            for order, (name, kind) in enumerate(
                [
                    ("negotiation", "intermediate"),
                    ("won", "won"),
                    ("lost", "lost"),
                    ("parked", "parked"),
                ]
            )
        }
        session.add_all(statuses.values())
        await session.flush()

        user = User(
            keycloak_id=str(uuid.uuid4()),
            email=f"kam-numbers-{token}@rt-it-school.ru",
            full_name="Камов К. К.",
            role="KAM",
            status="active",
            consent_version="1.0",
        )
        session.add(user)
        await session.flush()
        world.user_id = user.id

        org = Organization(name=f"ВУЗ КАМа {token}", org_type="university", owner_id=user.id)
        session.add(org)
        await session.flush()

        session.add_all(
            [
                Deal(
                    number=f"D-kam-{token}-1",
                    title="В работе",
                    deal_type="b2b",
                    workflow_id=workflow.id,
                    status_id=statuses["negotiation"].id,
                    organization_id=org.id,
                    owner_id=user.id,
                    amount=100,
                ),
                Deal(
                    number=f"D-kam-{token}-2",
                    title="Выиграна",
                    deal_type="b2b",
                    workflow_id=workflow.id,
                    status_id=statuses["won"].id,
                    organization_id=org.id,
                    owner_id=user.id,
                    amount=200,
                ),
                Deal(
                    number=f"D-kam-{token}-3",
                    title="Проиграна",
                    deal_type="b2b",
                    workflow_id=workflow.id,
                    status_id=statuses["lost"].id,
                    organization_id=org.id,
                    owner_id=user.id,
                    amount=Decimal("0"),
                ),
                Deal(
                    number=f"D-kam-{token}-4",
                    title="Заморожена",
                    deal_type="b2b",
                    workflow_id=workflow.id,
                    status_id=statuses["parked"].id,
                    organization_id=org.id,
                    owner_id=user.id,
                    amount=300,
                ),
            ]
        )
        await session.flush()

    return world


class TestKamSummaryExcludesFrozenFromInProgress:
    def test_frozen_deal_is_not_in_progress_but_has_its_own_column(self, client) -> None:
        from app.core.db import session_scope

        world = run(client, _make_kam_world)
        principal = _principal(world.user_id, "KAM")

        async def _report():
            async with session_scope() as session:
                return await build_kam_summary(session, principal, {})

        dataset = run(client, _report)
        assert dataset.columns[-1] == "Заморожено"
        row = dataset.rows[0]
        _name, in_progress, won, lost, _amount, _cycle, frozen = row
        assert in_progress == 1
        assert won == 1
        assert lost == 1
        assert frozen == 1
