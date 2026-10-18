"""B4-13 (I16): `User.is_system` отличает служебную учётку без входа от обычного сотрудника.

Прежнее определение (`keycloak_id IS NULL`) считало служебными ещё и сотрудников, заведённых
локально до первого входа (их `keycloak_id` появляется при привязке по email), и все обезличенные
записи: фронтенд скрывал бы таких из выбора ответственного за сделку и вуз.
"""

from __future__ import annotations

import uuid

import pytest

from app.modules.identity.models import User
from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import login


def _user(*, role: str = "KAM", email: str | None, keycloak_id: str | None) -> User:
    return User(role=role, email=email, keycloak_id=keycloak_id, full_name="Тест", status="active")


class TestIsSystem:
    def test_integration_account_is_system(self) -> None:
        assert _user(
            role="INTEGRATION", email="integration@system.local", keycloak_id=None
        ).is_system

    def test_integration_role_is_system_even_with_a_keycloak_service_account(self) -> None:
        assert _user(role="INTEGRATION", email=None, keycloak_id="sa-1").is_system

    def test_phantom_admin_on_the_system_domain_is_system(self) -> None:
        assert _user(role="ADMIN", email="Demo-Reports@System.Local", keycloak_id=None).is_system

    def test_employee_created_locally_before_the_first_login_is_not_system(self) -> None:
        assert not _user(email="ivanov@rt-it-school.ru", keycloak_id=None).is_system

    def test_anonymized_user_is_not_system(self) -> None:
        assert not _user(email=None, keycloak_id=None).is_system

    def test_regular_employee_is_not_system(self) -> None:
        assert not _user(email="petrov@rt-it-school.ru", keycloak_id="kc-1").is_system

    def test_a_system_domain_email_with_a_keycloak_account_is_not_system(self) -> None:
        # Привязать Keycloak к записи на @system.local нельзя (`_email_binding_allowed`), но
        # если `keycloak_id` всё же есть, это человек со входом, а не служебная учётка.
        assert not _user(email="someone@system.local", keycloak_id="kc-2").is_system


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с миграциями")
class TestUserOutIsSystem:
    def test_admin_api_reports_is_system_per_account(self, client) -> None:
        async def _create() -> dict[str, uuid.UUID]:
            from app.core.db import session_scope

            token = uuid.uuid4().hex[:8]
            async with session_scope() as session:
                local_employee = _user(email=f"local-{token}@rt-it-school.ru", keycloak_id=None)
                service = _user(
                    role="INTEGRATION", email=f"svc-{token}@system.local", keycloak_id=None
                )
                session.add_all([local_employee, service])
                await session.flush()
                return {"employee": local_employee.id, "service": service.id}

        ids = run(client, _create)
        login(client, "ADMIN")

        employee = client.get(f"/api/admin/users/{ids['employee']}")
        service = client.get(f"/api/admin/users/{ids['service']}")

        assert employee.status_code == 200, employee.text
        assert employee.json()["is_system"] is False
        assert service.status_code == 200, service.text
        assert service.json()["is_system"] is True
