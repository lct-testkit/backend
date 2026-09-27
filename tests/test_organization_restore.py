"""Восстановление мягко удалённой организации (A-18): `POST /api/organizations/{id}/restore`.

До этой правки организация, зашедшая в `deleted_at` (мягкое удаление сейчас руками — через БД
или будущий сценарий 152-ФЗ), не могла вернуться в строй: клиент видел «уже удалена» в дубле по
ИНН/названию и не имел ручки, чтобы это исправить (backend-issues A-18). Настоящая Postgres
обязательна — см. `tests/conftest.py`.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import login

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _marker() -> str:
    return uuid.uuid4().hex[:10]


def _create(client, name: str | None = None, **fields):
    return client.post(
        "/api/organizations",
        json={"name": name or f"ООО «Реставратор {_marker()}»", "org_type": "company", **fields},
    )


def _soft_delete_organization(client, organization_id: str) -> None:
    async def _delete() -> None:
        from app.core.db import session_scope
        from app.modules.catalog.models import Organization

        async with session_scope() as session:
            organization = await session.get(Organization, uuid.UUID(organization_id))
            assert organization is not None
            organization.deleted_at = dt.datetime.now(dt.UTC)

    run(client, _delete)


def _restore(client, organization_id: str):
    return client.post(f"/api/organizations/{organization_id}/restore")


class TestRestoreHappyPath:
    def test_admin_restores_a_deleted_organization_and_can_read_it_again(self, client) -> None:
        login(client, "ADMIN")
        created = _create(client)
        assert created.status_code == 201, created.text
        org_id = created.json()["id"]
        _soft_delete_organization(client, org_id)

        # Мягко удалённая — не видна обычному чтению.
        assert client.get(f"/api/organizations/{org_id}").status_code == 404

        response = _restore(client, org_id)
        assert response.status_code == 200, response.text
        assert response.json()["id"] == org_id

        again = client.get(f"/api/organizations/{org_id}")
        assert again.status_code == 200, again.text

    def test_head_restores_his_own_deleted_organization(self, client) -> None:
        # HEAD видит организацию по owner_id — по умолчанию это её создатель.
        login(client, "HEAD")
        created = _create(client)
        assert created.status_code == 201, created.text
        org_id = created.json()["id"]
        _soft_delete_organization(client, org_id)

        response = _restore(client, org_id)
        assert response.status_code == 200, response.text


class TestRestorePermission:
    def test_kam_is_forbidden(self, client) -> None:
        login(client, "ADMIN")
        created = _create(client)
        org_id = created.json()["id"]
        _soft_delete_organization(client, org_id)

        login(client, "KAM")
        response = _restore(client, org_id)
        assert response.status_code == 403, response.text


class TestRestoreConflictsAndMissing:
    def test_active_organization_is_a_conflict_not_a_silent_success(self, client) -> None:
        login(client, "ADMIN")
        created = _create(client)
        org_id = created.json()["id"]

        response = _restore(client, org_id)
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "CRM-1304"

    def test_unknown_organization_is_not_found(self, client) -> None:
        login(client, "ADMIN")
        response = _restore(client, str(uuid.uuid4()))
        assert response.status_code == 404, response.text

    def test_deleted_organization_out_of_the_kam_s_scope_is_not_found_not_forbidden(
        self, client
    ) -> None:
        login(client, "ADMIN")
        created = _create(client)
        org_id = created.json()["id"]
        _soft_delete_organization(client, org_id)

        # KAM не видит эту организацию вовсе (не её owner, нет сделок с ней) — 403 по праву
        # проверяется раньше скоупа объекта, так что этот сценарий проверяет HEAD без своих
        # сделок/владения: тоже 404, а не 403, раз право на саму ручку есть.
        login(client, "HEAD")
        response = _restore(client, org_id)
        assert response.status_code == 404, response.text
