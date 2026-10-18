"""B7-8: пагинация и `party_display` списка соглашений об ЭДО
(`GET /api/admin/edm-agreements`, `app/modules/signing/router.py`).

Было: `stmt.limit(100)` без курсора и без имени стороны — список обрывался на
сотой записи и показывал только `party_type`/`party_id` (UUID вместо названия
вуза или ФИО контакта). Теперь — курсорная пагинация (`app.core.pagination`,
как у `GET /admin/incidents`) и `party_display`, резолвящийся по `party_type`
среди строк страницы одним доп. запросом на тип (`EdmAgreementService.
party_displays`), без N+1.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import event

from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _login(client, user) -> None:
    client.headers["X-CSRF-Token"] = authenticate(client, user)


async def _agreement(party_type: str, party_id: uuid.UUID) -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.signing.models import EdmAgreement

    async with session_scope() as session:
        agreement = EdmAgreement(
            party_type=party_type, party_id=party_id, conclusion_method="paper"
        )
        session.add(agreement)
        await session.flush()
        return agreement.id


async def _organization(name: str) -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.catalog.models import Organization

    async with session_scope() as session:
        org = Organization(name=name, org_type="university")
        session.add(org)
        await session.flush()
        return org.id


async def _contact(last_name: str, first_name: str) -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.catalog.models import Contact

    async with session_scope() as session:
        contact = Contact(last_name=last_name, first_name=first_name, middle_name=None)
        session.add(contact)
        await session.flush()
        return contact.id


class TestPartyDisplay:
    def test_organization_name_is_resolved(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        org_id = run(client, _organization, "РТ Университет Б7-8")
        run(client, _agreement, "organization", org_id)

        response = client.get("/api/admin/edm-agreements")

        assert response.status_code == 200, response.text
        item = next(i for i in response.json()["items"] if i["party_id"] == str(org_id))
        assert item["party_display"] == "РТ Университет Б7-8"

    def test_contact_full_name_is_resolved(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        contact_id = run(client, _contact, "Иванов", "Иван")
        run(client, _agreement, "contact", contact_id)

        response = client.get("/api/admin/edm-agreements")

        assert response.status_code == 200, response.text
        item = next(i for i in response.json()["items"] if i["party_id"] == str(contact_id))
        assert item["party_display"] == "Иванов Иван"


class TestKeysetPagination:
    def test_small_pages_cover_every_agreement_once(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        org_id = run(client, _organization, "Вуз для пагинации Б7-8")
        agreement_ids = {str(run(client, _agreement, "organization", org_id)) for _ in range(5)}

        seen: list[str] = []
        cursor: str | None = None
        for _ in range(20):  # предохранитель от бесконечного цикла при поломанном курсоре
            params: dict[str, object] = {"limit": 2}
            if cursor:
                params["cursor"] = cursor
            response = client.get("/api/admin/edm-agreements", params=params)
            assert response.status_code == 200, response.text
            assert len(response.json()["items"]) <= 2
            body = response.json()
            seen.extend(item["id"] for item in body["items"])
            cursor = body["next_cursor"]
            if cursor is None:
                break

        assert len(seen) == len(set(seen)), "курсор вернул одну и ту же строку дважды"
        assert agreement_ids <= set(seen)


class TestQueryBudget:
    def test_the_query_count_does_not_grow_with_rows(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        org_id = run(client, _organization, "Вуз для бюджета запросов Б7-8")

        def _count_queries() -> int:
            from app.core.db import get_engine

            counter = {"n": 0}

            def _before(*_args: object, **_kwargs: object) -> None:
                counter["n"] += 1

            sync_engine = get_engine().sync_engine
            event.listen(sync_engine, "before_cursor_execute", _before)
            try:
                response = client.get("/api/admin/edm-agreements?limit=100")
            finally:
                event.remove(sync_engine, "before_cursor_execute", _before)
            assert response.status_code == 200, response.text
            return counter["n"]

        run(client, _agreement, "organization", org_id)
        run(client, _agreement, "organization", org_id)
        _count_queries()  # прогрев кэшей прав/настроек
        small = _count_queries()

        for _ in range(20):
            run(client, _agreement, "organization", org_id)
        large = _count_queries()

        assert small > 0, "счётчик запросов не сработал — тест ничего не проверяет"
        assert large <= small + 2, (
            f"{small} запросов на 2+ строки и {large} на 22+ (тот же party_id) — "
            "вероятен N+1 в резолве party_display."
        )
