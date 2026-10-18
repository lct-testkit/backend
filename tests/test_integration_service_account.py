"""B5-10: доступ роли INTEGRATION без согласия на ПДн и API-ключи (`X-API-Key`).

Сквозные тесты на настоящей PostgreSQL (`TEST_DATABASE_URL`).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid
from typing import Any

import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import login, sign_in

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

_KEYS_URL = "/api/admin/integrations/api-keys"
_DEALS_URL = "/api/deals"


async def _integration_user(*, consent_version: str | None = None) -> Any:
    from app.core.db import session_scope
    from app.modules.identity.models import Role, User

    async with session_scope() as session:
        user = User(
            keycloak_id=str(uuid.uuid4()),
            email=f"{uuid.uuid4().hex[:10]}@system.local",
            full_name="Служебная учётка",
            role=Role.INTEGRATION.value,
            status="active",
            consent_version=consent_version,
        )
        session.add(user)
        await session.flush()
        session.expunge(user)
        return user


async def _published_b2c_workflow_and_contact() -> dict[str, Any]:
    from app.core.db import session_scope
    from app.modules.catalog.models import Contact
    from app.modules.workflow.seed import _b2c_spec, seed_workflow

    token = uuid.uuid4().hex[:10]
    async with session_scope() as session:
        spec = dataclasses.replace(_b2c_spec(), code=f"b2c_apikey_{token}")
        workflow = await seed_workflow(session, spec, is_default=False)
        assert workflow is not None
        contact = Contact(
            first_name="Иван",
            last_name="Служебный",
            email=f"{token}@example.ru",
        )
        session.add(contact)
        await session.flush()
        return {"workflow_id": workflow.id, "contact_id": contact.id}


async def _own_deal_count(user_id: uuid.UUID) -> int:
    from sqlalchemy import func, select

    from app.core.db import session_scope
    from app.modules.crm.models import Deal

    async with session_scope() as session:
        return (await session.scalar(select(func.count()).where(Deal.created_by == user_id))) or 0


class TestConsentBypassForIntegration:
    def test_integration_without_consent_can_create_a_deal(self, client) -> None:
        user = run(client, lambda: _integration_user(consent_version=None))
        scene = run(client, _published_b2c_workflow_and_contact)
        sign_in(client, user)

        response = client.post(
            _DEALS_URL,
            json={
                "title": "Заявка от сайта",
                "deal_type": "b2c",
                "workflow_id": str(scene["workflow_id"]),
                "contact_id": str(scene["contact_id"]),
                "source": "cms",
                "external_ids": {"lead_id": "42"},
            },
        )

        assert response.status_code == 201, response.text

    def test_kam_without_consent_is_still_blocked(self, client) -> None:
        async def _kam_without_consent() -> Any:
            from app.core.db import session_scope
            from app.modules.identity.models import User

            async with session_scope() as session:
                user = User(
                    keycloak_id=str(uuid.uuid4()),
                    email=f"{uuid.uuid4().hex[:10]}@rt-it-school.ru",
                    full_name="Без согласия",
                    role="KAM",
                    status="active",
                    consent_version=None,
                )
                session.add(user)
                await session.flush()
                session.expunge(user)
                return user

        kam = run(client, _kam_without_consent)
        sign_in(client, kam)

        response = client.get("/api/deals")

        assert response.status_code == 403
        assert response.json()["code"] == "CRM-1105"


class TestApiKeys:
    def test_key_is_shown_once_and_works_for_deals_scoped_to_itself(self, client) -> None:
        login(client, "ADMIN")
        integration_user = run(client, lambda: _integration_user(consent_version="1.0"))

        created = client.post(
            _KEYS_URL,
            json={"name": "1С", "user_id": str(integration_user.id), "expires_in_days": 90},
        )
        assert created.status_code == 201, created.text
        key = created.json()["key"]
        assert key.startswith("crm_")

        listed = client.get(_KEYS_URL)
        assert key not in listed.text

        scene = run(client, _published_b2c_workflow_and_contact)
        response = client.post(
            _DEALS_URL,
            json={
                "title": "Через API-ключ",
                "deal_type": "b2c",
                "workflow_id": str(scene["workflow_id"]),
                "contact_id": str(scene["contact_id"]),
                "source": "cms",
                "external_ids": {"lead_id": "43"},
            },
            headers={"X-API-Key": key},
        )
        assert response.status_code == 201, response.text

        own_count = run(client, _own_deal_count, integration_user.id)
        read = client.get(_DEALS_URL, headers={"X-API-Key": key})
        assert read.status_code == 200
        assert len(read.json()["items"]) == own_count

    def test_unknown_key_is_rejected(self, client) -> None:
        response = client.get(_DEALS_URL, headers={"X-API-Key": "crm_not-a-real-key"})
        assert response.status_code == 401

    def test_key_for_non_integration_user_is_rejected(self, client) -> None:
        login(client, "ADMIN")
        from tests.conftest import _make_user

        kam = run(client, lambda: _make_user("KAM", "active"))

        response = client.post(
            _KEYS_URL, json={"name": "x", "user_id": str(kam.id), "expires_in_days": 10}
        )

        assert response.status_code == 422

    def test_revoked_key_stops_working(self, client) -> None:
        login(client, "ADMIN")
        integration_user = run(client, lambda: _integration_user(consent_version="1.0"))
        created = client.post(
            _KEYS_URL,
            json={"name": "x", "user_id": str(integration_user.id), "expires_in_days": 10},
        )
        key = created.json()["key"]
        key_id = created.json()["id"]

        revoked = client.delete(f"{_KEYS_URL}/{key_id}")
        assert revoked.status_code == 200

        response = client.get(_DEALS_URL, headers={"X-API-Key": key})
        assert response.status_code == 401

    def test_expired_key_is_rejected(self, client) -> None:
        login(client, "ADMIN")
        integration_user = run(client, lambda: _integration_user(consent_version="1.0"))
        created = client.post(
            _KEYS_URL,
            json={"name": "x", "user_id": str(integration_user.id), "expires_in_days": 1},
        )
        key = created.json()["key"]
        key_id = created.json()["id"]

        async def _expire() -> None:
            from app.core.db import session_scope
            from app.modules.integration.models import ApiKey

            async with session_scope() as session:
                api_key = await session.get(ApiKey, uuid.UUID(key_id))
                assert api_key is not None
                api_key.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)

        run(client, _expire)

        response = client.get(_DEALS_URL, headers={"X-API-Key": key})
        assert response.status_code == 401

    def test_blocking_the_account_invalidates_the_key(self, client) -> None:
        login(client, "ADMIN")
        integration_user = run(client, lambda: _integration_user(consent_version="1.0"))
        created = client.post(
            _KEYS_URL,
            json={"name": "x", "user_id": str(integration_user.id), "expires_in_days": 10},
        )
        key = created.json()["key"]

        async def _block() -> None:
            from app.core.db import session_scope
            from app.modules.identity.models import User

            async with session_scope() as session:
                user = await session.get(User, integration_user.id)
                assert user is not None
                user.status = "blocked"

        run(client, _block)

        response = client.get(_DEALS_URL, headers={"X-API-Key": key})
        assert response.status_code == 401

    def test_soft_deleted_account_invalidates_the_key(self, client) -> None:
        """Bearer-путь (`provision_from_claims`) не видит мягко удалённых пользователей; ключ
        удалённой учётки со статусом `active` не должен оставаться рабочим."""
        login(client, "ADMIN")
        integration_user = run(client, lambda: _integration_user(consent_version="1.0"))
        created = client.post(
            _KEYS_URL,
            json={"name": "x", "user_id": str(integration_user.id), "expires_in_days": 10},
        )
        key = created.json()["key"]

        async def _soft_delete() -> None:
            from app.core.db import session_scope
            from app.modules.identity.models import User

            async with session_scope() as session:
                user = await session.get(User, integration_user.id)
                assert user is not None
                user.deleted_at = dt.datetime.now(dt.UTC)

        run(client, _soft_delete)
        client.cookies.clear()

        response = client.get(_DEALS_URL, headers={"X-API-Key": key})
        assert response.status_code == 401

    def test_disabling_the_feature_flag_ignores_the_header(
        self, client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.core.config import get_settings

        login(client, "ADMIN")
        integration_user = run(client, lambda: _integration_user(consent_version="1.0"))
        created = client.post(
            _KEYS_URL,
            json={"name": "x", "user_id": str(integration_user.id), "expires_in_days": 10},
        )
        key = created.json()["key"]
        # Сессия ADMIN (от `login` выше) иначе осталась бы в cookie-jar клиента и
        # аутентифицировала бы запрос ею — проверяем именно отключённый X-API-Key «в вакууме».
        client.cookies.clear()

        monkeypatch.setattr(get_settings(), "api_keys_enabled", False)

        # Без сессии и без Bearer заголовок X-API-Key один больше не аутентифицирует —
        # запрос уходит как анонимный и отклоняется как обычно (401, нет сессии).
        response = client.get(_DEALS_URL, headers={"X-API-Key": key})
        assert response.status_code == 401
