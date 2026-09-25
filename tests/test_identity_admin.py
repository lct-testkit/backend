"""Сквозные тесты администрирования пользователей (`identity/admin_service.py`,
`identity/router_admin.py`) на настоящей PostgreSQL (`TEST_DATABASE_URL`) —
см. докстринг `tests/conftest.py`. Keycloak в тестах недоступен: там, где
операция доходит до него, вызовы подменяются.
"""

from __future__ import annotations

import datetime as dt
import functools
import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _login(client, user) -> None:
    csrf = authenticate(client, user)
    client.headers["X-CSRF-Token"] = csrf


def _four_eyes(client, requester, approver, path: str, body: dict):
    """«Четыре глаза»: заявка → подтверждение вторым администратором → повтор."""
    _login(client, requester)
    first = client.post(path, json=body)
    assert first.status_code == 409 and first.json()["code"] == "CRM-1902", first.text
    approval_id = first.json()["approval_id"]

    _login(client, approver)
    approved = client.post(f"/api/admin/approvals/{approval_id}/approve")
    assert approved.status_code == 200, approved.text

    _login(client, requester)
    return client.post(path, json={**body, "approval_id": approval_id}), approval_id


async def _load_user(user_id: uuid.UUID):
    from app.core.db import session_scope
    from app.modules.identity.models import User

    async with session_scope() as session:
        user = await session.get(User, user_id)
        session.expunge(user)
        return user


async def _count_approvals(operation: str, email: str) -> int:
    from sqlalchemy import func, select

    from app.core.db import session_scope
    from app.modules.admin.models import AdminApproval

    async with session_scope() as session:
        return int(
            (
                await session.execute(
                    select(func.count(AdminApproval.id)).where(
                        AdminApproval.operation == operation,
                        AdminApproval.payload["email"].astext == email,
                    )
                )
            ).scalar_one()
        )


async def _approval_status(approval_id: str) -> str:
    from app.core.db import session_scope
    from app.modules.admin.models import AdminApproval

    async with session_scope() as session:
        approval = await session.get(AdminApproval, uuid.UUID(approval_id))
        return approval.status


class TestPatchUserStatus:
    """Активация — не правка поля: `activated_at`, `USER_ACTIVATED` и погашение
    приглашения происходят при первом входе (`IdentityService.provision_from_claims`),
    а не по `PATCH` администратора."""

    def _patch(self, client, user, body: dict):
        return client.patch(
            f"/api/admin/users/{user.id}", json=body, headers={"If-Match": str(user.version)}
        )

    def test_invited_user_cannot_be_activated_by_patch(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        invited = run(client, _make_user, "KAM", "invited")

        response = self._patch(client, invited, {"status": "active"})

        assert response.status_code == 422, response.text
        assert response.json()["code"] == "CRM-1001"
        stored = run(client, _load_user, invited.id)
        assert stored.status == "invited"
        assert stored.activated_at is None

    def test_active_user_cannot_be_moved_back_to_invited(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        active = run(client, _make_user, "KAM", "active")

        response = self._patch(client, active, {"status": "invited"})

        assert response.status_code == 422, response.text
        assert run(client, _load_user, active.id).status == "active"

    def test_repeating_the_current_status_is_not_a_change(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        active = run(client, _make_user, "KAM", "active")

        response = self._patch(client, active, {"status": "active", "position": "Менеджер"})

        assert response.status_code == 200, response.text
        assert response.json()["position"] == "Менеджер"


class TestFirstLoginActivatesInvitedUser:
    """`create_user` заводит учётку в Keycloak сразу, `keycloak_id` у неё уже
    есть — активация при первом входе не должна зависеть от пустого `keycloak_id`."""

    def test_invited_user_becomes_active_at_first_request(self, client) -> None:
        invited = run(client, _make_user, "KAM", "invited")
        authenticate(client, invited)

        response = client.get("/api/me")

        assert response.status_code == 200, response.text
        assert response.json()["status"] == "active"
        stored = run(client, _load_user, invited.id)
        assert stored.status == "active"
        assert stored.activated_at is not None

    def test_activation_is_written_to_the_audit_log(self, client) -> None:
        from sqlalchemy import func, select

        from app.core.db import session_scope
        from app.modules.audit.models import AuditLog

        invited = run(client, _make_user, "KAM", "invited")
        authenticate(client, invited)
        assert client.get("/api/me").status_code == 200

        async def _count() -> int:
            async with session_scope() as session:
                return int(
                    (
                        await session.execute(
                            select(func.count(AuditLog.id)).where(
                                AuditLog.action == "USER_ACTIVATED",
                                AuditLog.entity_id == invited.id,
                            )
                        )
                    ).scalar_one()
                )

        assert run(client, _count) == 1

    def test_blocked_user_is_not_reactivated_by_logging_in(self, client) -> None:
        blocked = run(client, _make_user, "KAM", "blocked")
        authenticate(client, blocked)

        assert client.get("/api/me").status_code == 403
        assert run(client, _load_user, blocked.id).status == "blocked"


class TestCreateAdminValidatesBeforeFourEyes:
    """Заявка второму администратору открывается только для запроса, который
    в принципе может выполниться: иначе подтверждение уходит на операцию,
    повтор которой заведомо упадёт (дубль email, неизвестная команда)."""

    def _body(self, **overrides) -> dict:
        body = {
            "full_name": "Новый Администратор",
            "email": f"{uuid.uuid4().hex[:10]}@rt-it-school.ru",
            "role": "ADMIN",
        }
        body.update(overrides)
        return body

    def test_duplicate_email_is_rejected_without_opening_an_approval(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        existing = run(client, _make_user, "KAM")

        response = client.post("/api/admin/users", json=self._body(email=existing.email))

        assert response.status_code == 409, response.text
        assert response.json()["code"] == "CRM-1301"
        assert run(client, _count_approvals, "user.create_admin", existing.email) == 0

    def test_unknown_team_is_rejected_without_opening_an_approval(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        body = self._body(team_id=str(uuid.uuid4()))

        response = client.post("/api/admin/users", json=body)

        assert response.status_code == 404, response.text
        assert run(client, _count_approvals, "user.create_admin", body["email"]) == 0

    def test_unknown_manager_is_rejected_without_opening_an_approval(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        body = self._body(manager_id=str(uuid.uuid4()))

        response = client.post("/api/admin/users", json=body)

        assert response.status_code == 404, response.text
        assert run(client, _count_approvals, "user.create_admin", body["email"]) == 0

    def test_valid_request_still_goes_through_four_eyes(self, client, monkeypatch) -> None:
        from app.modules.identity.keycloak import keycloak_client

        async def _create_user(**_kwargs: object) -> str:
            return str(uuid.uuid4())

        async def _noop(*_args: object, **_kwargs: object) -> bool:
            return False

        monkeypatch.setattr(keycloak_client, "create_user", _create_user)
        monkeypatch.setattr(keycloak_client, "set_realm_role", _noop)
        monkeypatch.setattr(keycloak_client, "execute_actions_email", _noop)

        requester = run(client, _make_user, "ADMIN")
        approver = run(client, _make_user, "ADMIN")
        body = self._body()

        created, approval_id = _four_eyes(client, requester, approver, "/api/admin/users", body)

        assert created.status_code == 201, created.text
        assert created.json()["user"]["role"] == "ADMIN"
        assert run(client, _approval_status, approval_id) == "consumed"

    def test_failed_repeat_leaves_the_approval_usable(self, client, monkeypatch) -> None:
        from app.modules.identity.keycloak import keycloak_client

        async def _create_user(**_kwargs: object) -> str:
            return str(uuid.uuid4())

        async def _noop(*_args: object, **_kwargs: object) -> bool:
            return False

        monkeypatch.setattr(keycloak_client, "create_user", _create_user)
        monkeypatch.setattr(keycloak_client, "set_realm_role", _noop)
        monkeypatch.setattr(keycloak_client, "execute_actions_email", _noop)

        requester = run(client, _make_user, "ADMIN")
        approver = run(client, _make_user, "ADMIN")
        body = self._body()

        _login(client, requester)
        approval_id = client.post("/api/admin/users", json=body).json()["approval_id"]
        _login(client, approver)
        assert client.post(f"/api/admin/approvals/{approval_id}/approve").status_code == 200
        _login(client, requester)

        # Повтор с неизвестной командой падает — и подтверждение остаётся живым.
        failed = client.post(
            "/api/admin/users",
            json={**body, "team_id": str(uuid.uuid4()), "approval_id": approval_id},
        )
        assert failed.status_code == 404, failed.text
        assert run(client, _approval_status, approval_id) == "approved"

        ok = client.post("/api/admin/users", json={**body, "approval_id": approval_id})
        assert ok.status_code == 201, ok.text
        assert run(client, _approval_status, approval_id) == "consumed"


class TestErasureRequestGrace:
    """`grace_until` в ответе — то, что сохранено в запросе: у `blocked`
    отсрочка не начинается (`new_spec §4.8.4` шаг 4)."""

    def _request_erasure(self, client, subject):
        requester = run(client, _make_user, "ADMIN")
        approver = run(client, _make_user, "ADMIN")
        body = {
            "mode": "anonymize",
            "reason": "увольнение сотрудника",
            "legal_basis": "ст. 21 152-ФЗ",
        }
        response, _ = _four_eyes(
            client, requester, approver, f"/api/admin/users/{subject.id}/erasure-request", body
        )
        return response

    def test_blocked_request_has_no_grace_period(self, client) -> None:
        subject = run(client, _make_user, "KAM", "active")

        response = self._request_erasure(client, subject)

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == "blocked"
        assert body["grace_until"] is None
        detail = client.get(f"/api/admin/erasure-requests/{body['id']}").json()
        assert detail["grace_until"] is None

    def test_pending_request_reports_the_stored_grace_period(self, client) -> None:
        subject = run(client, _make_user, "KAM", "terminated")

        response = self._request_erasure(client, subject)

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == "pending"
        detail = client.get(f"/api/admin/erasure-requests/{body['id']}").json()
        assert body["grace_until"] == detail["grace_until"]
        grace = dt.datetime.fromisoformat(body["grace_until"])
        expected = dt.datetime.now(dt.UTC) + dt.timedelta(days=30)
        assert abs((grace - expected).total_seconds()) < 60


def _keycloak(monkeypatch, *, fail_disable: bool = False) -> list[tuple[str, bool]]:
    """Keycloak в тестах недоступен: включение и выключение учётки записываем."""
    from app.modules.identity.keycloak import keycloak_client

    calls: list[tuple[str, bool]] = []

    async def set_enabled(keycloak_id: str, *, enabled: bool) -> None:
        if fail_disable and not enabled:
            raise RuntimeError("keycloak недоступен")
        calls.append((keycloak_id, enabled))

    async def noop(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(keycloak_client, "set_enabled", set_enabled)
    monkeypatch.setattr(keycloak_client, "logout_all_sessions", noop)
    return calls


async def _update_user(user_id: uuid.UUID, **values: object) -> None:
    from sqlalchemy import update

    from app.core.db import session_scope
    from app.modules.identity.models import User

    async with session_scope() as session:
        await session.execute(update(User).where(User.id == user_id).values(**values))


def _set_user(client, user, **values: object) -> None:
    run(client, functools.partial(_update_user, user.id, **values))


async def _sweep_lifecycle() -> dict[str, int]:
    from app.modules.identity.tasks import sweep_user_lifecycle

    return await sweep_user_lifecycle({})


async def _audit_count(action: str, entity_id: uuid.UUID) -> int:
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


class TestAutoUnblock:
    """`auto_unblock_at` из запроса на блокировку исполняется фоновой задачей."""

    def _block(self, client, subject, **body):
        return client.post(
            f"/api/admin/users/{subject.id}/block", json={"reason": "Плановая блокировка", **body}
        )

    def _now(self, **delta) -> str:
        return (dt.datetime.now(dt.UTC) + dt.timedelta(**delta)).isoformat()

    def test_expired_block_is_lifted_by_the_sweep(self, client, monkeypatch) -> None:
        calls = _keycloak(monkeypatch)
        _login(client, run(client, _make_user, "ADMIN"))
        subject = run(client, _make_user, "KAM")
        assert (
            self._block(client, subject, auto_unblock_at=self._now(minutes=-1)).status_code == 200
        )
        blocked = client.get(f"/api/admin/users/{subject.id}").json()
        assert (blocked["status"], blocked["auto_unblock_at"] is not None) == ("blocked", True)

        result = run(client, _sweep_lifecycle)

        assert result["unblocked"] >= 1
        stored = run(client, _load_user, subject.id)
        assert (stored.status, stored.auto_unblock_at, stored.blocked_at) == ("active", None, None)
        assert calls.index((subject.keycloak_id, False)) < calls.index((subject.keycloak_id, True))
        assert run(client, _audit_count, "USER_UNBLOCKED", subject.id) == 1

    def test_deadline_in_the_future_keeps_the_block(self, client, monkeypatch) -> None:
        _keycloak(monkeypatch)
        _login(client, run(client, _make_user, "ADMIN"))
        subject = run(client, _make_user, "KAM")
        self._block(client, subject, auto_unblock_at=self._now(days=3))

        run(client, _sweep_lifecycle)

        assert run(client, _load_user, subject.id).status == "blocked"

    def test_block_without_a_deadline_stays_until_an_admin_decides(
        self, client, monkeypatch
    ) -> None:
        _keycloak(monkeypatch)
        _login(client, run(client, _make_user, "ADMIN"))
        subject = run(client, _make_user, "KAM")
        self._block(client, subject)

        run(client, _sweep_lifecycle)

        assert run(client, _load_user, subject.id).status == "blocked"

    def test_manual_unblock_forgets_the_deadline(self, client, monkeypatch) -> None:
        _keycloak(monkeypatch)
        _login(client, run(client, _make_user, "ADMIN"))
        subject = run(client, _make_user, "KAM")
        self._block(client, subject, auto_unblock_at=self._now(days=3))

        assert client.post(f"/api/admin/users/{subject.id}/unblock", json={}).status_code == 200

        assert run(client, _load_user, subject.id).auto_unblock_at is None

    def test_deadline_without_a_time_zone_is_taken_as_utc(self, client, monkeypatch) -> None:
        _keycloak(monkeypatch)
        _login(client, run(client, _make_user, "ADMIN"))
        subject = run(client, _make_user, "KAM")

        assert (
            self._block(client, subject, auto_unblock_at="2031-01-01T10:00:00").status_code == 200
        )

        stored = run(client, _load_user, subject.id).auto_unblock_at
        assert stored == dt.datetime(2031, 1, 1, 10, 0, tzinfo=dt.UTC)

    def test_someone_who_left_is_not_brought_back(self, client, monkeypatch) -> None:
        # Срок блокировки истёк, но пользователь к этому времени уволен.
        calls = _keycloak(monkeypatch)
        subject = run(client, _make_user, "KAM", "terminated")
        _set_user(
            client,
            subject,
            auto_unblock_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=1),
        )

        run(client, _sweep_lifecycle)

        assert run(client, _load_user, subject.id).status == "terminated"
        assert subject.keycloak_id not in {kc for kc, _ in calls}


class TestInviteExpired:
    """new_spec §4.1: не вошёл за 30 дней — `INVITE_EXPIRED`, учётка отключается,
    администраторам приходит уведомление."""

    def _invited(self, client, *, days_ago: int, with_invited_at: bool = True, logged_in=False):
        user = run(client, _make_user, "KAM", "invited")
        when = dt.datetime.now(dt.UTC) - dt.timedelta(days=days_ago)
        _set_user(
            client,
            user,
            created_at=when,
            invited_at=when if with_invited_at else None,
            last_login_at=dt.datetime.now(dt.UTC) if logged_in else None,
        )
        return user

    def test_account_that_never_logged_in_is_disabled(self, client, monkeypatch) -> None:
        calls = _keycloak(monkeypatch)
        run(client, _make_user, "ADMIN")
        user = self._invited(client, days_ago=31)

        result = run(client, _sweep_lifecycle)

        assert result["invites_expired"] >= 1
        stored = run(client, _load_user, user.id)
        assert (stored.status, stored.status_reason) == ("blocked", "invite_expired")
        assert stored.blocked_at is not None
        assert (user.keycloak_id, False) in calls
        assert run(client, _audit_count, "USER_INVITE_EXPIRED", user.id) == 1

    def test_administrators_get_a_notification(self, client, monkeypatch) -> None:
        from sqlalchemy import select

        from app.core.db import session_scope
        from app.modules.identity.models import User
        from app.modules.notification.models import Notification

        _keycloak(monkeypatch)
        admin = run(client, _make_user, "ADMIN")
        user = self._invited(client, days_ago=45)
        run(client, _sweep_lifecycle)

        async def _recipients() -> tuple[set[uuid.UUID], set[str]]:
            async with session_scope() as session:
                rows = (
                    await session.execute(
                        select(Notification.recipient_id, User.role)
                        .join(User, User.id == Notification.recipient_id)
                        .where(
                            Notification.template_code == "USER_INVITE_EXPIRED",
                            Notification.entity_id == user.id,
                        )
                    )
                ).all()
                return {row[0] for row in rows}, {row[1] for row in rows}

        recipients, roles = run(client, _recipients)
        assert admin.id in recipients
        assert roles == {"ADMIN"}

    def test_a_fresh_invite_is_left_alone(self, client, monkeypatch) -> None:
        calls = _keycloak(monkeypatch)
        user = self._invited(client, days_ago=29)

        run(client, _sweep_lifecycle)

        assert run(client, _load_user, user.id).status == "invited"
        assert user.keycloak_id not in {kc for kc, _ in calls}

    def test_created_at_counts_when_there_is_no_invited_at(self, client, monkeypatch) -> None:
        _keycloak(monkeypatch)
        user = self._invited(client, days_ago=40, with_invited_at=False)

        run(client, _sweep_lifecycle)

        assert run(client, _load_user, user.id).status == "blocked"

    def test_someone_who_did_log_in_is_not_expired(self, client, monkeypatch) -> None:
        _keycloak(monkeypatch)
        user = self._invited(client, days_ago=90, logged_in=True)

        run(client, _sweep_lifecycle)

        assert run(client, _load_user, user.id).status == "invited"

    def test_active_users_are_never_touched(self, client, monkeypatch) -> None:
        _keycloak(monkeypatch)
        user = run(client, _make_user, "KAM", "active")
        _set_user(client, user, created_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=400))

        run(client, _sweep_lifecycle)

        assert run(client, _load_user, user.id).status == "active"

    def test_keycloak_failure_leaves_the_account_for_the_next_tick(
        self, client, monkeypatch
    ) -> None:
        _keycloak(monkeypatch, fail_disable=True)
        user = self._invited(client, days_ago=31)

        result = run(client, _sweep_lifecycle)

        assert result["failed"] >= 1
        assert run(client, _load_user, user.id).status == "invited"
        # Не оставляем в общей БД учётку, которую подхватит чужой прогон задачи.
        now = dt.datetime.now(dt.UTC)
        _set_user(client, user, invited_at=now, created_at=now)

    def test_resending_the_invite_brings_an_expired_account_back(self, client, monkeypatch) -> None:
        calls = _keycloak(monkeypatch)
        _login(client, run(client, _make_user, "ADMIN"))
        user = self._invited(client, days_ago=31)
        run(client, _sweep_lifecycle)
        assert run(client, _load_user, user.id).status == "blocked"

        response = client.post(f"/api/admin/users/{user.id}/invite")

        assert response.status_code == 200, response.text
        assert response.json()["invite_url"]
        stored = run(client, _load_user, user.id)
        assert (stored.status, stored.status_reason, stored.blocked_at) == ("invited", None, None)
        assert (user.keycloak_id, True) in calls

    def test_other_blocked_accounts_still_cannot_be_reinvited(self, client, monkeypatch) -> None:
        _keycloak(monkeypatch)
        _login(client, run(client, _make_user, "ADMIN"))
        user = run(client, _make_user, "KAM", "blocked")

        response = client.post(f"/api/admin/users/{user.id}/invite")

        assert response.status_code == 422, response.text


class TestWorkerRegistration:
    def test_lifecycle_sweep_is_scheduled(self) -> None:
        from app.modules.identity.tasks import sweep_user_lifecycle
        from app.worker.main import WorkerSettings

        assert sweep_user_lifecycle in WorkerSettings.functions
        assert any(job.coroutine is sweep_user_lifecycle for job in WorkerSettings.cron_jobs)


async def _deals_of(owner_id: uuid.UUID, count: int) -> list[uuid.UUID]:
    from app.core.db import session_scope
    from app.modules.catalog.models import Organization
    from app.modules.crm.models import Deal
    from app.modules.workflow.models import Workflow, WorkflowStatus

    async with session_scope() as session:
        workflow = Workflow(
            code=f"wf-{uuid.uuid4().hex[:10]}",
            name="Воронка передачи",
            deal_type="b2b",
            state="draft",
        )
        session.add(workflow)
        await session.flush()
        status = WorkflowStatus(workflow_id=workflow.id, code="new", name="Новая")
        org = Organization(name=f"Вуз {uuid.uuid4().hex[:6]}", org_type="university")
        session.add_all([status, org])
        await session.flush()
        ids = []
        for index in range(count):
            deal = Deal(
                number=f"D-{uuid.uuid4().hex[:10]}",
                title=f"Сделка {index}",
                deal_type="b2b",
                workflow_id=workflow.id,
                status_id=status.id,
                organization_id=org.id,
                owner_id=owner_id,
            )
            session.add(deal)
            await session.flush()
            ids.append(deal.id)
        return ids


async def _owners(deal_ids: list[uuid.UUID]) -> dict[uuid.UUID, uuid.UUID]:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.crm.models import Deal

    async with session_scope() as session:
        rows = await session.execute(select(Deal.id, Deal.owner_id).where(Deal.id.in_(deal_ids)))
        return {row.id: row.owner_id for row in rows}


class TestOffboardPerDeal:
    """new_spec §4.7 шаг 2: преемник — «одного на всё или по-сделочно»."""

    def _offboard(self, client, leaving, successor, **extra):
        return client.post(
            f"/api/admin/users/{leaving.id}/offboard",
            json={
                "mode": "confirm",
                "successor_id": str(successor.id),
                "reason": "Увольнение сотрудника",
                **extra,
            },
        )

    def _scene(self, client, monkeypatch):
        _keycloak(monkeypatch)
        _login(client, run(client, _make_user, "ADMIN"))
        leaving = run(client, _make_user, "KAM")
        main, other = run(client, _make_user, "KAM"), run(client, _make_user, "KAM")
        deals = run(client, _deals_of, leaving.id, 3)
        return leaving, main, other, deals

    def test_named_deals_go_to_their_successors_and_the_rest_to_the_main_one(
        self, client, monkeypatch
    ) -> None:
        leaving, main, other, deals = self._scene(client, monkeypatch)

        response = self._offboard(
            client, leaving, main, deal_successors={str(deals[0]): str(other.id)}
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body["reassigned_deals"]) == {str(d) for d in deals}
        owners = run(client, _owners, deals)
        assert owners[deals[0]] == other.id
        assert owners[deals[1]] == main.id and owners[deals[2]] == main.id
        assert run(client, _load_user, leaving.id).status == "terminated"

    def test_without_deal_successors_everything_goes_to_the_main_one(
        self, client, monkeypatch
    ) -> None:
        leaving, main, _, deals = self._scene(client, monkeypatch)

        response = self._offboard(client, leaving, main)

        assert response.status_code == 200, response.text
        assert set(run(client, _owners, deals).values()) == {main.id}

    def test_a_deal_of_someone_else_cancels_the_whole_offboarding(
        self, client, monkeypatch
    ) -> None:
        leaving, main, other, deals = self._scene(client, monkeypatch)
        stranger_deal = run(client, _deals_of, other.id, 1)[0]

        response = self._offboard(
            client,
            leaving,
            main,
            deal_successors={str(deals[0]): str(other.id), str(stranger_deal): str(main.id)},
        )

        assert response.status_code == 422, response.text
        assert run(client, _load_user, leaving.id).status == "active"
        assert set(run(client, _owners, deals).values()) == {leaving.id}

    def test_a_deal_successor_must_be_active(self, client, monkeypatch) -> None:
        leaving, main, _, deals = self._scene(client, monkeypatch)
        blocked = run(client, _make_user, "KAM", "blocked")

        response = self._offboard(
            client, leaving, main, deal_successors={str(deals[0]): str(blocked.id)}
        )

        assert response.status_code == 422, response.text
        assert set(run(client, _owners, deals).values()) == {leaving.id}

    def test_a_deal_successor_cannot_be_the_person_who_leaves(self, client, monkeypatch) -> None:
        leaving, main, _, deals = self._scene(client, monkeypatch)

        response = self._offboard(
            client, leaving, main, deal_successors={str(deals[0]): str(leaving.id)}
        )

        assert response.status_code == 422, response.text

    def test_preview_ignores_the_mapping(self, client, monkeypatch) -> None:
        leaving, _, other, deals = self._scene(client, monkeypatch)

        response = client.post(
            f"/api/admin/users/{leaving.id}/offboard",
            json={"mode": "preview", "deal_successors": {str(deals[0]): str(other.id)}},
        )

        assert response.status_code == 200, response.text
        assert set(run(client, _owners, deals).values()) == {leaving.id}
