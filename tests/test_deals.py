"""Тесты сделок (спринт 3, раздел 6.6).

Как и `tests/test_workflow.py`, эти тесты не поднимают PostgreSQL, Redis или
Keycloak: там, где сервисной функции реально нужна сессия, подставляется
`_FakeScalarSession` — тонкая замена, которая умеет только `scalar`, `add` и
`flush`, ровно то подмножество, которым пользуются проверяемые функции.
Цель — поймать то, что ломается молча: неверная арифметика SLA по рабочим
дням, рассинхрон whitelist'а DSL с формой контекста сделки, дыра в скоупе.
"""

from __future__ import annotations

import datetime as dt
import uuid

from app.core.permissions import DealScope, Permission, deal_scope_for, has_permission
from app.modules.crm import service as crm_service
from app.modules.crm.models import (
    OPEN_TASK_STATUSES,
    Deal,
    DealEventType,
    SlaState,
    TaskStatus,
)
from app.modules.workflow import dsl
from app.modules.workflow.models import StatusType
from app.modules.workflow.seed import _LOST_CONDITION, _WON_CONDITION


class _FakeScalarSession:
    """Достаточно `scalar`/`add`/`flush` для функций, которые трогают сессию
    только через них — реальная СУБД в этих тестах не участвует."""

    def __init__(self, scalar_value: int = 0) -> None:
        self.scalar_value = scalar_value
        self.added: list[object] = []

    async def scalar(self, stmt: object) -> int:
        return self.scalar_value

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None


def _deal(**overrides: object) -> Deal:
    deal = Deal(
        number="D-2026-000001",
        title="Тестовая сделка",
        deal_type="b2b",
        workflow_id=uuid.uuid4(),
        status_id=uuid.uuid4(),
        owner_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        currency="RUB",
        custom_fields={},
        external_ids={},
    )
    deal.id = uuid.uuid4()
    # `version`/`id` носят server_default/Python-default, применяемые только
    # при реальном flush — вне сессии их приходится выставлять руками, как
    # `tests/test_workflow.py` уже делает для `WorkflowStatus.id`.
    deal.version = 1
    deal.sla_paused_total = dt.timedelta(0)
    for key, value in overrides.items():
        setattr(deal, key, value)
    return deal


class TestSlaBusinessDays:
    def test_calendar_mode_ignores_weekends(self) -> None:
        start = dt.datetime(2026, 1, 2, 10, 0, tzinfo=dt.UTC)  # пятница
        due = crm_service.compute_sla_due_at(
            start, dt.timedelta(hours=48), count_business_days=False
        )
        assert due == start + dt.timedelta(hours=48)

    def test_business_day_mode_fits_in_same_day(self) -> None:
        start = dt.datetime(2026, 1, 2, 10, 0, tzinfo=dt.UTC)  # пятница
        due = crm_service.compute_sla_due_at(start, dt.timedelta(hours=8), count_business_days=True)
        assert due == dt.datetime(2026, 1, 2, 18, 0, tzinfo=dt.UTC)

    def test_business_day_mode_freezes_across_weekend(self) -> None:
        # Пятница 22:00, остаток срока «замирает» на выходных и продолжается
        # с понедельника 00:00.
        start = dt.datetime(2026, 1, 2, 22, 0, tzinfo=dt.UTC)
        due = crm_service.compute_sla_due_at(start, dt.timedelta(hours=4), count_business_days=True)
        assert due == dt.datetime(2026, 1, 5, 2, 0, tzinfo=dt.UTC)

    def test_zero_duration_returns_start(self) -> None:
        start = dt.datetime(2026, 1, 2, 10, 0, tzinfo=dt.UTC)
        due = crm_service.compute_sla_due_at(start, dt.timedelta(0), count_business_days=True)
        assert due == start

    def test_saturday_and_sunday_are_not_business_days(self) -> None:
        assert not crm_service.is_business_day(dt.date(2026, 1, 3))
        assert not crm_service.is_business_day(dt.date(2026, 1, 4))
        assert crm_service.is_business_day(dt.date(2026, 1, 5))


class TestApplySlaForStatus:
    def test_parked_status_pauses_timer(self) -> None:
        deal = _deal()
        crm_service._apply_sla_for_status(
            deal,
            {"sla_rules": []},
            {"id": "s1", "type": StatusType.PARKED.value},
            dt.datetime.now(dt.UTC),
        )
        assert deal.sla_state == SlaState.PAUSED.value
        assert deal.sla_due_at is None

    def test_status_without_rule_has_no_timer(self) -> None:
        deal = _deal()
        crm_service._apply_sla_for_status(
            deal,
            {"sla_rules": []},
            {"id": "s1", "type": StatusType.INTERMEDIATE.value},
            dt.datetime.now(dt.UTC),
        )
        assert deal.sla_state == SlaState.OK.value
        assert deal.sla_due_at is None

    def test_status_with_rule_sets_due_at(self) -> None:
        deal = _deal()
        now = dt.datetime(2026, 1, 5, 10, 0, tzinfo=dt.UTC)  # понедельник
        graph = {
            "sla_rules": [
                {"status_id": "s1", "max_duration_seconds": 3600, "count_business_days": False}
            ]
        }
        crm_service._apply_sla_for_status(
            deal, graph, {"id": "s1", "type": StatusType.INTERMEDIATE.value}, now
        )
        assert deal.sla_due_at == now + dt.timedelta(hours=1)
        assert deal.sla_state == SlaState.OK.value


class TestFieldPresent:
    def test_direct_field(self) -> None:
        deal = _deal(amount=None)
        assert not crm_service._field_present(deal, "amount")
        deal.amount = 100
        assert crm_service._field_present(deal, "amount")

    def test_custom_field(self) -> None:
        deal = _deal(custom_fields={"resume_at": "2026-01-01"})
        assert crm_service._field_present(deal, "custom_fields.resume_at")
        assert not crm_service._field_present(deal, "custom_fields.missing")


class TestReassignOwner:
    async def test_no_op_when_same_owner(self) -> None:
        deal = _deal()
        session = _FakeScalarSession()
        changed = await crm_service._reassign_owner(
            session, deal, deal.owner_id, reason="x", actor_id=None
        )
        assert changed is False
        assert session.added == []

    async def test_reassigns_and_records_event(self) -> None:
        deal = _deal(owner_unavailable=True)
        new_owner = uuid.uuid4()
        session = _FakeScalarSession()
        changed = await crm_service._reassign_owner(
            session, deal, new_owner, reason="увольнение", actor_id=uuid.uuid4()
        )
        assert changed is True
        assert deal.owner_id == new_owner
        assert deal.owner_unavailable is False
        assert len(session.added) == 1
        assert session.added[0].event_type == DealEventType.OWNER_CHANGED.value


class TestBuildDealContext:
    async def test_shape_matches_dsl_allowed_fields(self) -> None:
        deal = _deal(students_planned=5, custom_fields={"contract_number": "X"})
        session = _FakeScalarSession(scalar_value=2)
        context = await crm_service.build_deal_context(session, deal)

        assert context["tasks"] == {"open_count": 2}
        assert context["products"] == {"count": 2}
        assert context["attachments"] == {}
        assert context["students_planned"] == 5

        # Ни одно поле белого списка DSL не должно падать при резолве по
        # форме, которую строит build_deal_context — иначе конструктор
        # воронок и реальные сделки расходятся в проекции.
        for field_name in dsl.ALLOWED_FIELDS:
            dsl.resolve_field(field_name, context)


class TestPermissionsAndScope:
    def test_kam_scope_is_own(self) -> None:
        assert deal_scope_for("KAM") is DealScope.OWN

    def test_head_scope_is_team(self) -> None:
        assert deal_scope_for("HEAD") is DealScope.TEAM

    def test_admin_scope_is_all(self) -> None:
        assert deal_scope_for("ADMIN") is DealScope.ALL

    def test_auditor_scope_is_none(self) -> None:
        assert deal_scope_for("AUDITOR") is DealScope.NONE

    def test_auditor_has_no_deal_permissions(self) -> None:
        for perm in (
            Permission.DEAL_READ,
            Permission.DEAL_CREATE,
            Permission.DEAL_UPDATE,
            Permission.DEAL_TRANSITION,
        ):
            assert not has_permission("AUDITOR", perm)

    def test_only_head_and_admin_reassign(self) -> None:
        assert not has_permission("KAM", Permission.DEAL_REASSIGN)
        assert has_permission("HEAD", Permission.DEAL_REASSIGN)
        assert has_permission("ADMIN", Permission.DEAL_REASSIGN)

    def test_kam_can_create_and_transition_own_deals(self) -> None:
        assert has_permission("KAM", Permission.DEAL_CREATE)
        assert has_permission("KAM", Permission.DEAL_TRANSITION)


class TestDslFlattenLeaves:
    def test_flattens_all_and_any(self) -> None:
        node = {
            "all": [
                {"field": "a", "op": "not_null"},
                {
                    "any": [
                        {"field": "b", "op": "eq", "value": 1},
                        {"field": "c", "op": "eq", "value": 2},
                    ]
                },
            ]
        }
        leaves = dsl.flatten_leaves(node)
        assert {leaf["field"] for leaf in leaves} == {"a", "b", "c"}

    def test_empty_node(self) -> None:
        assert dsl.flatten_leaves({}) == []
        assert dsl.flatten_leaves(None) == []

    def test_single_leaf_returned_as_is(self) -> None:
        node = {"field": "amount", "op": "gt", "value": 0}
        assert dsl.flatten_leaves(node) == [node]


class TestSeedConditionsAgainstDealContext:
    """Условия закрытия из сидов (`app/modules/workflow/seed.py`) обязаны
    работать против той же формы контекста, что строит `build_deal_context` —
    иначе демо-воронка не закрывается ни в won, ни в lost."""

    def test_won_condition_satisfied_with_amount_and_date(self) -> None:
        context = {"amount": 100, "expected_close_date": dt.date(2026, 6, 1)}
        assert dsl.evaluate(_WON_CONDITION, context).ok

    def test_won_condition_unmet_without_amount(self) -> None:
        context = {"amount": None, "expected_close_date": dt.date(2026, 6, 1)}
        assert not dsl.evaluate(_WON_CONDITION, context).ok

    def test_lost_condition_requires_reason(self) -> None:
        assert not dsl.evaluate(_LOST_CONDITION, {"loss_reason_id": None}).ok
        assert dsl.evaluate(_LOST_CONDITION, {"loss_reason_id": str(uuid.uuid4())}).ok


class TestModelConstants:
    def test_open_task_statuses(self) -> None:
        assert {TaskStatus.OPEN.value, TaskStatus.IN_PROGRESS.value} == OPEN_TASK_STATUSES
