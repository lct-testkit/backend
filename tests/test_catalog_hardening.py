"""Каталог: лицензии в скоупе, поиск контактов без подбора email, обезличенный контакт, псевдонимы.

Найдено внешним тестированием: любой KAM/HEAD читал лицензии (с именами менеджеров) всех вузов;
поиск по подстроке email позволял подбирать адреса, скрытые маскированием в выдаче; обезличенный
контакт снова заполнялся ПДн обычным PATCH; псевдоним брался из старших битов UUIDv7 и совпадал у
объектов, созданных в одну минуту.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import create_user, login, sign_in
from tests.people_helpers import create_contact, unique_email

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _org_with_license(client, owner_id) -> tuple[str, str]:
    from app.core.db import session_scope
    from app.modules.catalog.models import Organization, OrganizationLicense

    async def _create() -> tuple[str, str]:
        async with session_scope() as session:
            org = Organization(
                name=f"Вуз лицензий {uuid.uuid4().hex[:6]}",
                org_type="university",
                owner_id=owner_id,
            )
            session.add(org)
            await session.flush()
            license_ = OrganizationLicense(
                organization_id=org.id,
                vendor="Вендор",
                product_name="ПО",
                contract_number=f"К-{uuid.uuid4().hex[:8]}",
                manager_full_name="Менеджеров Менеджер",
            )
            session.add(license_)
            await session.flush()
            return str(org.id), str(license_.id)

    return run(client, _create)


class TestLicensesAreScoped:
    def test_a_kam_sees_only_licenses_of_his_own_organizations(self, client) -> None:
        mine_owner = create_user(client, "KAM")
        other_owner = create_user(client, "KAM")
        _my_org, my_license = _org_with_license(client, mine_owner.id)
        _their_org, their_license = _org_with_license(client, other_owner.id)
        sign_in(client, mine_owner)

        listed = client.get("/api/organization-licenses", params={"limit": 100})
        assert listed.status_code == 200, listed.text
        ids = {item["id"] for item in listed.json()["items"]}
        assert my_license in ids and their_license not in ids

        assert client.get(f"/api/organization-licenses/{my_license}").status_code == 200
        assert client.get(f"/api/organization-licenses/{their_license}").status_code == 404

    def test_filtering_by_a_foreign_organization_returns_nothing(self, client) -> None:
        mine_owner = create_user(client, "KAM")
        other_owner = create_user(client, "KAM")
        their_org, _their_license = _org_with_license(client, other_owner.id)
        sign_in(client, mine_owner)

        listed = client.get("/api/organization-licenses", params={"organization_id": their_org})

        assert listed.status_code == 200
        assert listed.json()["items"] == []

    def test_admin_sees_everything_and_a_denied_read_is_audited(self, client) -> None:
        from sqlalchemy import select

        from app.core.db import session_scope
        from app.modules.audit.models import AuditLog

        kam = create_user(client, "KAM")
        outsider = create_user(client, "KAM")
        _org, license_id = _org_with_license(client, kam.id)
        sign_in(client, outsider)
        assert client.get(f"/api/organization-licenses/{license_id}").status_code == 404

        async def _denials() -> int:
            async with session_scope() as session:
                rows = await session.scalars(
                    select(AuditLog).where(
                        AuditLog.entity_id == uuid.UUID(license_id),
                        AuditLog.action == "ACCESS_DENIED",
                        AuditLog.actor_id == outsider.id,
                    )
                )
                return len(rows.all())

        assert run(client, _denials) == 1
        login(client, "ADMIN")
        assert client.get(f"/api/organization-licenses/{license_id}").status_code == 200


class TestContactSearchCannotGuessEmails:
    def test_a_substring_of_the_email_finds_nothing_but_the_full_address_does(self, client) -> None:
        login(client, "KAM")
        email = unique_email("zzsecret")
        contact = create_contact(client, email=email)

        by_fragment = client.get("/api/contacts", params={"q": "zzsecret", "limit": 100})
        assert by_fragment.status_code == 200, by_fragment.text
        assert contact["id"] not in {c["id"] for c in by_fragment.json()["items"]}

        by_prefix = client.get("/api/contacts", params={"q": email[:6], "limit": 100})
        assert contact["id"] not in {c["id"] for c in by_prefix.json()["items"]}

        exact = client.get("/api/contacts", params={"q": email.upper(), "limit": 100})
        assert contact["id"] in {c["id"] for c in exact.json()["items"]}

    def test_search_by_name_still_works(self, client) -> None:
        login(client, "KAM")
        contact = create_contact(client)

        found = client.get("/api/contacts", params={"q": contact["last_name"][:10], "limit": 100})

        assert contact["id"] in {c["id"] for c in found.json()["items"]}


class TestAnonymizedContact:
    def _anonymize(self, client, contact_id: str) -> None:
        from app.core.db import session_scope
        from app.modules.catalog.models import Contact
        from app.modules.catalog.service import ContactService

        async def _do() -> None:
            async with session_scope() as session:
                contact = await session.get(Contact, uuid.UUID(contact_id))
                await ContactService(session).anonymize(contact)

        run(client, _do)

    def test_patch_cannot_refill_an_anonymized_contact(self, client) -> None:
        login(client, "KAM")
        contact = create_contact(client)
        self._anonymize(client, contact["id"])
        card = client.get(f"/api/contacts/{contact['id']}").json()

        response = client.patch(
            f"/api/contacts/{contact['id']}",
            json={"first_name": "Вернули", "last_name": "Данные"},
            headers={"If-Match": str(card["version"])},
        )

        assert response.status_code == 409, response.text
        assert client.get(f"/api/contacts/{contact['id']}").json()["is_anonymized"] is True

    def test_pseudonyms_of_contacts_created_together_differ(self, client) -> None:
        login(client, "KAM")
        first = create_contact(client)
        second = create_contact(client)
        self._anonymize(client, first["id"])
        self._anonymize(client, second["id"])

        names = {
            client.get(f"/api/contacts/{c['id']}").json()["first_name"] for c in (first, second)
        }

        assert len(names) == 2, names
