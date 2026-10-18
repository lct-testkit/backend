"""B2-3: тесты CLI переноса сделок между воронками (`app.modules.workflow.migrate_deals`).

7 кейсов плана спринта (backend-sprints.md, раздел B2-3):
(1) `--apply`: `workflow_id`/`status_id` по таблице соответствия, запись в историю с
    `reason=workflow_migration`, событие в ленте сделки, у закрытой сделки `closed_at`
    не меняется;
(2) сухой прогон (без `--apply`) не меняет ничего — ни сделку, ни историю, ни ленту;
(3) статус без пары в целевой воронке — код выхода 2, ноль изменений;
(4) батч размером 2 при 5 сделках не теряет ни одной строки;
(5) сделка с открытой подписью (`pending`/`partially_signed`) пропускается;
(6) повторный запуск после переноса — 0 изменений (идемпотентность по `workflow_id`);
(7) после переноса `POST /api/deals/{id}/transition` проходит по графу v2.

Вызывает `run_migration` напрямую — так же, как сам CLI (`main`) вызывает его после
разбора аргументов, но без `subprocess`. Воронки `b2b_university_v1`/`b2b_university_v2` —
те же спеки, что видит сид при реальном запуске (`_b2b_sales_spec`/`_b2b_spec`), сидятся
`is_default=False`, как в `tests/test_workflow_seed_backfill.py`, чтобы не конкурировать за
`is_default` с воронкой, уже поднятой другим тестом этого прогона.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select, update

from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import create_deal, login, transition_deal

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


async def _ensure_workflow(spec) -> uuid.UUID:
    """Сидит воронку, если её с таким кодом ещё нет в БД, иначе отдаёт id существующей —
    тесты этого файла не первые и не последние в прогоне, код воронки может быть занят."""
    from app.core.db import session_scope
    from app.modules.workflow.models import Workflow
    from app.modules.workflow.seed import seed_workflow

    async with session_scope() as session:
        workflow = await seed_workflow(session, spec, is_default=False)
        if workflow is not None:
            return workflow.id
        existing = await session.scalar(select(Workflow).where(Workflow.code == spec.code))
        assert existing is not None
        return existing.id


async def _v1_id() -> uuid.UUID:
    from app.modules.workflow.seed import _b2b_sales_spec

    return await _ensure_workflow(_b2b_sales_spec())


async def _v2_id() -> uuid.UUID:
    from app.modules.workflow.seed import _b2b_spec

    return await _ensure_workflow(_b2b_spec())


async def _status_id(workflow_id: uuid.UUID, code: str) -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.workflow.models import WorkflowStatus

    async with session_scope() as session:
        status_id = await session.scalar(
            select(WorkflowStatus.id).where(
                WorkflowStatus.workflow_id == workflow_id, WorkflowStatus.code == code
            )
        )
        assert status_id is not None, f"статус {code!r} не найден в воронке {workflow_id}"
        return status_id


async def _set_deal(deal_id: uuid.UUID, values: dict[str, object]) -> None:
    from app.core.db import session_scope
    from app.modules.crm.models import Deal

    async with session_scope() as session:
        await session.execute(update(Deal).where(Deal.id == deal_id).values(**values))


async def _deal_row(deal_id: uuid.UUID):
    from app.core.db import session_scope
    from app.modules.crm.models import Deal

    async with session_scope() as session:
        deal = await session.get(Deal, deal_id)
        assert deal is not None
        session.expunge(deal)
        return deal


async def _history_reasons(deal_id: uuid.UUID) -> list[str]:
    from app.core.db import session_scope
    from app.modules.crm.models import DealStatusHistory

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(DealStatusHistory.reason).where(DealStatusHistory.deal_id == deal_id)
            )
        ).scalars()
        return list(rows)


async def _event_types(deal_id: uuid.UUID) -> list[str]:
    from app.core.db import session_scope
    from app.modules.crm.models import DealEvent

    async with session_scope() as session:
        rows = (
            await session.execute(select(DealEvent.event_type).where(DealEvent.deal_id == deal_id))
        ).scalars()
        return list(rows)


async def _migrate(params: dict[str, object]) -> int:
    from app.modules.workflow.migrate_deals import run_migration

    return await run_migration(**params)  # type: ignore[arg-type]


def _migrate_params(
    *,
    apply: bool,
    batch_size: int = 100,
    sla_mode: str = "recalculate",
    overrides: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "from_code": "b2b_university_v1",
        "to_code": "b2b_university_v2",
        "overrides": overrides or {},
        "apply": apply,
        "batch_size": batch_size,
        "sla_mode": sla_mode,
    }


class TestApplyMovesDealsByTheTable:
    def test_an_open_deal_gets_the_mapped_workflow_and_status(self, client) -> None:
        login(client, "ADMIN")
        v1 = run(client, _v1_id)
        v2 = run(client, _v2_id)
        deal = create_deal(client, str(v1))
        meeting_scheduled = run(client, _status_id, v1, "meeting_scheduled")
        run(client, _set_deal, uuid.UUID(deal["id"]), {"status_id": meeting_scheduled})

        code = run(client, _migrate, _migrate_params(apply=True))

        assert code == 0
        row = run(client, _deal_row, uuid.UUID(deal["id"]))
        assert row.workflow_id == v2
        assert row.status_id == run(client, _status_id, v2, "meeting")

    def test_the_move_is_written_to_history_and_the_feed(self, client) -> None:
        login(client, "ADMIN")
        v1 = run(client, _v1_id)
        run(client, _v2_id)
        deal = create_deal(client, str(v1))
        qualification = run(client, _status_id, v1, "qualification")
        run(client, _set_deal, uuid.UUID(deal["id"]), {"status_id": qualification})

        code = run(client, _migrate, _migrate_params(apply=True))

        assert code == 0
        reasons = run(client, _history_reasons, uuid.UUID(deal["id"]))
        assert "workflow_migration" in reasons
        events = run(client, _event_types, uuid.UUID(deal["id"]))
        assert "WORKFLOW_MIGRATION" in events

    def test_a_closed_deal_keeps_its_closed_at(self, client) -> None:
        login(client, "ADMIN")
        v1 = run(client, _v1_id)
        v2 = run(client, _v2_id)
        deal = create_deal(client, str(v1))
        won_v1 = run(client, _status_id, v1, "won")
        closed_at = dt.datetime(2026, 1, 5, 12, 0, tzinfo=dt.UTC)
        run(
            client,
            _set_deal,
            uuid.UUID(deal["id"]),
            {"status_id": won_v1, "closed_at": closed_at, "sla_due_at": None},
        )

        code = run(client, _migrate, _migrate_params(apply=True))

        assert code == 0
        row = run(client, _deal_row, uuid.UUID(deal["id"]))
        assert row.workflow_id == v2
        assert row.status_id == run(client, _status_id, v2, "won")
        assert row.closed_at == closed_at


class TestDryRunChangesNothing:
    def test_dry_run_leaves_the_deal_and_the_history_untouched(self, client) -> None:
        login(client, "ADMIN")
        v1 = run(client, _v1_id)
        run(client, _v2_id)
        deal = create_deal(client, str(v1))
        meeting_scheduled = run(client, _status_id, v1, "meeting_scheduled")
        run(client, _set_deal, uuid.UUID(deal["id"]), {"status_id": meeting_scheduled})
        before = run(client, _deal_row, uuid.UUID(deal["id"]))

        code = run(client, _migrate, _migrate_params(apply=False))

        assert code == 0
        after = run(client, _deal_row, uuid.UUID(deal["id"]))
        assert after.workflow_id == before.workflow_id == v1
        assert after.status_id == before.status_id == meeting_scheduled
        assert after.version == before.version
        assert "workflow_migration" not in run(client, _history_reasons, uuid.UUID(deal["id"]))
        assert "WORKFLOW_MIGRATION" not in run(client, _event_types, uuid.UUID(deal["id"]))


class TestAStatusWithoutAPairStopsTheRun:
    def test_an_unmapped_status_returns_exit_code_two_and_changes_nothing(self, client) -> None:
        login(client, "ADMIN")
        v1 = run(client, _v1_id)
        run(client, _v2_id)
        deal = create_deal(client, str(v1))
        meeting_scheduled = run(client, _status_id, v1, "meeting_scheduled")
        run(client, _set_deal, uuid.UUID(deal["id"]), {"status_id": meeting_scheduled})

        code = run(
            client,
            _migrate,
            _migrate_params(apply=True, overrides={"meeting_scheduled": "no_such_status_zzz"}),
        )

        assert code == 2
        row = run(client, _deal_row, uuid.UUID(deal["id"]))
        assert row.workflow_id == v1
        assert row.status_id == meeting_scheduled
        assert "workflow_migration" not in run(client, _history_reasons, uuid.UUID(deal["id"]))


class TestBatchingDoesNotLoseRows:
    def test_five_deals_with_a_batch_of_two_all_move(self, client) -> None:
        login(client, "ADMIN")
        v1 = run(client, _v1_id)
        v2 = run(client, _v2_id)
        qualification_v1 = run(client, _status_id, v1, "qualification")
        communication_v2 = run(client, _status_id, v2, "communication")
        deal_ids = []
        for _ in range(5):
            deal = create_deal(client, str(v1))
            run(client, _set_deal, uuid.UUID(deal["id"]), {"status_id": qualification_v1})
            deal_ids.append(uuid.UUID(deal["id"]))

        code = run(client, _migrate, _migrate_params(apply=True, batch_size=2))

        assert code == 0
        for deal_id in deal_ids:
            row = run(client, _deal_row, deal_id)
            assert row.workflow_id == v2
            assert row.status_id == communication_v2


async def _migrate_while_locked(deal_id: uuid.UUID, params: dict[str, object]) -> int:
    """Запускает перенос, пока строку сделки держит другая транзакция (`FOR UPDATE`)."""
    from app.core.db import get_session_factory
    from app.modules.crm.models import Deal

    async with get_session_factory()() as holder:
        await holder.execute(select(Deal.id).where(Deal.id == deal_id).with_for_update())
        try:
            return await _migrate(params)
        finally:
            await holder.rollback()


class TestDealHeldByAnotherTransaction:
    """`SKIP LOCKED` пропускает занятую строку молча — без сверки остатка скрипт отчитывался бы
    «итого N» и выходил с кодом 0, оставляя часть сделок на старой воронке."""

    def test_a_locked_deal_is_reported_and_the_rerun_moves_it(self, client) -> None:
        login(client, "ADMIN")
        v1 = run(client, _v1_id)
        v2 = run(client, _v2_id)
        qualification_v1 = run(client, _status_id, v1, "qualification")
        deal_ids = []
        for _ in range(3):
            deal = create_deal(client, str(v1))
            run(client, _set_deal, uuid.UUID(deal["id"]), {"status_id": qualification_v1})
            deal_ids.append(uuid.UUID(deal["id"]))
        locked_id = deal_ids[1]

        code = run(client, _migrate_while_locked, locked_id, _migrate_params(apply=True))

        assert code == 3
        assert run(client, _deal_row, locked_id).workflow_id == v1
        for deal_id in (deal_ids[0], deal_ids[2]):
            assert run(client, _deal_row, deal_id).workflow_id == v2

        assert run(client, _migrate, _migrate_params(apply=True)) == 0
        assert run(client, _deal_row, locked_id).workflow_id == v2


class TestOpenSignatureDealsAreSkipped:
    def test_a_deal_pending_signature_is_not_moved(self, client) -> None:
        login(client, "ADMIN")
        v1 = run(client, _v1_id)
        run(client, _v2_id)
        deal = create_deal(client, str(v1))
        meeting_scheduled = run(client, _status_id, v1, "meeting_scheduled")
        run(
            client,
            _set_deal,
            uuid.UUID(deal["id"]),
            {"status_id": meeting_scheduled, "signature_status": "pending"},
        )

        code = run(client, _migrate, _migrate_params(apply=True))

        assert code == 0
        row = run(client, _deal_row, uuid.UUID(deal["id"]))
        assert row.workflow_id == v1
        assert row.status_id == meeting_scheduled
        assert "workflow_migration" not in run(client, _history_reasons, uuid.UUID(deal["id"]))


class TestRunningTwiceIsIdempotent:
    def test_the_second_run_moves_nothing(self, client) -> None:
        login(client, "ADMIN")
        v1 = run(client, _v1_id)
        run(client, _v2_id)
        deal = create_deal(client, str(v1))
        meeting_scheduled = run(client, _status_id, v1, "meeting_scheduled")
        run(client, _set_deal, uuid.UUID(deal["id"]), {"status_id": meeting_scheduled})

        first_code = run(client, _migrate, _migrate_params(apply=True))
        assert first_code == 0
        after_first = run(client, _history_reasons, uuid.UUID(deal["id"]))
        assert after_first.count("workflow_migration") == 1

        second_code = run(client, _migrate, _migrate_params(apply=True))

        assert second_code == 0
        after_second = run(client, _history_reasons, uuid.UUID(deal["id"]))
        # Второй прогон не находит сделку (её `workflow_id` уже v2) — не добавилась вторая
        # запись в историю, список не изменился ни по длине, ни по составу.
        assert after_second == after_first


class TestTransitionsWorkAfterTheMove:
    def test_the_deal_can_transition_along_the_v2_graph(self, client) -> None:
        login(client, "ADMIN")
        v1 = run(client, _v1_id)
        v2 = run(client, _v2_id)
        deal = create_deal(client, str(v1))
        qualification = run(client, _status_id, v1, "qualification")
        run(client, _set_deal, uuid.UUID(deal["id"]), {"status_id": qualification})

        code = run(client, _migrate, _migrate_params(apply=True))
        assert code == 0

        current = client.get(f"/api/deals/{deal['id']}")
        assert current.status_code == 200, current.text
        card = current.json()["deal"]
        meeting_v2 = run(client, _status_id, v2, "meeting")

        # Перенос поставил сделку на "communication" (v2): следующий шаг графа v2 — "meeting"
        # (единственный исходящий переход с таким требованием — обязательный комментарий, без
        # условий по полям сделки). 200 здесь означает, что движок перехода читает граф именно
        # того `workflow_id`, который записал перенос, а не прежний (v1) и не 404.
        response = transition_deal(
            client,
            {"id": deal["id"], "version": card["version"]},
            str(meeting_v2),
            comment="Организована встреча после переноса на v2",
        )

        assert response.status_code == 200, response.text
        assert response.json()["deal"]["status_id"] == str(meeting_v2)
