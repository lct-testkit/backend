"""Тесты каркаса: соглашения API, на которые опираются все остальные спринты.

Проверяются вещи, которые ломаются молча: формат ошибок, UUIDv7, курсор,
маскирование ПДн и целостность хэш-цепочки аудита. Тесты не требуют
поднятых PostgreSQL, Redis и Keycloak.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError as PydanticValidationError
from starlette.requests import Request

from app.core.errors import ERROR_CATALOG, AppError, ErrorCode
from app.core.ids import is_uuid7, uuid7, uuid7_timestamp_ms
from app.core.masking import (
    mask_email,
    mask_inn,
    mask_mapping,
    mask_phone,
    pseudonym,
)
from app.core.pagination import MAX_LIMIT, Cursor, Page, PageParams
from app.core.permissions import Permission, deal_scope_for, has_permission, scopes_for
from app.core.problem import (
    ERROR_CODE_SCOPE_KEY,
    REQUEST_ID_SCOPE_KEY,
    build_problem,
    problem_response,
)
from app.modules.audit.service import GENESIS_HASH, compute_hash, diff_changes


class TestErrorCatalog:
    def test_every_code_has_spec(self) -> None:
        # Каждый код каталога должен иметь status и title, иначе Problem
        # Details соберётся с KeyError уже в рантайме.
        for code in ErrorCode:
            assert code in ERROR_CATALOG, f"нет спецификации для {code}"
            spec = ERROR_CATALOG[code]
            assert 200 <= spec.status <= 599
            assert spec.title

    def test_codes_follow_crm_format(self) -> None:
        for code in ErrorCode:
            assert code.value.startswith("CRM-")
            assert code.value[4:].isdigit()

    @pytest.mark.parametrize(
        ("code", "expected_status"),
        [
            (ErrorCode.VALIDATION, 422),
            (ErrorCode.VERSION_CONFLICT, 409),
            (ErrorCode.UNAUTHENTICATED, 401),
            (ErrorCode.FORBIDDEN, 403),
            (ErrorCode.FILE_TOO_LARGE, 413),
            (ErrorCode.LAST_ADMIN, 409),
        ],
    )
    def test_status_mapping(self, code: ErrorCode, expected_status: int) -> None:
        assert AppError(code).status == expected_status

    def test_app_error_carries_detail_and_extra(self) -> None:
        error = AppError(
            ErrorCode.ORGANIZATION_INN_EXISTS,
            "Организация уже создана",
            extra={"existing_id": "abc"},
        )
        assert error.code == ErrorCode.ORGANIZATION_INN_EXISTS
        assert error.detail == "Организация уже создана"
        assert error.extra["existing_id"] == "abc"


class TestProblemDetails:
    def _request(self) -> Request:
        return Request({"type": "http", "method": "GET", "path": "/api/test", "headers": []})

    def test_body_has_mandatory_fields(self) -> None:
        body = build_problem(
            code=ErrorCode.FORBIDDEN,
            status=403,
            title="Недостаточно прав",
            detail="нет права",
            instance="/api/test",
        )
        # Раздел 2: перечисленные поля обязательны в каждой ошибке.
        for field in ("type", "title", "status", "detail", "instance", "request_id", "code"):
            assert field in body
        assert body["code"] == "CRM-1102"
        assert body["type"].endswith("/crm-1102")  # type: ignore[union-attr]

    def test_code_lands_in_scope_for_metrics(self) -> None:
        # http_errors_total разбит по коду каталога, а middleware читает его
        # из scope. Без этой записи метрика деградирует до code="unknown".
        request = self._request()
        problem_response(
            code=ErrorCode.FORBIDDEN,
            detail="нет права",
            instance="/api/test",
            request=request,
        )
        assert request.scope[ERROR_CODE_SCOPE_KEY] == "CRM-1102"

    def test_request_id_survives_context_reset(self) -> None:
        # Обработчик 500 работает снаружи middleware, когда contextvars уже
        # сброшены. request_id должен браться из scope, иначе в теле будет
        # null — при том что detail просит сообщить его администратору.
        request = self._request()
        request.scope[REQUEST_ID_SCOPE_KEY] = "req-42"
        response = problem_response(
            code=ErrorCode.INTERNAL,
            detail="сломалось",
            instance="/api/test",
            request=request,
        )
        assert response.headers["X-Request-Id"] == "req-42"

    def test_extra_is_masked(self) -> None:
        body = build_problem(
            code=ErrorCode.VALIDATION,
            status=422,
            title="Ошибка валидации",
            detail="повтор",
            instance="/api/test",
            extra={"email": "ivanov@rt-it-school.ru"},
        )
        assert body["email"] == "i***@rt-it-school.ru"


class TestUuid7:
    def test_version_is_seven(self) -> None:
        assert is_uuid7(uuid7())

    def test_monotonic_within_same_millisecond(self) -> None:
        # Порядок важен: на нём держится курсорная пагинация.
        values = [uuid7() for _ in range(1000)]
        assert values == sorted(values)

    def test_unique(self) -> None:
        assert len({uuid7() for _ in range(5000)}) == 5000

    def test_timestamp_is_recoverable(self) -> None:
        import time

        before = time.time_ns() // 1_000_000
        value = uuid7()
        after = time.time_ns() // 1_000_000
        assert before <= uuid7_timestamp_ms(value) <= after


class TestMasking:
    def test_phone_format_matches_spec(self) -> None:
        # Спецификация задаёт ровно этот вид: +7 (9**) ***-**-12
        assert mask_phone("+79991234512") == "+7 (9**) ***-**-12"

    def test_email_format_matches_spec(self) -> None:
        assert mask_email("ivanov@domain.ru") == "i***@domain.ru"

    def test_inn_is_partially_hidden(self) -> None:
        masked = mask_inn("7707083893")
        assert masked.startswith("77")
        assert masked.endswith("93")
        assert "0708389" not in masked

    def test_pseudonym_is_stable_and_not_faceless(self) -> None:
        subject = uuid.uuid4()
        assert pseudonym(subject) == pseudonym(subject)
        assert pseudonym(subject).startswith("Пользователь #")

    def test_secrets_are_redacted(self) -> None:
        masked = mask_mapping(
            {
                "password": "hunter2",
                "access_token": "eyJhbGciOi",
                "phone": "+79991234512",
                "email": "ivanov@domain.ru",
                "nested": {"current_password": "x", "comment": "оставить как есть"},
            }
        )
        assert masked["password"] == "***"
        assert masked["access_token"] == "***"
        assert masked["phone"] == "+7 (9**) ***-**-12"
        assert masked["email"] == "i***@domain.ru"
        assert masked["nested"]["current_password"] == "***"
        assert masked["nested"]["comment"] == "оставить как есть"


class TestPagination:
    def test_cursor_roundtrip(self) -> None:
        cursor = Cursor(value="2026-06-01T12:30:00+00:00", id=uuid7())
        decoded = Cursor.decode(cursor.encode())
        assert decoded.id == cursor.id
        assert decoded.value == cursor.value

    def test_cursor_is_opaque(self) -> None:
        # Клиент не должен читать курсор: внутри base64, а не сырой UUID.
        cursor = Cursor(value="2026-06-01T12:30:00+00:00", id=uuid7())
        assert str(cursor.id) not in cursor.encode()

    def test_broken_cursor_is_validation_error(self) -> None:
        with pytest.raises(AppError) as exc:
            Cursor.decode("не-курсор!!")
        assert exc.value.code == ErrorCode.VALIDATION

    def test_limit_is_capped(self) -> None:
        with pytest.raises(PydanticValidationError):
            PageParams(limit=MAX_LIMIT + 1)

    def test_page_build_truncates_and_sets_cursor(self) -> None:
        import datetime as dt

        class Row:
            def __init__(self) -> None:
                self.id = uuid7()
                self.created_at = dt.datetime.now(dt.UTC)

        rows = [Row() for _ in range(6)]
        page: Page = Page.build(rows, limit=5)
        assert len(page.items) == 5
        assert page.next_cursor is not None

    def test_page_without_more_rows_has_null_cursor(self) -> None:
        import datetime as dt

        class Row:
            def __init__(self) -> None:
                self.id = uuid7()
                self.created_at = dt.datetime.now(dt.UTC)

        page: Page = Page.build([Row() for _ in range(3)], limit=5)
        assert page.next_cursor is None


class TestPermissions:
    def test_kam_cannot_manage_users(self) -> None:
        assert not has_permission("KAM", Permission.USER_WRITE)
        assert has_permission("KAM", Permission.DEAL_CREATE)

    def test_admin_has_everything(self) -> None:
        for permission in Permission:
            assert has_permission("ADMIN", permission)

    def test_auditor_has_no_deal_access(self) -> None:
        # По умолчанию аудитор видит только журнал, без сделок и ПДн.
        assert not has_permission("AUDITOR", Permission.DEAL_READ)
        assert not has_permission("AUDITOR", Permission.CONTACT_REVEAL)
        assert has_permission("AUDITOR", Permission.AUDIT_READ)

    def test_head_inherits_kam_rights(self) -> None:
        for permission in (Permission.DEAL_CREATE, Permission.DEAL_TRANSITION):
            assert has_permission("HEAD", permission)
        assert has_permission("HEAD", Permission.DEAL_REASSIGN)

    def test_scope_per_role(self) -> None:
        assert deal_scope_for("KAM").value == "own"
        assert deal_scope_for("HEAD").value == "team"
        assert deal_scope_for("ADMIN").value == "all"
        assert deal_scope_for("AUDITOR").value == "none"
        assert deal_scope_for("INTEGRATION").value == "source"

    def test_scopes_are_sorted_strings(self) -> None:
        scopes = scopes_for("KAM")
        assert scopes == sorted(scopes)
        assert all(isinstance(s, str) for s in scopes)


class TestAuditHashChain:
    def _entry(self, prev: str | None, action: str = "DEAL_CREATED") -> str:
        return compute_hash(
            prev_hash=prev,
            created_at="2026-06-01T12:30:00+00:00",
            actor_id="11111111-1111-1111-1111-111111111111",
            action=action,
            entity_type="deal",
            entity_id="22222222-2222-2222-2222-222222222222",
            changes={"title": {"old": None, "new": "Сделка"}},
            result="success",
            request_id="req-1",
        )

    def test_hash_is_deterministic(self) -> None:
        assert self._entry(None) == self._entry(None)

    def test_hash_depends_on_previous(self) -> None:
        # Иначе вырезание записи из середины не было бы обнаружимым.
        assert self._entry(GENESIS_HASH) != self._entry("a" * 64)

    def test_hash_depends_on_payload(self) -> None:
        assert self._entry(None, "DEAL_CREATED") != self._entry(None, "DEAL_UPDATED")

    def test_hash_is_sha256_hex(self) -> None:
        digest = self._entry(None)
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)


class TestAuditDiff:
    def test_only_changed_fields_are_recorded(self) -> None:
        changes = diff_changes(
            {"title": "Старое", "amount": "100.00"},
            {"title": "Новое", "amount": "100.00"},
        )
        assert changes == {"title": {"old": "Старое", "new": "Новое"}}

    def test_sensitive_fields_are_masked_in_diff(self) -> None:
        changes = diff_changes({"phone": "+79991234512"}, {"phone": "+79995556677"})
        assert changes["phone"]["old"] == "+7 (9**) ***-**-12"
        assert changes["phone"]["new"] == "+7 (9**) ***-**-77"
