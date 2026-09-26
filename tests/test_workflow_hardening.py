"""Воронки: parked не терминален, архивация статуса без потерянных сделок, снимок из
опубликованного графа, сиды проходят валидацию.

Найдено внешним тестированием:

* «заморозка» (`parked`) закрывала сделку — возобновить её было нечем;
* архивация статуса пропускала сделки без обязательных полей цели и всё равно архивировала статус
  (сделки застревали без выходов), а при остатке от 100 сделок задача крутилась вечно;
* после архивации в опубликованный снимок попадал черновик воронки без проверок публикации;
* кэш воронки сбрасывался до коммита.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from typing import Any

import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import (
    body_from_graph,
    by_code,
    create_deal,
    create_draft_workflow,
    create_published_workflow,
    create_user,
    get_graph,
    graph_body,
    login,
    put_graph,
    transition_deal,
)

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _parked_graph() -> dict[str, Any]:
    return graph_body(
        statuses=[
            {"code": "new", "name": "Новая", "type": "initial", "sort_order": 10},
            {"code": "work", "name": "В работе", "type": "intermediate", "sort_order": 20},
            {"code": "hold", "name": "Заморожена", "type": "parked", "sort_order": 30},
            {"code": "won", "name": "Закрыта", "type": "won", "sort_order": 40},
        ],
        transitions=[
            {"from_status": "new", "to_status": "work", "name": "В работу"},
            {"from_status": "work", "to_status": "hold", "name": "Заморозить"},
            {"from_status": "hold", "to_status": "work", "name": "Возобновить"},
            {"from_status": "work", "to_status": "won", "name": "Закрыть"},
        ],
        sla_rules=[{"status": "work", "max_duration_hours": 24, "count_business_days": False}],
    )


class TestParkedIsNotTerminal:
    def test_freezing_keeps_the_deal_open_and_it_can_be_resumed(self, client) -> None:
        login(client)
        graph = create_published_workflow(client, _parked_graph())
        codes = by_code(graph)
        deal = create_deal(client, graph["workflow"]["id"])
        deal = transition_deal(client, deal, codes["work"]["id"]).json()["deal"]

        frozen = transition_deal(client, deal, codes["hold"]["id"])
        assert frozen.status_code == 200, frozen.text
        body = frozen.json()["deal"]
        assert body["closed_at"] is None
        assert body["sla_state"] == "paused"
        assert body["sla_due_at"] is None

        # Замороженная сделка — «открытая» в списке.
        open_ids = {
            d["id"]
            for d in client.get("/api/deals", params={"is_closed": "false", "limit": 100}).json()[
                "items"
            ]
        }
        closed_ids = {
            d["id"]
            for d in client.get("/api/deals", params={"is_closed": "true", "limit": 100}).json()[
                "items"
            ]
        }
        assert deal["id"] in open_ids and deal["id"] not in closed_ids

        resumed = transition_deal(client, body, codes["work"]["id"])
        assert resumed.status_code == 200, resumed.text
        after = resumed.json()["deal"]
        assert after["closed_at"] is None
        assert after["sla_state"] == "ok" and after["sla_due_at"] is not None

    def test_a_deal_frozen_before_the_fix_can_still_leave_parked(self, client) -> None:
        # Старые строки: заморозка успела поставить `closed_at`. Выход из parked от него не зависит.
        from sqlalchemy import update

        from app.core.db import session_scope
        from app.modules.crm.models import Deal

        login(client)
        graph = create_published_workflow(client, _parked_graph())
        codes = by_code(graph)
        deal = create_deal(client, graph["workflow"]["id"])
        deal = transition_deal(client, deal, codes["work"]["id"]).json()["deal"]
        frozen = transition_deal(client, deal, codes["hold"]["id"]).json()["deal"]

        async def _legacy_close() -> None:
            async with session_scope() as session:
                await session.execute(
                    update(Deal)
                    .where(Deal.id == uuid.UUID(frozen["id"]))
                    .values(closed_at=dt.datetime.now(dt.UTC))
                )

        run(client, _legacy_close)
        current = client.get(f"/api/deals/{frozen['id']}").json()["deal"]
        assert current["closed_at"] is not None

        resumed = transition_deal(client, current, codes["work"]["id"])
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["deal"]["closed_at"] is None

    def test_won_and_lost_still_close_the_deal(self, client) -> None:
        login(client)
        graph = create_published_workflow(client, _parked_graph())
        codes = by_code(graph)
        deal = create_deal(client, graph["workflow"]["id"])
        deal = transition_deal(client, deal, codes["work"]["id"]).json()["deal"]

        closed = transition_deal(client, deal, codes["won"]["id"])

        assert closed.status_code == 200, closed.text
        assert closed.json()["deal"]["closed_at"] is not None
        again = transition_deal(client, closed.json()["deal"], codes["work"]["id"])
        assert again.status_code == 409  # закрытая сделка не двигается

    def test_parked_without_an_exit_is_a_warning_not_a_publication_blocker(self, client) -> None:
        login(client)
        workflow = create_draft_workflow(client)
        body = graph_body(
            statuses=[
                {"code": "new", "name": "Новая", "type": "initial"},
                {"code": "hold", "name": "Заморожена", "type": "parked"},
                {"code": "won", "name": "Закрыта", "type": "won"},
            ],
            transitions=[
                {"from_status": "new", "to_status": "hold", "name": "Заморозить"},
                {"from_status": "new", "to_status": "won", "name": "Закрыть"},
            ],
            sla_rules=[],
        )
        saved = put_graph(client, workflow, body)
        assert saved.status_code == 200, saved.text

        validated = client.post(f"/api/workflows/{workflow['id']}/validate")

        assert validated.status_code == 200, validated.text
        assert validated.json()["ok"] is True
        assert any("parked" in w for w in validated.json()["warnings"]), validated.json()


class TestSeedsAgreeWithValidation:
    """Сид и валидатор обязаны согласоваться: `python -m app.modules.workflow.seed` на чистой БД
    падал бы с «ловушками: parked», как только parked перестал быть терминальным."""

    @pytest.mark.parametrize("builder", ["_b2b_spec", "_b2c_spec"])
    def test_seed_publishes_and_parked_has_a_way_out(self, client, builder: str) -> None:
        from sqlalchemy import update

        from app.core.db import session_scope
        from app.modules.workflow import seed
        from app.modules.workflow.models import Workflow

        async def _seed() -> dict[str, Any]:
            async with session_scope() as session:
                # Воронка по умолчанию на тип — одна; общая БД тестов может её уже держать.
                await session.execute(
                    update(Workflow)
                    .where(Workflow.is_default.is_(True), Workflow.state == "published")
                    .values(is_default=False)
                )
                spec = dataclasses.replace(
                    getattr(seed, builder)(), code=f"{builder}_{uuid.uuid4().hex[:8]}"
                )
                workflow = await seed.seed_workflow(session, spec)  # RuntimeError, если не проходит
                snapshot = workflow.published_graph
                await session.rollback()  # демо-воронку в общей БД не оставляем
                return snapshot

        snapshot = run(client, _seed)

        parked = next(s for s in snapshot["statuses"] if s["type"] == "parked")
        exits = [t for t in snapshot["transitions"] if t["from_status_id"] == parked["id"]]
        assert exits, "из parked нет ни одного перехода"
        targets = {t["to_status_id"] for t in exits}
        assert targets <= {s["id"] for s in snapshot["statuses"]}


class TestArchiveKeepsEveryDeal:
    def _graph(self, client):
        login(client)
        return create_published_workflow(
            client,
            graph_body(
                statuses=[
                    {"code": "new", "name": "Новая", "type": "initial", "sort_order": 10},
                    {"code": "work", "name": "В работе", "type": "intermediate", "sort_order": 20},
                    {
                        "code": "review",
                        "name": "Проверка",
                        "type": "intermediate",
                        "sort_order": 30,
                        "required_fields": ["amount"],
                    },
                    {"code": "won", "name": "Закрыта", "type": "won", "sort_order": 40},
                ],
                transitions=[
                    {"from_status": "new", "to_status": "work", "name": "В работу"},
                    {"from_status": "work", "to_status": "review", "name": "На проверку"},
                    {"from_status": "review", "to_status": "won", "name": "Закрыть"},
                    {"from_status": "new", "to_status": "review", "name": "Сразу"},
                ],
                sla_rules=[],
            ),
        )

    def _archive(self, client, graph, status_code: str, target: str, fallback: str | None = None):
        codes = by_code(graph)
        body: dict[str, Any] = {"target_status_id": codes[target]["id"]}
        if fallback:
            body["fallback_status_id"] = codes[fallback]["id"]
        current = get_graph(client, graph["workflow"]["id"])
        return client.post(
            f"/api/workflows/{graph['workflow']['id']}/statuses/{codes[status_code]['id']}/archive",
            json=body,
            headers={"If-Match": str(current["workflow"]["version"])},
        )

    def test_deals_without_the_required_fields_need_a_fallback(self, client) -> None:
        graph = self._graph(client)
        codes = by_code(graph)
        deal = create_deal(client, graph["workflow"]["id"])
        transition_deal(client, deal, codes["work"]["id"])

        refused = self._archive(client, graph, "work", "review")

        assert refused.status_code == 422, refused.text
        assert refused.json()["errors"][0]["field"] == "fallback_status_id"
        # Ничего не изменилось: статус жив, сделка на месте.
        after = get_graph(client, graph["workflow"]["id"])
        assert not by_code(after)["work"]["is_archived"]

    def test_with_a_fallback_the_deal_lands_there(self, client) -> None:
        graph = self._graph(client)
        codes = by_code(graph)
        deal = create_deal(client, graph["workflow"]["id"])
        moved = transition_deal(client, deal, codes["work"]["id"]).json()["deal"]

        done = self._archive(client, graph, "work", "review", fallback="new")

        assert done.status_code == 200, done.text
        assert done.json()["job_status"] == "completed"
        current = client.get(f"/api/deals/{moved['id']}").json()["deal"]
        assert current["status_id"] == codes["new"]["id"]

    def test_stuck_deals_fail_the_job_and_leave_the_status_alive(self, client) -> None:
        """Сделок больше партии (100) и ни одну перенести нельзя: задача не должна ни крутиться
        вечно, ни архивировать статус вместе со сделками, у которых не будет выхода."""
        from sqlalchemy import select

        from app.core.db import session_scope
        from app.modules.crm.models import Deal
        from app.modules.workflow.models import StatusMappingJob, Workflow, WorkflowStatus
        from app.modules.workflow.tasks import sweep_status_mapping_jobs

        graph = self._graph(client)
        codes = by_code(graph)
        owner = create_user(client, "KAM")
        seed_deal = create_deal(client, graph["workflow"]["id"], owner_id=str(owner.id))

        async def _prepare() -> uuid.UUID:
            from app.modules.catalog.models import Organization

            async with session_scope() as session:
                org = Organization(name=f"Вуз {uuid.uuid4().hex[:6]}", org_type="university")
                session.add(org)
                await session.flush()
                for _ in range(120):
                    session.add(
                        Deal(
                            number=f"D-X1-{uuid.uuid4().hex[:10]}",
                            title="Застрявшая",
                            deal_type="b2b",
                            workflow_id=uuid.UUID(graph["workflow"]["id"]),
                            status_id=uuid.UUID(codes["work"]["id"]),
                            organization_id=org.id,
                            owner_id=owner.id,
                        )
                    )
                job = StatusMappingJob(
                    workflow_id=uuid.UUID(graph["workflow"]["id"]),
                    from_status_id=uuid.UUID(codes["work"]["id"]),
                    mapping_rules={
                        "rules": {},
                        "target_status_id": codes["review"]["id"],
                        "fallback_status_id": None,
                        "sla_mode": "keep",
                    },
                    affected_count=120,
                    status="running",
                )
                session.add(job)
                await session.flush()
                return job.id

        job_id = run(client, _prepare)

        for _ in range(4):  # две партии по 100 → конец просмотра → отказ
            run(client, sweep_status_mapping_jobs, {})

        async def _inspect() -> tuple[str, int, bool, bool]:
            async with session_scope() as session:
                job = await session.get(StatusMappingJob, job_id)
                status = await session.get(WorkflowStatus, uuid.UUID(codes["work"]["id"]))
                workflow = await session.get(Workflow, uuid.UUID(graph["workflow"]["id"]))
                in_snapshot = any(
                    s["id"] == codes["work"]["id"] for s in workflow.published_graph["statuses"]
                )
                stuck = await session.scalar(
                    select(Deal.id).where(Deal.status_id == status.id).limit(1)
                )
                return job.status, job.failed_count, status.is_archived, in_snapshot and stuck

        job_status, failed, archived, still_there = run(client, _inspect)
        assert job_status == "failed"
        assert failed >= 120
        assert archived is False
        assert still_there
        assert seed_deal["id"]  # сделка из API тоже осталась в воронке


class TestRepublishDoesNotLeakTheDraft:
    def test_archiving_edits_the_published_snapshot_not_the_draft(self, client) -> None:
        from app.core.db import session_scope
        from app.modules.workflow.models import Workflow

        login(client)
        graph = create_published_workflow(
            client,
            graph_body(
                statuses=[
                    {"code": "new", "name": "Новая", "type": "initial"},
                    {"code": "extra", "name": "Лишний"},
                    {"code": "won", "name": "Закрыта", "type": "won"},
                ],
                transitions=[
                    {"from_status": "new", "to_status": "extra", "name": "Дальше"},
                    {"from_status": "extra", "to_status": "won", "name": "Закрыть"},
                    {"from_status": "new", "to_status": "won", "name": "Сразу"},
                ],
                sla_rules=[],
            ),
        )
        codes = by_code(graph)
        # Черновик правится, но не публикуется: переименование статуса «Новая».
        body = body_from_graph(graph)
        next(s for s in body["statuses"] if s["code"] == "new")["name"] = "ЧЕРНОВИК-НЕ-ПУБЛИКОВАТЬ"
        assert put_graph(client, graph["workflow"], body).status_code == 200
        current = get_graph(client, graph["workflow"]["id"])

        archived = client.post(
            f"/api/workflows/{graph['workflow']['id']}/statuses/{codes['extra']['id']}/archive",
            json={"target_status_id": codes["won"]["id"]},
            headers={"If-Match": str(current["workflow"]["version"])},
        )
        assert archived.status_code == 200, archived.text

        async def _snapshot() -> dict[str, Any]:
            async with session_scope() as session:
                workflow = await session.get(Workflow, uuid.UUID(graph["workflow"]["id"]))
                return workflow.published_graph

        snapshot = run(client, _snapshot)
        names = {s["code"]: s["name"] for s in snapshot["statuses"]}
        assert names == {"new": "Новая", "won": "Закрыта"}, names  # без «лишнего», без черновика
        assert all(
            codes["extra"]["id"] not in (t["from_status_id"], t["to_status_id"])
            for t in snapshot["transitions"]
        )
        # Черновик по-прежнему отличается от опубликованного: правку ещё надо опубликовать.
        assert get_graph(client, graph["workflow"]["id"])["workflow"]["has_unpublished_changes"]

    def test_archive_warns_when_the_remaining_graph_is_broken(self, client) -> None:
        login(client)
        graph = create_published_workflow(client)  # new → work → won, единственный путь через work
        codes = by_code(graph)

        archived = client.post(
            f"/api/workflows/{graph['workflow']['id']}/statuses/{codes['work']['id']}/archive",
            json={"target_status_id": codes["won"]["id"]},
            headers={"If-Match": str(graph["workflow"]["version"])},
        )

        assert archived.status_code == 200, archived.text
        assert archived.json()["warnings"], archived.json()


class TestWorkflowCacheAfterCommit:
    def test_cache_survives_a_rolled_back_publication_and_is_dropped_after_a_commit(
        self, client
    ) -> None:
        from app.core.db import session_scope
        from app.core.redis_client import get_redis, key_workflow_graph
        from app.modules.workflow.models import Workflow
        from app.modules.workflow.service import WorkflowService, get_cached_published_graph

        login(client)
        graph = create_published_workflow(client)
        workflow_id = uuid.UUID(graph["workflow"]["id"])

        async def _rolled_back() -> bool:
            async with session_scope() as session:
                workflow = await session.get(Workflow, workflow_id)
                await get_cached_published_graph(workflow)  # прогрели
            assert await get_redis().exists(key_workflow_graph(workflow_id)) == 1
            try:
                async with session_scope() as session:
                    workflow = await session.get(Workflow, workflow_id)
                    principal = type("P", (), {"user_id": uuid.uuid4()})()
                    await WorkflowService(session).publish(
                        workflow, principal, expected_version=workflow.version
                    )
                    assert await get_redis().exists(key_workflow_graph(workflow_id)) == 1  # ещё жив
                    raise RuntimeError("откат")
            except RuntimeError:
                pass
            return await get_redis().exists(key_workflow_graph(workflow_id)) == 1

        assert run(client, _rolled_back), "откат публикации не должен сбрасывать кэш"

        latest = get_graph(client, graph["workflow"]["id"])
        published = client.post(
            f"/api/workflows/{graph['workflow']['id']}/publish",
            headers={"If-Match": str(latest["workflow"]["version"])},
        )
        assert published.status_code == 200, published.text

        async def _dropped() -> bool:
            return await get_redis().exists(key_workflow_graph(workflow_id)) == 0

        assert run(client, _dropped)

    def test_a_cache_entry_of_another_snapshot_is_ignored(self, client) -> None:
        import json

        from app.core.db import session_scope
        from app.core.redis_client import get_redis, key_workflow_graph
        from app.modules.workflow.models import Workflow
        from app.modules.workflow.service import get_cached_published_graph

        login(client)
        graph = create_published_workflow(client)
        workflow_id = uuid.UUID(graph["workflow"]["id"])

        async def _stale() -> tuple[dict, dict]:
            await get_redis().set(
                key_workflow_graph(workflow_id),
                json.dumps({"h": "0" * 64, "g": {"statuses": [], "transitions": []}}),
            )
            async with session_scope() as session:
                workflow = await session.get(Workflow, workflow_id)
                return await get_cached_published_graph(workflow), workflow.published_graph

        served, real = run(client, _stale)
        assert served == real


class TestSignatureGateHasItsOwnCode:
    """Переход, которому не хватает только подписи, отвечает CRM-1206 (клиент ведёт на вкладку
    «Подписание»); всё прочее и смешанные отказы остаются CRM-1201 со списком условий."""

    def _graph(self, client, conditions: dict[str, Any]):
        login(client)
        return create_published_workflow(
            client,
            graph_body(
                transitions=[
                    {
                        "from_status": "new",
                        "to_status": "work",
                        "name": "В работу",
                        "conditions": conditions,
                    },
                    {"from_status": "work", "to_status": "won", "name": "Закрыть"},
                ],
                sla_rules=[],
            ),
        )

    def _try(self, client, conditions: dict[str, Any]):
        graph = self._graph(client, conditions)
        deal = create_deal(client, graph["workflow"]["id"])
        return transition_deal(client, deal, by_code(graph)["work"]["id"])

    def test_missing_signature_alone_is_1206(self, client) -> None:
        response = self._try(client, {"field": "signature_status", "op": "eq", "value": "signed"})

        assert response.status_code == 422, response.text
        assert response.json()["code"] == "CRM-1206"
        assert response.json()["unmet"][0]["field"] == "signature_status"

    def test_signature_or_attachment_stays_1201(self, client) -> None:
        response = self._try(
            client,
            {
                "any": [
                    {"field": "signature_status", "op": "eq", "value": "signed"},
                    {"field": "attachments.contract", "op": "exists"},
                ]
            },
        )

        assert response.status_code == 422, response.text
        assert response.json()["code"] == "CRM-1201"

    def test_signature_and_something_else_missing_stays_1201(self, client) -> None:
        response = self._try(
            client,
            {
                "all": [
                    {"field": "signature_status", "op": "eq", "value": "signed"},
                    {"field": "amount", "op": "not_null"},
                ]
            },
        )

        assert response.json()["code"] == "CRM-1201", response.text
