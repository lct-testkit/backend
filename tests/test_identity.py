"""Тесты спринта identity.

Проверяется то, что ломается молча и дорого: обход CSRF, чужой
идемпотентный ответ, потерянный tie-breaker курсора, вечная сессия без
idle-таймаута, подтверждение «четырёх глаз» под другие параметры.

Тесты не требуют поднятых PostgreSQL, Redis и Keycloak.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.core.csrf import SAFE_METHODS, verify_csrf
from app.core.errors import AppError, ErrorCode
from app.core.idempotency import request_hash, scoped_key
from app.core.pagination import Cursor, keyset_after, keyset_before
from app.core.permissions import Permission, has_permission
from app.modules.identity.admin_service import ApprovalService
from app.modules.identity.models import User
from app.modules.identity.schemas import (
    OffboardRequest,
    PasswordChangeRequest,
    UserPatchRequest,
)
from app.modules.identity.session_store import SessionData, SessionStore


def _compile(condition) -> str:
    return str(
        condition.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


class TestKeysetPagination:
    def test_condition_is_real_row_comparison(self) -> None:
        # Питоновское `(a, b) < (x, y)` для колонок схлопывается в сравнение
        # только по первому элементу: `id` как tie-breaker молча теряется,
        # и строки с одинаковым created_at дублируются между страницами.
        cursor = Cursor(value=dt.datetime.now(dt.UTC), id=uuid.uuid4())
        sql = _compile(keyset_before(User.created_at, User.id, cursor))
        assert "(users.created_at, users.id) <" in sql
        assert "users.id" in sql

    def test_ascending_variant(self) -> None:
        cursor = Cursor(value=dt.datetime.now(dt.UTC), id=uuid.uuid4())
        assert "(users.created_at, users.id) >" in _compile(
            keyset_after(User.created_at, User.id, cursor)
        )

    def test_string_cursor_value_is_cast_to_timestamp(self) -> None:
        # Курсор едет в base64-JSON и возвращается строкой: без приведения
        # PostgreSQL сравнивал бы timestamptz с text.
        cursor = Cursor.decode(
            Cursor(value=dt.datetime(2026, 6, 1, 12, 30, tzinfo=dt.UTC), id=uuid.uuid4()).encode()
        )
        assert isinstance(cursor.value, str)
        sql = _compile(keyset_before(User.created_at, User.id, cursor))
        assert "2026-06-01 12:30:00+00:00" in sql

    def test_query_is_composable(self) -> None:
        cursor = Cursor(value=dt.datetime.now(dt.UTC), id=uuid.uuid4())
        stmt = select(User.id).where(keyset_before(User.created_at, User.id, cursor))
        assert "WHERE" in _compile(stmt)


class TestCsrf:
    def test_safe_methods_pass_without_token(self) -> None:
        for method in SAFE_METHODS:
            verify_csrf(method=method, cookie_value=None, header_value=None)

    def test_mutating_request_without_token_is_rejected(self) -> None:
        with pytest.raises(AppError) as exc:
            verify_csrf(method="POST", cookie_value="abc", header_value=None)
        assert exc.value.code == ErrorCode.CSRF_FAILED

    def test_mismatched_token_is_rejected(self) -> None:
        with pytest.raises(AppError) as exc:
            verify_csrf(method="DELETE", cookie_value="abc", header_value="def")
        assert exc.value.code == ErrorCode.CSRF_FAILED

    def test_matching_token_passes(self) -> None:
        verify_csrf(method="POST", cookie_value="abc", header_value="abc")


class TestIdempotencyScope:
    def test_key_is_scoped_per_actor(self) -> None:
        # Без скоупа угаданный чужой Idempotency-Key возвращал бы чужое
        # сохранённое тело ответа.
        first, second = uuid.uuid4(), uuid.uuid4()
        assert scoped_key("order-1", first) != scoped_key("order-1", second)

    def test_anonymous_scope_is_stable(self) -> None:
        assert scoped_key("k", None) == scoped_key("k", None)
        assert scoped_key("k", None).startswith("anonymous:")

    def test_request_hash_depends_on_body(self) -> None:
        base = request_hash("POST", "/api/deals", b'{"a":1}')
        assert base != request_hash("POST", "/api/deals", b'{"a":2}')
        assert base != request_hash("POST", "/api/other", b'{"a":1}')
        assert base == request_hash("post", "/api/deals", b'{"a":1}')


class TestSessionIdleTimeout:
    def _session(self, *, last_seen: dt.datetime) -> SessionData:
        return SessionData(
            sid="s",
            user_id=str(uuid.uuid4()),
            keycloak_id="kc",
            access_token="t",
            created_at=last_seen.isoformat(),
            last_seen_at=last_seen.isoformat(),
        )

    def test_fresh_session_is_alive(self) -> None:
        session = self._session(last_seen=dt.datetime.now(dt.UTC))
        assert not SessionStore._is_idle_expired(session)

    def test_idle_session_expires(self) -> None:
        # Забытая открытой вкладка не должна оставаться валидным доступом
        # все 12 часов абсолютного TTL (new_spec §3.1).
        stale = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
        assert SessionStore._is_idle_expired(self._session(last_seen=stale))

    def test_naive_timestamp_is_treated_as_utc(self) -> None:
        # Сессии, записанные старой версией без таймзоны, не должны
        # мгновенно считаться протухшими.
        session = self._session(last_seen=dt.datetime.now(dt.UTC))
        session.last_seen_at = dt.datetime.now(dt.UTC).replace(tzinfo=None).isoformat()
        assert not SessionStore._is_idle_expired(session)

    def test_get_does_not_recurse_into_delete(self) -> None:
        """`get` гасит простоявшую сессию, `delete` читает её через `get`.

        Если гашение сделать вызовом `delete`, пара уходит во взаимную
        рекурсию и запрос виснет до исчерпания стека.
        """
        import inspect

        source = inspect.getsource(SessionStore.get)
        assert "self.delete(" not in source
        assert "self._purge(" in source

    def test_public_view_has_no_tokens(self) -> None:
        view = self._session(last_seen=dt.datetime.now(dt.UTC)).public_view()
        assert "access_token" not in view and "refresh_token" not in view


class TestApprovalHash:
    def test_hash_binds_operation_and_payload(self) -> None:
        # Подтверждается конкретный набор параметров: «подтвердили одно,
        # выполнили другое» не должно проходить.
        payload = {"email": "a@rt.ru", "role": "ADMIN"}
        digest = ApprovalService.request_hash("user.create_admin", payload)
        assert digest == ApprovalService.request_hash(
            "user.create_admin", {"role": "ADMIN", "email": "a@rt.ru"}
        )
        assert digest != ApprovalService.request_hash(
            "user.create_admin", {"email": "b@rt.ru", "role": "ADMIN"}
        )
        assert digest != ApprovalService.request_hash("user.erasure", payload)


class TestSchemas:
    def test_password_repeat_must_match(self) -> None:
        with pytest.raises(ValueError):
            PasswordChangeRequest(
                current_password="Old-Password-1",
                new_password="New-Password-12",
                new_password_repeat="Other-Password-1",
            )

    def test_new_password_must_differ(self) -> None:
        with pytest.raises(ValueError):
            PasswordChangeRequest(
                current_password="Same-Password-12",
                new_password="Same-Password-12",
                new_password_repeat="Same-Password-12",
            )

    def test_password_minimum_length_matches_policy(self) -> None:
        # Политика realm: length(12).
        with pytest.raises(ValueError):
            PasswordChangeRequest(
                current_password="x", new_password="short", new_password_repeat="short"
            )

    def test_empty_patch_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            UserPatchRequest()

    def test_patch_accepts_single_field(self) -> None:
        assert UserPatchRequest(role="HEAD").role == "HEAD"

    def test_offboard_confirm_requires_successor_and_reason(self) -> None:
        with pytest.raises(ValueError):
            OffboardRequest(mode="confirm")
        with pytest.raises(ValueError):
            OffboardRequest(mode="confirm", successor_id=uuid.uuid4())
        assert (
            OffboardRequest(mode="confirm", successor_id=uuid.uuid4(), reason="увольнение").mode
            == "confirm"
        )

    def test_preview_needs_nothing(self) -> None:
        assert OffboardRequest().mode == "preview"


class TestPermissionMatrix:
    """Матрица прав из части 5 new_spec."""

    def test_only_admin_manages_users(self) -> None:
        for role in ("KAM", "HEAD", "AUDITOR", "INTEGRATION"):
            assert not has_permission(role, Permission.USER_WRITE)
        assert has_permission("ADMIN", Permission.USER_WRITE)

    def test_audit_export_is_admin_and_auditor_only(self) -> None:
        assert has_permission("AUDITOR", Permission.AUDIT_EXPORT)
        assert has_permission("ADMIN", Permission.AUDIT_EXPORT)
        # HEAD читает журнал по своей команде, но не выгружает его.
        assert not has_permission("HEAD", Permission.AUDIT_EXPORT)
        assert has_permission("HEAD", Permission.AUDIT_READ)

    def test_erasure_is_admin_only(self) -> None:
        for role in ("KAM", "HEAD", "AUDITOR", "INTEGRATION"):
            assert not has_permission(role, Permission.ERASURE_MANAGE)
        assert has_permission("ADMIN", Permission.ERASURE_MANAGE)

    def test_integration_has_no_ui_rights(self) -> None:
        assert not has_permission("INTEGRATION", Permission.DEAL_READ)
        assert not has_permission("INTEGRATION", Permission.REPORT_CREATE)
        assert has_permission("INTEGRATION", Permission.INTEGRATION_INGEST)
