"""B1-1 (BUG-1): фильтр по продукту/направлению в реестре сделок ограничивает строки, не только
сделки; сумма сделки указывается один раз; выбор направления включает поддерево. Настоящая
Postgres обязательна — см. докстринг `tests/conftest.py`.
"""

from __future__ import annotations

import uuid

import pytest

from app.modules.reporting.builders import build_deal_register, estimate_deal_register
from tests.conftest import TEST_DATABASE_URL, run
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
