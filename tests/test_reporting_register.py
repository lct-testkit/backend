"""B1-1 (BUG-1): фильтр по продукту/направлению в реестре сделок ограничивает строки, не только
сделки; сумма сделки указывается один раз; выбор направления включает поддерево.

B1-2 (`TestColumns`): выбор колонок (`params.columns`) определяет и заголовки файла, и зерно
строки (`_register_grain`) — без колонки продукта сделка с несколькими продуктами не плодит
лишних строк. Настоящая Postgres обязательна — см. докстринг `tests/conftest.py`.
"""

from __future__ import annotations

import uuid

import pytest

from app.modules.reporting.builders import build_deal_register, estimate_deal_register
from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run
from tests.report_helpers import RegisterWorld, make_register_world

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _principal(user_id: uuid.UUID, role: str, *, team_id: uuid.UUID | None = None):
    from app.core.security import Principal

    return Principal(
        user_id=user_id,
        keycloak_id="k",
        role=role,
        status="active",
        email=None,
        full_name="x",
        team_id=team_id,
        manager_id=None,
        perm_epoch=1,
        session_id=None,
        consent_version="1.0",
        must_change_password=False,
        claims=None,  # type: ignore[arg-type]
    )


def _role_of(user_key: str) -> str:
    # `world.user_ids` — ключи "HEAD"/"KAM1"/"KAM2" (различают двух КАМов фабрики), а роль для
    # Principal.role/deal_scope_for — всегда "HEAD"/"KAM" без цифры.
    return "HEAD" if user_key == "HEAD" else "KAM"


async def _rows(world: RegisterWorld, user_key: str, params: dict) -> list[list]:
    from app.core.db import session_scope

    team_id = world.team_id if user_key == "HEAD" else None
    principal = _principal(world.user_ids[user_key], _role_of(user_key), team_id=team_id)
    async with session_scope() as session:
        dataset = await build_deal_register(session, principal, params)
        return dataset.rows


async def _estimate(world: RegisterWorld, user_key: str, params: dict) -> int:
    from app.core.db import session_scope

    team_id = world.team_id if user_key == "HEAD" else None
    principal = _principal(world.user_ids[user_key], _role_of(user_key), team_id=team_id)
    async with session_scope() as session:
        return await estimate_deal_register(session, principal, params)


class TestProductFilterDoesNotLeakSiblingProducts:
    def test_filter_by_product_p_excludes_rows_with_product_q(self, client) -> None:
        world = run(client, make_register_world)
        rows = run(
            client,
            _rows,
            world,
            "HEAD",
            {"product_ids": [str(world.product_ids["P"])]},
        )
        products = {row[2] for row in rows}
        assert products == {"Python: базовый курс"}
        # D1 имеет продукты P и Q -> при фильтре "только P" ровно одна строка на D1, не две.
        d1_rows = [row for row in rows if row[5] == world.deal_numbers["D1"]]
        assert len(d1_rows) == 1

    def test_deal_with_two_products_gives_two_rows_without_filter(self, client) -> None:
        world = run(client, make_register_world)
        rows = run(client, _rows, world, "HEAD", {})
        d1_rows = [row for row in rows if row[5] == world.deal_numbers["D1"]]
        assert len(d1_rows) == 2
        assert {row[2] for row in d1_rows} == {"Python: базовый курс", "Java: курс"}

    def test_deleted_deal_is_never_shown(self, client) -> None:
        world = run(client, make_register_world)
        rows = run(client, _rows, world, "HEAD", {})
        numbers = {row[5] for row in rows}
        assert world.deal_numbers["D5"] not in numbers

    def test_deal_without_products_shown_as_dash_without_filter(self, client) -> None:
        world = run(client, make_register_world)
        rows = run(client, _rows, world, "HEAD", {})
        d4_rows = [row for row in rows if row[5] == world.deal_numbers["D4"]]
        assert len(d4_rows) == 1
        assert d4_rows[0][2] == "—"

    def test_deal_without_products_excluded_when_filtering_by_product(self, client) -> None:
        world = run(client, make_register_world)
        rows = run(
            client,
            _rows,
            world,
            "HEAD",
            {"product_ids": [str(world.product_ids["P"])]},
        )
        numbers = {row[5] for row in rows}
        assert world.deal_numbers["D4"] not in numbers


class TestDirectionHierarchy:
    def test_parent_direction_includes_child(self, client) -> None:
        world = run(client, make_register_world)
        rows = run(
            client,
            _rows,
            world,
            "HEAD",
            {"direction_ids": [str(world.direction_ids["Программирование"])]},
        )
        products = {row[2] for row in rows}
        # «Программирование» -> «Python» (дочернее): продукт P должен попасть, Q (Java) — нет.
        assert "Python: базовый курс" in products
        assert "Java: курс" not in products

    def test_java_direction_is_separate(self, client) -> None:
        world = run(client, make_register_world)
        rows = run(
            client,
            _rows,
            world,
            "HEAD",
            {"direction_ids": [str(world.direction_ids["Java"])]},
        )
        products = {row[2] for row in rows}
        assert products == {"Java: курс"}


class TestAmountIsNotDuplicated:
    def test_sum_over_filtered_rows_equals_distinct_deal_amounts(self, client) -> None:
        world = run(client, make_register_world)
        rows = run(
            client,
            _rows,
            world,
            "HEAD",
            {"product_ids": [str(world.product_ids["P"]), str(world.product_ids["Q"])]},
        )
        # D1 даёт две строки (P и Q), но сумма должна встретиться только один раз.
        total = sum(row[7] for row in rows if row[7] is not None)
        assert total == pytest.approx(100000.0 + 50000.0 + 70000.0)

    def test_amount_is_on_exactly_one_row_per_deal(self, client) -> None:
        world = run(client, make_register_world)
        rows = run(client, _rows, world, "HEAD", {})
        by_deal: dict[str, int] = {}
        for row in rows:
            if row[7] is not None:
                by_deal[row[5]] = by_deal.get(row[5], 0) + 1
        assert all(count == 1 for count in by_deal.values())


class TestScope:
    def test_kam_sees_only_their_own_deals(self, client) -> None:
        world = run(client, make_register_world)
        rows = run(client, _rows, world, "KAM1", {})
        amounts = sorted(row[7] for row in rows if row[7] is not None)
        assert amounts == [50000.0, 100000.0]

    def test_head_sees_the_whole_team(self, client) -> None:
        world = run(client, make_register_world)
        rows = run(client, _rows, world, "HEAD", {})
        amounts = sorted(row[7] for row in rows if row[7] is not None)
        assert amounts == [10000.0, 50000.0, 70000.0, 100000.0]


class TestEstimateMatchesBuild:
    @pytest.mark.parametrize(
        "params",
        [
            {},
            {"product_ids": "P"},
            {"product_ids": "P,Q"},
            {"direction_ids": "Программирование"},
        ],
    )
    def test_estimate_equals_row_count(self, client, params: dict) -> None:
        world = run(client, make_register_world)
        resolved = dict(params)
        if "product_ids" in resolved:
            resolved["product_ids"] = [
                str(world.product_ids[key]) for key in resolved["product_ids"].split(",")
            ]
        if "direction_ids" in resolved:
            resolved["direction_ids"] = [str(world.direction_ids[resolved["direction_ids"]])]

        rows = run(client, _rows, world, "HEAD", resolved)
        estimate = run(client, _estimate, world, "HEAD", resolved)
        assert estimate == len(rows)


async def _dataset(world: RegisterWorld, user_key: str, params: dict):
    from app.core.db import session_scope

    team_id = world.team_id if user_key == "HEAD" else None
    principal = _principal(world.user_ids[user_key], _role_of(user_key), team_id=team_id)
    async with session_scope() as session:
        return await build_deal_register(session, principal, params)


class TestColumns:
    """B1-2: `params.columns` — состав и порядок заголовков файла, и зерно строки реестра."""

    def test_subset_of_columns_returns_exactly_those_headers_in_order(self, client) -> None:
        world = run(client, make_register_world)
        dataset = run(
            client,
            _dataset,
            world,
            "HEAD",
            {"columns": ["product", "organization", "owner", "status"]},
        )
        assert dataset.columns == ["Продукт", "Вуз", "Ответственный", "Статус"]
        assert dataset.keys == ["product", "organization", "owner", "status"]

    def test_column_order_is_independent_of_catalog_order(self, client) -> None:
        world = run(client, make_register_world)
        rows = run(
            client,
            _rows,
            world,
            "HEAD",
            {"columns": ["product", "organization"]},
        )
        d2_rows = [row for row in rows if row[0] == "Python: базовый курс"]
        assert any(row[1].startswith("ВУЗ 1") for row in d2_rows)

    def test_no_product_or_direction_column_gives_one_row_per_deal(self, client) -> None:
        # Зерно "deal" (B1-1/B1-2): D1 имеет продукты P и Q, но без колонки продукта/направления
        # должен дать РОВНО одну строку, не две.
        world = run(client, make_register_world)
        rows = run(
            client,
            _rows,
            world,
            "HEAD",
            {"columns": ["organization", "status", "owner", "number", "title"]},
        )
        numbers = [row[3] for row in rows]
        assert numbers.count(world.deal_numbers["D1"]) == 1
        # Четыре сделки видит HEAD (D1..D4), D5 удалена.
        assert len(rows) == 4

    def test_direction_column_without_product_collapses_same_direction_rows(self, client) -> None:
        # Зерно "direction": D1 (P -> Python, Q -> Java) имеет два РАЗНЫХ направления -> две
        # строки; D2 (только P) -> одна строка; D4 (без продуктов) -> одна строка с «—».
        world = run(client, make_register_world)
        rows = run(
            client,
            _rows,
            world,
            "HEAD",
            {"columns": ["organization", "direction", "number"]},
        )
        d1_directions = {row[1] for row in rows if row[2] == world.deal_numbers["D1"]}
        assert d1_directions == {"Python", "Java"}
        d2_rows = [row for row in rows if row[2] == world.deal_numbers["D2"]]
        assert len(d2_rows) == 1
        assert d2_rows[0][1] == "Python"
        d4_rows = [row for row in rows if row[2] == world.deal_numbers["D4"]]
        assert len(d4_rows) == 1
        assert d4_rows[0][1] == "—"

    def test_amount_still_appears_exactly_once_per_deal_in_direction_grain(self, client) -> None:
        world = run(client, make_register_world)
        rows = run(
            client,
            _rows,
            world,
            "HEAD",
            {"columns": ["direction", "number", "amount"]},
        )
        by_deal: dict[str, int] = {}
        for _direction, number, amount in rows:
            if amount is not None:
                by_deal[number] = by_deal.get(number, 0) + 1
        assert all(count == 1 for count in by_deal.values())
        assert by_deal.get(world.deal_numbers["D1"]) == 1

    def test_empty_columns_list_behaves_like_absent_and_uses_defaults(self, client) -> None:
        world = run(client, make_register_world)
        with_empty = run(client, _dataset, world, "HEAD", {"columns": []})
        without = run(client, _dataset, world, "HEAD", {})
        assert with_empty.columns == without.columns

    def test_unknown_column_raises_without_touching_the_database(self, client) -> None:
        from app.core.errors import ValidationError

        world = run(client, make_register_world)
        with pytest.raises(ValidationError):
            run(client, _dataset, world, "HEAD", {"columns": ["vuz"]})

    def test_unknown_column_via_api_gives_422_and_leaves_no_report_job(self, client) -> None:
        from sqlalchemy import func, select

        from app.core.db import session_scope
        from app.modules.reporting.models import ReportJob, ReportTemplate

        async def _ensure_template() -> None:
            async with session_scope() as session:
                existing = await session.scalar(
                    select(ReportTemplate.id).where(ReportTemplate.code == "deal_register")
                )
                if existing is not None:
                    return
                session.add(
                    ReportTemplate(
                        code="deal_register",
                        name="Сделки по вузам и направлениям",
                        description="Реестр сделок.",
                        query_def={"kind": "deal_register"},
                        allowed_roles=[],
                        default_params={"limit": 1000},
                        output_formats=["xlsx", "pdf"],
                        is_active=True,
                    )
                )

        run(client, _ensure_template)

        async def _job_count() -> int:
            async with session_scope() as session:
                return int((await session.execute(select(func.count(ReportJob.id)))).scalar_one())

        before = run(client, _job_count)
        kam = run(client, _make_user, "KAM")
        client.headers["X-CSRF-Token"] = authenticate(client, kam)
        response = client.post(
            "/api/reports",
            json={
                "template_code": "deal_register",
                "format": "xlsx",
                "params": {"columns": ["vuz"]},
            },
        )
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "CRM-1001"
        after = run(client, _job_count)
        assert after == before
