"""Проверка значений пользовательских полей по определениям (B-32).

Раньше `custom_fields` сохранялись как есть: определение поля (тип, варианты, границы,
обязательность) существовало только для интерфейса. Тесты фиксируют правила, выбранные так,
чтобы не ломать имеющееся: неизвестные ключи сохраняются, `null` сбрасывает значение,
обязательность проверяется при создании, а при PATCH — только для присланного ключа."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.modules.catalog.custom_fields import check_values
from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import (
    by_code,
    create_deal,
    create_organization,
    create_published_workflow,
    graph_body,
    login,
    transition_deal,
)
from tests.test_cms_webhook import _accepted, hook  # noqa: F401  (фикстура вебхука)

PREFIX = "t32_"


def _def(code: str, field_type: str, **extra: Any) -> SimpleNamespace:
    return SimpleNamespace(
        code=code,
        label=code,
        field_type=field_type,
        options=extra.get("options"),
        validation=extra.get("validation"),
        is_required=extra.get("is_required", False),
    )


def _fields(errors) -> list[str]:
    return [error.field for error in errors]


class TestCheckValues:
    """Чистая логика без БД: что считается верным значением для каждого типа."""

    def test_string_length_and_pattern(self) -> None:
        d = _def("s", "string", validation={"max_length": 4, "pattern": r"^[A-Z]+$"})

        assert check_values([d], {"s": "ABCD"}, creating=False) == []
        assert _fields(check_values([d], {"s": "ABCDE"}, creating=False)) == ["custom_fields.s"]
        assert _fields(check_values([d], {"s": "abc"}, creating=False)) == ["custom_fields.s"]
        assert _fields(check_values([d], {"s": 5}, creating=False)) == ["custom_fields.s"]

    def test_broken_pattern_in_definition_does_not_block(self) -> None:
        d = _def("s", "string", validation={"pattern": "("})

        assert check_values([d], {"s": "что угодно"}, creating=False) == []

    def test_number_type_and_bounds(self) -> None:
        d = _def("n", "number", validation={"min": 1, "max": 10})

        assert check_values([d], {"n": 5}, creating=False) == []
        assert check_values([d], {"n": 2.5}, creating=False) == []
        for bad in (0, 11, "5", True, float("nan"), float("inf")):
            assert _fields(check_values([d], {"n": bad}, creating=False)) == [
                "custom_fields.n"
            ], bad

    def test_date_accepts_iso_only(self) -> None:
        d = _def("d", "date")

        assert check_values([d], {"d": "2026-09-27"}, creating=False) == []
        assert check_values([d], {"d": "2026-09-27T10:30:00+03:00"}, creating=False) == []
        for bad in ("27.09.2026", "2026-13-40", "завтра", 20260927):
            assert _fields(check_values([d], {"d": bad}, creating=False)) == ["custom_fields.d"]

    def test_bool_is_a_real_boolean(self) -> None:
        d = _def("b", "bool")

        assert check_values([d], {"b": False}, creating=False) == []
        assert _fields(check_values([d], {"b": "true"}, creating=False)) == ["custom_fields.b"]
        assert _fields(check_values([d], {"b": 1}, creating=False)) == ["custom_fields.b"]

    def test_select_and_multiselect_use_the_choices(self) -> None:
        one = _def("one", "select", options={"choices": ["A", "B"]})
        many = _def("many", "multiselect", options={"choices": ["A", "B"]})

        assert check_values([one, many], {"one": "A", "many": ["A", "B"]}, creating=False) == []
        assert _fields(check_values([one], {"one": "C"}, creating=False)) == ["custom_fields.one"]
        assert _fields(check_values([many], {"many": ["A", "C"]}, creating=False)) == [
            "custom_fields.many"
        ]
        assert _fields(check_values([many], {"many": "A"}, creating=False)) == [
            "custom_fields.many"
        ]

    def test_select_without_choices_accepts_any_string(self) -> None:
        assert check_values([_def("one", "select")], {"one": "что угодно"}, creating=False) == []

    def test_unknown_keys_and_file_type_are_left_alone(self) -> None:
        d = _def("f", "file")

        assert check_values([d], {"f": {"что-то": 1}, "чужой": [1, 2]}, creating=False) == []

    def test_null_and_empty_clear_a_value_unless_required(self) -> None:
        optional = _def("o", "number")
        required = _def("r", "string", is_required=True)

        assert check_values([optional], {"o": None}, creating=False) == []
        assert check_values([optional], {"o": ""}, creating=False) == []
        assert _fields(check_values([required], {"r": None}, creating=False)) == ["custom_fields.r"]
        assert _fields(check_values([required], {"r": ""}, creating=False)) == ["custom_fields.r"]

    def test_required_is_enforced_on_create_but_only_for_sent_keys_on_update(self) -> None:
        required = _def("r", "string", is_required=True)

        assert _fields(check_values([required], {}, creating=True)) == ["custom_fields.r"]
        assert check_values([required], {}, creating=False) == []
        assert check_values([required], {"r": "есть"}, creating=True) == []

    def test_required_bool_is_not_enforced(self) -> None:
        # false — осмысленный ответ, как и в форме интерфейса.
        assert check_values([_def("b", "bool", is_required=True)], {}, creating=True) == []

    def test_required_can_be_switched_off_for_transitions(self) -> None:
        required = _def("r", "string", is_required=True)

        assert check_values([required], {"r": None}, creating=False, check_required=False) == []

    def test_all_problems_are_reported_at_once(self) -> None:
        a = _def("a", "number")
        b = _def("b", "date")

        errors = check_values([a, b], {"a": "x", "b": "y"}, creating=False)

        assert _fields(errors) == ["custom_fields.a", "custom_fields.b"]


# --- Через API ------------------------------------------------------------------------------


async def _add_def(entity_type: str, code: str, field_type: str, extra: dict[str, Any]) -> None:
    from app.core.db import session_scope
    from app.modules.catalog.models import CustomFieldDef

    async with session_scope() as session:
        session.add(
            CustomFieldDef(
                entity_type=entity_type,
                code=PREFIX + code,
                label=f"Поле {code}",
                field_type=field_type,
                options=extra.get("options"),
                validation=extra.get("validation"),
                is_required=extra.get("is_required", False),
                is_active=extra.get("is_active", True),
                workflow_id=extra.get("workflow_id"),
            )
        )


async def _purge() -> None:
    from sqlalchemy import delete

    from app.core.db import session_scope
    from app.modules.catalog.models import CustomFieldDef

    async with session_scope() as session:
        await session.execute(delete(CustomFieldDef).where(CustomFieldDef.code.like(PREFIX + "%")))


@pytest.fixture
def defs(client):
    """Определения этого файла глобальны (не привязаны к тесту): убираем до и после, иначе
    обязательное поле оставило бы 422 на создании сделок в остальных тестах."""
    run(client, _purge)

    def add(entity_type: str, code: str, field_type: str, **extra: Any) -> None:
        run(client, _add_def, entity_type, code, field_type, extra)

    yield add
    run(client, _purge)


def _problem_fields(response) -> list[str]:
    return [item["field"] for item in response.json()["errors"]]


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL")
class TestDealCustomFields:
    def _setup(self, client, defs) -> str:
        defs("deal", "qty", "number", validation={"min": 1, "max": 10})
        defs("deal", "due", "date")
        defs("deal", "kind", "select", options={"choices": ["A", "B"]})
        defs("deal", "code", "string", validation={"pattern": r"^[A-Z]{2}-\d+$"})
        login(client)
        return create_published_workflow(client)["workflow"]["id"]

    def _create(self, client, workflow_id: str, custom: dict[str, Any]):
        return client.post(
            "/api/deals",
            json={
                "title": "Сделка с полями",
                "deal_type": "b2b",
                "workflow_id": workflow_id,
                "organization_id": create_organization(client),
                "custom_fields": custom,
            },
        )

    def test_valid_values_and_unknown_keys_are_stored(self, client, defs) -> None:
        workflow_id = self._setup(client, defs)

        response = self._create(
            client,
            workflow_id,
            {
                PREFIX + "qty": 3,
                PREFIX + "due": "2026-10-01",
                PREFIX + "kind": "A",
                PREFIX + "code": "AB-12",
                "служебное": {"любое": True},
            },
        )

        assert response.status_code == 201, response.text
        stored = response.json()["custom_fields"]
        assert stored[PREFIX + "qty"] == 3
        assert stored["служебное"] == {"любое": True}

    def test_invalid_values_give_422_with_one_error_per_field(self, client, defs) -> None:
        workflow_id = self._setup(client, defs)

        response = self._create(
            client,
            workflow_id,
            {
                PREFIX + "qty": 99,
                PREFIX + "due": "вчера",
                PREFIX + "kind": "Z",
                PREFIX + "code": "x",
            },
        )

        assert response.status_code == 422, response.text
        assert response.json()["code"] == "CRM-1001"
        assert sorted(_problem_fields(response)) == sorted(
            f"custom_fields.{PREFIX}{name}" for name in ("qty", "due", "kind", "code")
        )
        assert all(item["reason"] for item in response.json()["errors"])

    def test_patch_validates_only_the_sent_keys(self, client, defs) -> None:
        workflow_id = self._setup(client, defs)
        deal = self._create(client, workflow_id, {PREFIX + "qty": 3}).json()

        bad = client.patch(
            f"/api/deals/{deal['id']}",
            json={"custom_fields": {PREFIX + "qty": 0}},
            headers={"If-Match": str(deal["version"])},
        )
        assert bad.status_code == 422, bad.text
        assert _problem_fields(bad) == [f"custom_fields.{PREFIX}qty"]

        ok = client.patch(
            f"/api/deals/{deal['id']}",
            json={"custom_fields": {PREFIX + "qty": 7, PREFIX + "code": None}},
            headers={"If-Match": str(deal["version"])},
        )
        assert ok.status_code == 200, ok.text
        current = client.get(f"/api/deals/{deal['id']}").json()["deal"]["custom_fields"]
        assert current[PREFIX + "qty"] == 7

    def test_required_field_blocks_create_but_not_editing_old_deals(self, client, defs) -> None:
        login(client)
        workflow_id = create_published_workflow(client)["workflow"]["id"]
        old = create_deal(client, workflow_id)  # создана до появления обязательного поля
        defs("deal", "must", "string", is_required=True)

        missing = self._create(client, workflow_id, {})
        assert missing.status_code == 422, missing.text
        assert _problem_fields(missing) == [f"custom_fields.{PREFIX}must"]
        assert self._create(client, workflow_id, {PREFIX + "must": "есть"}).status_code == 201

        # у старой сделки значения нет — прочие поля правятся как раньше
        renamed = client.patch(
            f"/api/deals/{old['id']}",
            json={"title": "Новое название"},
            headers={"If-Match": str(old["version"])},
        )
        assert renamed.status_code == 200, renamed.text
        # а очистить обязательное значение явно нельзя
        cleared = client.patch(
            f"/api/deals/{old['id']}",
            json={"custom_fields": {PREFIX + "must": ""}},
            headers={"If-Match": str(renamed.json()["version"])},
        )
        assert cleared.status_code == 422, cleared.text

    def test_definition_of_another_workflow_and_inactive_ones_do_not_apply(
        self, client, defs
    ) -> None:
        login(client)
        workflow_id = create_published_workflow(client)["workflow"]["id"]
        # `workflow_id` — внешний ключ: берём реальную вторую воронку
        other_workflow = uuid.UUID(create_published_workflow(client)["workflow"]["id"])
        defs("deal", "scoped", "string", is_required=True, workflow_id=other_workflow)
        defs("deal", "off", "number", is_required=True, is_active=False)

        response = self._create(client, workflow_id, {PREFIX + "off": "не число"})

        assert response.status_code == 201, response.text

    def test_transition_fields_are_type_checked_but_not_required(self, client, defs) -> None:
        login(client)
        graph = create_published_workflow(
            client,
            graph_body(
                statuses=[
                    {"code": "new", "name": "Новая", "type": "initial"},
                    {"code": "next", "name": "Закрыта", "type": "won"},
                ],
                transitions=[{"from_status": "new", "to_status": "next", "name": "Вперёд"}],
                sla_rules=[],
            ),
        )
        deal = create_deal(client, graph["workflow"]["id"])  # до появления обязательного поля
        defs("deal", "when", "date")
        defs("deal", "must", "string", is_required=True)
        target = by_code(graph)["next"]["id"]

        bad = transition_deal(client, deal, target, fields={f"custom_fields.{PREFIX}when": "х"})
        assert bad.status_code == 422, bad.text
        assert _problem_fields(bad) == [f"custom_fields.{PREFIX}when"]

        ok = transition_deal(
            client, deal, target, fields={f"custom_fields.{PREFIX}when": "2026-10-01"}
        )
        assert ok.status_code == 200, ok.text


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL")
class TestOrganizationAndProductCustomFields:
    def test_organization_create_and_patch(self, client, defs) -> None:
        defs("organization", "level", "number", validation={"max": 5})
        login(client)

        bad = client.post(
            "/api/organizations",
            json={
                "name": f"Вуз с полями {uuid.uuid4().hex[:6]}",
                "org_type": "university",
                "custom_fields": {PREFIX + "level": 9},
            },
        )
        assert bad.status_code == 422, bad.text
        assert _problem_fields(bad) == [f"custom_fields.{PREFIX}level"]

        created = client.post(
            "/api/organizations",
            json={
                "name": f"Вуз с полями два {uuid.uuid4().hex[:6]}",
                "org_type": "university",
                "custom_fields": {PREFIX + "level": 4, "как-есть": "x"},
            },
        )
        assert created.status_code == 201, created.text
        org = created.json()

        patched = client.patch(
            f"/api/organizations/{org['id']}",
            json={"custom_fields": {PREFIX + "level": "много"}},
            headers={"If-Match": str(org["version"])},
        )
        assert patched.status_code == 422, patched.text

    def test_definitions_of_another_entity_do_not_leak(self, client, defs) -> None:
        defs("product", "grade", "number", is_required=True)
        login(client)

        created = client.post(
            "/api/organizations",
            json={
                "name": f"Вуз без чужих правил {uuid.uuid4().hex[:6]}",
                "org_type": "university",
                "custom_fields": {},
            },
        )

        assert created.status_code == 201, created.text

    def test_product_create_and_patch(self, client, defs) -> None:
        defs("product", "grade", "select", options={"choices": ["базовый", "про"]})
        login(client)

        bad = client.post(
            "/api/products",
            json={
                "code": f"p-{uuid.uuid4().hex[:8]}",
                "name": "Курс",
                "custom_fields": {PREFIX + "grade": "другой"},
            },
        )
        assert bad.status_code == 422, bad.text
        assert _problem_fields(bad) == [f"custom_fields.{PREFIX}grade"]

        created = client.post(
            "/api/products",
            json={
                "code": f"p-{uuid.uuid4().hex[:8]}",
                "name": "Курс",
                "custom_fields": {PREFIX + "grade": "про"},
            },
        )
        assert created.status_code == 201, created.text
        product = created.json()

        bad_patch = client.patch(
            f"/api/products/{product['id']}",
            json={"custom_fields": {PREFIX + "grade": "чужой"}},
            headers={"If-Match": str(product["version"])},
        )
        assert bad_patch.status_code == 422, bad_patch.text


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL")
class TestSystemFlowsAreNotBlocked:
    def test_site_lead_creates_a_deal_even_with_a_required_field(self, client, defs, hook) -> None:  # noqa: F811
        # Заявку с сайта создаёт учётка интеграции; обязательное поле, которого сайт не знает,
        # не должно её терять.
        defs("deal", "must", "string", is_required=True)

        body = _accepted(hook.send(hook.lead()))

        assert body["deal_id"]
