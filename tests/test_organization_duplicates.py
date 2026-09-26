"""Дубли организаций без ИНН (`POST /api/organizations`).

С ИНН дубль ловит уникальный индекс и `ORGANIZATION_INN_EXISTS` (CRM-1302). Без ИНН отличить
организацию нечем, кроме названия: `ООО «Базис»`, `ооо "Базис"` и `ООО  Базис` — одна компания
(`app.core.normalize.company_key`), и вторая такая запись — 409 CRM-1301 с кандидатами. Настоящая
Postgres обязательна — см. `tests/conftest.py`.
"""

from __future__ import annotations

import datetime as dt
import random
import uuid

import pytest

from app.modules.catalog.validators import validate_inn
from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import login

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _marker() -> str:
    return uuid.uuid4().hex[:10]


def _create(client, name: str, **fields):
    return client.post("/api/organizations", json={"name": name, "org_type": "company", **fields})


def _valid_inn() -> str:
    """Случайный ИНН юрлица с верной контрольной суммой (каждый десятый подходит)."""
    while True:
        candidate = "77" + "".join(str(random.randint(0, 9)) for _ in range(8))
        if validate_inn(candidate).ok:
            return candidate


def _soft_delete_organization(client, organization_id: str) -> None:
    async def _delete() -> None:
        from app.core.db import session_scope
        from app.modules.catalog.models import Organization

        async with session_scope() as session:
            organization = await session.get(Organization, uuid.UUID(organization_id))
            assert organization is not None
            organization.deleted_at = dt.datetime.now(dt.UTC)

    run(client, _delete)


class TestSameNameWithoutInn:
    def test_second_organization_with_the_same_name_is_a_duplicate(self, client) -> None:
        login(client, "KAM")
        name = f"ООО «Ромашка {_marker()}»"
        first = _create(client, name)
        assert first.status_code == 201, first.text

        second = _create(client, name)
        assert second.status_code == 409, second.text
        body = second.json()
        assert body["code"] == "CRM-1301"
        assert body["candidates"] == [
            {
                "id": first.json()["id"],
                "name": name,
                "match": "same_name",
                "accessible": True,
            }
        ]

    @pytest.mark.parametrize(
        ("existing", "again"),
        [
            ("ООО «Ёлка {m}»", 'ооо "елка {m}"'),
            ("ООО «Ёлка {m}»", "ООО  Ёлка   {m}"),
            ("АО «Сеть – {m}»", "АО «СЕТЬ - {m}»"),
            ("ИП Иванов {m}", "ип  иванов {m} "),
        ],
    )
    def test_case_quotes_yo_dashes_and_spaces_do_not_make_a_new_name(
        self, client, existing: str, again: str
    ) -> None:
        login(client, "KAM")
        marker = _marker()
        assert _create(client, existing.format(m=marker)).status_code == 201

        response = _create(client, again.format(m=marker))
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "CRM-1301"

    def test_legal_form_stays_in_the_name_key(self, client) -> None:
        login(client, "KAM")
        marker = _marker()
        assert _create(client, f"ПАО «Ростелеком {marker}»").status_code == 201

        # Другое юрлицо: `ПАО` и `ООО` — разные организации с похожим названием.
        assert _create(client, f"ООО «Ростелеком {marker}»").status_code == 201

    def test_short_name_of_an_existing_organization_counts_too(self, client) -> None:
        login(client, "KAM")
        marker = _marker()
        first = _create(
            client, f"Общество с ограниченной ответственностью {marker}", short_name=f"РТК {marker}"
        )
        assert first.status_code == 201, first.text

        response = _create(client, f"ртк {marker}")
        assert response.status_code == 409, response.text
        assert response.json()["candidates"][0]["id"] == first.json()["id"]

    def test_deleted_organization_does_not_count(self, client) -> None:
        login(client, "KAM")
        name = f"ООО «Удалённая {_marker()}»"
        first = _create(client, name)
        assert first.status_code == 201, first.text
        _soft_delete_organization(client, first.json()["id"])

        assert _create(client, name).status_code == 201

    def test_existing_organization_with_an_inn_blocks_the_same_name_without_one(
        self, client
    ) -> None:
        login(client, "KAM")
        name = f"ООО «С ИНН {_marker()}»"
        with_inn = _create(client, name, inn=_valid_inn())
        assert with_inn.status_code == 201, with_inn.text

        response = _create(client, name)
        assert response.status_code == 409, response.text
        assert response.json()["candidates"][0]["id"] == with_inn.json()["id"]

    def test_candidate_out_of_scope_shows_no_id(self, client) -> None:
        login(client, "ADMIN")
        name = f"ООО «Чужая {_marker()}»"
        first = _create(client, name)
        assert first.status_code == 201, first.text

        # Менеджер этой организации не ведёт: узнаёт только, что такое название уже занято.
        login(client, "KAM")
        response = _create(client, name)
        assert response.status_code == 409, response.text
        assert response.json()["candidates"] == [
            {"id": None, "name": name, "match": "same_name", "accessible": False}
        ]


class TestOrganizationsWithInnKeepTheInnRules:
    def test_same_name_with_different_inns_is_allowed(self, client) -> None:
        login(client, "KAM")
        name = f"ООО «Одноимённая {_marker()}»"
        first = _create(client, name, inn=_valid_inn())
        assert first.status_code == 201, first.text

        second = _create(client, name, inn=_valid_inn())
        assert second.status_code == 201, second.text
        assert second.json()["id"] != first.json()["id"]

    def test_same_inn_is_still_the_inn_conflict(self, client) -> None:
        login(client, "KAM")
        inn = _valid_inn()
        first = _create(client, f"ООО «Первая {_marker()}»", inn=inn)
        assert first.status_code == 201, first.text

        second = _create(client, f"ООО «Вторая {_marker()}»", inn=inn)
        assert second.status_code == 409, second.text
        assert second.json()["code"] == "CRM-1302"
