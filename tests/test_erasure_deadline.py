"""Срок обезличивания контакта и ИП не короче отсрочки исполнения.

Найдено внешним тестированием: у запроса контакта срок исполнения (7 дней, ст. 21 152-ФЗ) был
раньше отсрочки (30 дней): исполнение начиналось только после срока, то есть срок нарушался у
каждого такого запроса. Отсрочка «Режим A» задумана для сотрудника — у сотрудника она осталась
30 днями.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _login(client, user) -> None:
    client.headers["X-CSRF-Token"] = authenticate(client, user)


async def _new_contact() -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.catalog.models import Contact

    async with session_scope() as session:
        contact = Contact(first_name="Пётр", last_name=f"Тестов-{uuid.uuid4().hex[:6]}")
        session.add(contact)
        await session.flush()
        return contact.id


async def _new_ip() -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.catalog.models import Organization

    async with session_scope() as session:
        org = Organization(
            name=f"ИП Тестов {uuid.uuid4().hex[:6]}", org_type="individual_entrepreneur"
        )
        session.add(org)
        await session.flush()
        return org.id


def _erasure(client, path: str):
    requester = run(client, _make_user, "ADMIN")
    approver = run(client, _make_user, "ADMIN")
    body = {"mode": "anonymize", "reason": "заявление субъекта", "legal_basis": "ст. 21 152-ФЗ"}
    _login(client, requester)
    first = client.post(path, json=body)
    assert first.status_code == 409 and first.json()["code"] == "CRM-1902", first.text
    approval_id = first.json()["approval_id"]
    _login(client, approver)
    assert client.post(f"/api/admin/approvals/{approval_id}/approve").status_code == 200
    _login(client, requester)
    return client.post(path, json={**body, "approval_id": approval_id})


def _dates(response) -> tuple[dt.datetime, dt.datetime]:
    body = response.json()
    request = body.get("request", body)
    return (
        dt.datetime.fromisoformat(request["grace_until"]),
        dt.datetime.fromisoformat(request["deadline_at"]),
    )


class TestGraceDoesNotOutlastTheDeadline:
    def test_contact_request_is_executable_within_the_deadline(self, client) -> None:
        contact_id = run(client, _new_contact)
        response = _erasure(client, f"/api/admin/contacts/{contact_id}/erasure-request")
        assert response.status_code == 201, response.text
        grace_until, deadline_at = _dates(response)
        assert grace_until <= deadline_at

    def test_individual_entrepreneur_request_is_executable_within_the_deadline(
        self, client
    ) -> None:
        org_id = run(client, _new_ip)
        response = _erasure(client, f"/api/admin/organizations/{org_id}/erasure-request")
        assert response.status_code == 201, response.text
        grace_until, deadline_at = _dates(response)
        assert grace_until <= deadline_at

    def test_employee_keeps_the_thirty_day_grace(self, client) -> None:
        from app.core.config import get_settings

        assert get_settings().erasure_grace_days == 30  # сотрудник — по-прежнему 30 дней
