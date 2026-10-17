"""B1-9: рейтинг востребованности ИТ-продуктов и направлений.

`compute_demand_index` — чистая функция, без БД (`TestComputeDemandIndex`). `build_product_demand`
нужна настоящая Postgres (`tests/conftest.py`) — сценарий дословно по примеру задачи спринта B1
(документ `backend-sprints.md`, раздел B1-9, «Тесты»): продукты P1/P2, сделки D1-D6.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.core.security import Principal
from app.modules.reporting.builders import build_product_demand, compute_demand_index
from tests.conftest import TEST_DATABASE_URL, run


class TestComputeDemandIndex:
    def test_three_positions_equal_shares_give_the_same_index(self) -> None:
        rows = [
            {"applications": 10, "learners": 10, "streams": 10},
            {"applications": 10, "learners": 10, "streams": 10},
            {"applications": 20, "learners": 20, "streams": 20},
        ]
        ranked, excluded = compute_demand_index(rows)
        assert excluded == []
        indexes = [r["index"] for r in ranked]
        assert indexes[0] == pytest.approx(50.0)
        assert indexes[1] == pytest.approx(25.0)
        assert indexes[2] == pytest.approx(25.0)

    def test_a_tie_gives_the_same_rank(self) -> None:
        rows = [
            {"applications": 10, "learners": 10, "streams": 10},
            {"applications": 10, "learners": 10, "streams": 10},
            {"applications": 20, "learners": 20, "streams": 20},
        ]
        ranked, _ = compute_demand_index(rows)
        # Две позиции с одинаковой долей (25%) должны получить одно и то же место.
        tied = [r["rank"] for r in ranked if r["index"] == pytest.approx(25.0)]
        assert len(tied) == 2
        assert tied[0] == tied[1]

    def test_zero_sum_metric_is_excluded_from_the_average(self) -> None:
        rows = [
            {"applications": 10, "learners": 10, "streams": 0},
            {"applications": 20, "learners": 20, "streams": 0},
        ]
        ranked, excluded = compute_demand_index(rows)
        assert excluded == ["streams"]
        # Без потоков индекс считается по двум показателям (заявки, обучающиеся) — те же доли.
        first, second = sorted(ranked, key=lambda r: -r["applications"])
        assert first["index"] == pytest.approx((20 / 30 + 20 / 30) / 2 * 100, abs=0.1)
        assert second["index"] == pytest.approx((10 / 30 + 10 / 30) / 2 * 100, abs=0.1)

    def test_rows_come_back_sorted_by_index_descending(self) -> None:
        rows = [
            {"applications": 1, "learners": 1, "streams": 1},
            {"applications": 9, "learners": 9, "streams": 9},
        ]
        ranked, _ = compute_demand_index(rows)
        assert ranked[0]["applications"] == 9
        assert ranked[0]["rank"] == 1
        assert ranked[1]["rank"] == 2

    def test_does_not_mutate_the_input_rows(self) -> None:
        rows = [{"applications": 1, "learners": 1, "streams": 1}]
        compute_demand_index(rows)
        assert "index" not in rows[0]


pytestmark_db = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


async def _make_scene() -> dict:
    """Дословно пример задачи B1-9: P1 (направление A), P2 (направление B); D1 (P1, 10 мест,
    поток 1, в работе), D2 (P1, 5, поток 2, выиграна), D3 (P1, 7, поток 1, проиграна), D4 (P2,
    20, без потока), D5 (P1: 3 места, поток 1; P2: 4 места, поток 1), D6 (P1, 100 мест, удалена).
    Одна сделка принадлежит KAM1 (D1-D6 все КАМ1) — KAM2 без сделок, для проверки скоупа."""
    from app.core.db import session_scope
    from app.modules.catalog.models import Direction, Organization, Product
    from app.modules.crm.models import Deal, DealProduct
    from app.modules.identity.models import User
    from app.modules.workflow.models import Workflow, WorkflowStatus

    token = uuid.uuid4().hex[:10]
    async with session_scope() as session:
        kam1 = User(
            keycloak_id=str(uuid.uuid4()),
            email=f"kam1-demand-{token}@rt-it-school.ru",
            full_name="Камов К. К.",
            role="KAM",
            status="active",
            consent_version="1.0",
        )
        kam2 = User(
            keycloak_id=str(uuid.uuid4()),
            email=f"kam2-demand-{token}@rt-it-school.ru",
            full_name="Камова К. К.",
            role="KAM",
            status="active",
            consent_version="1.0",
        )
        session.add_all([kam1, kam2])
        await session.flush()

        direction_a = Direction(code=f"dir-a-{token}", name="Направление A")
        direction_b = Direction(code=f"dir-b-{token}", name="Направление B")
        session.add_all([direction_a, direction_b])
        await session.flush()
        product_p1 = Product(code=f"p1-{token}", name="Продукт P1", direction_id=direction_a.id)
        product_p2 = Product(code=f"p2-{token}", name="Продукт P2", direction_id=direction_b.id)
        session.add_all([product_p1, product_p2])
        await session.flush()

        org = Organization(name=f"ВУЗ рейтинга {token}", org_type="university", owner_id=kam1.id)
        session.add(org)
        await session.flush()

        workflow = Workflow(code=f"wf-demand-{token}", name="Воронка рейтинга", deal_type="b2b")
        session.add(workflow)
        await session.flush()
        status_in_progress = WorkflowStatus(
            workflow_id=workflow.id,
            code="in_progress",
            name="В работе",
            type="intermediate",
            sort_order=0,
        )
        status_won = WorkflowStatus(
            workflow_id=workflow.id, code="won", name="Выиграна", type="won", sort_order=1
        )
        status_lost = WorkflowStatus(
            workflow_id=workflow.id, code="lost", name="Отказ", type="lost", sort_order=2
        )
        session.add_all([status_in_progress, status_won, status_lost])
        await session.flush()

        def _deal(number: str, status) -> Deal:
            return Deal(
                number=f"D-demand-{token}-{number}",
                title=f"Сделка {number}",
                deal_type="b2b",
                workflow_id=workflow.id,
                status_id=status.id,
                organization_id=org.id,
                owner_id=kam1.id,
            )

        d1 = _deal("1", status_in_progress)
        d2 = _deal("2", status_won)
        d3 = _deal("3", status_lost)
        d4 = _deal("4", status_in_progress)
        d5 = _deal("5", status_in_progress)
        d6 = _deal("6", status_in_progress)
        session.add_all([d1, d2, d3, d4, d5, d6])
        await session.flush()
        d6.deleted_at = dt.datetime.now(dt.UTC)

        session.add_all(
            [
                DealProduct(deal_id=d1.id, product_id=product_p1.id, quantity=10, stream_number=1),
                DealProduct(deal_id=d2.id, product_id=product_p1.id, quantity=5, stream_number=2),
                DealProduct(deal_id=d3.id, product_id=product_p1.id, quantity=7, stream_number=1),
                DealProduct(
                    deal_id=d4.id, product_id=product_p2.id, quantity=20, stream_number=None
                ),
                DealProduct(deal_id=d5.id, product_id=product_p1.id, quantity=3, stream_number=1),
                DealProduct(deal_id=d5.id, product_id=product_p2.id, quantity=4, stream_number=1),
                DealProduct(deal_id=d6.id, product_id=product_p1.id, quantity=100, stream_number=1),
            ]
        )
        await session.flush()

        return {
            "kam1_id": kam1.id,
            "kam2_id": kam2.id,
            "product_p1_id": product_p1.id,
            "product_p2_id": product_p2.id,
            "direction_a_id": direction_a.id,
            "direction_b_id": direction_b.id,
        }


def _principal(user_id: uuid.UUID, role: str = "KAM") -> Principal:
    return Principal(
        user_id=user_id,
        keycloak_id="k",
        role=role,
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


async def _dataset(scene: dict, user_key: str, params: dict):
    from app.core.db import session_scope

    principal = _principal(scene[f"{user_key}_id"])
    async with session_scope() as session:
        return await build_product_demand(session, principal, params)


@pytestmark_db
class TestProductDemandProductLevel:
    def test_matches_the_worked_example_from_the_sprint_doc(self, client) -> None:
        scene = run(client, _make_scene)
        dataset = run(client, _dataset, scene, "kam1", {"level": "product", "top": 100})
        by_title = {row[1]: row for row in dataset.rows}

        p1 = by_title["Продукт P1"]
        # Место, Продукт, Направление, Заявок, Выиграно, Обучающихся, Потоков, Индекс
        assert p1[3] == 4  # заявок
        assert p1[4] == 1  # выиграно
        assert p1[5] == 18  # обучающихся
        assert p1[6] == 2  # потоков
        assert p1[7] == pytest.approx(58.7, abs=0.1)

        p2 = by_title["Продукт P2"]
        assert p2[3] == 2
        assert p2[4] == 0
        assert p2[5] == 24
        assert p2[6] == 1
        assert p2[7] == pytest.approx(41.3, abs=0.1)

    def test_sort_by_learners_puts_p2_first(self, client) -> None:
        scene = run(client, _make_scene)
        dataset = run(client, _dataset, scene, "kam1", {"level": "product", "sort_by": "learners"})
        assert dataset.rows[0][1] == "Продукт P2"
        assert dataset.rows[0][0] == 1  # место

    def test_kam_without_deals_sees_nothing(self, client) -> None:
        scene = run(client, _make_scene)
        dataset = run(client, _dataset, scene, "kam2", {"level": "product"})
        assert dataset.rows == []

    def test_top_limits_the_shown_rows_and_notes_it(self, client) -> None:
        scene = run(client, _make_scene)
        dataset = run(client, _dataset, scene, "kam1", {"level": "product", "top": 1})
        assert len(dataset.rows) == 1
        assert dataset.note is not None
        assert "Показано 1 из 2" in dataset.note

    def test_invalid_level_is_rejected(self, client) -> None:
        from app.core.errors import ValidationError

        scene = run(client, _make_scene)
        with pytest.raises(ValidationError):
            run(client, _dataset, scene, "kam1", {"level": "bogus"})


async def _make_same_direction_scene() -> dict:
    """Два продукта одного направления, одна сделка берёт оба — проверяет, что на уровне
    направления это одна заявка (`count(DISTINCT dp.deal_id)` по направлению), не две."""
    from app.core.db import session_scope
    from app.modules.catalog.models import Direction, Organization, Product
    from app.modules.crm.models import Deal, DealProduct
    from app.modules.identity.models import User
    from app.modules.workflow.models import Workflow, WorkflowStatus

    token = uuid.uuid4().hex[:10]
    async with session_scope() as session:
        kam = User(
            keycloak_id=str(uuid.uuid4()),
            email=f"kam-demand-dir-{token}@rt-it-school.ru",
            full_name="Камов К. К.",
            role="KAM",
            status="active",
            consent_version="1.0",
        )
        session.add(kam)
        await session.flush()
        direction = Direction(code=f"dir-shared-{token}", name="Общее направление")
        session.add(direction)
        await session.flush()
        product_a = Product(code=f"pa-{token}", name="Продукт A", direction_id=direction.id)
        product_b = Product(code=f"pb-{token}", name="Продукт B", direction_id=direction.id)
        session.add_all([product_a, product_b])
        await session.flush()
        org = Organization(name=f"ВУЗ направления {token}", org_type="university", owner_id=kam.id)
        session.add(org)
        await session.flush()
        workflow = Workflow(
            code=f"wf-demand-dir-{token}", name="Воронка направления", deal_type="b2b"
        )
        session.add(workflow)
        await session.flush()
        status = WorkflowStatus(
            workflow_id=workflow.id,
            code="in_progress",
            name="В работе",
            type="intermediate",
            sort_order=0,
        )
        session.add(status)
        await session.flush()
        deal = Deal(
            number=f"D-demand-dir-{token}",
            title="Сделка с двумя продуктами одного направления",
            deal_type="b2b",
            workflow_id=workflow.id,
            status_id=status.id,
            organization_id=org.id,
            owner_id=kam.id,
        )
        session.add(deal)
        await session.flush()
        session.add_all(
            [
                DealProduct(deal_id=deal.id, product_id=product_a.id, quantity=1),
                DealProduct(deal_id=deal.id, product_id=product_b.id, quantity=1),
            ]
        )
        await session.flush()
        return {"kam_id": kam.id}


@pytestmark_db
class TestProductDemandDirectionLevel:
    def test_a_deal_with_two_products_of_the_same_direction_gives_one_row(self, client) -> None:
        scene = run(client, _make_same_direction_scene)
        dataset = run(client, _dataset, scene, "kam", {"level": "direction"})
        assert len(dataset.rows) == 1
        # Место, Направление, Продуктов, Заявок, ...
        assert dataset.rows[0][1] == "Общее направление"
        assert dataset.rows[0][2] == 2  # два разных продукта
        assert dataset.rows[0][3] == 1  # одна заявка, не две

    def test_product_count_column_reflects_distinct_products(self, client) -> None:
        scene = run(client, _make_scene)
        dataset = run(client, _dataset, scene, "kam1", {"level": "direction", "top": 100})
        by_title = {row[1]: row for row in dataset.rows}
        assert by_title["Направление A"][2] == 1
        assert by_title["Направление B"][2] == 1
