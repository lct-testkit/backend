"""B4-12 (A-18): запрос временного доступа к чужому вузу.

Это функция утечки ПДн: `organization_scope_clause_for_rules` добавляет отдельную ветку
по одобренным запросам, и критично, что ограничение B4-5/B4-6 (`deny`) всё равно
накладывается поверх через AND — одобренный доступ не должен его пробивать
(`TestLeakPrevention` ниже). Остальные классы — обычные сквозные проверки CRUD,
прав на решение и просрочки, на настоящей PostgreSQL (`TEST_DATABASE_URL`).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest

from tests.conftest import TEST_DATABASE_URL, _make_user, run
from tests.crm_helpers import (
    create_deal,
    create_organization,
    create_published_workflow,
    create_team,
    create_user,
    login,
    sign_in,
)

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


async def _set_team_head(team_id: uuid.UUID, head_id: uuid.UUID) -> None:
    from app.core.db import session_scope
    from app.modules.identity.models import Team

    async with session_scope() as session:
        team = await session.get(Team, team_id)
        assert team is not None
        team.head_id = head_id
        await session.flush()


async def _set_owner(org_id: str, owner_id: uuid.UUID) -> None:
    from app.core.db import session_scope
    from app.modules.catalog.models import Organization

    async with session_scope() as session:
        org = await session.get(Organization, uuid.UUID(org_id))
        assert org is not None
        org.owner_id = owner_id
        await session.flush()


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


async def _force_expire(request_id: str) -> None:
    from app.core.db import session_scope
    from app.modules.catalog.models import AccessRequest

    async with session_scope() as session:
        req = await session.get(AccessRequest, uuid.UUID(request_id))
        assert req is not None
        req.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1)
        await session.flush()


async def _force_old_pending(request_id: str) -> None:
    from app.core.db import session_scope
    from app.modules.catalog.models import AccessRequest

    async with session_scope() as session:
        req = await session.get(AccessRequest, uuid.UUID(request_id))
        assert req is not None
        req.created_at = dt.datetime.now(dt.UTC) - dt.timedelta(days=15)
        await session.flush()


def _create(client, entity_id: str, reason: str = "Нужен доступ для сверки"):
    return client.post("/api/access-requests", json={"entity_id": entity_id, "reason": reason})


def _decide(client, request_id: str, **body: Any):
    return client.post(f"/api/access-requests/{request_id}/decision", json=body)


class TestCreate:
    def test_happy_path_notifies_owner_and_team_head(self, client) -> None:
        team_id = create_team(client)
        head = create_user(client, "HEAD", team_id=team_id)
        owner = create_user(client, "KAM", team_id=team_id)
        requester = create_user(client, "KAM")
        org_id = create_organization(client)
        run(client, lambda: _set_owner(org_id, owner.id))
        run(client, lambda: _set_team_head(team_id, head.id))

        sign_in(client, requester)
        response = _create(client, org_id)

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == "pending"
        assert body["requester_id"] == str(requester.id)
        assert body["entity_id"] == org_id

        async def _unread_for(recipient_id: uuid.UUID) -> int:
            from sqlalchemy import func, select

            from app.core.db import session_scope
            from app.modules.notification.models import Notification

            async with session_scope() as session:
                return int(
                    (
                        await session.execute(
                            select(func.count(Notification.id)).where(
                                Notification.recipient_id == recipient_id,
                                Notification.template_code == "ACCESS_REQUEST_CREATED",
                            )
                        )
                    ).scalar_one()
                )

        assert run(client, _unread_for, owner.id) == 1
        assert run(client, _unread_for, head.id) == 1

    def test_duplicate_pending_request_is_409(self, client) -> None:
        owner = create_user(client, "KAM")
        requester = create_user(client, "KAM")
        org_id = create_organization(client)
        run(client, lambda: _set_owner(org_id, owner.id))

        sign_in(client, requester)
        first = _create(client, org_id)
        assert first.status_code == 201, first.text

        second = _create(client, org_id)
        assert second.status_code == 409, second.text

    def test_organization_without_an_owner_is_422(self, client) -> None:
        requester = login(client, "KAM")
        org_id = create_organization(client)

        response = _create(client, org_id)
        assert response.status_code == 422, response.text
        _ = requester

    def test_organization_already_in_scope_is_422(self, client) -> None:
        owner = login(client, "KAM")
        org_id = create_organization(client)
        run(client, lambda: _set_owner(org_id, owner.id))

        # Запрашивает сам владелец — организация уже в его скоупе.
        response = _create(client, org_id)
        assert response.status_code == 422, response.text

    def test_unknown_organization_is_404(self, client) -> None:
        login(client, "KAM")
        response = _create(client, str(uuid.uuid4()))
        assert response.status_code == 404, response.text

    def test_eleventh_pending_request_is_422(self, client) -> None:
        requester = login(client, "KAM")
        owner = run(client, _make_user, "KAM")
        for _ in range(10):
            org_id = create_organization(client)
            run(client, lambda oid=org_id: _set_owner(oid, owner.id))
            created = _create(client, org_id)
            assert created.status_code == 201, created.text

        extra_org = create_organization(client)
        run(client, lambda: _set_owner(extra_org, owner.id))
        response = _create(client, extra_org)
        assert response.status_code == 422, response.text
        _ = requester

    def test_restricted_visibility_blocks_the_request_before_it_is_created(self, client) -> None:
        """B4-6: вуз вне `allow`-ограничения запрашивающего — запрос доступа не поможет
        обойти его, 422 сразу при создании."""
        admin = login(client, "ADMIN")
        kam = run(client, _make_user, "KAM")
        owner = run(client, _make_user, "KAM")
        allowed_org = create_organization(client)
        blocked_org = create_organization(client)
        run(client, lambda: _set_owner(allowed_org, owner.id))
        run(client, lambda: _set_owner(blocked_org, owner.id))

        rules_response = client.put(
            f"/api/admin/users/{kam.id}/access-rules",
            json={
                "organizations": {"mode": "allow", "ids": [allowed_org]},
                "directions": {"mode": "all", "ids": []},
                "reason": "Тест ограничения",
            },
            headers={"If-Match": "1"},
        )
        assert rules_response.status_code == 200, rules_response.text

        sign_in(client, kam)
        response = _create(client, blocked_org)
        assert response.status_code == 422, response.text
        _ = admin


class TestDecisionPermissions:
    def test_owner_can_approve(self, client) -> None:
        owner = create_user(client, "KAM")
        requester = create_user(client, "KAM")
        org_id = create_organization(client)
        run(client, lambda: _set_owner(org_id, owner.id))
        sign_in(client, requester)
        request_id = _create(client, org_id).json()["id"]

        sign_in(client, owner)
        response = _decide(client, request_id, decision="approve")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "approved"

    def test_team_head_can_approve(self, client) -> None:
        team_id = create_team(client)
        head = create_user(client, "HEAD", team_id=team_id)
        owner = create_user(client, "KAM", team_id=team_id)
        requester = create_user(client, "KAM")
        org_id = create_organization(client)
        run(client, lambda: _set_owner(org_id, owner.id))
        run(client, lambda: _set_team_head(team_id, head.id))
        sign_in(client, requester)
        request_id = _create(client, org_id).json()["id"]

        sign_in(client, head)
        response = _decide(client, request_id, decision="approve")
        assert response.status_code == 200, response.text

    def test_admin_can_approve(self, client) -> None:
        owner = create_user(client, "KAM")
        requester = create_user(client, "KAM")
        org_id = create_organization(client)
        run(client, lambda: _set_owner(org_id, owner.id))
        sign_in(client, requester)
        request_id = _create(client, org_id).json()["id"]

        login(client, "ADMIN")
        response = _decide(client, request_id, decision="approve")
        assert response.status_code == 200, response.text

    def test_unrelated_kam_cannot_decide(self, client) -> None:
        owner = create_user(client, "KAM")
        requester = create_user(client, "KAM")
        stranger = create_user(client, "KAM")
        org_id = create_organization(client)
        run(client, lambda: _set_owner(org_id, owner.id))
        sign_in(client, requester)
        request_id = _create(client, org_id).json()["id"]

        sign_in(client, stranger)
        response = _decide(client, request_id, decision="approve")
        assert response.status_code == 403, response.text

    def test_head_of_a_different_team_cannot_decide(self, client) -> None:
        owner_team = create_team(client)
        other_team = create_team(client)
        owner = create_user(client, "KAM", team_id=owner_team)
        other_head = create_user(client, "HEAD", team_id=other_team)
        requester = create_user(client, "KAM")
        org_id = create_organization(client)
        run(client, lambda: _set_owner(org_id, owner.id))
        sign_in(client, requester)
        request_id = _create(client, org_id).json()["id"]

        sign_in(client, other_head)
        response = _decide(client, request_id, decision="approve")
        assert response.status_code == 403, response.text

    def test_reject_without_comment_is_422(self, client) -> None:
        owner = create_user(client, "KAM")
        requester = create_user(client, "KAM")
        org_id = create_organization(client)
        run(client, lambda: _set_owner(org_id, owner.id))
        sign_in(client, requester)
        request_id = _create(client, org_id).json()["id"]

        sign_in(client, owner)
        response = _decide(client, request_id, decision="reject")
        assert response.status_code == 422, response.text

    def test_reject_with_comment_notifies_requester(self, client) -> None:
        owner = create_user(client, "KAM")
        requester = create_user(client, "KAM")
        org_id = create_organization(client)
        run(client, lambda: _set_owner(org_id, owner.id))
        sign_in(client, requester)
        request_id = _create(client, org_id).json()["id"]

        sign_in(client, owner)
        response = _decide(client, request_id, decision="reject", comment="Не сейчас")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "rejected"
        assert run(client, _count_audit, "ACCESS_REJECTED", uuid.UUID(org_id)) == 1


class TestVisibilityAfterApproval:
    def test_organization_is_404_until_approved_then_200_after(self, client) -> None:
        owner = create_user(client, "KAM")
        requester = create_user(client, "KAM")
        org_id = create_organization(client)
        run(client, lambda: _set_owner(org_id, owner.id))

        sign_in(client, requester)
        before = client.get(f"/api/organizations/{org_id}")
        assert before.status_code == 404, before.text

        request_id = _create(client, org_id).json()["id"]
        sign_in(client, owner)
        decided = _decide(client, request_id, decision="approve")
        assert decided.status_code == 200, decided.text
        assert decided.json()["expires_at"] is not None

        sign_in(client, requester)
        after = client.get(f"/api/organizations/{org_id}")
        assert after.status_code == 200, after.text

        assert run(client, _count_audit, "ACCESS_GRANTED", uuid.UUID(org_id)) == 1

    def test_the_deal_owner_still_does_not_see_the_owners_deal(self, client) -> None:
        """Одобренный доступ открывает карточку вуза, но не чужую сделку — B4-12 не
        переносит владение и не расширяет скоуп сделок."""
        owner = create_user(client, "KAM")
        requester = create_user(client, "KAM")
        org_id = create_organization(client)
        run(client, lambda: _set_owner(org_id, owner.id))

        login(client, "ADMIN")
        graph = create_published_workflow(client)

        sign_in(client, owner)
        deal = create_deal(
            client, graph["workflow"]["id"], organization_id=org_id, owner_id=str(owner.id)
        )

        sign_in(client, requester)
        request_id = _create(client, org_id).json()["id"]
        sign_in(client, owner)
        _decide(client, request_id, decision="approve")

        sign_in(client, requester)
        org_response = client.get(f"/api/organizations/{org_id}")
        assert org_response.status_code == 200, org_response.text
        deal_response = client.get(f"/api/deals/{deal['id']}")
        assert deal_response.status_code == 404, deal_response.text

    def test_expired_access_falls_back_to_404(self, client) -> None:
        owner = create_user(client, "KAM")
        requester = create_user(client, "KAM")
        org_id = create_organization(client)
        run(client, lambda: _set_owner(org_id, owner.id))

        sign_in(client, requester)
        request_id = _create(client, org_id).json()["id"]
        sign_in(client, owner)
        _decide(client, request_id, decision="approve")
        run(client, lambda: _force_expire(request_id))

        sign_in(client, requester)
        response = client.get(f"/api/organizations/{org_id}")
        assert response.status_code == 404, response.text

    def test_revoke_removes_access_again(self, client) -> None:
        owner = create_user(client, "KAM")
        requester = create_user(client, "KAM")
        org_id = create_organization(client)
        run(client, lambda: _set_owner(org_id, owner.id))

        sign_in(client, requester)
        request_id = _create(client, org_id).json()["id"]
        sign_in(client, owner)
        _decide(client, request_id, decision="approve")

        sign_in(client, requester)
        assert client.get(f"/api/organizations/{org_id}").status_code == 200

        sign_in(client, owner)
        revoked = client.post(f"/api/access-requests/{request_id}/revoke")
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["status"] == "revoked"
        assert run(client, _count_audit, "ACCESS_REVOKED", uuid.UUID(org_id)) == 1

        sign_in(client, requester)
        after_revoke = client.get(f"/api/organizations/{org_id}")
        assert after_revoke.status_code == 404, after_revoke.text


class TestLeakPrevention:
    """Критический тест: B4-5/B4-6 `deny`-ограничение сильнее одобренного B4-12-доступа
    (`organization_scope_clause_for_rules` комбинирует их через AND, не OR)."""

    def test_deny_restriction_still_hides_the_organization_after_approval(self, client) -> None:
        owner = create_user(client, "KAM")
        requester = create_user(client, "KAM")
        org_id = create_organization(client)
        run(client, lambda: _set_owner(org_id, owner.id))

        sign_in(client, requester)
        request_id = _create(client, org_id).json()["id"]
        sign_in(client, owner)
        approved = _decide(client, request_id, decision="approve")
        assert approved.status_code == 200, approved.text

        sign_in(client, requester)
        assert client.get(f"/api/organizations/{org_id}").status_code == 200

        # Администратор теперь явно запрещает requester-у видеть именно этот вуз.
        login(client, "ADMIN")
        deny_response = client.put(
            f"/api/admin/users/{requester.id}/access-rules",
            json={
                "organizations": {"mode": "deny", "ids": [org_id]},
                "directions": {"mode": "all", "ids": []},
                "reason": "Тест: deny сильнее одобренного доступа",
            },
            headers={"If-Match": "1"},
        )
        assert deny_response.status_code == 200, deny_response.text

        sign_in(client, requester)
        blocked = client.get(f"/api/organizations/{org_id}")
        assert blocked.status_code == 404, blocked.text


class TestListingAndCancel:
    def test_mine_shows_only_own_requests(self, client) -> None:
        owner = create_user(client, "KAM")
        requester_a = create_user(client, "KAM")
        requester_b = create_user(client, "KAM")
        org_a = create_organization(client)
        org_b = create_organization(client)
        run(client, lambda: _set_owner(org_a, owner.id))
        run(client, lambda: _set_owner(org_b, owner.id))

        sign_in(client, requester_a)
        _create(client, org_a)
        sign_in(client, requester_b)
        _create(client, org_b)

        sign_in(client, requester_a)
        mine = client.get("/api/access-requests", params={"box": "mine"})
        assert mine.status_code == 200, mine.text
        items = mine.json()["items"]
        assert {i["entity_id"] for i in items} == {org_a}

    def test_inbox_shows_only_decidable_requests(self, client) -> None:
        owner_a = create_user(client, "KAM")
        owner_b = create_user(client, "KAM")
        requester = create_user(client, "KAM")
        org_a = create_organization(client)
        org_b = create_organization(client)
        run(client, lambda: _set_owner(org_a, owner_a.id))
        run(client, lambda: _set_owner(org_b, owner_b.id))

        sign_in(client, requester)
        _create(client, org_a)
        _create(client, org_b)

        sign_in(client, owner_a)
        inbox = client.get("/api/access-requests", params={"box": "inbox"})
        assert inbox.status_code == 200, inbox.text
        items = inbox.json()["items"]
        assert {i["entity_id"] for i in items} == {org_a}

    def test_requester_can_cancel_their_own_pending_request(self, client) -> None:
        owner = create_user(client, "KAM")
        requester = create_user(client, "KAM")
        org_id = create_organization(client)
        run(client, lambda: _set_owner(org_id, owner.id))

        sign_in(client, requester)
        request_id = _create(client, org_id).json()["id"]
        cancelled = client.post(f"/api/access-requests/{request_id}/cancel")
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled"

    def test_someone_else_cannot_cancel_a_foreign_request(self, client) -> None:
        owner = create_user(client, "KAM")
        requester = create_user(client, "KAM")
        stranger = create_user(client, "KAM")
        org_id = create_organization(client)
        run(client, lambda: _set_owner(org_id, owner.id))

        sign_in(client, requester)
        request_id = _create(client, org_id).json()["id"]

        sign_in(client, stranger)
        response = client.post(f"/api/access-requests/{request_id}/cancel")
        assert response.status_code == 403, response.text

    def test_a_cancelled_request_can_be_re_requested(self, client) -> None:
        """Отменённый запрос не должен навечно занимать уникальный pending-слот."""
        owner = create_user(client, "KAM")
        requester = create_user(client, "KAM")
        org_id = create_organization(client)
        run(client, lambda: _set_owner(org_id, owner.id))

        sign_in(client, requester)
        first_id = _create(client, org_id).json()["id"]
        client.post(f"/api/access-requests/{first_id}/cancel")

        second = _create(client, org_id)
        assert second.status_code == 201, second.text

    def test_a_stale_pending_request_past_ttl_can_be_re_requested(self, client) -> None:
        """B4-12: ожидающий дольше 14 дней не блокирует новый запрос (лениво просрочен)."""
        owner = create_user(client, "KAM")
        requester = create_user(client, "KAM")
        org_id = create_organization(client)
        run(client, lambda: _set_owner(org_id, owner.id))

        sign_in(client, requester)
        first_id = _create(client, org_id).json()["id"]
        run(client, lambda: _force_old_pending(first_id))

        second = _create(client, org_id)
        assert second.status_code == 201, second.text


def _principal_of(user) -> Any:
    from app.core.security import Principal, TokenClaims

    return Principal(
        user_id=user.id,
        keycloak_id=user.keycloak_id or "x",
        role=user.role,
        status=user.status,
        email=user.email,
        full_name=user.full_name,
        team_id=user.team_id,
        manager_id=user.manager_id,
        perm_epoch=user.perm_epoch,
        session_id=None,
        consent_version=user.consent_version,
        must_change_password=False,
        claims=TokenClaims(subject="x", raw={}),
        restricted=False,
    )


class TestConcurrentDecisions:
    """Решение по запросу — переход `pending → …`. Два одновременных решения читали статус до
    блокировки строки, оба проходили `_ensure_pending`, и каждое писало аудит и уведомление."""

    def test_two_simultaneous_approvals_decide_once(self, client) -> None:
        owner = create_user(client, "KAM")
        requester = create_user(client, "KAM")
        org_id = create_organization(client)
        run(client, lambda: _set_owner(org_id, owner.id))
        sign_in(client, requester)
        request_id = _create(client, org_id).json()["id"]

        async def _race() -> list[str]:
            import asyncio

            from app.core.db import session_scope
            from app.core.errors import AppError
            from app.modules.catalog.access_requests import AccessRequestService

            loaded = asyncio.Barrier(2)

            async def _approve() -> str:
                async with session_scope() as session:
                    service = AccessRequestService(session)
                    request = await service.get_or_404(uuid.UUID(request_id))
                    await loaded.wait()  # оба уже прочитали статус pending
                    try:
                        await service.approve(request, _principal_of(owner), days=7, comment=None)
                    except AppError:
                        return "refused"
                    return "approved"

            return list(await asyncio.gather(_approve(), _approve()))

        outcomes = run(client, _race)

        assert sorted(outcomes) == ["approved", "refused"]
        assert run(client, lambda: _count_audit("ACCESS_GRANTED", uuid.UUID(org_id))) == 1

    def test_approval_racing_the_requesters_cancel_has_one_winner(self, client) -> None:
        owner = create_user(client, "KAM")
        requester = create_user(client, "KAM")
        org_id = create_organization(client)
        run(client, lambda: _set_owner(org_id, owner.id))
        sign_in(client, requester)
        request_id = _create(client, org_id).json()["id"]

        async def _race() -> list[str]:
            import asyncio

            from app.core.db import session_scope
            from app.core.errors import AppError
            from app.modules.catalog.access_requests import AccessRequestService

            loaded = asyncio.Barrier(2)

            async def _decide(action: str) -> str:
                async with session_scope() as session:
                    service = AccessRequestService(session)
                    request = await service.get_or_404(uuid.UUID(request_id))
                    await loaded.wait()
                    try:
                        if action == "approve":
                            await service.approve(
                                request, _principal_of(owner), days=7, comment=None
                            )
                        else:
                            await service.cancel(request, _principal_of(requester))
                    except AppError:
                        return "refused"
                    return action

            return list(await asyncio.gather(_decide("approve"), _decide("cancel")))

        outcomes = run(client, _race)

        assert outcomes.count("refused") == 1
        granted = run(client, lambda: _count_audit("ACCESS_GRANTED", uuid.UUID(org_id)))
        assert granted == (1 if "approve" in outcomes else 0)
