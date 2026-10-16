"""B1-3: период по выбранной дате (`date_field`) и границы суток в часовом поясе пользователя, не
UTC. Настоящая Postgres обязательна (`timezone(...)`/`date_trunc` считаются на стороне БД) — см.
докстринг `tests/conftest.py`. Каждый тест заводит свой КАМ с уникальным токеном в имени/почте —
скоуп `deal_scope_clause` ограничивает отчёт его же сделками, поэтому чужие данные в общей БД не
мешают (тот же приём изоляции, что и в `tests/test_reporting_register.py`).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

from app.modules.reporting.builders import build_deal_register, build_monthly_dynamics
from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run
from tests.test_reporting_register import _principal

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


@dataclass(slots=True)
class PeriodWorld:
    user_id: uuid.UUID = field(default_factory=uuid.uuid4)
    deal_numbers: dict[str, str] = field(default_factory=dict)
    deal_ids: dict[str, uuid.UUID] = field(default_factory=dict)


async def _make_period_world(tz_name: str, deals: dict[str, dict]) -> PeriodWorld:
    """`deals` — `{ключ: {"created_at": ..., "closed_at": ...}}`, значения опциональны."""
    from app.core.db import session_scope
    from app.modules.catalog.models import Organization
    from app.modules.crm.models import Deal
    from app.modules.identity.models import User
    from app.modules.workflow.models import Workflow, WorkflowStatus

    token = uuid.uuid4().hex[:10]
    world = PeriodWorld()

    async with session_scope() as session:
        workflow = Workflow(code=f"wf-period-{token}", name="Воронка периода", deal_type="b2b")
        session.add(workflow)
        await session.flush()
        status = WorkflowStatus(
            workflow_id=workflow.id,
            code="initial",
            name="Выявление",
            type="initial",
            sort_order=0,
        )
        session.add(status)
        await session.flush()

        user = User(
            keycloak_id=str(uuid.uuid4()),
            email=f"period-{token}@rt-it-school.ru",
            full_name="Периодов П. П.",
            role="KAM",
            status="active",
            consent_version="1.0",
            timezone=tz_name,
        )
        session.add(user)
        await session.flush()
        world.user_id = user.id

        org = Organization(name=f"ВУЗ периода {token}", org_type="university", owner_id=user.id)
        session.add(org)
        await session.flush()

        for key, fields in deals.items():
            deal = Deal(
                number=f"D-period-{token}-{key}",
                title=f"Сделка {key}",
                deal_type="b2b",
                workflow_id=workflow.id,
                status_id=status.id,
                organization_id=org.id,
                owner_id=user.id,
                amount=1000,
                created_at=fields.get("created_at", dt.datetime.now(dt.UTC)),
            )
            if "closed_at" in fields:
                deal.closed_at = fields["closed_at"]
            session.add(deal)
            await session.flush()
            world.deal_numbers[key] = deal.number
            world.deal_ids[key] = deal.id

    return world


async def _register_numbers(world: PeriodWorld, params: dict) -> set[str]:
    from app.core.db import session_scope

    principal = _principal(world.user_id, "KAM")
    async with session_scope() as session:
        dataset = await build_deal_register(session, principal, params)
        return {row[5] for row in dataset.rows}


class TestDateFieldDefaultsToCreatedAt:
    def test_moscow_user_sees_the_late_night_deal_in_february(self, client) -> None:
        # 2026-01-31T21:30:00Z = 2026-02-01T00:30 МСК.
        boundary = dt.datetime(2026, 1, 31, 21, 30, tzinfo=dt.UTC)
        world = run(
            client,
            _make_period_world,
            "Europe/Moscow",
            {"d1": {"created_at": boundary}},
        )
        feb = run(
            client, _register_numbers, world, {"date_from": "2026-02-01", "date_to": "2026-02-28"}
        )
        jan = run(
            client, _register_numbers, world, {"date_from": "2026-01-01", "date_to": "2026-01-31"}
        )
        assert world.deal_numbers["d1"] in feb
        assert world.deal_numbers["d1"] not in jan

    def test_utc_user_sees_the_same_deal_in_january(self, client) -> None:
        boundary = dt.datetime(2026, 1, 31, 21, 30, tzinfo=dt.UTC)
        world = run(
            client,
            _make_period_world,
            "UTC",
            {"d1": {"created_at": boundary}},
        )
        feb = run(
            client, _register_numbers, world, {"date_from": "2026-02-01", "date_to": "2026-02-28"}
        )
        jan = run(
            client, _register_numbers, world, {"date_from": "2026-01-01", "date_to": "2026-01-31"}
        )
        assert world.deal_numbers["d1"] in jan
        assert world.deal_numbers["d1"] not in feb


class TestDayBoundariesAreInclusive:
    def test_date_from_equals_date_to_covers_exactly_one_local_day(self, client) -> None:
        last_second = dt.datetime(2026, 3, 10, 23, 59, 59, tzinfo=dt.UTC)
        midnight_next = dt.datetime(2026, 3, 11, 0, 0, 0, tzinfo=dt.UTC)
        world = run(
            client,
            _make_period_world,
            "UTC",
            {"in": {"created_at": last_second}, "out": {"created_at": midnight_next}},
        )
        numbers = run(
            client, _register_numbers, world, {"date_from": "2026-03-10", "date_to": "2026-03-10"}
        )
        assert world.deal_numbers["in"] in numbers
        assert world.deal_numbers["out"] not in numbers


class TestDateFieldSelectsTheColumn:
    def test_closed_at_excludes_open_deals(self, client) -> None:
        closed_at = dt.datetime(2026, 4, 5, 10, 0, tzinfo=dt.UTC)
        world = run(
            client,
            _make_period_world,
            "UTC",
            {
                "closed": {
                    "created_at": dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                    "closed_at": closed_at,
                },
                "open": {"created_at": dt.datetime(2026, 1, 1, tzinfo=dt.UTC)},
            },
        )
        numbers = run(
            client,
            _register_numbers,
            world,
            {"date_from": "2026-04-05", "date_to": "2026-04-05", "date_field": "closed_at"},
        )
        assert world.deal_numbers["closed"] in numbers
        assert world.deal_numbers["open"] not in numbers

    def test_updated_at_reflects_the_edit_not_the_creation(self, client) -> None:
        from app.core.db import session_scope
        from app.modules.crm.models import Deal

        world = run(
            client,
            _make_period_world,
            "UTC",
            {"d1": {"created_at": dt.datetime(2020, 1, 1, tzinfo=dt.UTC)}},
        )

        async def _touch() -> None:
            async with session_scope() as session:
                deal = await session.get(Deal, world.deal_ids["d1"])
                assert deal is not None
                deal.amount = Decimal("9999")

        run(client, _touch)

        today = dt.datetime.now(dt.UTC).date().isoformat()
        by_created = run(
            client,
            _register_numbers,
            world,
            {"date_from": today, "date_to": today, "date_field": "created_at"},
        )
        by_updated = run(
            client,
            _register_numbers,
            world,
            {"date_from": today, "date_to": today, "date_field": "updated_at"},
        )
        assert world.deal_numbers["d1"] not in by_created
        assert world.deal_numbers["d1"] in by_updated


class TestUnknownUserTimezoneFallsBackToMoscow:
    def test_garbage_timezone_does_not_crash_and_behaves_like_moscow(self, client) -> None:
        boundary = dt.datetime(2026, 1, 31, 21, 30, tzinfo=dt.UTC)
        world = run(
            client,
            _make_period_world,
            "Нет/Такого",
            {"d1": {"created_at": boundary}},
        )
        feb = run(
            client, _register_numbers, world, {"date_from": "2026-02-01", "date_to": "2026-02-28"}
        )
        assert world.deal_numbers["d1"] in feb


class TestMonthlyDynamicsDateField:
    def test_date_field_param_is_rejected_by_the_api(self, client) -> None:
        from sqlalchemy import select as sa_select

        from app.core.db import session_scope
        from app.modules.reporting.models import ReportTemplate

        async def _ensure_template() -> None:
            async with session_scope() as session:
                existing = await session.scalar(
                    sa_select(ReportTemplate.id).where(ReportTemplate.code == "monthly_dynamics")
                )
                if existing is not None:
                    return
                session.add(
                    ReportTemplate(
                        code="monthly_dynamics",
                        name="Динамика по месяцам",
                        description="Динамика по месяцам.",
                        query_def={"kind": "monthly_dynamics"},
                        allowed_roles=[],
                        default_params={"months": 12},
                        output_formats=["xlsx", "pdf", "png"],
                        is_active=True,
                    )
                )

        run(client, _ensure_template)
        kam = run(client, _make_user, "KAM")
        client.headers["X-CSRF-Token"] = authenticate(client, kam)

        response = client.post(
            "/api/reports",
            json={
                "template_code": "monthly_dynamics",
                "format": "xlsx",
                "params": {"date_field": "created_at"},
            },
        )
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "CRM-1001"

    def test_late_night_moscow_deal_lands_in_the_february_row(self, client) -> None:
        from app.core.db import session_scope

        boundary = dt.datetime(2026, 1, 31, 22, 0, tzinfo=dt.UTC)
        world = run(
            client,
            _make_period_world,
            "Europe/Moscow",
            {"d1": {"created_at": boundary}},
        )
        principal = _principal(world.user_id, "KAM")

        async def _dynamics() -> dict[str, int]:
            async with session_scope() as session:
                dataset = await build_monthly_dynamics(
                    session, principal, {"date_from": "2026-01-01", "date_to": "2026-02-28"}
                )
                return {row[0]: row[1] for row in dataset.rows}

        rows = run(client, _dynamics)
        assert rows.get("2026-02", 0) >= 1
        assert rows.get("2026-01", 0) == 0
