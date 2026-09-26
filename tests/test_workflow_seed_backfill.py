"""Сид воронок: выход из «Заморожена» для воронок, развёрнутых старым сидом.

Старые сиды заводили `parked` без единого исходящего перехода. Новые несут «Возобновить» сразу, а
уже развёрнутую воронку сид не трогал. `backfill_resume_transitions` добавляет недостающее там, где
это безопасно для живых сделок (они идут по снимку `published_graph`): только добавляет, в снимок и
в живые строки сразу, и только когда выходов из `parked` нет ни в снимке, ни в черновике.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import create_deal, login, transition_deal

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _specs(name: str):
    """Текущая спецификация сида, её «старая» версия (без выходов из `parked`, свой `code`) и
    коды шагов, с которых сделку можно заморозить."""
    from app.modules.workflow import seed

    current = getattr(seed, name)()
    old = dataclasses.replace(
        current,
        code=f"seed_bf_{uuid.uuid4().hex[:8]}",
        transitions=[t for t in current.transitions if t.from_code != "parked"],
    )
    full = dataclasses.replace(current, code=old.code)
    steps = sorted(s.code for s in current.statuses if s.type in ("initial", "intermediate"))
    return old, full, steps


async def _seed(spec) -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.workflow.seed import seed_workflow

    async with session_scope() as session:
        # Не «по умолчанию»: у типа сделки в общей БД может уже быть своя опубликованная.
        workflow = await seed_workflow(session, spec, is_default=False)
        assert workflow is not None
        return workflow.id


async def _reseed(spec, workflow_id: uuid.UUID) -> int:
    """Повторный запуск сида на существующей воронке; возвращает, сколько переходов добавилось."""
    from sqlalchemy import func, select

    from app.core.db import session_scope
    from app.modules.workflow.models import WorkflowTransition
    from app.modules.workflow.seed import seed_workflow

    count = (
        select(func.count())
        .select_from(WorkflowTransition)
        .where(WorkflowTransition.workflow_id == workflow_id)
    )
    async with session_scope() as session:
        before = await session.scalar(count)
        assert await seed_workflow(session, spec) is None
        await session.flush()
        return await session.scalar(count) - before


async def _state(workflow_id: uuid.UUID) -> dict[str, Any]:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.workflow.models import (
        SlaRule,
        Workflow,
        WorkflowStatus,
        WorkflowTransition,
    )
    from app.modules.workflow.service import has_unpublished_changes, snapshot_digest

    async with session_scope() as session:
        workflow = await session.get(Workflow, workflow_id)
        statuses = list(
            (
                await session.execute(
                    select(WorkflowStatus).where(WorkflowStatus.workflow_id == workflow_id)
                )
            ).scalars()
        )
        transitions = list(
            (
                await session.execute(
                    select(WorkflowTransition).where(WorkflowTransition.workflow_id == workflow_id)
                )
            ).scalars()
        )
        sla_rules = list(
            (
                await session.execute(select(SlaRule).where(SlaRule.workflow_id == workflow_id))
            ).scalars()
        )
        code_of = {str(s.id): s.code for s in statuses}
        snapshot = workflow.published_graph
        return {
            "version": workflow.version,
            "hash": workflow.graph_hash,
            "hash_ok": workflow.graph_hash == snapshot_digest(snapshot),
            "in_sync": not has_unpublished_changes(workflow, statuses, transitions, sla_rules),
            "snapshot_ids": {t["id"] for t in snapshot["transitions"]},
            "snapshot_exits": sorted(
                code_of[t["to_status_id"]]
                for t in snapshot["transitions"]
                if code_of[t["from_status_id"]] == "parked"
            ),
            "live_exits": sorted(
                code_of[str(t.to_status_id)]
                for t in transitions
                if code_of[str(t.from_status_id)] == "parked"
            ),
            "live_ids": {str(t.id) for t in transitions},
        }


async def _add_exit_from_parked(workflow_id: uuid.UUID, publish: bool) -> None:
    """Выход `parked → identification`, заведённый администратором: в черновик и, при `publish`,
    ещё и в снимок."""
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.workflow.models import Workflow, WorkflowStatus, WorkflowTransition
    from app.modules.workflow.service import snapshot_digest, transition_snapshot_entry

    async with session_scope() as session:
        statuses = {
            s.code: s
            for s in (
                await session.execute(
                    select(WorkflowStatus).where(WorkflowStatus.workflow_id == workflow_id)
                )
            ).scalars()
        }
        row = WorkflowTransition(
            workflow_id=workflow_id,
            from_status_id=statuses["parked"].id,
            to_status_id=statuses["identification"].id,
            name="Свой выход",
            sort_order=1,
        )
        session.add(row)
        await session.flush()
        if publish:
            workflow = await session.get(Workflow, workflow_id)
            snapshot = {
                **workflow.published_graph,
                "transitions": [
                    *workflow.published_graph["transitions"],
                    transition_snapshot_entry(row),
                ],
            }
            workflow.published_graph = snapshot
            workflow.graph_hash = snapshot_digest(snapshot)


class TestBackfillResumeTransitions:
    @pytest.mark.parametrize("spec_name", ["_b2b_spec", "_b2c_spec"])
    def test_an_old_workflow_gets_a_way_out_of_parked(self, client, spec_name) -> None:
        old, full, steps = _specs(spec_name)
        workflow_id = run(client, _seed, old)
        before = run(client, _state, workflow_id)
        assert before["snapshot_exits"] == [] and before["live_exits"] == []

        added = run(client, _reseed, full, workflow_id)

        after = run(client, _state, workflow_id)
        assert added == len(steps)
        assert after["snapshot_exits"] == steps == after["live_exits"]
        # Существующие переходы не тронуты: их идентификаторы остались, добавились только новые.
        assert before["snapshot_ids"] < after["snapshot_ids"]
        assert after["snapshot_ids"] == after["live_ids"]
        assert after["hash"] != before["hash"] and after["hash_ok"]
        assert after["version"] == before["version"] + 1
        assert after["in_sync"] is True

    def test_a_second_run_changes_nothing(self, client) -> None:
        old, full, _ = _specs("_b2b_spec")
        workflow_id = run(client, _seed, old)
        run(client, _reseed, full, workflow_id)
        first = run(client, _state, workflow_id)

        added = run(client, _reseed, full, workflow_id)

        assert added == 0
        assert run(client, _state, workflow_id) == first

    def test_a_workflow_from_the_current_seed_is_left_alone(self, client) -> None:
        _, full, _ = _specs("_b2c_spec")
        workflow_id = run(client, _seed, full)
        before = run(client, _state, workflow_id)

        assert run(client, _reseed, full, workflow_id) == 0
        assert run(client, _state, workflow_id) == before

    def test_an_exit_configured_by_an_administrator_is_respected(self, client) -> None:
        old, full, _ = _specs("_b2b_spec")
        workflow_id = run(client, _seed, old)
        run(client, _add_exit_from_parked, workflow_id, True)
        before = run(client, _state, workflow_id)

        added = run(client, _reseed, full, workflow_id)

        assert added == 0
        assert run(client, _state, workflow_id) == before
        assert before["snapshot_exits"] == ["identification"]

    def test_an_exit_only_in_the_draft_is_not_published_behind_the_admins_back(
        self, client
    ) -> None:
        old, full, _ = _specs("_b2b_spec")
        workflow_id = run(client, _seed, old)
        run(client, _add_exit_from_parked, workflow_id, False)
        before = run(client, _state, workflow_id)

        added = run(client, _reseed, full, workflow_id)

        assert added == 0
        after = run(client, _state, workflow_id)
        assert after["snapshot_exits"] == [] and after == before

    def test_an_unpublished_workflow_is_left_alone(self, client) -> None:
        from app.core.db import session_scope
        from app.modules.workflow.models import Workflow

        old, full, _ = _specs("_b2b_spec")
        workflow_id = run(client, _seed, old)

        async def unpublish() -> None:
            async with session_scope() as session:
                (await session.get(Workflow, workflow_id)).state = "draft"

        run(client, unpublish)
        before = run(client, _state, workflow_id)

        assert run(client, _reseed, full, workflow_id) == 0
        assert run(client, _state, workflow_id) == before

    def test_the_change_is_written_to_the_audit_log(self, client) -> None:
        from sqlalchemy import select

        from app.core.db import session_scope
        from app.modules.audit.models import AuditLog

        old, full, steps = _specs("_b2b_spec")
        workflow_id = run(client, _seed, old)
        run(client, _reseed, full, workflow_id)

        async def entries() -> list[dict]:
            async with session_scope() as session:
                rows = await session.execute(
                    select(AuditLog.changes).where(
                        AuditLog.entity_id == workflow_id, AuditLog.action == "WORKFLOW_UPDATED"
                    )
                )
                return list(rows.scalars())

        (changes,) = run(client, entries)
        assert changes["resume_transitions_added"]["new"] == len(steps)


class TestFrozenDealsCanResume:
    def test_a_deal_frozen_under_the_old_seed_gets_back_to_work(self, client) -> None:
        from sqlalchemy import select, update

        from app.core.db import session_scope
        from app.modules.crm.models import Deal
        from app.modules.workflow.models import WorkflowStatus

        old, full, _ = _specs("_b2b_spec")
        workflow_id = run(client, _seed, old)
        login(client, "ADMIN")
        deal = create_deal(client, str(workflow_id))

        async def status_id(code: str) -> str:
            async with session_scope() as session:
                return str(
                    await session.scalar(
                        select(WorkflowStatus.id).where(
                            WorkflowStatus.workflow_id == workflow_id, WorkflowStatus.code == code
                        )
                    )
                )

        async def freeze() -> None:
            parked = uuid.UUID(await status_id("parked"))
            async with session_scope() as session:
                await session.execute(
                    update(Deal).where(Deal.id == uuid.UUID(deal["id"])).values(status_id=parked)
                )

        run(client, freeze)
        first_step = run(client, status_id, "identification")

        def available() -> list[str]:
            response = client.get(f"/api/deals/{deal['id']}/available-transitions")
            assert response.status_code == 200, response.text
            return [item["to_status_id"] for item in response.json()["items"]]

        assert available() == []  # до сида выхода нет

        run(client, _reseed, full, workflow_id)

        assert first_step in available()
        # Версия сделки не менялась (статус правился напрямую), `deal` из создания подходит.
        moved = transition_deal(client, deal, first_step, comment="Возобновляем работу")
        assert moved.status_code == 200, moved.text
        assert moved.json()["deal"]["status_id"] == first_step
