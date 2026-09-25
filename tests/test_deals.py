"""Тесты сделок (спринт 3, раздел 6.6).

Как и `tests/test_workflow.py`, эти тесты не поднимают PostgreSQL, Redis или
Keycloak: там, где сервисной функции реально нужна сессия, подставляется
`_FakeScalarSession` — тонкая замена, которая умеет только `scalar`, `add` и
`flush`, ровно то подмножество, которым пользуются проверяемые функции.
Цель — поймать то, что ломается молча: неверная арифметика SLA по рабочим
дням, рассинхрон whitelist'а DSL с формой контекста сделки, дыра в скоупе.

Исключение — классы ниже с `pytestmark = skipif(not TEST_DATABASE_URL)`
(ответственный сделки, обязательные поля перехода, счётчики карточки, порог
SLA, вложения в условиях, `conditions_tree`, продукты сделки, список с
`total`/`sort`, имена связанных сущностей, постраничные комментарии и
история): им нужна настоящая Postgres, см. `tests/conftest.py`; воронки,
пользователи и сделки заводятся через `tests/crm_helpers.py`.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

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
from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import (
    by_code,
    create_contact,
    create_deal,
    create_organization,
    create_published_workflow,
    create_team,
    create_user,
    graph_body,
    login,
    sign_in,
    transition_deal,
)


class _FakeRows:
    def all(self) -> list[object]:
        return []


class _FakeScalarSession:
    """Достаточно `scalar`/`execute`/`add`/`flush` для функций, которые трогают сессию
    только через них — реальная СУБД в этих тестах не участвует. `execute` всегда
    отдаёт пустую выборку."""

    def __init__(self, scalar_value: int = 0) -> None:
        self.scalar_value = scalar_value
        self.added: list[object] = []

    async def scalar(self, stmt: object) -> int:
        return self.scalar_value

    async def execute(self, stmt: object) -> _FakeRows:
        return _FakeRows()

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


class TestCreateDealOwner:
    """A#2 (`frontend/docs/backend-issues.md`): `owner_id` при создании сделки.
    ADMIN назначает любого активного сотрудника, HEAD — своей команды, КАМ —
    только себя. Настоящая Postgres обязательна — см. `tests/conftest.py`."""

    pytestmark = pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
    )

    def _workflow_id(self, client) -> str:
        login(client)  # ADMIN публикует воронку, дальше тесты входят под нужной ролью
        return create_published_workflow(client)["workflow"]["id"]

    def _create(self, client, workflow_id: str, owner_id: uuid.UUID | str | None):
        payload = {
            "title": "Сделка для проверки ответственного",
            "deal_type": "b2b",
            "workflow_id": workflow_id,
            "organization_id": create_organization(client),
        }
        if owner_id is not None:
            payload["owner_id"] = str(owner_id)
        return client.post("/api/deals", json=payload)

    def test_kam_cannot_create_a_deal_for_a_colleague(self, client) -> None:
        workflow_id = self._workflow_id(client)
        kam = create_user(client, "KAM")
        colleague = create_user(client, "KAM")
        sign_in(client, kam)

        response = self._create(client, workflow_id, colleague.id)
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "CRM-1102"

    def test_kam_does_not_learn_whether_the_user_exists(self, client) -> None:
        workflow_id = self._workflow_id(client)
        sign_in(client, create_user(client, "KAM"))

        response = self._create(client, workflow_id, uuid.uuid4())
        assert response.status_code == 403, response.text

    def test_kam_can_name_themselves(self, client) -> None:
        workflow_id = self._workflow_id(client)
        kam = create_user(client, "KAM")
        sign_in(client, kam)

        for owner_id in (kam.id, None):
            response = self._create(client, workflow_id, owner_id)
            assert response.status_code == 201, response.text
            assert response.json()["owner_id"] == str(kam.id)

    def test_head_assigns_within_the_team_only(self, client) -> None:
        workflow_id = self._workflow_id(client)
        team_id = create_team(client)
        head = create_user(client, "HEAD", team_id=team_id)
        member = create_user(client, "KAM", team_id=team_id)
        outsider = create_user(client, "KAM", team_id=create_team(client))
        sign_in(client, head)

        assigned = self._create(client, workflow_id, member.id)
        assert assigned.status_code == 201, assigned.text
        assert assigned.json()["owner_id"] == str(member.id)

        refused = self._create(client, workflow_id, outsider.id)
        assert refused.status_code == 403, refused.text

    def test_admin_assigns_any_active_user(self, client) -> None:
        workflow_id = self._workflow_id(client)
        kam = create_user(client, "KAM")

        assigned = self._create(client, workflow_id, kam.id)
        assert assigned.status_code == 201, assigned.text
        assert assigned.json()["owner_id"] == str(kam.id)

    def test_admin_cannot_assign_a_missing_or_blocked_user(self, client) -> None:
        workflow_id = self._workflow_id(client)

        missing = self._create(client, workflow_id, uuid.uuid4())
        assert missing.status_code == 404, missing.text

        blocked = self._create(client, workflow_id, create_user(client, "KAM", status="blocked").id)
        assert blocked.status_code == 422, blocked.text
        assert blocked.json()["errors"][0]["field"] == "owner_id"

    def test_integration_picks_the_owner_itself(self, client) -> None:
        # Вебхук CMS создаёт сделку от имени интеграции и назначает наименее загруженного КАМа.
        workflow_id = self._workflow_id(client)
        kam = create_user(client, "KAM")
        sign_in(client, create_user(client, "INTEGRATION"))

        response = client.post(
            "/api/deals",
            json={
                "title": "Заявка с сайта",
                "deal_type": "b2b",
                "workflow_id": workflow_id,
                "organization_id": create_organization(client),
                "owner_id": str(kam.id),
                "source": "cms",
                "external_ids": {"cms_lead_id": uuid.uuid4().hex},
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["owner_id"] == str(kam.id)


class TestTransitionRequiredFields:
    """A#5: `required_fields` целевого статуса проверяются при переходе, а не только
    `conditions`. Настоящая Postgres обязательна — см. `tests/conftest.py`."""

    pytestmark = pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
    )

    def _deal_before_won(self, client, required_fields: list[str], **transition):
        login(client)
        graph = create_published_workflow(
            client,
            graph_body(
                statuses=[
                    {"code": "new", "name": "Новая", "type": "initial"},
                    {
                        "code": "won",
                        "name": "Закрыта",
                        "type": "won",
                        "required_fields": required_fields,
                    },
                ],
                transitions=[
                    {"from_status": "new", "to_status": "won", "name": "Закрыть", **transition}
                ],
                sla_rules=[],
            ),
        )
        return by_code(graph)["won"], create_deal(client, graph["workflow"]["id"])

    def test_missing_required_fields_block_the_transition(self, client) -> None:
        won, deal = self._deal_before_won(client, ["amount", "custom_fields.contract_number"])

        response = transition_deal(client, deal, won["id"])
        assert response.status_code == 422, response.text
        problem = response.json()
        assert problem["code"] == "CRM-1205"
        assert [e["field"] for e in problem["errors"]] == [
            "amount",
            "custom_fields.contract_number",
        ]
        assert (
            client.get(f"/api/deals/{deal['id']}").json()["deal"]["status_id"] == deal["status_id"]
        )

    def test_fields_sent_with_the_transition_satisfy_them(self, client) -> None:
        won, deal = self._deal_before_won(client, ["amount", "custom_fields.contract_number"])

        response = transition_deal(
            client,
            deal,
            won["id"],
            fields={"amount": "1000", "custom_fields.contract_number": "Д-1"},
        )
        assert response.status_code == 200, response.text
        moved = response.json()["deal"]
        assert moved["status_id"] == won["id"]
        assert float(moved["amount"]) == 1000
        assert moved["custom_fields"]["contract_number"] == "Д-1"

    def test_a_zero_or_false_custom_value_counts_as_filled(self, client) -> None:
        won, deal = self._deal_before_won(client, ["custom_fields.paid"])

        response = transition_deal(client, deal, won["id"], fields={"custom_fields.paid": False})
        assert response.status_code == 200, response.text

    def test_condition_error_keeps_priority_over_required_fields(self, client) -> None:
        # Сид-воронки дублируют `required_fields` условием — фронтенд ждёт от них CRM-1201.
        won, deal = self._deal_before_won(
            client, ["amount"], conditions={"field": "amount", "op": "not_null"}
        )

        response = transition_deal(client, deal, won["id"])
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "CRM-1201"


class TestDealCardCounters:
    """A#6: счётчики карточки не устаревают после комментария или задачи. Карточка
    кэшируется по `version` сделки, а комментарии и задачи её не меняют. Настоящая
    Postgres обязательна — см. `tests/conftest.py`."""

    pytestmark = pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
    )

    def _deal(self, client):
        user = login(client)
        graph = create_published_workflow(client)
        return user, create_deal(client, graph["workflow"]["id"])

    def _card(self, client, deal: dict) -> dict:
        response = client.get(f"/api/deals/{deal['id']}")
        assert response.status_code == 200, response.text
        return response.json()

    def test_comment_counter_follows_create_and_delete(self, client) -> None:
        _user, deal = self._deal(client)
        assert self._card(client, deal)["comments_count"] == 0  # карточка попала в кэш

        created = client.post(f"/api/deals/{deal['id']}/comments", json={"body": "Первый"})
        assert created.status_code == 201, created.text
        assert self._card(client, deal)["comments_count"] == 1

        deleted = client.request(
            "DELETE", f"/api/comments/{created.json()['id']}", json={"reason": "лишний"}
        )
        assert deleted.status_code == 200, deleted.text
        assert self._card(client, deal)["comments_count"] == 0

    def test_open_tasks_counter_follows_create_and_status_changes(self, client) -> None:
        user, deal = self._deal(client)
        assert self._card(client, deal)["open_tasks_count"] == 0

        created = client.post(
            "/api/tasks",
            json={"deal_id": deal["id"], "title": "Позвонить", "assignee_id": str(user.id)},
        )
        assert created.status_code == 201, created.text
        task_id = created.json()["id"]
        assert self._card(client, deal)["open_tasks_count"] == 1

        done = client.post(f"/api/tasks/{task_id}/complete")
        assert done.status_code == 200, done.text
        assert self._card(client, deal)["open_tasks_count"] == 0

        reopened = client.patch(f"/api/tasks/{task_id}", json={"status": "open"})
        assert reopened.status_code == 200, reopened.text
        assert self._card(client, deal)["open_tasks_count"] == 1

        cancelled = client.patch(f"/api/tasks/{task_id}", json={"status": "cancelled"})
        assert cancelled.status_code == 200, cancelled.text
        assert self._card(client, deal)["open_tasks_count"] == 0


class TestSlaSweepWarnThreshold:
    """A#9: порог предупреждения — `warn_threshold_pct` правила SLA статуса, а не зашитые
    75%. Настоящая Postgres обязательна — см. `tests/conftest.py`."""

    pytestmark = pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
    )

    def _sweep_after(self, client, *, warn_pct: int, elapsed_share: float) -> str:
        """Сделка в статусе с SLA на 10 часов, в котором она уже провела `elapsed_share`
        срока; возвращает `sla_state` после прохода `sweep_sla_breaches`."""
        from sqlalchemy import update

        from app.core.db import session_scope
        from app.modules.crm.tasks import sweep_sla_breaches

        login(client)
        graph = create_published_workflow(
            client,
            graph_body(
                sla_rules=[
                    {
                        "status": "work",
                        "max_duration_hours": 10,
                        "warn_threshold_pct": warn_pct,
                        "count_business_days": False,
                    }
                ]
            ),
        )
        deal = create_deal(client, graph["workflow"]["id"])
        deal_id = transition_deal(client, deal, by_code(graph)["work"]["id"]).json()["deal"]["id"]

        entered_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=10 * elapsed_share)

        async def _move_entry_time_to_the_past() -> None:
            async with session_scope() as session:
                await session.execute(
                    update(Deal)
                    .where(Deal.id == uuid.UUID(deal_id))
                    .values(
                        status_changed_at=entered_at,
                        sla_due_at=entered_at + dt.timedelta(hours=10),
                    )
                )

        run(client, _move_entry_time_to_the_past)
        run(client, sweep_sla_breaches, {})
        return client.get(f"/api/deals/{deal_id}").json()["deal"]["sla_state"]

    def test_lower_threshold_of_the_rule_warns_earlier(self, client) -> None:
        assert self._sweep_after(client, warn_pct=50, elapsed_share=0.6) == "warning"

    def test_higher_threshold_of_the_rule_does_not_warn_yet(self, client) -> None:
        assert self._sweep_after(client, warn_pct=90, elapsed_share=0.8) == "ok"

    def test_breach_does_not_depend_on_the_threshold(self, client) -> None:
        assert self._sweep_after(client, warn_pct=90, elapsed_share=1.2) == "breached"


def _attach_to_deal(client, deal_id: str, category: str, *, deleted: bool = False) -> None:
    """Вложение сделки в обход загрузки в S3: готовый файл и привязка к сделке."""

    async def _create() -> None:
        from app.core.db import session_scope
        from app.modules.files.models import Attachment, File

        async with session_scope() as session:
            file = File(
                storage_key=f"test/{uuid.uuid4()}.pdf",
                bucket="files",
                original_filename="Договор.pdf",
                mime_type="application/pdf",
                size_bytes=10,
                status="ready",
                refcount=1,
            )
            session.add(file)
            await session.flush()
            session.add(
                Attachment(
                    file_id=file.id,
                    entity_type="deal",
                    entity_id=uuid.UUID(deal_id),
                    category=category,
                    deleted_at=dt.datetime.now(dt.UTC) if deleted else None,
                )
            )

    run(client, _create)


class TestAttachmentGuard:
    """A#3: `attachments.{category}` в условиях перехода строится из вложений сделки
    (ветка «вложение» сид-воронок раньше не выполнялась никогда). Настоящая Postgres
    обязательна — см. `tests/conftest.py`."""

    pytestmark = pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
    )

    def _deal_needing_a_contract(self, client):
        login(client)
        graph = create_published_workflow(
            client,
            graph_body(
                transitions=[
                    {
                        "from_status": "new",
                        "to_status": "work",
                        "name": "В работу",
                        "conditions": {"field": "attachments.contract", "op": "exists"},
                    },
                    {"from_status": "work", "to_status": "won", "name": "Закрыть"},
                ],
                sla_rules=[],
            ),
        )
        return by_code(graph)["work"], create_deal(client, graph["workflow"]["id"])

    def _available(self, client, deal: dict) -> dict:
        response = client.get(f"/api/deals/{deal['id']}/available-transitions")
        assert response.status_code == 200, response.text
        return response.json()["items"][0]

    def test_transition_is_refused_without_an_attachment(self, client) -> None:
        work, deal = self._deal_needing_a_contract(client)

        assert self._available(client, deal)["satisfied"] is False
        response = transition_deal(client, deal, work["id"])
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "CRM-1201"

    def test_attachment_of_the_category_opens_the_transition(self, client) -> None:
        work, deal = self._deal_needing_a_contract(client)
        _attach_to_deal(client, deal["id"], "contract")

        assert self._available(client, deal)["satisfied"] is True
        response = transition_deal(client, deal, work["id"])
        assert response.status_code == 200, response.text
        assert response.json()["deal"]["status_id"] == work["id"]

    def test_other_category_or_deleted_attachment_does_not_count(self, client) -> None:
        work, deal = self._deal_needing_a_contract(client)
        _attach_to_deal(client, deal["id"], "act")
        _attach_to_deal(client, deal["id"], "contract", deleted=True)

        response = transition_deal(client, deal, work["id"])
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "CRM-1201"


class TestConditionsTree:
    """A#4: `conditions_tree` в `available-transitions` сохраняет структуру `all`/`any` с
    `satisfied` на каждом узле — по плоскому списку не понять, что достаточно одного из
    условий. Настоящая Postgres обязательна — см. `tests/conftest.py`."""

    pytestmark = pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
    )

    def _deal(self, client) -> dict:
        login(client)
        graph = create_published_workflow(
            client,
            graph_body(
                transitions=[
                    {
                        "from_status": "new",
                        "to_status": "work",
                        "name": "В работу",
                        "conditions": {
                            "any": [
                                {"field": "signature_status", "op": "eq", "value": "signed"},
                                {"field": "attachments.contract", "op": "exists"},
                            ]
                        },
                    },
                    {"from_status": "work", "to_status": "won", "name": "Закрыть"},
                ],
                sla_rules=[],
            ),
        )
        return create_deal(client, graph["workflow"]["id"])

    def _item(self, client, deal: dict) -> dict:
        response = client.get(f"/api/deals/{deal['id']}/available-transitions")
        assert response.status_code == 200, response.text
        return response.json()["items"][0]

    def test_tree_keeps_the_any_group_and_marks_every_node(self, client) -> None:
        item = self._item(client, self._deal(client))

        tree = item["conditions_tree"]
        assert tree["satisfied"] is False
        assert [leaf["field"] for leaf in tree["any"]] == [
            "signature_status",
            "attachments.contract",
        ]
        assert [leaf["satisfied"] for leaf in tree["any"]] == [False, False]
        assert tree["any"][0]["value"] == "signed"
        assert tree["any"][0]["actual"] == "none"
        # Плоский список остаётся как был.
        assert [c["field"] for c in item["conditions"]] == [
            "signature_status",
            "attachments.contract",
        ]

    def test_one_branch_of_any_is_enough(self, client) -> None:
        deal = self._deal(client)
        _attach_to_deal(client, deal["id"], "contract")

        tree = self._item(client, deal)["conditions_tree"]
        assert tree["satisfied"] is True
        assert [leaf["satisfied"] for leaf in tree["any"]] == [False, True]

    def test_transition_without_conditions_has_an_empty_tree(self, client) -> None:
        login(client)
        graph = create_published_workflow(client)
        deal = create_deal(client, graph["workflow"]["id"])

        assert self._item(client, deal)["conditions_tree"] == {}


def _create_product(client, **fields) -> dict:
    """Продукт каталога (нужна роль ADMIN: `catalog:write`)."""
    response = client.post(
        "/api/products", json={"code": f"prod-{uuid.uuid4().hex[:8]}", "name": "Курс", **fields}
    )
    assert response.status_code == 201, response.text
    return response.json()


class TestReplaceDealProducts:
    """A#7: продукты сделки меняются после создания — `PUT /deals/{id}/products`. Настоящая
    Postgres обязательна — см. `tests/conftest.py`."""

    pytestmark = pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
    )

    def _deal_with(self, client, *product_ids: str) -> dict:
        login(client)
        graph = create_published_workflow(client)
        return create_deal(
            client,
            graph["workflow"]["id"],
            products=[{"product_id": product_id} for product_id in product_ids],
        )

    def _put(self, client, deal: dict, items: list[dict], *, version: int | None = None):
        return client.put(
            f"/api/deals/{deal['id']}/products",
            json={"items": items},
            headers={"If-Match": str(version if version is not None else deal["version"])},
        )

    def test_replaces_the_whole_list_and_bumps_the_version(self, client) -> None:
        login(client)
        first, second, third = (_create_product(client)["id"] for _ in range(3))
        deal = self._deal_with(client, first)

        response = self._put(
            client,
            deal,
            [
                {"product_id": second, "quantity": 3, "price": "100.00", "total": "300.00"},
                {"product_id": third},
            ],
        )
        assert response.status_code == 200, response.text
        card = response.json()
        assert {p["product_id"]: p["quantity"] for p in card["products"]} == {second: 3, third: 1}
        assert card["deal"]["version"] == deal["version"] + 1

        # Карточка (в том числе из кэша по версии) показывает то же.
        again = client.get(f"/api/deals/{deal['id']}").json()
        assert {p["product_id"] for p in again["products"]} == {second, third}

    def test_empty_list_clears_the_products(self, client) -> None:
        login(client)
        deal = self._deal_with(client, _create_product(client)["id"])

        response = self._put(client, deal, [])
        assert response.status_code == 200, response.text
        assert response.json()["products"] == []

    def test_body_without_items_does_not_wipe_the_products(self, client) -> None:
        login(client)
        kept = _create_product(client)["id"]
        deal = self._deal_with(client, kept)

        response = client.put(
            f"/api/deals/{deal['id']}/products", json={}, headers={"If-Match": str(deal["version"])}
        )
        assert response.status_code == 422, response.text
        assert response.json()["errors"][0]["field"] == "items"
        card = client.get(f"/api/deals/{deal['id']}").json()
        assert [p["product_id"] for p in card["products"]] == [kept]

    def test_stale_version_is_a_conflict(self, client) -> None:
        deal = self._deal_with(client)

        response = self._put(client, deal, [], version=deal["version"] + 5)
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "CRM-1002"

    def test_unknown_product_is_refused_and_nothing_changes(self, client) -> None:
        login(client)
        kept = _create_product(client)["id"]
        deal = self._deal_with(client, kept)

        response = self._put(client, deal, [{"product_id": str(uuid.uuid4())}])
        assert response.status_code == 404, response.text
        card = client.get(f"/api/deals/{deal['id']}").json()
        assert [p["product_id"] for p in card["products"]] == [kept]
        assert card["deal"]["version"] == deal["version"]

    def test_deal_out_of_scope_is_hidden(self, client) -> None:
        deal = self._deal_with(client)
        sign_in(client, create_user(client, "KAM"))

        response = self._put(client, deal, [])
        assert response.status_code == 404, response.text


class TestDealListing:
    """A#11: `total`, `sort`, несколько `status_id`, `is_closed` в `GET /deals`. Настоящая
    Postgres обязательна — см. `tests/conftest.py`."""

    pytestmark = pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
    )

    def _workflow(self, client) -> tuple[dict, dict]:
        login(client)
        graph = create_published_workflow(client)
        return graph, by_code(graph)

    def _ids(self, response) -> list[str]:
        assert response.status_code == 200, response.text
        return [item["id"] for item in response.json()["items"]]

    def _walk(self, client, **params) -> list[str]:
        """Все страницы списка подряд: `limit=2`, пока есть `next_cursor`."""
        collected: list[str] = []
        cursor = None
        for _ in range(10):
            page = client.get(
                "/api/deals",
                params={"limit": 2, **params, **({"cursor": cursor} if cursor else {})},
            )
            assert page.status_code == 200, page.text
            body = page.json()
            collected += [item["id"] for item in body["items"]]
            cursor = body["next_cursor"]
            if cursor is None:
                return collected
        raise AssertionError("курсор не закончился")

    def test_total_counts_the_whole_filtered_set(self, client) -> None:
        graph, _ = self._workflow(client)
        workflow_id = graph["workflow"]["id"]
        for _ in range(5):
            create_deal(client, workflow_id)

        page = client.get("/api/deals", params={"workflow_id": workflow_id, "limit": 2})
        assert page.status_code == 200, page.text
        body = page.json()
        assert (len(body["items"]), body["total"]) == (2, 5)
        assert body["next_cursor"] is not None

    def test_status_id_accepts_several_values_and_is_closed_splits_open_from_closed(
        self, client
    ) -> None:
        login(client)
        # У сделки один переход: лок перехода в fakeredis без Lua не отпускается (30 с).
        graph = create_published_workflow(
            client,
            graph_body(
                transitions=[
                    {"from_status": "new", "to_status": "work", "name": "В работу"},
                    {"from_status": "new", "to_status": "won", "name": "Сразу закрыть"},
                    {"from_status": "work", "to_status": "won", "name": "Закрыть"},
                ]
            ),
        )
        statuses = by_code(graph)
        workflow_id = graph["workflow"]["id"]
        in_new = create_deal(client, workflow_id)
        in_work = transition_deal(
            client, create_deal(client, workflow_id), statuses["work"]["id"]
        ).json()["deal"]
        done = transition_deal(
            client, create_deal(client, workflow_id), statuses["won"]["id"]
        ).json()["deal"]
        params = {"workflow_id": workflow_id}

        several = client.get(
            "/api/deals",
            params={**params, "status_id": [statuses["new"]["id"], statuses["work"]["id"]]},
        )
        assert set(self._ids(several)) == {in_new["id"], in_work["id"]}
        assert several.json()["total"] == 2

        single = client.get("/api/deals", params={**params, "status_id": statuses["won"]["id"]})
        assert self._ids(single) == [done["id"]]

        closed = client.get("/api/deals", params={**params, "is_closed": "true"})
        assert self._ids(closed) == [done["id"]]
        opened = client.get("/api/deals", params={**params, "is_closed": "false"})
        assert set(self._ids(opened)) == {in_new["id"], in_work["id"]}

    def test_sort_by_a_nullable_column_pages_through_every_deal_once(self, client) -> None:
        graph, _ = self._workflow(client)
        workflow_id = graph["workflow"]["id"]
        amounts = [None, "100.00", "100.00", "300.00", None]
        deals = [
            create_deal(client, workflow_id, title=f"Сделка {i}", **({"amount": a} if a else {}))
            for i, a in enumerate(amounts)
        ]
        by_id = {d["id"]: d for d in deals}

        ascending = self._walk(client, workflow_id=workflow_id, sort="amount")
        assert sorted(ascending) == sorted(by_id)  # каждая сделка ровно один раз
        assert [by_id[i]["amount"] for i in ascending] == ["100.00", "100.00", "300.00", None, None]

        descending = self._walk(client, workflow_id=workflow_id, sort="-amount")
        assert sorted(descending) == sorted(by_id)
        assert [by_id[i]["amount"] for i in descending] == [
            "300.00",
            "100.00",
            "100.00",
            None,
            None,
        ]

    def test_sort_by_title(self, client) -> None:
        graph, _ = self._workflow(client)
        workflow_id = graph["workflow"]["id"]
        created = {t: create_deal(client, workflow_id, title=t)["id"] for t in ("Б", "А", "В")}

        ascending = self._walk(client, workflow_id=workflow_id, sort="title")
        assert ascending == [created["А"], created["Б"], created["В"]]
        descending = self._walk(client, workflow_id=workflow_id, sort="-title")
        assert descending == [created["В"], created["Б"], created["А"]]

    def test_sort_by_a_date_and_by_a_timestamp(self, client) -> None:
        graph, _ = self._workflow(client)
        workflow_id = graph["workflow"]["id"]
        dated = {
            date: create_deal(client, workflow_id, expected_close_date=date)["id"]
            for date in ("2026-12-01", "2026-03-15", "2026-07-20")
        }
        undated = create_deal(client, workflow_id)["id"]

        by_date = self._walk(client, workflow_id=workflow_id, sort="expected_close_date")
        assert by_date == [dated["2026-03-15"], dated["2026-07-20"], dated["2026-12-01"], undated]
        by_date_desc = self._walk(client, workflow_id=workflow_id, sort="-expected_close_date")
        assert by_date_desc == [
            dated["2026-12-01"],
            dated["2026-07-20"],
            dated["2026-03-15"],
            undated,
        ]

        # Курсор по `created_at` восходящей сортировки несёт метку времени с часовым поясом.
        oldest_first = self._walk(client, workflow_id=workflow_id, sort="created_at")
        assert oldest_first == [*dated.values(), undated]

    def test_default_order_is_newest_first(self, client) -> None:
        graph, _ = self._workflow(client)
        workflow_id = graph["workflow"]["id"]
        first = create_deal(client, workflow_id)["id"]
        second = create_deal(client, workflow_id)["id"]

        assert self._walk(client, workflow_id=workflow_id) == [second, first]

    def test_cursor_from_another_sort_is_a_validation_error(self, client) -> None:
        from app.core.pagination import Cursor

        self._workflow(client)
        foreign = Cursor(value="не число", id=uuid.uuid4()).encode()

        response = client.get("/api/deals", params={"sort": "amount", "cursor": foreign})
        assert response.status_code == 422, response.text
        assert response.json()["errors"][0]["field"] == "cursor"

    def test_unknown_sort_is_a_validation_error(self, client) -> None:
        self._workflow(client)

        response = client.get("/api/deals", params={"sort": "owner_id"})
        assert response.status_code == 422, response.text
        assert response.json()["errors"][0]["field"] == "sort"


class TestRelatedNames:
    """A#13, A#29: название организации и имя контакта в `DealOut`, номер и название сделки в
    `TaskOut` — таблицам не нужен запрос на каждую строку. Настоящая Postgres обязательна —
    см. `tests/conftest.py`."""

    pytestmark = pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
    )

    def test_deal_carries_the_organization_name_everywhere(self, client) -> None:
        user = login(client)
        graph = create_published_workflow(client)
        workflow_id = graph["workflow"]["id"]
        organization_id = create_organization(client, "Вуз «Тест-А»")

        deal = create_deal(client, workflow_id, organization_id=organization_id)
        assert deal["organization_name"] == "Вуз «Тест-А»"
        assert deal["contact_name"] is None

        listed = client.get("/api/deals", params={"workflow_id": workflow_id}).json()["items"]
        assert [d["organization_name"] for d in listed] == ["Вуз «Тест-А»"]
        card = client.get(f"/api/deals/{deal['id']}").json()
        assert card["deal"]["organization_name"] == "Вуз «Тест-А»"

        moved = transition_deal(client, deal, by_code(graph)["work"]["id"]).json()["deal"]
        assert moved["organization_name"] == "Вуз «Тест-А»"
        assert user.id  # сделка создана и ведётся одним пользователем

    def test_contact_name_is_shown_only_with_contact_read(self, client) -> None:
        login(client)
        graph = create_published_workflow(client, deal_type="b2c")
        contact_id = create_contact(client, "Петров", "Пётр", "Петрович")
        payload = {
            "title": "Заявка физлица",
            "deal_type": "b2c",
            "workflow_id": graph["workflow"]["id"],
            "contact_id": contact_id,
        }

        created = client.post("/api/deals", json=payload)
        assert created.status_code == 201, created.text
        assert created.json()["contact_name"] == "Петров Пётр Петрович"

        # Интеграция создаёт сделки, но ФИО контактов не читает (`contact:read` у неё нет).
        sign_in(client, create_user(client, "INTEGRATION"))
        by_integration = client.post(
            "/api/deals",
            json={**payload, "source": "cms", "external_ids": {"cms_lead_id": uuid.uuid4().hex}},
        )
        assert by_integration.status_code == 201, by_integration.text
        assert by_integration.json()["contact_name"] is None

    def test_task_carries_the_deal_number_and_title(self, client) -> None:
        user = login(client)
        graph = create_published_workflow(client)
        deal = create_deal(client, graph["workflow"]["id"], title="Сделка для задачи")

        created = client.post(
            "/api/tasks",
            json={"deal_id": deal["id"], "title": "Позвонить", "assignee_id": str(user.id)},
        )
        assert created.status_code == 201, created.text
        task = created.json()
        assert (task["deal_number"], task["deal_title"]) == (deal["number"], "Сделка для задачи")

        listed = client.get("/api/tasks", params={"deal_id": deal["id"]}).json()["items"]
        assert [(t["deal_number"], t["deal_title"]) for t in listed] == [
            (deal["number"], "Сделка для задачи")
        ]

        updated = client.patch(f"/api/tasks/{task['id']}", json={"priority": "high"}).json()
        assert updated["deal_number"] == deal["number"]
        completed = client.post(f"/api/tasks/{task['id']}/complete").json()
        assert completed["deal_title"] == "Сделка для задачи"


class TestCommentsAndHistoryPagination:
    """A#12: комментарии и история сделки отдаются страницами, если передан `limit`; без него —
    целиком, как раньше. Настоящая Postgres обязательна — см. `tests/conftest.py`."""

    pytestmark = pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
    )

    def _deal(self, client):
        user = login(client)
        graph = create_published_workflow(client)
        return user, graph, create_deal(client, graph["workflow"]["id"])

    def _comment(self, client, deal: dict, body: str) -> str:
        response = client.post(f"/api/deals/{deal['id']}/comments", json={"body": body})
        assert response.status_code == 201, response.text
        return response.json()["id"]

    def test_comments_without_limit_come_whole(self, client) -> None:
        _user, _graph, deal = self._deal(client)
        expected = [self._comment(client, deal, f"Комментарий {i}") for i in range(3)]

        response = client.get(f"/api/deals/{deal['id']}/comments")
        assert response.status_code == 200, response.text
        assert [c["id"] for c in response.json()["items"]] == expected
        assert response.json()["next_cursor"] is None

    def test_comments_are_paged_in_chronological_order(self, client) -> None:
        _user, _graph, deal = self._deal(client)
        expected = [self._comment(client, deal, f"Комментарий {i}") for i in range(5)]

        collected: list[str] = []
        cursor = None
        for _ in range(5):
            page = client.get(
                f"/api/deals/{deal['id']}/comments",
                params={"limit": 2, **({"cursor": cursor} if cursor else {})},
            )
            assert page.status_code == 200, page.text
            collected += [c["id"] for c in page.json()["items"]]
            cursor = page.json()["next_cursor"]
            if cursor is None:
                break
        assert collected == expected

    def test_history_is_paged_per_list(self, client) -> None:
        _user, graph, deal = self._deal(client)
        moved = transition_deal(client, deal, by_code(graph)["work"]["id"]).json()["deal"]
        successor = create_user(client, "KAM")
        reassigned = client.post(
            f"/api/deals/{deal['id']}/reassign",
            json={"owner_id": str(successor.id), "reason": "проверка"},
            headers={"If-Match": str(moved["version"])},
        )
        assert reassigned.status_code == 200, reassigned.text

        whole = client.get(f"/api/deals/{deal['id']}/history").json()
        assert len(whole["statuses"]) == 2  # создание и переход
        assert len(whole["events"]) == 2  # создание и смена ответственного
        assert whole["next_statuses_cursor"] is None and whole["next_events_cursor"] is None

        # Списки листаются независимо: у каждого свой курсор.
        first = client.get(f"/api/deals/{deal['id']}/history", params={"limit": 1}).json()
        assert (len(first["statuses"]), len(first["events"])) == (1, 1)
        assert first["next_statuses_cursor"] and first["next_events_cursor"]

        rest = client.get(
            f"/api/deals/{deal['id']}/history",
            params={
                "limit": 1,
                "statuses_cursor": first["next_statuses_cursor"],
                "events_cursor": first["next_events_cursor"],
            },
        ).json()
        assert [first["statuses"][0]["id"], rest["statuses"][0]["id"]] == [
            s["id"] for s in whole["statuses"]
        ]
        assert [first["events"][0]["id"], rest["events"][0]["id"]] == [
            e["id"] for e in whole["events"]
        ]
        assert rest["next_statuses_cursor"] is None and rest["next_events_cursor"] is None

    def test_limit_is_bounded(self, client) -> None:
        _user, _graph, deal = self._deal(client)

        assert (
            client.get(f"/api/deals/{deal['id']}/comments", params={"limit": 0}).status_code == 422
        )
        assert (
            client.get(f"/api/deals/{deal['id']}/history", params={"limit": 101}).status_code == 422
        )
