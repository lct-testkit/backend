"""B4-4: мастер передачи дел — правила, пул, вузы, откат, предпросмотр и передача
без увольнения (`POST /api/users/{id}/handover`).

Настоящая Postgres обязательна (`tests/conftest.py`). Сцены собираются напрямую в БД
(сделки, продукты, направления, вузы) тем же приёмом, что `tests/test_identity_admin.py`
(`_deals_of`), а мастер вызывается через настоящее API.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _login(client, user) -> None:
    client.headers["X-CSRF-Token"] = authenticate(client, user)


def _keycloak(monkeypatch) -> None:
    """Keycloak недоступен в тестах: `offboard` отключает учётку через него —
    подменяем так же, как `tests/test_identity_admin.py::_keycloak`."""
    from app.modules.identity.keycloak import keycloak_client

    async def set_enabled(keycloak_id: str, *, enabled: bool) -> None:
        return None

    async def noop(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(keycloak_client, "set_enabled", set_enabled)
    monkeypatch.setattr(keycloak_client, "logout_all_sessions", noop)


async def _make_scene(owner_id: uuid.UUID, deal_specs: list[dict]) -> dict:
    """Воронка + вуз + сделки с продуктами/направлениями под `owner_id`.

    `deal_specs` — список `{"direction": "QA" | None, "closed": bool}`. Возвращает
    `{"organization_id", "deal_ids", "direction_ids": {name: id}, "product_ids": {name: id}}`.
    """
    import datetime as dt

    from app.core.db import session_scope
    from app.modules.catalog.models import Direction, Organization, Product
    from app.modules.crm.models import Deal, DealProduct
    from app.modules.workflow.models import Workflow, WorkflowStatus

    async with session_scope() as session:
        workflow = Workflow(
            code=f"wf-{uuid.uuid4().hex[:10]}",
            name="Воронка передачи",
            deal_type="b2b",
            state="draft",
        )
        session.add(workflow)
        await session.flush()
        status = WorkflowStatus(workflow_id=workflow.id, code="new", name="Новая")
        org = Organization(name=f"Вуз {uuid.uuid4().hex[:6]}", org_type="university")
        session.add_all([status, org])
        await session.flush()

        direction_ids: dict[str, uuid.UUID] = {}
        product_ids: dict[str, uuid.UUID] = {}
        deal_ids: list[uuid.UUID] = []
        for index, spec in enumerate(deal_specs):
            direction_name = spec.get("direction")
            product_id = None
            if direction_name is not None:
                if direction_name not in direction_ids:
                    direction = Direction(code=f"dir-{uuid.uuid4().hex[:10]}", name=direction_name)
                    session.add(direction)
                    await session.flush()
                    direction_ids[direction_name] = direction.id
                    product = Product(
                        code=f"prod-{uuid.uuid4().hex[:10]}",
                        name=f"Курс {direction_name}",
                        direction_id=direction.id,
                    )
                    session.add(product)
                    await session.flush()
                    product_ids[direction_name] = product.id
                product_id = product_ids[direction_name]

            deal = Deal(
                number=f"D-{uuid.uuid4().hex[:10]}",
                title=f"Сделка {index}",
                deal_type="b2b",
                workflow_id=workflow.id,
                status_id=status.id,
                organization_id=org.id,
                owner_id=owner_id,
                closed_at=dt.datetime.now(dt.UTC) if spec.get("closed") else None,
            )
            session.add(deal)
            await session.flush()
            if product_id is not None:
                session.add(DealProduct(deal_id=deal.id, product_id=product_id, quantity=1))
                await session.flush()
            deal_ids.append(deal.id)

        return {
            "organization_id": org.id,
            "deal_ids": deal_ids,
            "direction_ids": direction_ids,
            "product_ids": product_ids,
        }


async def _owners(deal_ids: list[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.crm.models import Deal

    async with session_scope() as session:
        rows = await session.execute(select(Deal.id, Deal.owner_id).where(Deal.id.in_(deal_ids)))
        return {row.id: row.owner_id for row in rows}


async def _org_owner(org_id: uuid.UUID) -> uuid.UUID | None:
    from app.core.db import session_scope
    from app.modules.catalog.models import Organization

    async with session_scope() as session:
        org = await session.get(Organization, org_id)
        return org.owner_id if org else None


async def _set_org_owner(org_id: uuid.UUID, owner_id: uuid.UUID) -> None:
    from app.core.db import session_scope
    from app.modules.catalog.models import Organization

    async with session_scope() as session:
        org = await session.get(Organization, org_id)
        org.owner_id = owner_id


async def _make_task(deal_id: uuid.UUID, assignee_id: uuid.UUID) -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.crm.models import Task

    async with session_scope() as session:
        task = Task(deal_id=deal_id, title="Задача передачи", assignee_id=assignee_id)
        session.add(task)
        await session.flush()
        return task.id


async def _task_assignee(task_id: uuid.UUID) -> uuid.UUID | None:
    from app.core.db import session_scope
    from app.modules.crm.models import Task

    async with session_scope() as session:
        task = await session.get(Task, task_id)
        return task.assignee_id if task else None


def _offboard(client, leaving, successor, **extra):
    return client.post(
        f"/api/admin/users/{leaving.id}/offboard",
        json={
            "mode": "confirm",
            "successor_id": str(successor.id),
            "reason": "Передача портфеля",
            **extra,
        },
    )


class TestHandoverRules:
    def test_direction_rule_routes_deals_and_explicit_mapping_wins(
        self, client, monkeypatch
    ) -> None:
        _keycloak(monkeypatch)
        _login(client, run(client, _make_user, "ADMIN"))
        leaving = run(client, _make_user, "KAM")
        successor_a = run(client, _make_user, "KAM")
        successor_b = run(client, _make_user, "KAM")
        scene = run(
            client,
            _make_scene,
            leaving.id,
            [{"direction": "QA"}, {"direction": "QA"}, {"direction": None}],
        )
        qa_direction = scene["direction_ids"]["QA"]
        deal_qa_1, deal_qa_2, deal_plain = scene["deal_ids"]

        response = _offboard(
            client,
            leaving,
            successor_a,
            deal_successors={str(deal_qa_1): str(successor_a.id)},
            rules=[
                {
                    "match": {"direction_id": str(qa_direction)},
                    "successor_id": str(successor_b.id),
                }
            ],
        )
        assert response.status_code == 200, response.text

        owners = run(client, _owners, scene["deal_ids"])
        # Явный deal_successors сильнее правила.
        assert owners[deal_qa_1] == successor_a.id
        # Правило по направлению двигает деал без явного указания.
        assert owners[deal_qa_2] == successor_b.id
        # Сделка без направления уходит дефолтному преемнику.
        assert owners[deal_plain] == successor_a.id


class TestHandoverPool:
    def test_pool_distributes_evenly(self, client, monkeypatch) -> None:
        _keycloak(monkeypatch)
        _login(client, run(client, _make_user, "ADMIN"))
        leaving = run(client, _make_user, "KAM")
        default_successor = run(client, _make_user, "KAM")
        pool = [run(client, _make_user, "KAM") for _ in range(3)]
        scene = run(client, _make_scene, leaving.id, [{"direction": None} for _ in range(9)])

        response = _offboard(
            client,
            leaving,
            default_successor,
            pool_ids=[str(p.id) for p in pool],
        )
        assert response.status_code == 200, response.text

        owners = run(client, _owners, scene["deal_ids"])
        counts = {p.id: list(owners.values()).count(p.id) for p in pool}
        assert sum(counts.values()) == 9
        assert all(2 <= count <= 4 for count in counts.values()), counts
        # Пул забирает все сделки — дефолтный преемник ничего не получает.
        assert default_successor.id not in owners.values()


class TestHandoverOrganizations:
    def test_organization_and_task_follow_the_plan(self, client, monkeypatch) -> None:
        _keycloak(monkeypatch)
        _login(client, run(client, _make_user, "ADMIN"))
        leaving = run(client, _make_user, "KAM")
        successor = run(client, _make_user, "KAM")
        scene = run(client, _make_scene, leaving.id, [{"direction": None}])
        run(client, _set_org_owner, scene["organization_id"], leaving.id)
        task_id = run(client, _make_task, scene["deal_ids"][0], leaving.id)

        response = _offboard(client, leaving, successor)
        assert response.status_code == 200, response.text
        body = response.json()
        assert scene["organization_id"] in [uuid.UUID(i) for i in body["reassigned_organizations"]]

        assert run(client, _org_owner, scene["organization_id"]) == successor.id
        assert run(client, _task_assignee, task_id) == successor.id


class TestHandoverRollback:
    def test_inactive_successor_rolls_back_deals_and_organizations(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        leaving = run(client, _make_user, "KAM")
        main = run(client, _make_user, "KAM")
        blocked = run(client, _make_user, "KAM", "blocked")
        scene = run(client, _make_scene, leaving.id, [{"direction": None}, {"direction": None}])
        run(client, _set_org_owner, scene["organization_id"], leaving.id)

        response = _offboard(
            client,
            leaving,
            main,
            deal_successors={str(scene["deal_ids"][0]): str(blocked.id)},
        )
        assert response.status_code == 422, response.text
        assert set(run(client, _owners, scene["deal_ids"]).values()) == {leaving.id}
        assert run(client, _org_owner, scene["organization_id"]) == leaving.id


class TestHandoverPreview:
    def test_preview_with_successor_computes_distribution_without_writing(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        leaving = run(client, _make_user, "KAM")
        successor = run(client, _make_user, "KAM")
        scene = run(client, _make_scene, leaving.id, [{"direction": None}, {"direction": None}])

        response = client.post(
            f"/api/admin/users/{leaving.id}/offboard",
            json={"mode": "preview", "successor_id": str(successor.id)},
        )
        assert response.status_code == 200, response.text
        distribution = response.json()["distribution"]
        assert any(
            item["successor_id"] == str(successor.id) and item["deals"] == 2
            for item in distribution
        )
        # Сухой прогон: владельцы не изменились.
        assert set(run(client, _owners, scene["deal_ids"]).values()) == {leaving.id}


class TestHandoverEndpointWithoutOffboarding:
    def test_head_transfers_own_team_member_without_changing_status(self, client) -> None:
        admin = run(client, _make_user, "ADMIN")
        _login(client, admin)

        team_id = run(client, _make_team)
        head = run(client, _make_member, team_id, "HEAD")
        member = run(client, _make_member, team_id, "KAM")
        successor = run(client, _make_member, team_id, "KAM")
        scene = run(client, _make_scene, member.id, [{"direction": None}])

        _login(client, head)
        response = client.post(
            f"/api/users/{member.id}/handover",
            json={"default_successor_id": str(successor.id), "reason": "Отпуск"},
        )
        assert response.status_code == 200, response.text
        assert run(client, _owners, scene["deal_ids"])[scene["deal_ids"][0]] == successor.id
        assert run(client, _load_user_status, member.id) == "active"

    def test_head_cannot_transfer_a_stranger(self, client) -> None:
        admin = run(client, _make_user, "ADMIN")
        _login(client, admin)
        head = run(client, _make_user, "HEAD")
        stranger = run(client, _make_user, "KAM")
        successor = run(client, _make_user, "KAM")

        _login(client, head)
        response = client.post(
            f"/api/users/{stranger.id}/handover",
            json={"default_successor_id": str(successor.id), "reason": "Отпуск"},
        )
        assert response.status_code == 403, response.text

    def test_kam_cannot_use_the_endpoint(self, client) -> None:
        admin = run(client, _make_user, "ADMIN")
        _login(client, admin)
        kam = run(client, _make_user, "KAM")
        someone = run(client, _make_user, "KAM")
        successor = run(client, _make_user, "KAM")

        _login(client, kam)
        response = client.post(
            f"/api/users/{someone.id}/handover",
            json={"default_successor_id": str(successor.id), "reason": "Отпуск"},
        )
        assert response.status_code == 403, response.text


async def _make_team() -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.identity.models import Team

    async with session_scope() as session:
        team = Team(name=f"Команда {uuid.uuid4().hex[:6]}")
        session.add(team)
        await session.flush()
        return team.id


async def _make_member(team_id: uuid.UUID, role: str = "KAM"):
    from app.core.db import session_scope
    from app.modules.identity.models import User

    async with session_scope() as session:
        user = User(
            keycloak_id=str(uuid.uuid4()),
            email=f"{uuid.uuid4().hex[:12]}@rt-it-school.ru",
            full_name="Сотрудник Команды",
            role=role,
            status="active",
            team_id=team_id,
            consent_version="1.0",
        )
        session.add(user)
        await session.flush()
        session.expunge(user)
        return user


async def _load_user_status(user_id: uuid.UUID) -> str:
    from app.core.db import session_scope
    from app.modules.identity.models import User

    async with session_scope() as session:
        user = await session.get(User, user_id)
        return user.status if user else ""


class TestOffboardItemsPagination:
    def test_lists_all_deals_beyond_the_preview_limit(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        leaving = run(client, _make_user, "KAM")
        scene = run(client, _make_scene, leaving.id, [{"direction": None} for _ in range(25)])

        seen: set[str] = set()
        cursor = None
        for _ in range(10):
            params = {"kind": "deals", "limit": 10}
            if cursor:
                params["cursor"] = cursor
            response = client.get(f"/api/admin/users/{leaving.id}/offboard/items", params=params)
            assert response.status_code == 200, response.text
            body = response.json()
            seen.update(item["id"] for item in body["items"])
            cursor = body["next_cursor"]
            if not cursor:
                break

        assert seen == {str(d) for d in scene["deal_ids"]}
