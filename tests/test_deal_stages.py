"""B2-10: сводка исполнения этапов (шаг 14 задания), `GET /api/deals/{id}/stages`
(`app/modules/crm/service.py::DealService.stage_summary`, `app/modules/crm/router.py`).

Плюс защитная правка в `DealService.transition`: `DealStatusHistory.sla_state_at_change`
раньше писал состояние НОВОГО статуса (`apply_sla` сбрасывает его в `ok` сразу после
входа) — по истории было не восстановить, был ли просрочен статус, который сделка
ПОКИДАЕТ. Теперь пишется состояние покидаемого статуса, а если срок истёк между сканами
(раз в 15 минут) — `"breached"` считается прямо в момент перехода, не дожидаясь скана.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import event, select

from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run
from tests.crm_helpers import create_deal, transition_deal

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _login(client, user) -> None:
    client.headers["X-CSRF-Token"] = authenticate(client, user)


async def _v2_id() -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.workflow.models import Workflow
    from app.modules.workflow.seed import _b2b_spec, seed_workflow

    async with session_scope() as session:
        workflow = await seed_workflow(session, _b2b_spec(), is_default=False)
        if workflow is not None:
            return workflow.id
        existing = await session.scalar(
            select(Workflow).where(Workflow.code == "b2b_university_v2")
        )
        assert existing is not None
        return existing.id


async def _status_id(workflow_id: uuid.UUID, code: str) -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.workflow.models import WorkflowStatus

    async with session_scope() as session:
        status_id = await session.scalar(
            select(WorkflowStatus.id).where(
                WorkflowStatus.workflow_id == workflow_id, WorkflowStatus.code == code
            )
        )
        assert status_id is not None, f"статус {code!r} не найден"
        return status_id


async def _set_deal(deal_id: uuid.UUID, values: dict[str, object]) -> None:
    from sqlalchemy import update

    from app.core.db import session_scope
    from app.modules.crm.models import Deal

    async with session_scope() as session:
        await session.execute(update(Deal).where(Deal.id == deal_id).values(**values))


async def _contact() -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.catalog.models import Contact

    async with session_scope() as session:
        contact = Contact(last_name="Петров", first_name="Пётр", middle_name=None)
        session.add(contact)
        await session.flush()
        return contact.id


async def _attach_contract(deal_id: uuid.UUID, status_id: uuid.UUID) -> None:
    from app.core.db import session_scope
    from app.modules.files.models import Attachment, File

    async with session_scope() as session:
        file = File(
            storage_key=f"{uuid.uuid4()}/dogovor.pdf",
            bucket="files",
            original_filename="dogovor.pdf",
            mime_type="application/pdf",
            size_bytes=10,
            sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            status="ready",
            uploaded_by=None,
        )
        session.add(file)
        await session.flush()
        session.add(
            Attachment(
                file_id=file.id,
                entity_type="deal",
                entity_id=deal_id,
                category="contract",
                status_id=status_id,
            )
        )
        await session.flush()


async def _last_history_sla_state(deal_id: uuid.UUID) -> str | None:
    from app.core.db import session_scope
    from app.modules.crm.models import DealStatusHistory

    async with session_scope() as session:
        row = (
            (
                await session.execute(
                    select(DealStatusHistory)
                    .where(DealStatusHistory.deal_id == deal_id)
                    .order_by(DealStatusHistory.changed_at.desc())
                )
            )
            .scalars()
            .first()
        )
        assert row is not None
        return row.sla_state_at_change


class TestStageSummaryAcceptanceScenario:
    """Ровно сценарий из приёмки B2-10: 1→2→3→4→6 (в обход 5), просрочка на шаге 4."""

    def test_the_documented_walk_matches_the_contract_example(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        workflow_id = run(client, _v2_id)
        deal = create_deal(client, str(workflow_id))
        deal_id = uuid.UUID(deal["id"])
        run(client, _set_deal, deal_id, {"contact_id": run(client, _contact)})

        communication = run(client, _status_id, workflow_id, "communication")
        response = transition_deal(client, deal, str(communication), comment="Поговорили")
        assert response.status_code == 200, response.text
        deal = response.json()["deal"]

        meeting = run(client, _status_id, workflow_id, "meeting")
        response = transition_deal(client, deal, str(meeting), comment="Итог разговора")
        assert response.status_code == 200, response.text
        deal = response.json()["deal"]

        documents_exchange = run(client, _status_id, workflow_id, "documents_exchange")
        response = transition_deal(client, deal, str(documents_exchange))
        assert response.status_code == 200, response.text
        deal = response.json()["deal"]

        # На шаге 4: срок истёк и приложен файл категории contract (условие перехода 4→6).
        run(
            client,
            _set_deal,
            deal_id,
            {"sla_due_at": dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)},
        )
        run(client, _attach_contract, deal_id, documents_exchange)

        signing = run(client, _status_id, workflow_id, "signing")
        response = transition_deal(client, deal, str(signing))
        assert response.status_code == 200, response.text

        page = client.get(f"/api/deals/{deal_id}/stages")
        assert page.status_code == 200, page.text
        body = page.json()

        assert len(body["items"]) == 14
        assert body["current_status_id"] == str(signing)
        assert body["summary"] == {
            "done": 4,
            "current": 1,
            "skipped": 1,
            "pending": 8,
            "sla_breached": 1,
        }

        by_code = {item["code"]: item for item in body["items"]}
        assert by_code["contact_search"]["state"] == "done"
        assert by_code["contact_search"]["sla_breached"] is False
        assert by_code["communication"]["state"] == "done"
        assert by_code["meeting"]["state"] == "done"
        assert by_code["documents_exchange"]["state"] == "done"
        assert by_code["documents_exchange"]["sla_breached"] is True
        assert by_code["documents_exchange"]["files_count"] == 1
        assert by_code["documents_exchange"]["evidence"]["attached"] == ["contract"]
        assert by_code["documents_correction"]["state"] == "skipped"
        assert by_code["signing"]["state"] == "current"
        for code in (
            "materials_transfer",
            "implementation_support",
            "teacher_training",
            "curriculum_update",
            "classes",
            "docs_update",
            "teacher_qualification",
            "execution_control",
        ):
            assert by_code[code]["state"] == "pending", code

    def test_a_foreign_deal_is_not_found(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))

        response = client.get(f"/api/deals/{uuid.uuid4()}/stages")

        assert response.status_code == 404, response.text


class TestSlaStateAtChange:
    """Состояние покидаемого (не нового) статуса, см. докстринг модуля."""

    def test_on_time_writes_ok(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        workflow_id = run(client, _v2_id)
        deal = create_deal(client, str(workflow_id))
        run(client, _set_deal, uuid.UUID(deal["id"]), {"contact_id": run(client, _contact)})
        communication = run(client, _status_id, workflow_id, "communication")

        response = transition_deal(client, deal, str(communication), comment="Поговорили")

        assert response.status_code == 200, response.text
        assert run(client, _last_history_sla_state, uuid.UUID(deal["id"])) == "ok"

    def test_an_expired_deadline_writes_breached_without_waiting_for_the_scan(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        workflow_id = run(client, _v2_id)
        deal = create_deal(client, str(workflow_id))
        deal_id = uuid.UUID(deal["id"])
        run(client, _set_deal, deal_id, {"contact_id": run(client, _contact)})
        # Срок истёк, но периодический скан (раз в 15 минут) ещё не пересчитал
        # `deal.sla_state` — переход обязан увидеть просрочку сам, не дожидаясь скана.
        run(
            client,
            _set_deal,
            deal_id,
            {"sla_due_at": dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)},
        )
        communication = run(client, _status_id, workflow_id, "communication")

        response = transition_deal(client, deal, str(communication), comment="Поговорили")

        assert response.status_code == 200, response.text
        assert run(client, _last_history_sla_state, deal_id) == "breached"

    def test_leaving_parked_writes_paused(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        workflow_id = run(client, _v2_id)
        deal = create_deal(client, str(workflow_id))
        deal_id = uuid.UUID(deal["id"])
        parked = run(client, _status_id, workflow_id, "parked")
        contact_search = run(client, _status_id, workflow_id, "contact_search")

        response = transition_deal(
            client,
            deal,
            str(parked),
            comment="Вуз взял паузу",
            fields={"custom_fields.park_reason": "ждём бюджет"},
        )
        assert response.status_code == 200, response.text
        deal = response.json()["deal"]

        response = transition_deal(client, deal, str(contact_search), comment="Возобновили работу")

        assert response.status_code == 200, response.text
        assert run(client, _last_history_sla_state, deal_id) == "paused"


class TestStageSummaryQueryBudget:
    def test_the_endpoint_makes_at_most_six_queries(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        workflow_id = run(client, _v2_id)
        deal = create_deal(client, str(workflow_id))
        deal_id = deal["id"]
        run(client, _set_deal, uuid.UUID(deal_id), {"contact_id": run(client, _contact)})

        def _count_queries() -> int:
            from app.core.db import get_engine

            counter = {"n": 0}

            def _before(*_args: object, **_kwargs: object) -> None:
                counter["n"] += 1

            sync_engine = get_engine().sync_engine
            event.listen(sync_engine, "before_cursor_execute", _before)
            try:
                response = client.get(f"/api/deals/{deal_id}/stages")
            finally:
                event.remove(sync_engine, "before_cursor_execute", _before)
            assert response.status_code == 200, response.text
            return counter["n"]

        _count_queries()  # прогрев кэшей прав/графа воронки
        small = _count_queries()

        communication = run(client, _status_id, workflow_id, "communication")
        response = transition_deal(client, deal, str(communication), comment="Поговорили")
        deal = response.json()["deal"]
        meeting = run(client, _status_id, workflow_id, "meeting")
        transition_deal(client, deal, str(meeting), comment="Итог разговора")

        large = _count_queries()

        assert small > 0, "счётчик запросов не сработал — тест ничего не проверяет"
        assert small <= 6, f"{small} запросов на пустую историю — бюджет 6"
        assert large <= small + 2, (
            f"{small} запросов на короткую историю и {large} на более длинную — "
            "вероятен запрос на каждую запись истории/вложение."
        )
