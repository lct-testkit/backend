"""B4-13 (C-21/I16): замок пароля общих демо-учёток и лечение фантомного ADMIN в сиде.

Сквозные тесты на настоящей PostgreSQL — тот же приём, что и у остальных
`tests/test_*` с фикстурой `client` (см. `tests/conftest.py`): `settings.
demo_locked_emails` действует только при `app_profile == "demo"`, поэтому
каждый тест включает профиль и список e-mail явным `monkeypatch`.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.config import get_settings
from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import login, sign_in

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


async def _user_with_email(email: str, *, role: str = "KAM", with_keycloak: bool = True):
    from app.core.db import session_scope
    from app.modules.identity.models import User

    async with session_scope() as session:
        user = User(
            keycloak_id=str(uuid.uuid4()) if with_keycloak else None,
            email=email,
            full_name="Демо-эксперт",
            role=role,
            status="active",
            consent_version="1.0",
        )
        session.add(user)
        await session.flush()
        session.expunge(user)
        return user


def _lock(monkeypatch, email: str) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "app_profile", "demo")
    monkeypatch.setattr(settings, "demo_locked_password_emails", email)


class TestSelfServicePasswordChange:
    def test_locked_demo_account_gets_403_without_touching_keycloak_or_the_rate_limit(
        self, client, monkeypatch
    ) -> None:
        from app.modules.identity import router_me

        email = f"demo-{uuid.uuid4().hex[:8]}@rt-it-school.ru"
        user = run(client, _user_with_email, email)
        _lock(monkeypatch, email)
        sign_in(client, user)

        def _must_not_be_called(*args, **kwargs):
            raise AssertionError("keycloak_client.password_grant не должен вызываться")

        monkeypatch.setattr(router_me.keycloak_client, "password_grant", _must_not_be_called)

        body = {
            "current_password": "whatever-Current-1",
            "new_password": "New-Password-12345",
            "new_password_repeat": "New-Password-12345",
        }
        limit = get_settings().password_change_max_attempts
        for attempt in range(limit + 2):
            response = client.post("/api/me/password", json=body)
            assert response.status_code == 403, (attempt, response.text)
            assert response.json()["code"] == "CRM-1108", (attempt, response.text)
        # Если бы проверка шла после rate_limit, (limit+2)-я попытка упёрлась бы в 429
        # (счётчик исчерпан) вместо стабильного 403 — счётчик попыток не расходуется.

    def test_other_user_is_not_affected_by_someone_elses_lock(self, client, monkeypatch) -> None:
        from app.core.errors import AppError, ErrorCode
        from app.modules.identity import router_me

        locked_email = f"demo-{uuid.uuid4().hex[:8]}@rt-it-school.ru"
        _lock(monkeypatch, locked_email)

        other = run(client, _user_with_email, f"other-{uuid.uuid4().hex[:8]}@rt-it-school.ru")
        sign_in(client, other)

        async def fake_wrong_password(*, username: str, password: str):
            raise AppError(ErrorCode.VALIDATION, "Текущий пароль неверен")

        monkeypatch.setattr(router_me.keycloak_client, "password_grant", fake_wrong_password)

        response = client.post(
            "/api/me/password",
            json={
                "current_password": "wrong",
                "new_password": "New-Password-12345",
                "new_password_repeat": "New-Password-12345",
            },
        )
        # Не 403 CRM-1108 — дошли до настоящей проверки пароля, как раньше.
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "CRM-1001", response.text

    def test_app_profile_prod_ignores_the_locked_list(self, client, monkeypatch) -> None:
        from app.core.errors import AppError, ErrorCode
        from app.modules.identity import router_me

        email = f"demo-{uuid.uuid4().hex[:8]}@rt-it-school.ru"
        user = run(client, _user_with_email, email)
        settings = get_settings()
        monkeypatch.setattr(settings, "app_profile", "prod")
        monkeypatch.setattr(settings, "demo_locked_password_emails", email)
        sign_in(client, user)

        async def fake_wrong_password(*, username: str, password: str):
            raise AppError(ErrorCode.VALIDATION, "Текущий пароль неверен")

        monkeypatch.setattr(router_me.keycloak_client, "password_grant", fake_wrong_password)

        response = client.post(
            "/api/me/password",
            json={
                "current_password": "wrong",
                "new_password": "New-Password-12345",
                "new_password_repeat": "New-Password-12345",
            },
        )
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "CRM-1001", response.text


class TestAdminResetPassword:
    def test_reset_on_a_locked_account_is_403_and_changes_nothing(
        self, client, monkeypatch
    ) -> None:
        from tests.conftest import _open_session

        login(client, "ADMIN")
        email = f"demo-{uuid.uuid4().hex[:8]}@rt-it-school.ru"
        target = run(client, _user_with_email, email)
        _lock(monkeypatch, email)
        sid = run(client, _open_session, target)

        response = client.post(
            f"/api/admin/users/{target.id}/reset-password",
            json={"reason": "проверка замка", "suspect_compromise": False},
        )
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "CRM-1108", response.text

        async def _check() -> tuple[bool, int]:
            from app.core.db import session_scope
            from app.modules.identity.models import User
            from app.modules.identity.session_store import session_store

            async with session_scope() as session:
                refreshed = await session.get(User, target.id)
                assert refreshed is not None
                sessions = await session_store.list_for_user(target.id)
                return refreshed.must_change_password, len(sessions)

        must_change, sessions_left = run(client, _check)
        assert must_change is False
        assert sessions_left == 1
        assert sid  # сессия действительно была открыта до попытки сброса


class TestMePasswordLocked:
    def test_password_locked_flag_follows_the_list(self, client, monkeypatch) -> None:
        email = f"demo-{uuid.uuid4().hex[:8]}@rt-it-school.ru"
        locked = run(client, _user_with_email, email)
        _lock(monkeypatch, email)
        sign_in(client, locked)

        response = client.get("/api/me")
        assert response.status_code == 200, response.text
        assert response.json()["password_locked"] is True

    def test_password_locked_is_false_for_everyone_else(self, client, monkeypatch) -> None:
        locked_email = f"demo-{uuid.uuid4().hex[:8]}@rt-it-school.ru"
        _lock(monkeypatch, locked_email)

        other = run(client, _user_with_email, f"other-{uuid.uuid4().hex[:8]}@rt-it-school.ru")
        sign_in(client, other)

        response = client.get("/api/me")
        assert response.status_code == 200, response.text
        assert response.json()["password_locked"] is False


class TestPhantomAdminRemediation:
    """Стенд общий и долгоживущий, поэтому каждый тест подменяет `_PHANTOM_ADMIN_EMAIL`
    на свой уникальный e-mail через `monkeypatch`: иначе второй прогон этого файла (или
    сид эпика 2) упал бы `UniqueViolationError`-ом на фиксированной константе, а
    «чистая база» для настоящей `demo-reports@system.local` здесь недостижима и
    непроверяема — как и глобальное «нет ADMIN без keycloak_id» (такие учётки заводят
    и другие тестовые модули, например `test_identity_hardening.py`)."""

    @staticmethod
    def _unique_email() -> str:
        return f"demo-reports-{uuid.uuid4().hex[:8]}@system.local"

    async def _create_phantom_with_job(self, email: str) -> tuple[uuid.UUID, uuid.UUID]:
        from app.core.db import session_scope
        from app.modules.identity.models import User
        from app.modules.reporting.models import ReportJob

        async with session_scope() as session:
            phantom = User(
                keycloak_id=None,
                email=email,
                full_name="Фантом",
                role="ADMIN",
                status="active",
                consent_version="1.0",
            )
            session.add(phantom)
            await session.flush()
            job = ReportJob(
                template_code="deals_export",
                format="xlsx",
                requested_by=phantom.id,
                status="completed",
            )
            session.add(job)
            await session.flush()
            return phantom.id, job.id

    async def _run_remediation(self) -> int:
        from app.core.db import session_scope
        from app.modules.reporting.seed import remediate_phantom_admin

        async with session_scope() as session:
            return await remediate_phantom_admin(session)

    async def _state(self, phantom_id: uuid.UUID, job_id: uuid.UUID):
        from app.core.db import session_scope
        from app.modules.identity.models import User
        from app.modules.reporting.models import ReportJob

        async with session_scope() as session:
            phantom = await session.get(User, phantom_id)
            job = await session.get(ReportJob, job_id)
            assert phantom is not None
            assert job is not None
            # Стенд общий: `seed_integration_account` возвращает ПЕРВУЮ учётку роли
            # INTEGRATION, какая уже есть в базе (их может быть несколько от других
            # тестовых модулей) — не обязательно с каноническим e-mail. Важно, что
            # задание переехало на КАКУЮ-ТО учётку этой роли, а не осталось на фантоме.
            new_owner = await session.get(User, job.requested_by)
            assert new_owner is not None
            return phantom.status, job.requested_by, new_owner.role

    def test_phantom_is_terminated_and_jobs_move_to_the_integration_account(
        self, client, monkeypatch
    ) -> None:
        from app.modules.reporting import seed as reporting_seed

        email = self._unique_email()
        monkeypatch.setattr(reporting_seed, "_PHANTOM_ADMIN_EMAIL", email)
        phantom_id, job_id = run(client, self._create_phantom_with_job, email)

        moved = run(client, self._run_remediation)
        assert moved == 1

        status, requested_by, new_owner_role = run(client, self._state, phantom_id, job_id)
        assert status == "terminated"
        assert requested_by != phantom_id
        assert new_owner_role == "INTEGRATION"

    def test_a_second_run_is_a_no_op(self, client, monkeypatch) -> None:
        from app.modules.reporting import seed as reporting_seed

        email = self._unique_email()
        monkeypatch.setattr(reporting_seed, "_PHANTOM_ADMIN_EMAIL", email)
        phantom_id, job_id = run(client, self._create_phantom_with_job, email)
        run(client, self._run_remediation)

        second_pass = run(client, self._run_remediation)
        assert second_pass == 0

        status, requested_by, new_owner_role = run(client, self._state, phantom_id, job_id)
        assert status == "terminated"
        assert requested_by != phantom_id
        assert new_owner_role == "INTEGRATION"

    def test_a_database_without_this_phantom_is_a_no_op(self, client, monkeypatch) -> None:
        # Уникальный e-mail этого теста гарантированно не встречается в базе — проверяем
        # именно ветку `phantom is None: return 0`, а не всю таблицу users.
        from app.modules.reporting import seed as reporting_seed

        monkeypatch.setattr(reporting_seed, "_PHANTOM_ADMIN_EMAIL", self._unique_email())

        moved = run(client, self._run_remediation)
        assert moved == 0
