"""Identity и Keycloak: воскрешение уволенных, привязка по неподтверждённому email, сессии,
политика паролей, обязательные действия, кэш прав после коммита, жёсткое удаление.

Найдено внешним тестированием. Каждый тест держит один дефект.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from app.core.errors import AppError, ErrorCode
from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run
from tests.crm_helpers import (
    create_deal,
    create_published_workflow,
    create_team,
    create_user,
    login,
    sign_in,
)

pytestmark_db = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


# --- Keycloak-клиент без сети ---------------------------------------------------------------


class _FakeKeycloak:
    """Подмена `admin_request`/`get_user`/`update_user` у настоящего клиента."""

    def __init__(self, required_actions: list[str] | None = None) -> None:
        from app.modules.identity.keycloak import KeycloakClient

        self.client = KeycloakClient()
        self.required = required_actions
        self.updates: list[dict] = []
        self.client.get_user = self._get_user  # type: ignore[method-assign]
        self.client.update_user = self._update_user  # type: ignore[method-assign]

    async def _get_user(self, keycloak_id: str):
        return {"id": keycloak_id, "requiredActions": list(self.required or [])}

    async def _update_user(self, keycloak_id: str, payload: dict) -> None:
        self.updates.append(payload)


class TestRequiredActionsAreMerged:
    async def test_reset_adds_update_password_and_keeps_totp(self) -> None:
        fake = _FakeKeycloak(["CONFIGURE_TOTP"])
        await fake.client.update_required_actions("kc-1", add=["UPDATE_PASSWORD"])
        assert fake.updates == [{"requiredActions": ["CONFIGURE_TOTP", "UPDATE_PASSWORD"]}]

    async def test_password_change_removes_only_update_password(self) -> None:
        fake = _FakeKeycloak(["CONFIGURE_TOTP", "UPDATE_PASSWORD"])
        await fake.client.update_required_actions("kc-1", remove=["UPDATE_PASSWORD"])
        assert fake.updates == [{"requiredActions": ["CONFIGURE_TOTP"]}]

    async def test_no_change_no_call(self) -> None:
        fake = _FakeKeycloak(["UPDATE_PASSWORD"])
        await fake.client.update_required_actions("kc-1", add=["UPDATE_PASSWORD"])
        assert fake.updates == []


class TestPasswordPolicyIsNot500:
    async def _client_answering(self, status: int, body: dict):
        from app.modules.identity.keycloak import KeycloakClient

        client = KeycloakClient()

        async def _request(method, path, *, json_body=None, params=None):
            return httpx.Response(status, json=body)

        client.admin_request = _request  # type: ignore[method-assign]
        return client

    async def test_policy_violation_is_a_422_with_the_policy_text(self) -> None:
        client = await self._client_answering(
            400,
            {"error": "invalid_password", "error_description": "Invalid password: min length 12"},
        )
        with pytest.raises(AppError) as caught:
            await client.set_password("kc-1", password="short")
        assert caught.value.code is ErrorCode.VALIDATION
        assert caught.value.status == 422
        assert "min length 12" in caught.value.detail
        assert "short" not in caught.value.detail

    async def test_keycloak_failure_is_a_503_not_a_500(self) -> None:
        client = await self._client_answering(500, {})
        with pytest.raises(AppError) as caught:
            await client.set_password("kc-1", password="whatever-long-password")
        assert caught.value.code is ErrorCode.DEPENDENCY_UNAVAILABLE


# --- Привязка по email -----------------------------------------------------------------------


async def _local_user_without_keycloak(email: str, role: str = "ADMIN"):
    from app.core.db import session_scope
    from app.modules.identity.models import User

    async with session_scope() as session:
        user = User(
            keycloak_id=None,
            email=email,
            full_name="Служебный",
            role=role,
            status="active",
            consent_version="1.0",
        )
        session.add(user)
        await session.flush()
        return user.id


async def _provision(email: str, verified: bool | None):
    from app.core.db import session_scope
    from app.core.security import TokenClaims
    from app.modules.identity.service import IdentityService

    raw: dict = {"sub": f"kc-{uuid.uuid4()}"}
    if verified is not None:
        raw["email_verified"] = verified
    claims = TokenClaims(
        subject=raw["sub"], raw=raw, email=email, full_name="Пришедший", roles=frozenset({"KAM"})
    )
    try:
        async with session_scope() as session:
            user = await IdentityService(session).provision_from_claims(claims)
            return ("bound", str(user.keycloak_id))
    except AppError as exc:
        return ("refused", exc.code)


@pytestmark_db
class TestEmailBinding:
    def test_unverified_email_does_not_take_over_a_local_account(self, client) -> None:
        email = f"{uuid.uuid4().hex[:10]}@rt-it-school.ru"
        run(client, _local_user_without_keycloak, email)

        for verified in (None, False):
            outcome = run(client, _provision, email, verified)
            assert outcome == ("refused", ErrorCode.FORBIDDEN), outcome

    def test_verified_email_binds_as_before(self, client) -> None:
        email = f"{uuid.uuid4().hex[:10]}@rt-it-school.ru"
        run(client, _local_user_without_keycloak, email, "KAM")

        outcome = run(client, _provision, email, True)

        assert outcome[0] == "bound"

    def test_service_records_are_never_bound(self, client) -> None:
        email = f"demo-{uuid.uuid4().hex[:8]}@system.local"
        run(client, _local_user_without_keycloak, email)

        outcome = run(client, _provision, email, True)

        assert outcome == ("refused", ErrorCode.FORBIDDEN), outcome

    def test_a_phantom_admin_is_not_counted_as_an_active_administrator(self, client) -> None:
        from app.core.db import session_scope
        from app.modules.identity.service import IdentityService

        async def _count() -> int:
            async with session_scope() as session:
                return await IdentityService(session).count_active_admins()

        before = run(client, _count)
        run(client, _local_user_without_keycloak, f"demo-{uuid.uuid4().hex[:8]}@system.local")

        assert run(client, _count) == before


# --- Блокировка уволенных ------------------------------------------------------------------


@pytestmark_db
class TestBlockDoesNotResurrect:
    @pytest.mark.parametrize("status", ["terminated", "anonymized"])
    def test_a_terminated_or_anonymized_user_cannot_be_blocked(self, client, status: str) -> None:
        login(client, "ADMIN")
        gone = create_user(client, "KAM", status=status)

        response = client.post(
            f"/api/admin/users/{gone.id}/block", json={"reason": "проверка блокировки"}
        )

        assert response.status_code == 422, response.text

    def test_an_anonymized_blocked_account_cannot_be_unblocked(self, client) -> None:
        from sqlalchemy import update

        from app.core.db import session_scope
        from app.modules.identity.models import User

        login(client, "ADMIN")
        ghost = create_user(client, "KAM", status="blocked")

        async def _mark() -> None:
            import datetime as dt

            async with session_scope() as session:
                await session.execute(
                    update(User)
                    .where(User.id == ghost.id)
                    .values(anonymized_at=dt.datetime.now(dt.UTC))
                )

        run(client, _mark)

        response = client.post(f"/api/admin/users/{ghost.id}/unblock", json={})

        assert response.status_code == 422, response.text


# --- Сессии ---------------------------------------------------------------------------------


@pytestmark_db
class TestSessionListDoesNotLeakCookies:
    def test_list_shows_a_reference_not_the_cookie_and_delete_accepts_it(self, client) -> None:
        user = login(client, "KAM")
        cookie = client.cookies.get("crm_sid")

        listed = client.get("/api/me/sessions")
        assert listed.status_code == 200, listed.text
        items = listed.json()["items"]
        assert items and all(item["sid"] != cookie for item in items)
        assert cookie not in listed.text
        current = next(item for item in items if item["is_current"])

        deleted = client.delete(f"/api/me/sessions/{current['sid']}")

        assert deleted.status_code == 200, deleted.text
        assert client.get("/api/me/sessions").status_code == 401  # сессия действительно погашена
        assert user is not None

    def test_a_foreign_session_reference_is_not_found(self, client) -> None:
        other = run(client, _make_user, "KAM")
        authenticate(client, other)
        other_ref = client.get("/api/me/sessions").json()["items"][0]["sid"]
        me = run(client, _make_user, "KAM")
        client.headers["X-CSRF-Token"] = authenticate(client, me)

        assert client.delete(f"/api/me/sessions/{other_ref}").status_code == 404
        assert client.delete(f"/api/me/sessions/{uuid.uuid4().hex}").status_code == 404

    def test_ending_a_session_also_ends_it_in_keycloak(self, client, monkeypatch) -> None:
        from app.modules.identity import router_me
        from app.modules.identity.session_store import session_store

        calls: list[str] = []

        async def _logout(refresh_token: str) -> None:
            calls.append(refresh_token)

        monkeypatch.setattr(router_me.keycloak_client, "logout", _logout)
        user = run(client, _make_user, "KAM")
        csrf = authenticate(client, user)
        client.headers["X-CSRF-Token"] = csrf

        async def _second_session() -> str:
            stored = await session_store.create(
                user_id=user.id,
                keycloak_id=user.keycloak_id,
                access_token="access",
                refresh_token="refresh-token-of-second",
                id_token=None,
                kc_session_state=None,
                ip="127.0.0.1",
                user_agent="pytest",
            )
            return stored.public_id

        second_ref = run(client, _second_session)

        response = client.delete(f"/api/me/sessions/{second_ref}")

        assert response.status_code == 200, response.text
        assert calls == ["refresh-token-of-second"]


# --- Флаг обязательной смены пароля -----------------------------------------------------------


@pytestmark_db
class TestPasswordRequirementSync:
    def _run(self, client, required, *, fail: bool = False) -> bool:
        from app.core.db import session_scope
        from app.modules.identity import router_auth
        from app.modules.identity.models import User

        user = run(client, _make_user, "KAM")

        async def _flag(monkeypatch_target) -> bool:
            async with session_scope() as session:
                stored = await session.get(User, user.id)
                stored.must_change_password = True

                async def _get_user(keycloak_id: str):
                    if fail:
                        raise RuntimeError("keycloak down")
                    return {"id": keycloak_id, "requiredActions": required}

                original = router_auth.keycloak_client.get_user
                router_auth.keycloak_client.get_user = _get_user  # type: ignore[method-assign]
                try:
                    await router_auth._sync_password_requirement(session, stored)
                finally:
                    router_auth.keycloak_client.get_user = original  # type: ignore[method-assign]
                return stored.must_change_password

        return run(client, _flag, None)

    def test_flag_is_cleared_when_keycloak_no_longer_requires_the_change(self, client) -> None:
        assert self._run(client, []) is False
        assert self._run(client, ["CONFIGURE_TOTP"]) is False

    def test_flag_stays_while_keycloak_still_requires_it(self, client) -> None:
        assert self._run(client, ["UPDATE_PASSWORD"]) is True

    def test_keycloak_outage_does_not_break_the_login(self, client) -> None:
        assert self._run(client, [], fail=True) is True


# --- Кэш прав после коммита ---------------------------------------------------------------------


@pytestmark_db
class TestPrincipalCacheDropsAfterCommit:
    def test_cache_stays_until_the_commit_and_survives_a_rollback(self, client) -> None:
        from app.core.cache import (
            CachedPrincipal,
            get_principal_cache,
            invalidate_principal_after_commit,
            set_principal_cache,
        )
        from app.core.db import session_scope

        entry = CachedPrincipal(
            user_id=str(uuid.uuid4()),
            keycloak_id=f"kc-{uuid.uuid4()}",
            role="KAM",
            status="active",
            email=None,
            full_name="Кэш",
            team_id=None,
            manager_id=None,
            perm_epoch=1,
            consent_version="1.0",
            must_change_password=False,
        )

        async def _scenario() -> tuple[bool, bool, bool]:
            await set_principal_cache(entry)
            async with session_scope() as session:
                invalidate_principal_after_commit(
                    session, entry.user_id, keycloak_id=entry.keycloak_id
                )
                inside = await get_principal_cache(entry.keycloak_id) is not None
            after_commit = await get_principal_cache(entry.keycloak_id) is None

            await set_principal_cache(entry)
            try:
                async with session_scope() as session:
                    invalidate_principal_after_commit(
                        session, entry.user_id, keycloak_id=entry.keycloak_id
                    )
                    raise RuntimeError("откат")
            except RuntimeError:
                pass
            survived = await get_principal_cache(entry.keycloak_id) is not None
            return inside, after_commit, survived

        inside, after_commit, survived = run(client, _scenario)

        assert inside, "до коммита кэш сбрасываться не должен"
        assert after_commit, "после коммита кэш должен быть сброшен"
        assert survived, "при откате прежние права остаются верными — кэш цел"


# --- Скоуп руководителя -----------------------------------------------------------------------


@pytestmark_db
class TestHeadScope:
    def test_head_without_users_team_id_sees_the_deals_of_the_team_he_leads(self, client) -> None:
        from app.core.db import session_scope
        from app.modules.identity.models import Team

        team = create_team(client)
        kam = create_user(client, "KAM", team_id=team)
        head = create_user(client, "HEAD")  # users.team_id не выставлен

        async def _lead() -> None:
            async with session_scope() as session:
                (await session.get(Team, team)).head_id = head.id  # type: ignore[union-attr]

        run(client, _lead)
        login(client, "ADMIN")
        graph = create_published_workflow(client)
        deal = create_deal(client, graph["workflow"]["id"], owner_id=str(kam.id))
        sign_in(client, head)

        card = client.get(f"/api/deals/{deal['id']}")
        assert card.status_code == 200, card.text
        listed = client.get("/api/deals", params={"limit": 100}).json()["items"]
        assert deal["id"] in {d["id"] for d in listed}

    def test_head_of_another_team_still_does_not(self, client) -> None:
        team = create_team(client)
        kam = create_user(client, "KAM", team_id=team)
        stranger = create_user(client, "HEAD", team_id=create_team(client))
        login(client, "ADMIN")
        graph = create_published_workflow(client)
        deal = create_deal(client, graph["workflow"]["id"], owner_id=str(kam.id))
        sign_in(client, stranger)

        assert client.get(f"/api/deals/{deal['id']}").status_code == 404


# --- Жёсткое удаление: зависимые записи ----------------------------------------------------


@pytestmark_db
class TestHardDeleteDependents:
    def test_dependents_are_found_generically(self, client) -> None:
        from app.core.db import session_scope
        from app.core.dependents import restrict_dependents

        owner = create_user(client, "KAM")
        clean = create_user(client, "KAM")
        login(client, "ADMIN")
        graph = create_published_workflow(client)
        create_deal(client, graph["workflow"]["id"], owner_id=str(owner.id))

        async def _find() -> tuple[list[str], list[str]]:
            async with session_scope() as session:
                busy = await restrict_dependents(session, "users", owner.id)
                free = await restrict_dependents(session, "users", clean.id)
                return [b["code"] for b in busy], [b["code"] for b in free]

        busy_codes, free_codes = run(client, _find)

        assert "dependent_deals" in busy_codes
        assert free_codes == []

    def test_organization_with_a_contact_is_not_eligible_for_hard_delete(self, client) -> None:
        from app.core.db import session_scope
        from app.modules.catalog.models import Contact, Organization
        from app.modules.catalog.service import OrganizationService

        async def _scenario() -> tuple[bool, bool]:
            async with session_scope() as session:
                lonely = Organization(name="ИП Одинокий", org_type="individual_entrepreneur")
                busy = Organization(name="ИП С контактом", org_type="individual_entrepreneur")
                session.add_all([lonely, busy])
                await session.flush()
                session.add(Contact(first_name="А", last_name="Б", organization_id=busy.id))
                await session.flush()
                service = OrganizationService(session)
                result = (
                    await service.hard_delete_eligible(lonely),
                    await service.hard_delete_eligible(busy),
                )
                await session.rollback()
                return result

        assert run(client, _scenario) == (True, False)


# --- Роль из устаревшего токена -----------------------------------------------------------------


async def _login_with_token(local_role: str, token_role: str, raw_extra: dict, perm_epoch: int):
    """Вход с токеном, роль в котором расходится с локальной; возвращает роль после входа."""
    from app.core.db import session_scope
    from app.core.security import TokenClaims
    from app.modules.identity.models import User
    from app.modules.identity.service import IdentityService

    subject = f"kc-{uuid.uuid4()}"
    async with session_scope() as session:
        user = User(
            keycloak_id=subject,
            email=f"{uuid.uuid4().hex[:10]}@rt-it-school.ru",
            full_name="Понижаемый",
            role=local_role,
            status="active",
            perm_epoch=perm_epoch,
            consent_version="1.0",
        )
        session.add(user)
        await session.flush()
        claims = TokenClaims(
            subject=subject,
            raw={"sub": subject, **raw_extra},
            roles=frozenset({token_role}),
        )
        signed_in = await IdentityService(session).provision_from_claims(claims)
        return signed_in.role


@pytestmark_db
class TestStaleTokenDoesNotRestoreARole:
    def test_token_with_an_older_epoch_cannot_restore_the_previous_role(self, client) -> None:
        role = run(client, _login_with_token, "KAM", "ADMIN", {"perm_epoch": 1}, 3)
        assert role == "KAM"

    def test_token_without_an_epoch_issued_before_the_change_is_ignored(self, client) -> None:
        import time

        role = run(client, _login_with_token, "KAM", "ADMIN", {"iat": int(time.time()) - 3600}, 2)
        assert role == "KAM"

    def test_a_fresh_token_still_syncs_the_role_from_keycloak(self, client) -> None:
        import time

        role = run(client, _login_with_token, "KAM", "HEAD", {"iat": int(time.time()) + 5}, 2)
        assert role == "HEAD"

    def test_epoch_claim_equal_to_the_local_one_syncs(self, client) -> None:
        role = run(client, _login_with_token, "KAM", "HEAD", {"perm_epoch": 2}, 2)
        assert role == "HEAD"


@pytestmark_db
class TestContactHardDeleteDependents:
    def test_contact_with_a_deal_is_not_eligible_and_a_lonely_one_is(self, client) -> None:
        from app.core.db import session_scope
        from app.modules.catalog.models import Contact
        from app.modules.catalog.service import ContactService
        from tests.crm_helpers import create_contact

        login(client, "ADMIN")
        graph = create_published_workflow(client, deal_type="b2c")
        busy_id = create_contact(client, last_name=f"Занятой{uuid.uuid4().hex[:6]}")
        free_id = create_contact(client, last_name=f"Свободный{uuid.uuid4().hex[:6]}")
        response = client.post(
            "/api/deals",
            json={
                "title": "Сделка контакта",
                "deal_type": "b2c",
                "workflow_id": graph["workflow"]["id"],
                "contact_id": busy_id,
            },
        )
        assert response.status_code == 201, response.text

        async def _check() -> tuple[bool, bool]:
            async with session_scope() as session:
                service = ContactService(session)
                busy = await session.get(Contact, uuid.UUID(busy_id))
                free = await session.get(Contact, uuid.UUID(free_id))
                return (
                    await service.hard_delete_eligible(busy),
                    await service.hard_delete_eligible(free),
                )

        assert run(client, _check) == (False, True)
