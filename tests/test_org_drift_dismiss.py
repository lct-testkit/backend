"""B4-11 (A-22): «Оставить как есть» для расхождений реквизитов (`dismiss-drift`).

Сквозные тесты на настоящей PostgreSQL (`TEST_DATABASE_URL`).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import login

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


async def _org_with_drift(
    *, drift: dict[str, Any], registry_status: str | None = None
) -> dict[str, Any]:
    from app.core.db import session_scope
    from app.modules.catalog.models import Organization

    async with session_scope() as session:
        org = Organization(
            name=f"Вуз {uuid.uuid4().hex[:6]}",
            org_type="university",
            inn=str(uuid.uuid4().int % 10**10).zfill(10),
            legal_address="старый адрес",
            requisites_drift=drift,
            registry_status=registry_status,
        )
        session.add(org)
        await session.flush()
        return {"id": org.id, "version": org.version}


async def _org_state(org_id: uuid.UUID) -> dict[str, Any]:
    from app.core.db import session_scope
    from app.modules.catalog.models import Organization

    async with session_scope() as session:
        org = await session.get(Organization, org_id)
        assert org is not None
        return {
            "requisites_drift": org.requisites_drift,
            "drift_dismissed": org.drift_dismissed,
            "version": org.version,
        }


def _url(org_id: uuid.UUID, action: str) -> str:
    return f"/api/organizations/{org_id}/{action}"


class TestDismissDrift:
    def test_dismissing_a_field_clears_it_from_drift_and_remembers_the_value(self, client) -> None:
        login(client, "ADMIN")
        drift = {"legal_address": {"old": "старый адрес", "new": "новый адрес"}}
        scene = run(client, lambda: _org_with_drift(drift=drift))

        response = client.post(
            _url(scene["id"], "dismiss-drift"),
            json={"fields": ["legal_address"]},
            headers={"If-Match": str(scene["version"])},
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["requisites_drift"] is None
        assert body["drift_dismissed"] == {"legal_address": "новый адрес"}

    def test_empty_fields_dismisses_everything(self, client) -> None:
        login(client, "ADMIN")
        drift = {
            "legal_address": {"old": "старый адрес", "new": "новый адрес"},
            "short_name": {"old": None, "new": "Короткое имя"},
        }
        scene = run(client, lambda: _org_with_drift(drift=drift))

        response = client.post(
            _url(scene["id"], "dismiss-drift"),
            json={"fields": []},
            headers={"If-Match": str(scene["version"])},
        )

        assert response.status_code == 200, response.text
        assert response.json()["requisites_drift"] is None
        assert set(response.json()["drift_dismissed"]) == {"legal_address", "short_name"}

    def test_liquidation_status_cannot_be_dismissed(self, client) -> None:
        login(client, "ADMIN")
        drift = {"registry_status": {"old": "active", "new": "liquidating"}}
        scene = run(client, lambda: _org_with_drift(drift=drift, registry_status="active"))

        response = client.post(
            _url(scene["id"], "dismiss-drift"),
            json={"fields": ["registry_status"]},
            headers={"If-Match": str(scene["version"])},
        )

        assert response.status_code == 422

    def test_no_drift_is_422(self, client) -> None:
        login(client, "ADMIN")
        scene = run(client, lambda: _org_with_drift(drift={}))

        response = client.post(
            _url(scene["id"], "dismiss-drift"),
            json={"fields": []},
            headers={"If-Match": str(scene["version"])},
        )

        assert response.status_code == 422

    def test_stale_if_match_is_409(self, client) -> None:
        login(client, "ADMIN")
        drift = {"legal_address": {"old": "старый адрес", "new": "новый адрес"}}
        scene = run(client, lambda: _org_with_drift(drift=drift))

        response = client.post(
            _url(scene["id"], "dismiss-drift"),
            json={"fields": ["legal_address"]},
            headers={"If-Match": str(scene["version"] + 1)},
        )

        assert response.status_code == 409

    def test_second_sweep_with_the_same_value_does_not_bring_the_banner_back(self, client) -> None:
        """B4-11: повторная сверка с ТЕМ ЖЕ значением в реестре не должна вернуть баннер."""
        login(client, "ADMIN")
        drift = {"legal_address": {"old": "старый адрес", "new": "новый адрес"}}
        scene = run(client, lambda: _org_with_drift(drift=drift))
        dismissed = client.post(
            _url(scene["id"], "dismiss-drift"),
            json={"fields": ["legal_address"]},
            headers={"If-Match": str(scene["version"])},
        )
        assert dismissed.status_code == 200

        async def _recompute() -> dict[str, Any]:
            from app.core.db import session_scope
            from app.modules.catalog.models import Organization
            from app.modules.registry.models import EgrulEntry
            from app.modules.registry.tasks import _compute_drift

            async with session_scope() as session:
                org = await session.get(Organization, scene["id"])
                assert org is not None
                entry = await session.get(EgrulEntry, org.inn)
                if entry is None:
                    entry = EgrulEntry(
                        inn=org.inn,
                        full_name=org.name,
                        short_name=org.name,
                        status="active",
                        legal_address="новый адрес",
                        is_educational=True,
                    )
                    session.add(entry)
                    await session.flush()
                else:
                    entry.legal_address = "новый адрес"
                return _compute_drift(org, entry)

        recomputed = run(client, _recompute)
        assert "legal_address" not in recomputed

    def test_a_different_new_value_brings_the_banner_back(self, client) -> None:
        login(client, "ADMIN")
        drift = {"legal_address": {"old": "старый адрес", "new": "новый адрес"}}
        scene = run(client, lambda: _org_with_drift(drift=drift))
        client.post(
            _url(scene["id"], "dismiss-drift"),
            json={"fields": ["legal_address"]},
            headers={"If-Match": str(scene["version"])},
        )

        async def _recompute_with_third_value() -> dict[str, Any]:
            from app.core.db import session_scope
            from app.modules.catalog.models import Organization
            from app.modules.registry.models import EgrulEntry
            from app.modules.registry.tasks import _compute_drift

            async with session_scope() as session:
                org = await session.get(Organization, scene["id"])
                assert org is not None
                entry = EgrulEntry(
                    inn=f"temp-{uuid.uuid4().hex[:8]}",
                    full_name=org.name,
                    short_name=org.name,
                    status="active",
                    legal_address="третий адрес",
                    is_educational=True,
                )
                return _compute_drift(org, entry)

        recomputed = run(client, _recompute_with_third_value)
        assert recomputed.get("legal_address") == {"old": "старый адрес", "new": "третий адрес"}

    def test_apply_drift_removes_the_field_from_dismissed(self, client) -> None:
        admin = login(client, "ADMIN")
        drift = {
            "legal_address": {"old": "старый адрес", "new": "новый адрес"},
            "short_name": {"old": None, "new": "Короткое имя"},
        }
        scene = run(client, lambda: _org_with_drift(drift=drift))
        dismissed = client.post(
            _url(scene["id"], "dismiss-drift"),
            json={"fields": []},
            headers={"If-Match": str(scene["version"])},
        )
        assert dismissed.status_code == 200

        # Снова найден дрейф того же поля (другое значение) — apply_drift должен убрать его из
        # drift_dismissed, не трогая отклонённый "short_name".
        async def _reapply_drift_and_apply() -> dict[str, Any]:
            from app.core.db import session_scope
            from app.core.security import Principal, TokenClaims
            from app.modules.catalog.models import Organization
            from app.modules.catalog.service import OrganizationService

            async with session_scope() as session:
                org = await session.get(Organization, scene["id"])
                assert org is not None
                org.requisites_drift = {
                    "legal_address": {"old": "новый адрес", "new": "ещё один адрес"}
                }
                await session.flush()
                principal = Principal(
                    user_id=admin.id,
                    keycloak_id=admin.keycloak_id or "x",
                    role=admin.role,
                    status=admin.status,
                    email=admin.email,
                    full_name=admin.full_name,
                    team_id=admin.team_id,
                    manager_id=admin.manager_id,
                    perm_epoch=admin.perm_epoch,
                    session_id=None,
                    consent_version=admin.consent_version,
                    must_change_password=False,
                    claims=TokenClaims(subject="x", raw={}),
                    restricted=False,
                )
                updated = await OrganizationService(session).apply_drift(
                    org, principal, fields=["legal_address"], expected_version=org.version
                )
                return {"drift_dismissed": updated.drift_dismissed}

        result = run(client, _reapply_drift_and_apply)
        assert "legal_address" not in result["drift_dismissed"]
        assert "short_name" in result["drift_dismissed"]
