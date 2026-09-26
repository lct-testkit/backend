"""SLA: рабочие дни по производственному календарю и поясу владельца, эскалация по правилу.

Найдено внешним тестированием: рабочими считались пн-пт по UTC, справочник праздников расчёт не
читал; порог эскалации и получатели (`escalate_to_*`, `channels`) хранились в правиле, но воркер
их игнорировал (эскалация была константой 150% и срабатывала лишь в момент смены состояния);
скан читал в память все открытые сделки разом.
"""

from __future__ import annotations

import datetime as dt
import uuid
import zoneinfo
from typing import Any

import pytest

from app.modules.crm import service as crm_service
from app.modules.crm.service import BusinessCalendar
from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import (
    by_code,
    create_deal,
    create_published_workflow,
    create_team,
    create_user,
    graph_body,
    login,
    transition_deal,
)

MSK = zoneinfo.ZoneInfo("Europe/Moscow")
VLAT = zoneinfo.ZoneInfo("Asia/Vladivostok")


class TestBusinessCalendar:
    def test_holiday_on_a_weekday_is_skipped(self) -> None:
        # Пятница 22:00 UTC + 4 ч: остаток замирает на выходных; понедельник — праздник.
        calendar = BusinessCalendar(non_working=frozenset({dt.date(2026, 5, 11)}))
        start = dt.datetime(2026, 5, 8, 22, 0, tzinfo=dt.UTC)

        due = crm_service.compute_sla_due_at(
            start, dt.timedelta(hours=4), count_business_days=True, calendar=calendar
        )

        assert due == dt.datetime(2026, 5, 12, 2, 0, tzinfo=dt.UTC)  # вторник 02:00

    def test_without_the_holiday_the_deadline_is_monday(self) -> None:
        start = dt.datetime(2026, 5, 8, 22, 0, tzinfo=dt.UTC)
        due = crm_service.compute_sla_due_at(
            start, dt.timedelta(hours=4), count_business_days=True, calendar=BusinessCalendar()
        )
        assert due == dt.datetime(2026, 5, 11, 2, 0, tzinfo=dt.UTC)

    def test_a_working_saturday_counts(self) -> None:
        calendar = BusinessCalendar(working=frozenset({dt.date(2026, 5, 9)}))  # суббота
        start = dt.datetime(2026, 5, 8, 22, 0, tzinfo=dt.UTC)

        due = crm_service.compute_sla_due_at(
            start, dt.timedelta(hours=4), count_business_days=True, calendar=calendar
        )

        assert due == dt.datetime(2026, 5, 9, 2, 0, tzinfo=dt.UTC)

    def test_day_boundaries_follow_the_calendar_timezone(self) -> None:
        # Пятница 20:00 в Москве (17:00 UTC), срок 6 часов: до полуночи по Москве остаётся 4 часа,
        # остаток переносится на понедельник 00:00 по Москве → понедельник 02:00 MSK. В UTC-сутках
        # («как раньше») ответ был бы другим: пятница кончилась бы в 03:00 MSK.
        start = dt.datetime(2026, 5, 8, 17, 0, tzinfo=dt.UTC)

        due = crm_service.compute_sla_due_at(
            start,
            dt.timedelta(hours=6),
            count_business_days=True,
            calendar=BusinessCalendar(tz=MSK),
        )

        assert due == dt.datetime(2026, 5, 11, 2, 0, tzinfo=MSK)

    def test_a_far_east_owner_gets_the_end_of_his_own_day(self) -> None:
        # 08:00 UTC пятницы = 18:00 во Владивостоке: до конца его суток 6 часов.
        start = dt.datetime(2026, 5, 8, 8, 0, tzinfo=dt.UTC)

        due = crm_service.compute_sla_due_at(
            start,
            dt.timedelta(hours=8),
            count_business_days=True,
            calendar=BusinessCalendar(tz=VLAT),
        )

        assert due == dt.datetime(2026, 5, 11, 2, 0, tzinfo=VLAT)

    def test_a_calendar_of_only_holidays_terminates(self) -> None:
        start = dt.datetime(2026, 1, 5, 10, 0, tzinfo=dt.UTC)
        everything = frozenset(start.date() + dt.timedelta(days=i) for i in range(4000))
        due = crm_service.compute_sla_due_at(
            start,
            dt.timedelta(hours=1),
            count_business_days=True,
            calendar=BusinessCalendar(non_working=everything),
        )
        assert due >= start

    def test_default_behaviour_without_a_calendar_is_unchanged(self) -> None:
        start = dt.datetime(2026, 1, 2, 22, 0, tzinfo=dt.UTC)
        due = crm_service.compute_sla_due_at(start, dt.timedelta(hours=4), count_business_days=True)
        assert due == dt.datetime(2026, 1, 5, 2, 0, tzinfo=dt.UTC)
        assert crm_service.is_business_day(dt.date(2026, 1, 5))
        assert not crm_service.is_business_day(dt.date(2026, 1, 3))


pytestmark_db = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


@pytestmark_db
class TestApplySlaReadsTheHolidayTable:
    def test_holiday_row_and_owner_timezone_shape_the_deadline(self, client) -> None:
        from app.core.db import session_scope
        from app.modules.catalog.models import Holiday
        from app.modules.crm.models import Deal
        from app.modules.identity.models import User

        # Дата из далёкого будущего: общая БД тестов не должна пересекаться с настоящим календарём.
        friday = dt.date(2037, 5, 8)
        monday = friday + dt.timedelta(days=3)

        async def _scenario() -> tuple[dt.datetime, dt.datetime]:
            async with session_scope() as session:
                owner = User(
                    keycloak_id=str(uuid.uuid4()),
                    email=f"{uuid.uuid4().hex[:10]}@rt-it-school.ru",
                    full_name="Владивостокский Менеджер",
                    role="KAM",
                    status="active",
                    timezone="Asia/Vladivostok",
                )
                session.add(owner)
                await session.flush()
                graph = {
                    "sla_rules": [
                        {
                            "status_id": "s1",
                            "max_duration_seconds": 8 * 3600,
                            "count_business_days": True,
                        }
                    ]
                }
                status_view = {"id": "s1", "type": "intermediate"}
                entered = dt.datetime.combine(friday, dt.time(8, 0), tzinfo=dt.UTC)  # 18:00 VLAT

                plain = Deal(owner_id=owner.id, status_changed_at=entered)
                await crm_service.apply_sla(session, plain, graph, status_view, entered)

                session.add(Holiday(date=monday, name="Тестовый праздник"))
                await session.flush()
                holiday = Deal(owner_id=owner.id, status_changed_at=entered)
                await crm_service.apply_sla(session, holiday, graph, status_view, entered)
                due = (plain.sla_due_at, holiday.sla_due_at)
                await session.rollback()  # справочник общей БД не засоряем
                return due  # type: ignore[return-value]

        without_holiday, with_holiday = run(client, _scenario)

        # Пятница 18:00 во Владивостоке + 8 ч: 6 ч помещаются в пятницу (до 00:00 субботы), остаток
        # 2 ч — на следующие рабочие сутки. Без праздника это понедельник 02:00 VLAT.
        assert without_holiday == dt.datetime(2037, 5, 11, 2, 0, tzinfo=VLAT)
        # Понедельник — праздник из справочника: остаток уходит на вторник.
        assert with_holiday == dt.datetime(2037, 5, 12, 2, 0, tzinfo=VLAT)


@pytestmark_db
class TestSweepEscalation:
    def _deal_in_work(self, client, *, warn: int, escalate: int, to_user=None, role=None):
        login(client)
        rule: dict[str, Any] = {
            "status": "work",
            "max_duration_hours": 10,
            "warn_threshold_pct": warn,
            "escalate_threshold_pct": escalate,
            "count_business_days": False,
        }
        if to_user is not None:
            rule["escalate_to_user_id"] = str(to_user.id)
        if role is not None:
            rule["escalate_to_role"] = role
        graph = create_published_workflow(client, graph_body(sla_rules=[rule]))
        deal = create_deal(client, graph["workflow"]["id"])
        moved = transition_deal(client, deal, by_code(graph)["work"]["id"]).json()["deal"]
        return moved

    def _rewind(self, client, deal_id: str, share: float) -> None:
        from sqlalchemy import update

        from app.core.db import session_scope
        from app.modules.crm.models import Deal

        entered = dt.datetime.now(dt.UTC) - dt.timedelta(hours=10 * share)

        async def _move() -> None:
            async with session_scope() as session:
                await session.execute(
                    update(Deal)
                    .where(Deal.id == uuid.UUID(deal_id))
                    .values(status_changed_at=entered, sla_due_at=entered + dt.timedelta(hours=10))
                )

        run(client, _move)

    def _notifications(self, client, recipient_id, deal_id: str) -> list[dict]:
        from sqlalchemy import select

        from app.core.db import session_scope
        from app.modules.notification.models import Notification

        async def _load() -> list[dict]:
            async with session_scope() as session:
                rows = await session.scalars(
                    select(Notification).where(
                        Notification.recipient_id == recipient_id,
                        Notification.entity_id == uuid.UUID(deal_id),
                        Notification.template_code == "DEAL_SLA_BREACHED",
                    )
                )
                return [dict(row.payload) for row in rows]

        return run(client, _load)

    def _state(self, client, deal_id: str) -> dict:
        return client.get(f"/api/deals/{deal_id}").json()["deal"]

    def test_escalation_goes_to_the_person_named_by_the_rule_once(self, client) -> None:
        from app.modules.crm.tasks import sweep_sla_breaches

        boss = create_user(client, "HEAD")
        deal = self._deal_in_work(client, warn=50, escalate=120, to_user=boss)
        self._rewind(client, deal["id"], 1.3)

        first = run(client, sweep_sla_breaches, {})
        second = run(client, sweep_sla_breaches, {})

        assert first["breached"] >= 1 and first["escalated"] >= 1
        assert self._state(client, deal["id"])["sla_state"] == "breached"
        got = self._notifications(client, boss.id, deal["id"])
        assert len(got) == 1 and got[0]["escalated"] is True, got
        assert second["escalated"] == 0  # повторно не шлётся

    def test_between_breach_and_escalation_nobody_is_escalated(self, client) -> None:
        from app.modules.crm.tasks import sweep_sla_breaches

        boss = create_user(client, "HEAD")
        deal = self._deal_in_work(client, warn=50, escalate=150, to_user=boss)
        self._rewind(client, deal["id"], 1.2)

        run(client, sweep_sla_breaches, {})

        assert self._state(client, deal["id"])["sla_state"] == "breached"
        assert self._notifications(client, boss.id, deal["id"]) == []

        # Позже срок дошёл до порога эскалации: теперь она уходит (раньше — никогда: проверялась
        # только в момент перехода в breached).
        self._rewind(client, deal["id"], 1.6)
        again = run(client, sweep_sla_breaches, {})

        assert again["escalated"] >= 1
        assert len(self._notifications(client, boss.id, deal["id"])) == 1

    def test_role_head_means_the_head_of_the_owners_team(self, client) -> None:
        from app.modules.crm.tasks import sweep_sla_breaches

        team = create_team(client)
        head = create_user(client, "HEAD", team_id=team)
        kam = create_user(client, "KAM", team_id=team)
        login(client)

        async def _set_head() -> None:
            from app.core.db import session_scope
            from app.modules.identity.models import Team

            async with session_scope() as session:
                (await session.get(Team, team)).head_id = head.id  # type: ignore[union-attr]

        run(client, _set_head)
        rule = {
            "status": "work",
            "max_duration_hours": 10,
            "warn_threshold_pct": 50,
            "escalate_threshold_pct": 120,
            "escalate_to_role": "HEAD",
            "count_business_days": False,
        }
        graph = create_published_workflow(client, graph_body(sla_rules=[rule]))
        deal = create_deal(client, graph["workflow"]["id"], owner_id=str(kam.id))
        moved = transition_deal(client, deal, by_code(graph)["work"]["id"]).json()["deal"]
        self._rewind(client, moved["id"], 1.4)

        run(client, sweep_sla_breaches, {})

        assert len(self._notifications(client, head.id, moved["id"])) == 1

    def test_empty_channels_mean_no_notifications(self, client) -> None:
        from app.modules.crm.tasks import sweep_sla_breaches

        login(client)
        rule = {
            "status": "work",
            "max_duration_hours": 10,
            "warn_threshold_pct": 50,
            "channels": [],
            "count_business_days": False,
        }
        graph = create_published_workflow(client, graph_body(sla_rules=[rule]))
        deal = create_deal(client, graph["workflow"]["id"])
        moved = transition_deal(client, deal, by_code(graph)["work"]["id"]).json()["deal"]
        self._rewind(client, moved["id"], 1.1)

        run(client, sweep_sla_breaches, {})

        assert self._state(client, moved["id"])["sla_state"] == "breached"
        assert self._notifications(client, uuid.UUID(moved["owner_id"]), moved["id"]) == []

    def test_unknown_role_or_inactive_user_in_the_rule_is_refused(self, client) -> None:
        login(client)
        blocked = create_user(client, "HEAD", status="blocked")
        from tests.crm_helpers import create_draft_workflow, put_graph

        workflow = create_draft_workflow(client)
        bad_role = graph_body(
            sla_rules=[{"status": "work", "max_duration_hours": 5, "escalate_to_role": "BOSS"}]
        )
        assert put_graph(client, workflow, bad_role).status_code == 422
        bad_user = graph_body(
            sla_rules=[
                {"status": "work", "max_duration_hours": 5, "escalate_to_user_id": str(blocked.id)}
            ]
        )
        assert put_graph(client, workflow, bad_user).status_code == 422

    def test_scan_is_batched(self, client) -> None:
        # Партия меньше числа сделок: проход всё равно доходит до каждой (курсор по id).
        from app.modules.crm import tasks as sla_tasks

        boss = create_user(client, "HEAD")
        first = self._deal_in_work(client, warn=50, escalate=120, to_user=boss)
        second = self._deal_in_work(client, warn=50, escalate=120, to_user=boss)
        self._rewind(client, first["id"], 1.3)
        self._rewind(client, second["id"], 1.3)
        original = sla_tasks.BATCH_SIZE
        sla_tasks.BATCH_SIZE = 1
        try:
            result = run(client, sla_tasks.sweep_sla_breaches, {})
        finally:
            sla_tasks.BATCH_SIZE = original

        assert result["breached"] >= 2
        assert self._state(client, first["id"])["sla_state"] == "breached"
        assert self._state(client, second["id"])["sla_state"] == "breached"
