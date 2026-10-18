"""CRUD API лицензий/договоров вуз-вендор-ПО (B3-6).

До этой задачи `organization-licenses` был read-only (загрузка — только
импорт). Настоящая Postgres обязательна — см. докстринг `tests/
conftest.py`.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import create_user, login, sign_in

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _create_organization(client, *, owner_id=None, org_type: str = "university") -> str:
    from app.core.db import session_scope
    from app.modules.catalog.models import Organization

    async def _create() -> str:
        async with session_scope() as session:
            org = Organization(
                name=f"Вуз {uuid.uuid4().hex[:8]}", org_type=org_type, owner_id=owner_id
            )
            session.add(org)
            await session.flush()
            return str(org.id)

    return run(client, _create)


def _audit_entries(client, license_id: str) -> list[tuple[str, dict]]:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.audit.models import AuditLog

    async def _fetch() -> list[tuple[str, dict]]:
        async with session_scope() as session:
            rows = (
                await session.execute(
                    select(AuditLog.action, AuditLog.changes)
                    .where(AuditLog.entity_id == uuid.UUID(license_id))
                    .order_by(AuditLog.created_at)
                )
            ).all()
            return [(action, changes or {}) for action, changes in rows]

    return run(client, _fetch)


def _payload(organization_id: str, **extra) -> dict:
    base = {
        "organization_id": organization_id,
        "vendor": f"Вендор {uuid.uuid4().hex[:6]}",
        "product_name": f"ПО {uuid.uuid4().hex[:6]}",
        "contract_number": f"Д-{uuid.uuid4().hex[:8]}",
    }
    base.update(extra)
    return base


class TestCreateLicense:
    def test_kam_creates_license_for_own_organization(self, client) -> None:
        kam = login(client, "KAM")
        org_id = _create_organization(client, owner_id=kam.id)

        response = client.post(
            "/api/organization-licenses",
            json=_payload(org_id, manager_full_name="Иванов Иван Иванович"),
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["organization_id"] == org_id
        assert body["organization_name"] is not None
        assert body["version"] == 1

        actions = _audit_entries(client, body["id"])
        assert actions[0][0] == "LICENSE_CREATED"
        # Журнал не хранит ФИО как есть (ПДн маскируются при записи).
        assert actions[0][1]["manager_full_name"]["new"] == "Иванов И. И."

    def test_kam_cannot_create_for_foreign_organization(self, client) -> None:
        other_owner = create_user(client, "KAM")
        org_id = _create_organization(client, owner_id=other_owner.id)
        login(client, "KAM")

        response = client.post("/api/organization-licenses", json=_payload(org_id))
        assert response.status_code == 404, response.text

    def test_auditor_cannot_write(self, client) -> None:
        org_id = _create_organization(client)
        login(client, "AUDITOR")

        response = client.post("/api/organization-licenses", json=_payload(org_id))
        assert response.status_code == 403, response.text

    def test_duplicate_composite_key_is_409_with_license_id(self, client) -> None:
        login(client, "ADMIN")
        org_id = _create_organization(client)
        payload = _payload(org_id)

        first = client.post("/api/organization-licenses", json=payload)
        assert first.status_code == 201, first.text

        second = client.post("/api/organization-licenses", json=payload)
        assert second.status_code == 409, second.text
        body = second.json()
        assert body["code"] == "CRM-1301"
        assert body["license_id"] == first.json()["id"]

    def test_same_contract_number_different_product_is_two_rows(self, client) -> None:
        login(client, "ADMIN")
        org_id = _create_organization(client)
        contract = f"Д-{uuid.uuid4().hex[:8]}"
        vendor = f"Вендор {uuid.uuid4().hex[:6]}"

        first = client.post(
            "/api/organization-licenses",
            json=_payload(org_id, vendor=vendor, product_name="ПО Один", contract_number=contract),
        )
        second = client.post(
            "/api/organization-licenses",
            json=_payload(org_id, vendor=vendor, product_name="ПО Два", contract_number=contract),
        )
        assert first.status_code == 201, first.text
        assert second.status_code == 201, second.text
        assert first.json()["id"] != second.json()["id"]

    def test_missing_vendor_and_product_is_422(self, client) -> None:
        login(client, "ADMIN")
        org_id = _create_organization(client)

        response = client.post(
            "/api/organization-licenses",
            json={"organization_id": org_id, "contract_number": "Д-1"},
        )
        assert response.status_code == 422, response.text

    def test_idempotency_key_does_not_duplicate(self, client) -> None:
        login(client, "ADMIN")
        org_id = _create_organization(client)
        payload = _payload(org_id)
        key = str(uuid.uuid4())

        first = client.post(
            "/api/organization-licenses", json=payload, headers={"Idempotency-Key": key}
        )
        second = client.post(
            "/api/organization-licenses", json=payload, headers={"Idempotency-Key": key}
        )
        assert first.status_code == 201, first.text
        assert second.status_code == 201, second.text
        assert first.json()["id"] == second.json()["id"]


class TestUpdateLicense:
    def test_update_without_if_match_is_422(self, client) -> None:
        login(client, "ADMIN")
        org_id = _create_organization(client)
        created = client.post("/api/organization-licenses", json=_payload(org_id)).json()

        response = client.patch(
            f"/api/organization-licenses/{created['id']}", json={"comment": "правка"}
        )
        assert response.status_code == 422, response.text

    def test_stale_version_is_409_with_current_values(self, client) -> None:
        login(client, "ADMIN")
        org_id = _create_organization(client)
        created = client.post("/api/organization-licenses", json=_payload(org_id)).json()

        response = client.patch(
            f"/api/organization-licenses/{created['id']}",
            json={"comment": "правка"},
            headers={"If-Match": "99"},
        )
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "CRM-1002"

    def test_update_comment_bumps_version(self, client) -> None:
        login(client, "ADMIN")
        org_id = _create_organization(client)
        created = client.post("/api/organization-licenses", json=_payload(org_id)).json()

        response = client.patch(
            f"/api/organization-licenses/{created['id']}",
            json={"comment": "новый комментарий"},
            headers={"If-Match": str(created["version"])},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["comment"] == "новый комментарий"
        assert body["version"] == created["version"] + 1

    def test_update_to_an_already_used_key_is_409(self, client) -> None:
        login(client, "ADMIN")
        org_id = _create_organization(client)
        contract = f"Д-{uuid.uuid4().hex[:8]}"
        vendor = f"Вендор {uuid.uuid4().hex[:6]}"

        existing = client.post(
            "/api/organization-licenses",
            json=_payload(org_id, vendor=vendor, product_name="ПО А", contract_number=contract),
        ).json()
        other = client.post(
            "/api/organization-licenses",
            json=_payload(org_id, vendor=vendor, product_name="ПО Б", contract_number=contract),
        ).json()

        response = client.patch(
            f"/api/organization-licenses/{other['id']}",
            json={"product_name": "ПО А"},
            headers={"If-Match": str(other["version"])},
        )
        assert response.status_code == 409, response.text
        assert response.json()["license_id"] == existing["id"]

    def test_russian_transfer_status_is_accepted(self, client) -> None:
        login(client, "ADMIN")
        org_id = _create_organization(client)
        created = client.post("/api/organization-licenses", json=_payload(org_id)).json()

        response = client.patch(
            f"/api/organization-licenses/{created['id']}",
            json={"transfer_status": "Передано"},
            headers={"If-Match": str(created["version"])},
        )
        assert response.status_code == 200, response.text
        assert response.json()["transfer_status"] == "transferred"

    def test_unknown_transfer_status_is_422(self, client) -> None:
        login(client, "ADMIN")
        org_id = _create_organization(client)
        created = client.post("/api/organization-licenses", json=_payload(org_id)).json()

        response = client.patch(
            f"/api/organization-licenses/{created['id']}",
            json={"transfer_status": "как-то так"},
            headers={"If-Match": str(created["version"])},
        )
        assert response.status_code == 422, response.text


class TestDeleteLicense:
    def test_kam_cannot_delete(self, client) -> None:
        kam = login(client, "KAM")
        org_id = _create_organization(client, owner_id=kam.id)
        created = client.post("/api/organization-licenses", json=_payload(org_id)).json()

        response = client.delete(f"/api/organization-licenses/{created['id']}")
        assert response.status_code == 403, response.text

    def test_head_can_delete_and_key_is_reusable(self, client) -> None:
        head = create_user(client, "HEAD")
        org_id = _create_organization(client, owner_id=head.id)

        login(client, "ADMIN")
        payload = _payload(org_id)
        created = client.post("/api/organization-licenses", json=payload).json()

        sign_in(client, head)
        response = client.delete(f"/api/organization-licenses/{created['id']}")
        assert response.status_code == 204, response.text

        sign_in(client, create_user(client, "ADMIN"))
        recreated = client.post("/api/organization-licenses", json=payload)
        assert recreated.status_code == 201, recreated.text

        actions = _audit_entries(client, created["id"])
        assert actions[-1][0] == "LICENSE_DELETED"


class TestListLicenses:
    def test_with_total_counts_all_matching_rows_not_just_the_page(self, client) -> None:
        login(client, "ADMIN")
        org_id = _create_organization(client)
        for _ in range(3):
            client.post("/api/organization-licenses", json=_payload(org_id))

        response = client.get(
            "/api/organization-licenses",
            params={"organization_id": org_id, "limit": 1, "with_total": "true"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body["items"]) == 1
        assert body["total"] == 3

    def test_filter_by_transfer_status(self, client) -> None:
        login(client, "ADMIN")
        org_id = _create_organization(client)
        created = client.post(
            "/api/organization-licenses",
            json=_payload(org_id, transfer_status="transferred"),
        ).json()
        client.post("/api/organization-licenses", json=_payload(org_id, transfer_status="declined"))

        response = client.get(
            "/api/organization-licenses",
            params={"organization_id": org_id, "transfer_status": ["transferred"]},
        )
        assert response.status_code == 200, response.text
        ids = {item["id"] for item in response.json()["items"]}
        assert ids == {created["id"]}

    def test_q_searches_contract_number(self, client) -> None:
        login(client, "ADMIN")
        org_id = _create_organization(client)
        contract = f"УНИК-{uuid.uuid4().hex[:10]}"
        created = client.post(
            "/api/organization-licenses", json=_payload(org_id, contract_number=contract)
        ).json()

        response = client.get("/api/organization-licenses", params={"q": contract})
        assert response.status_code == 200, response.text
        ids = {item["id"] for item in response.json()["items"]}
        assert created["id"] in ids
