"""B4-5: ограничения видимости Администратора — модель данных и API
(`backend-sprints.md` строки 2808-2902). Проверки SQL-ограничений и утечек — в
`tests/test_access_rules_leaks.py` (B4-6)."""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _login(client, user) -> None:
    client.headers["X-CSRF-Token"] = authenticate(client, user)


async def _make_org(name: str) -> str:
    from app.core.db import session_scope
    from app.modules.catalog.models import Organization

    async with session_scope() as session:
        org = Organization(name=name, org_type="university")
        session.add(org)
        await session.flush()
        return str(org.id)


async def _soft_delete_org(org_id: str) -> None:
    import datetime as dt

    from app.core.db import session_scope
    from app.modules.catalog.models import Organization

    async with session_scope() as session:
        org = await session.get(Organization, uuid.UUID(org_id))
        org.deleted_at = dt.datetime.now(dt.UTC)


async def _count_audit(action: str, entity_id: uuid.UUID) -> int:
    from sqlalchemy import func, select

    from app.core.db import session_scope
    from app.modules.audit.models import AuditLog

    async with session_scope() as session:
        return int(
            (
                await session.execute(
                    select(func.count(AuditLog.id)).where(
                        AuditLog.action == action, AuditLog.entity_id == entity_id
                    )
                )
            ).scalar_one()
        )


def _put(client, user_id, *, organizations=None, directions=None, reason="Тест", version=1):
    body = {
        "organizations": organizations or {"mode": "all", "ids": []},
        "directions": directions or {"mode": "all", "ids": []},
        "reason": reason,
    }
    return client.put(
        f"/api/admin/users/{user_id}/access-rules",
        json=body,
        headers={"If-Match": str(version)},
    )


class TestPutAccessRules:
    def test_admin_restricts_a_kam_to_two_organizations(self, client) -> None:
        admin = run(client, _make_user, "ADMIN")
        kam = run(client, _make_user, "KAM")
        org_a = run(client, _make_org, f"Вуз А {uuid.uuid4().hex[:6]}")
        org_b = run(client, _make_org, f"Вуз Б {uuid.uuid4().hex[:6]}")

        # Прогреваем кэш прав КАМа без ограничений — дальше проверяем, что PUT
        # сбросил именно его, без ожидания TTL (5 минут).
        _login(client, kam)
        before_me = client.get("/api/me")
        assert before_me.json()["access_restricted"] is False

        _login(client, admin)
        response = _put(client, kam.id, organizations={"mode": "allow", "ids": [org_a, org_b]})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["access_restricted"] is True
        assert body["organizations"]["mode"] == "allow"
        assert {i["id"] for i in body["organizations"]["items"]} == {org_a, org_b}

        again = client.get(f"/api/admin/users/{kam.id}/access-rules")
        assert again.status_code == 200, again.text
        assert again.json()["organizations"]["mode"] == "allow"

        assert run(client, _count_audit, "ACCESS_RULES_CHANGED", kam.id) == 1

        _login(client, kam)
        after_me = client.get("/api/me")
        assert after_me.json()["access_restricted"] is True

    def test_head_and_kam_cannot_manage_access_rules(self, client) -> None:
        admin = run(client, _make_user, "ADMIN")
        _login(client, admin)
        target = run(client, _make_user, "KAM")
        head = run(client, _make_user, "HEAD")
        other_kam = run(client, _make_user, "KAM")

        for actor in (head, other_kam):
            _login(client, actor)
            response = _put(client, target.id)
            assert response.status_code == 403, response.text
            _login(client, admin)

    @pytest.mark.parametrize("role", ["ADMIN", "AUDITOR", "INTEGRATION"])
    def test_cannot_restrict_admin_auditor_or_integration(self, client, role: str) -> None:
        admin = run(client, _make_user, "ADMIN")
        _login(client, admin)
        target = run(client, _make_user, role)
        org = run(client, _make_org, f"Вуз {uuid.uuid4().hex[:6]}")

        response = _put(client, target.id, organizations={"mode": "allow", "ids": [org]})
        assert response.status_code == 422, response.text

    def test_allow_without_elements_is_rejected(self, client) -> None:
        admin = run(client, _make_user, "ADMIN")
        _login(client, admin)
        kam = run(client, _make_user, "KAM")

        response = _put(client, kam.id, organizations={"mode": "allow", "ids": []})
        assert response.status_code == 422, response.text

    def test_deleted_organization_is_rejected(self, client) -> None:
        admin = run(client, _make_user, "ADMIN")
        _login(client, admin)
        kam = run(client, _make_user, "KAM")
        org = run(client, _make_org, f"Вуз {uuid.uuid4().hex[:6]}")
        run(client, _soft_delete_org, org)

        response = _put(client, kam.id, organizations={"mode": "allow", "ids": [org]})
        assert response.status_code == 422, response.text

    def test_stale_if_match_is_conflict(self, client) -> None:
        admin = run(client, _make_user, "ADMIN")
        _login(client, admin)
        kam = run(client, _make_user, "KAM")
        org = run(client, _make_org, f"Вуз {uuid.uuid4().hex[:6]}")

        response = client.put(
            f"/api/admin/users/{kam.id}/access-rules",
            json={
                "organizations": {"mode": "allow", "ids": [org]},
                "directions": {"mode": "all", "ids": []},
                "reason": "Тест",
            },
            headers={"If-Match": "999"},
        )
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "CRM-1002"

    def test_mode_all_clears_the_restriction(self, client) -> None:
        admin = run(client, _make_user, "ADMIN")
        _login(client, admin)
        kam = run(client, _make_user, "KAM")
        org = run(client, _make_org, f"Вуз {uuid.uuid4().hex[:6]}")

        first = _put(client, kam.id, organizations={"mode": "allow", "ids": [org]})
        assert first.status_code == 200, first.text
        version = first.json()["version"]

        second = _put(client, kam.id, version=version)
        assert second.status_code == 200, second.text
        assert second.json()["access_restricted"] is False

    def test_repeating_the_same_put_does_not_duplicate_audit(self, client) -> None:
        admin = run(client, _make_user, "ADMIN")
        _login(client, admin)
        kam = run(client, _make_user, "KAM")
        org = run(client, _make_org, f"Вуз {uuid.uuid4().hex[:6]}")

        first = _put(client, kam.id, organizations={"mode": "allow", "ids": [org]})
        assert first.status_code == 200, first.text
        version = first.json()["version"]

        second = _put(
            client,
            kam.id,
            organizations={"mode": "allow", "ids": [org]},
            version=version,
        )
        assert second.status_code == 200, second.text
        # Версия не растёт: повторный PUT с тем же содержимым не пишет аудит.
        assert second.json()["version"] == version
        assert run(client, _count_audit, "ACCESS_RULES_CHANGED", kam.id) == 1

    def test_list_users_filters_by_access_restricted(self, client) -> None:
        admin = run(client, _make_user, "ADMIN")
        _login(client, admin)
        restricted_kam = run(client, _make_user, "KAM")
        plain_kam = run(client, _make_user, "KAM")
        org = run(client, _make_org, f"Вуз {uuid.uuid4().hex[:6]}")
        _put(client, restricted_kam.id, organizations={"mode": "allow", "ids": [org]})

        response = client.get("/api/admin/users", params={"access_restricted": "true"})
        assert response.status_code == 200, response.text
        ids = {item["id"] for item in response.json()["items"]}
        assert str(restricted_kam.id) in ids
        assert str(plain_kam.id) not in ids


class TestAccessRulesPreview:
    def test_preview_counts_without_writing(self, client) -> None:
        admin = run(client, _make_user, "ADMIN")
        _login(client, admin)
        kam = run(client, _make_user, "KAM")
        org_a = run(client, _make_org, f"Вуз А {uuid.uuid4().hex[:6]}")
        run(client, _make_org, f"Вуз Б {uuid.uuid4().hex[:6]}")

        response = client.get(
            f"/api/admin/users/{kam.id}/access-rules/preview",
            params={"organizations.mode": "allow", "organizations.ids": org_a},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["organizations"]["visible_before"] >= 0
        assert body["organizations"]["visible_after"] <= body["organizations"]["visible_before"]

        # Ничего не записано: правила по-прежнему `all`.
        after = client.get(f"/api/admin/users/{kam.id}/access-rules")
        assert after.json()["organizations"]["mode"] == "all"
