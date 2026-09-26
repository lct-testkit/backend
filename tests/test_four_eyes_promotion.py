"""«Четыре глаза» при повышении существующей учётки до ADMIN и отзыв заявки её инициатором.

Найдено внешним тестированием: `PATCH /admin/users/{id}` с `role=ADMIN` делал администратора без
подтверждения второго (создание через `POST` подтверждение требовало), а отклонение заявки самим
инициатором нарушало CHECK `requested_by <> approved_by` и давало 500.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _login(client, user) -> None:
    client.headers["X-CSRF-Token"] = authenticate(client, user)


def _stub_keycloak(monkeypatch) -> None:
    from app.modules.identity.keycloak import keycloak_client

    async def _noop(*_args: object, **_kwargs: object) -> bool:
        return True

    for name in ("set_realm_role", "set_attribute"):
        monkeypatch.setattr(keycloak_client, name, _noop)


async def _load(user_id: uuid.UUID):
    from app.core.db import session_scope
    from app.modules.identity.models import User

    async with session_scope() as session:
        user = await session.get(User, user_id)
        session.expunge(user)
        return user


async def _approval(approval_id: str):
    from app.core.db import session_scope
    from app.modules.admin.models import AdminApproval

    async with session_scope() as session:
        approval = await session.get(AdminApproval, uuid.UUID(approval_id))
        session.expunge(approval)
        return approval


def _patch(client, user, body: dict):
    return client.patch(
        f"/api/admin/users/{user.id}", json=body, headers={"If-Match": str(user.version)}
    )


class TestPromotionToAdmin:
    def test_patch_role_admin_needs_a_second_administrator(self, client, monkeypatch) -> None:
        _stub_keycloak(monkeypatch)
        requester = run(client, _make_user, "ADMIN")
        approver = run(client, _make_user, "ADMIN")
        target = run(client, _make_user, "KAM")

        _login(client, requester)
        first = _patch(client, target, {"role": "ADMIN"})
        assert first.status_code == 409 and first.json()["code"] == "CRM-1902", first.text
        assert run(client, _load, target.id).role == "KAM"  # не повышен
        approval_id = first.json()["approval_id"]

        # Сам себе инициатор подтвердить не может.
        assert client.post(f"/api/admin/approvals/{approval_id}/approve").status_code == 409
        again = _patch(client, target, {"role": "ADMIN", "approval_id": approval_id})
        assert again.status_code == 409  # заявка ещё не подтверждена
        assert run(client, _load, target.id).role == "KAM"

        _login(client, approver)
        assert client.post(f"/api/admin/approvals/{approval_id}/approve").status_code == 200

        _login(client, requester)
        done = _patch(client, target, {"role": "ADMIN", "approval_id": approval_id})
        assert done.status_code == 200, done.text
        assert done.json()["role"] == "ADMIN"
        assert run(client, _approval, approval_id).status == "consumed"

    def test_approval_is_bound_to_the_user_and_the_role(self, client, monkeypatch) -> None:
        _stub_keycloak(monkeypatch)
        requester = run(client, _make_user, "ADMIN")
        approver = run(client, _make_user, "ADMIN")
        target = run(client, _make_user, "KAM")
        other = run(client, _make_user, "KAM")

        _login(client, requester)
        approval_id = _patch(client, target, {"role": "ADMIN"}).json()["approval_id"]
        _login(client, approver)
        client.post(f"/api/admin/approvals/{approval_id}/approve")

        # Подтверждали повышение `target`, а применить пытаются к другому.
        _login(client, requester)
        stolen = _patch(client, other, {"role": "ADMIN", "approval_id": approval_id})
        assert stolen.status_code == 409, stolen.text
        assert run(client, _load, other.id).role == "KAM"

    def test_other_changes_and_other_roles_do_not_need_approval(self, client, monkeypatch) -> None:
        _stub_keycloak(monkeypatch)
        _login(client, run(client, _make_user, "ADMIN"))
        target = run(client, _make_user, "KAM")

        assert _patch(client, target, {"position": "Менеджер"}).status_code == 200
        refreshed = run(client, _load, target.id)
        assert _patch(client, refreshed, {"role": "AUDITOR"}).status_code == 200

    def test_an_administrator_keeping_the_role_is_not_a_promotion(
        self, client, monkeypatch
    ) -> None:
        _stub_keycloak(monkeypatch)
        _login(client, run(client, _make_user, "ADMIN"))
        other_admin = run(client, _make_user, "ADMIN")
        response = _patch(client, other_admin, {"role": "ADMIN", "position": "Главный"})
        assert response.status_code == 200, response.text


class TestInitiatorWithdrawsTheRequest:
    def test_rejecting_own_request_is_not_a_server_error(self, client, monkeypatch) -> None:
        _stub_keycloak(monkeypatch)
        requester = run(client, _make_user, "ADMIN")
        target = run(client, _make_user, "KAM")
        _login(client, requester)
        approval_id = _patch(client, target, {"role": "ADMIN"}).json()["approval_id"]

        response = client.post(
            f"/api/admin/approvals/{approval_id}/reject", json={"reason": "передумал"}
        )

        assert response.status_code == 200, response.text
        approval = run(client, _approval, approval_id)
        assert approval.status == "rejected"
        assert approval.approved_by is None  # отозвана: подтвердившего нет
        assert approval.reason == "передумал"

    def test_default_reason_marks_the_withdrawal(self, client, monkeypatch) -> None:
        _stub_keycloak(monkeypatch)
        requester = run(client, _make_user, "ADMIN")
        target = run(client, _make_user, "KAM")
        _login(client, requester)
        approval_id = _patch(client, target, {"role": "ADMIN"}).json()["approval_id"]

        assert client.post(f"/api/admin/approvals/{approval_id}/reject", json={}).status_code == 200
        assert run(client, _approval, approval_id).reason == "Отозвана инициатором"

    def test_second_administrator_rejection_still_records_who_decided(
        self, client, monkeypatch
    ) -> None:
        _stub_keycloak(monkeypatch)
        requester = run(client, _make_user, "ADMIN")
        approver = run(client, _make_user, "ADMIN")
        target = run(client, _make_user, "KAM")
        _login(client, requester)
        approval_id = _patch(client, target, {"role": "ADMIN"}).json()["approval_id"]

        _login(client, approver)
        assert (
            client.post(
                f"/api/admin/approvals/{approval_id}/reject", json={"reason": "не согласовано"}
            ).status_code
            == 200
        )
        approval = run(client, _approval, approval_id)
        assert approval.approved_by == approver.id
        assert approval.reason == "не согласовано"
