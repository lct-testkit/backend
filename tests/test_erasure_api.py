"""Запросы на удаление/обезличивание в админке: имя субъекта и фильтр по нему.

Запросы заводятся в БД напрямую (сам путь создания требует «четырёх глаз» —
он проверен в `tests/test_identity_admin.py`). Сквозные тесты на настоящей
PostgreSQL (`TEST_DATABASE_URL`) — см. докстринг `tests/conftest.py`.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _login(client, user) -> None:
    csrf = authenticate(client, user)
    client.headers["X-CSRF-Token"] = csrf


async def _request(subject_type: str, subject_id: uuid.UUID) -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.identity.models import DataErasureRequest

    async with session_scope() as session:
        request = DataErasureRequest(
            subject_type=subject_type,
            subject_id=subject_id,
            reason="по обращению субъекта",
            legal_basis="ст. 21 152-ФЗ",
            status="blocked",
            blockers={"mode": "anonymize", "items": []},
        )
        session.add(request)
        await session.flush()
        return request.id


async def _contact(first: str, last: str, middle: str | None = None) -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.catalog.models import Contact

    async with session_scope() as session:
        contact = Contact(first_name=first, last_name=last, middle_name=middle)
        session.add(contact)
        await session.flush()
        return contact.id


async def _organization(name: str) -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.catalog.models import Organization

    async with session_scope() as session:
        org = Organization(name=name, org_type="individual_entrepreneur")
        session.add(org)
        await session.flush()
        return org.id


def _by_id(body: dict) -> dict[str, dict]:
    return {item["id"]: item for item in body["items"]}


class TestSubjectDisplay:
    """Список «Удаляемые» показывает, чей это запрос, а не голый UUID."""

    def test_user_contact_and_organization_are_named(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        user = run(client, _make_user, "KAM")
        contact_id = run(client, _contact, "Пётр", "Сидоров", "Иванович")
        org_id = run(client, _organization, "ИП Кузнецов А. А.")
        user_request = run(client, _request, "user", user.id)
        contact_request = run(client, _request, "contact", contact_id)
        org_request = run(client, _request, "organization", org_id)

        response = client.get("/api/admin/erasure-requests", params={"limit": 100})

        assert response.status_code == 200, response.text
        items = _by_id(response.json())
        assert items[str(user_request)]["subject_display"] == user.full_name
        assert items[str(contact_request)]["subject_display"] == "Сидоров Пётр Иванович"
        assert items[str(org_request)]["subject_display"] == "ИП Кузнецов А. А."

    def test_display_name_wins_over_the_full_name(self, client) -> None:
        from app.core.db import session_scope
        from app.modules.identity.models import User

        _login(client, run(client, _make_user, "ADMIN"))
        user = run(client, _make_user, "KAM")

        async def _rename() -> None:
            async with session_scope() as session:
                (await session.get(User, user.id)).display_name = "Ваня"

        run(client, _rename)
        request_id = run(client, _request, "user", user.id)

        detail = client.get(f"/api/admin/erasure-requests/{request_id}").json()

        assert detail["subject_display"] == "Ваня"

    def test_subject_that_no_longer_exists_has_no_name(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        request_id = run(client, _request, "contact", uuid.uuid4())

        detail = client.get(f"/api/admin/erasure-requests/{request_id}")

        assert detail.status_code == 200, detail.text
        assert detail.json()["subject_display"] is None

    def test_detail_and_actions_carry_the_name_too(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        contact_id = run(client, _contact, "Анна", "Петрова")
        request_id = run(client, _request, "contact", contact_id)

        reject = client.post(
            f"/api/admin/erasure-requests/{request_id}/reject",
            json={"reason": "Действующий договор до конца года"},
        )

        assert reject.status_code == 200, reject.text
        assert reject.json()["subject_display"] == "Петрова Анна"


class TestSubjectFilter:
    def test_filter_returns_only_the_subjects_requests(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        subject = run(client, _make_user, "KAM")
        other = run(client, _make_user, "KAM")
        mine = run(client, _request, "user", subject.id)
        run(client, _request, "user", other.id)

        response = client.get("/api/admin/erasure-requests", params={"subject_id": str(subject.id)})

        assert response.status_code == 200, response.text
        assert [item["id"] for item in response.json()["items"]] == [str(mine)]

    def test_filter_without_matches_is_an_empty_list(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))

        response = client.get(
            "/api/admin/erasure-requests", params={"subject_id": str(uuid.uuid4())}
        )

        assert response.status_code == 200, response.text
        assert response.json()["items"] == []

    def test_filter_combines_with_the_others(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        subject = run(client, _make_user, "KAM")
        run(client, _request, "user", subject.id)

        wrong_status = client.get(
            "/api/admin/erasure-requests",
            params={"subject_id": str(subject.id), "status": "completed"},
        )

        assert wrong_status.json()["items"] == []

    def test_malformed_id_is_a_validation_error(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))

        response = client.get("/api/admin/erasure-requests", params={"subject_id": "не-uuid"})

        assert response.status_code == 422
