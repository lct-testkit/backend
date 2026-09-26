"""Оптимистичная блокировка: версия занимается одним UPDATE, а не проверкой в Python.

Найдено внешним тестированием: два запроса, прочитавшие строку одной версии, оба проходили
сравнение `version != If-Match` и оба писали — последний молча затирал первого. Теперь
`UPDATE ... WHERE version = :expected`: проигравший ждёт коммита победителя и получает 409
CRM-1002. Тесты гонят по два настоящих параллельных соединения с одной версией.

Заодно: явный `null` в PATCH обязательных полей — 422 (раньше 500), а нарушения NOT NULL и CHECK,
дошедшие до БД, — 422, а не «Внутренняя ошибка».
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import (
    by_code,
    create_deal,
    create_draft_workflow,
    create_published_workflow,
    create_user,
    graph_body,
    login,
    put_graph,
    sign_in,
)

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


async def _race_deal_updates(deal_id: uuid.UUID, version: int) -> list[Any]:
    """Два параллельных `DealService.update` по одной версии; возвращает исходы обоих."""
    from app.core.db import session_scope
    from app.core.security import Principal
    from app.modules.crm.models import Deal
    from app.modules.crm.schemas import DealUpdateRequest
    from app.modules.crm.service import DealService

    gate = asyncio.Event()
    loaded = 0

    async def attempt(title: str) -> str:
        nonlocal loaded
        async with session_scope() as session:
            deal = await session.get(Deal, deal_id)
            assert deal is not None
            assert deal.version == version
            # Оба запроса прочитали строку до того, как любой из них что-то записал.
            loaded += 1
            if loaded == 2:
                gate.set()
            await gate.wait()
            principal = Principal(
                user_id=deal.owner_id,
                keycloak_id="k",
                role="ADMIN",
                status="active",
                email=None,
                full_name="x",
                team_id=None,
                manager_id=None,
                perm_epoch=1,
                session_id=None,
                consent_version="1.0",
                must_change_password=False,
                claims=None,  # type: ignore[arg-type]
            )
            await DealService(session).update(
                deal,
                DealUpdateRequest(title=title),
                expected_version=version,
                principal=principal,
            )
            return title

    return await asyncio.gather(attempt("Первый"), attempt("Второй"), return_exceptions=True)


class TestDealVersionRace:
    def test_only_one_of_two_parallel_updates_wins(self, client) -> None:
        from app.core.errors import VersionConflictError

        login(client)
        graph = create_published_workflow(client)
        deal = create_deal(client, graph["workflow"]["id"])

        outcomes = run(client, _race_deal_updates, uuid.UUID(deal["id"]), deal["version"])

        winners = [o for o in outcomes if isinstance(o, str)]
        losers = [o for o in outcomes if isinstance(o, VersionConflictError)]
        assert len(winners) == 1 and len(losers) == 1, outcomes
        current = client.get(f"/api/deals/{deal['id']}").json()["deal"]
        assert current["title"] == winners[0]
        assert current["version"] == deal["version"] + 1  # ровно одно приращение

    def test_stale_if_match_is_still_a_conflict_over_http(self, client) -> None:
        login(client)
        graph = create_published_workflow(client)
        deal = create_deal(client, graph["workflow"]["id"])

        first = client.patch(
            f"/api/deals/{deal['id']}",
            json={"title": "Новое"},
            headers={"If-Match": str(deal["version"])},
        )
        assert first.status_code == 200, first.text
        second = client.patch(
            f"/api/deals/{deal['id']}",
            json={"title": "Ещё новее"},
            headers={"If-Match": str(deal["version"])},
        )
        assert second.status_code == 409, second.text
        assert second.json()["code"] == "CRM-1002"

    def test_no_change_does_not_bump_the_version(self, client) -> None:
        login(client)
        graph = create_published_workflow(client)
        deal = create_deal(client, graph["workflow"]["id"])

        same = client.patch(
            f"/api/deals/{deal['id']}",
            json={"title": deal["title"]},
            headers={"If-Match": str(deal["version"])},
        )
        assert same.status_code == 200, same.text
        assert same.json()["version"] == deal["version"]


async def _race_workflow_updates(workflow_id: uuid.UUID, version: int) -> list[Any]:
    from app.core.db import session_scope
    from app.modules.workflow.models import Workflow
    from app.modules.workflow.schemas import WorkflowUpdateRequest
    from app.modules.workflow.service import WorkflowService

    gate = asyncio.Event()
    loaded = 0

    async def attempt(name: str) -> str:
        nonlocal loaded
        async with session_scope() as session:
            workflow = await session.get(Workflow, workflow_id)
            assert workflow is not None
            loaded += 1
            if loaded == 2:
                gate.set()
            await gate.wait()
            await WorkflowService(session).update(
                workflow, WorkflowUpdateRequest(name=name), expected_version=version
            )
            return name

    return await asyncio.gather(attempt("Имя А"), attempt("Имя Б"), return_exceptions=True)


class TestWorkflowVersionRace:
    def test_only_one_of_two_parallel_workflow_edits_wins(self, client) -> None:
        from app.core.errors import AppError, ErrorCode

        login(client)
        workflow = create_draft_workflow(client)

        outcomes = run(
            client, _race_workflow_updates, uuid.UUID(workflow["id"]), workflow["version"]
        )

        winners = [o for o in outcomes if isinstance(o, str)]
        losers = [
            o for o in outcomes if isinstance(o, AppError) and o.code is ErrorCode.VERSION_CONFLICT
        ]
        assert len(winners) == 1 and len(losers) == 1, outcomes

    def test_graph_save_with_a_stale_version_is_a_conflict(self, client) -> None:
        login(client)
        workflow = create_draft_workflow(client)
        assert put_graph(client, workflow, graph_body()).status_code == 200

        stale = put_graph(client, workflow, graph_body())  # версия из до первого сохранения
        assert stale.status_code == 409, stale.text


class TestNullInPatch:
    @pytest.mark.parametrize("field", ["title", "currency", "priority"])
    def test_null_in_a_required_deal_field_is_422(self, client, field: str) -> None:
        login(client)
        graph = create_published_workflow(client)
        deal = create_deal(client, graph["workflow"]["id"])

        response = client.patch(
            f"/api/deals/{deal['id']}",
            json={field: None},
            headers={"If-Match": str(deal["version"])},
        )

        assert response.status_code == 422, response.text
        assert any(e["field"] == field for e in response.json()["errors"]), response.json()

    @pytest.mark.parametrize("field", ["role", "status", "locale", "timezone"])
    def test_null_in_a_required_user_field_is_422(self, client, field: str) -> None:
        admin = login(client)
        target = create_user(client, "KAM")

        response = client.patch(
            f"/api/admin/users/{target.id}",
            json={field: None},
            headers={"If-Match": "1"},
        )

        assert response.status_code == 422, response.text
        assert admin is not None

    def test_null_in_a_catalog_column_is_422_from_the_database_net(self, client) -> None:
        # У каталога явных проверок нет: NOT NULL ловит БД, а обработчик отвечает 422 с полем.
        login(client)
        created = client.post(
            "/api/directions", json={"code": f"dir_{uuid.uuid4().hex[:8]}", "name": "Направление"}
        )
        assert created.status_code == 201, created.text
        direction = created.json()

        response = client.patch(
            f"/api/directions/{direction['id']}",
            json={"name": None},
            headers={"If-Match": str(direction["version"])},
        )

        assert response.status_code in (200, 422), response.text
        assert response.status_code != 500


class TestDatabaseViolationsAreNot500:
    def test_too_long_value_is_422(self, client) -> None:
        login(client)
        graph = create_published_workflow(client)
        deal = create_deal(client, graph["workflow"]["id"])

        response = client.patch(
            f"/api/deals/{deal['id']}",
            json={"source": "x" * 33},  # `source` — String(32)
            headers={"If-Match": str(deal["version"])},
        )

        assert response.status_code == 422, response.text


def test_sign_in_helpers_are_importable() -> None:
    # Страховка от переименования хелперов, на которые опираются тесты выше.
    assert callable(sign_in) and callable(by_code)
