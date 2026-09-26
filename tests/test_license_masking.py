"""Лицензии: ФИО менеджера и «ответственные от вуза» без права `contact:reveal` — маской.

Это ПДн из исходного xls (свободный текст, а не ссылка на контакт), поэтому без права раскрытия
они отдаются так же, как контакты: инициалы вместо ФИО, email и телефоны по своим форматам.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.core.masking import mask_contacts_text
from app.modules.catalog.schemas import OrganizationLicenseOut
from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import create_user, login, sign_in

_MANAGER = "Менеджеров Михаил Петрович"
_CONTACTS = "Иванов Иван Иванович, +7 (999) 123-45-12, ivanov@vuz.ru; декан Петров Пётр"


class TestMaskContactsText:
    def test_email_phone_and_names_are_masked(self) -> None:
        masked = mask_contacts_text(_CONTACTS)

        assert "ivanov@vuz.ru" not in masked and "i***@vuz.ru" in masked
        assert "123-45" not in masked and "+7 (9**) ***-**-12" in masked
        assert "Иван Иванович" not in masked and "Иванов И. И." in masked
        assert "Пётр" not in masked
        # Пояснения без персональных данных остаются читаемыми.
        assert "декан" in masked

    @pytest.mark.parametrize("value", [None, ""])
    def test_empty_stays_empty(self, value) -> None:
        assert mask_contacts_text(value) == value

    def test_text_without_personal_data_is_untouched(self) -> None:
        assert mask_contacts_text("через деканат, приём по вторникам") == (
            "через деканат, приём по вторникам"
        )


def _license(**overrides) -> SimpleNamespace:
    import datetime as dt

    values = {
        "id": uuid.uuid4(),
        "organization_id": uuid.uuid4(),
        "vendor": "Вендор",
        "product_name": "ПО",
        "contract_number": "К-1",
        "license_signed_at": None,
        "license_valid_year": None,
        "transfer_status": None,
        "manager_full_name": _MANAGER,
        "responsible_contacts": _CONTACTS,
        "comment": None,
        "version": 1,
        "created_at": dt.datetime.now(dt.UTC),
        "updated_at": dt.datetime.now(dt.UTC),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class TestSerializer:
    def test_without_the_right_the_fields_are_masked(self) -> None:
        out = OrganizationLicenseOut.from_model(_license(), reveal=False)

        assert out.manager_full_name == "Менеджеров М. П."
        assert "ivanov@vuz.ru" not in out.responsible_contacts
        assert "Иван Иванович" not in out.responsible_contacts

    def test_with_the_right_they_are_as_stored(self) -> None:
        out = OrganizationLicenseOut.from_model(_license(), reveal=True)

        assert out.manager_full_name == _MANAGER
        assert out.responsible_contacts == _CONTACTS

    def test_empty_values_stay_none(self) -> None:
        out = OrganizationLicenseOut.from_model(
            _license(manager_full_name=None, responsible_contacts=None), reveal=False
        )

        assert out.manager_full_name is None and out.responsible_contacts is None


pytestmark_db = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _org_with_license(client, owner_id) -> tuple[str, str]:
    from app.core.db import session_scope
    from app.modules.catalog.models import Organization, OrganizationLicense

    async def _create() -> tuple[str, str]:
        async with session_scope() as session:
            org = Organization(
                name=f"Вуз маски {uuid.uuid4().hex[:6]}", org_type="university", owner_id=owner_id
            )
            session.add(org)
            await session.flush()
            license_ = OrganizationLicense(
                organization_id=org.id,
                vendor="Вендор",
                product_name="ПО",
                contract_number=f"К-{uuid.uuid4().hex[:8]}",
                manager_full_name=_MANAGER,
                responsible_contacts=_CONTACTS,
            )
            session.add(license_)
            await session.flush()
            return str(org.id), str(license_.id)

    return run(client, _create)


@pytest.fixture
def without_reveal(client, monkeypatch):
    """Роль, у которой есть чтение каталога, но нет `contact:reveal` (в текущей матрице такой
    нет: подменяется только решение о раскрытии, проверка входа в ручку остаётся настоящей)."""
    from app.core.permissions import Permission, has_permission
    from app.modules.catalog import router

    def decide(role: str, permission: Permission) -> bool:
        if permission is Permission.CONTACT_REVEAL:
            return False
        return has_permission(role, permission)

    monkeypatch.setattr(router, "has_permission", decide)


@pytestmark_db
class TestLicenseEndpoints:
    def test_roles_with_the_right_see_the_stored_values(self, client) -> None:
        owner = create_user(client, "KAM")
        _org, license_id = _org_with_license(client, owner.id)
        sign_in(client, owner)

        card = client.get(f"/api/organization-licenses/{license_id}").json()
        listed = client.get("/api/organization-licenses", params={"limit": 100}).json()

        assert card["manager_full_name"] == _MANAGER
        assert card["responsible_contacts"] == _CONTACTS
        (item,) = [i for i in listed["items"] if i["id"] == license_id]
        assert item["manager_full_name"] == _MANAGER

    def test_without_the_right_both_endpoints_mask(self, client, without_reveal) -> None:
        owner = create_user(client, "KAM")
        _org, license_id = _org_with_license(client, owner.id)
        sign_in(client, owner)

        card = client.get(f"/api/organization-licenses/{license_id}").json()
        listed = client.get("/api/organization-licenses", params={"limit": 100}).json()

        assert card["manager_full_name"] == "Менеджеров М. П."
        assert "ivanov@vuz.ru" not in card["responsible_contacts"]
        (item,) = [i for i in listed["items"] if i["id"] == license_id]
        assert item["manager_full_name"] == "Менеджеров М. П."
        assert "Иван Иванович" not in item["responsible_contacts"]
        # Остальные поля карточки как были.
        assert card["contract_number"].startswith("К-")

    def test_admin_keeps_full_access(self, client) -> None:
        owner = create_user(client, "KAM")
        _org, license_id = _org_with_license(client, owner.id)
        login(client, "ADMIN")

        card = client.get(f"/api/organization-licenses/{license_id}").json()

        assert card["manager_full_name"] == _MANAGER
