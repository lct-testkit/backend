"""Тесты модуля интеграций (спринт 9, new_spec §4.14/§7.8).

Как и в остальных тестах этого репозитория (см. `tests/test_signing.py`),
здесь нет поднятых PostgreSQL/Redis: покрываются чистые функции — проверка
HMAC-подписи (`integration.security`), маппинг полей Bitrix
(`integration.bitrix._deal_to_bitrix_fields`, сверен с официальной
документацией `crm.item.add`/`crm.item.update`), backoff-расписание
доставки outbox (`integration.tasks`), схемы-валидаторы
(`integration.schemas`) и права (`core.permissions`). Реальный приём
вебхука CMS (дедупликация, создание Contact/Deal), доставка outbox и
последовательность crm.item.add → crm.item.update против моков проверены
вживую в этой же сессии — того же типа верификация, что и для остального
DB/HTTP-слоя репозитория, просто не как pytest-тест.
"""

from __future__ import annotations

import datetime as dt
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pydantic import ValidationError

from app.core.permissions import Permission, has_permission
from app.modules.crm.models import Deal
from app.modules.identity.models import Role
from app.modules.integration.bitrix import (
    _DEAL_ENTITY_TYPE_ID,
    BitrixClient,
    _deal_to_bitrix_fields,
    _parse_bitrix_time,
    _reject_if_bitrix_moved_ahead,
)
from app.modules.integration.models import ExternalRef, IntegrationSourceCode, SyncDirection
from app.modules.integration.schemas import BitrixWebhookRequest, LmsProgressPushRequest
from app.modules.integration.security import compute_signature, resolve_secret, verify_signature
from app.modules.integration.tasks import _BACKOFF_SECONDS, _KNOWN_TARGETS, _MAX_ATTEMPTS


class TestVerifySignature:
    def test_valid_signature_with_prefix(self) -> None:
        body = b'{"lead_id": "42"}'
        secret = "top-secret"
        signature = f"sha256={compute_signature(secret, body)}"
        assert verify_signature(secret, body, signature) is True

    def test_valid_signature_without_prefix(self) -> None:
        body = b'{"lead_id": "42"}'
        secret = "top-secret"
        signature = compute_signature(secret, body)
        assert verify_signature(secret, body, signature) is True

    def test_wrong_secret_rejected(self) -> None:
        body = b'{"lead_id": "42"}'
        signature = f"sha256={compute_signature('right-secret', body)}"
        assert verify_signature("wrong-secret", body, signature) is False

    def test_tampered_body_rejected(self) -> None:
        secret = "top-secret"
        signature = f"sha256={compute_signature(secret, b'original')}"
        assert verify_signature(secret, b"tampered", signature) is False

    def test_missing_secret_never_passes(self) -> None:
        # Раздел 4.14/security.py докстринг: нет секрета — подпись
        # недействительна, а не «проверка пропущена».
        body = b"{}"
        signature = f"sha256={compute_signature('irrelevant', body)}"
        assert verify_signature(None, body, signature) is False

    def test_missing_header_rejected(self) -> None:
        assert verify_signature("secret", b"{}", None) is False

    def test_resolve_secret_reads_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_INTEGRATION_SECRET", "s3cr3t")
        assert resolve_secret("TEST_INTEGRATION_SECRET") == "s3cr3t"

    def test_resolve_secret_missing_ref_returns_none(self) -> None:
        assert resolve_secret(None) is None

    def test_resolve_secret_unset_env_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEST_INTEGRATION_SECRET_UNSET", raising=False)
        assert resolve_secret("TEST_INTEGRATION_SECRET_UNSET") is None


class TestOutboxBackoffSchedule:
    def test_matches_spec_literal_schedule(self) -> None:
        # Раздел 3.6, дословно: «1s, 5s, 30s, 5m, 30m, 2h».
        assert _BACKOFF_SECONDS == [1, 5, 30, 300, 1800, 7200]

    def test_dead_after_eight_attempts(self) -> None:
        assert _MAX_ATTEMPTS == 8

    def test_known_targets_are_lms_and_bitrix24(self) -> None:
        assert _KNOWN_TARGETS == frozenset({"lms", "bitrix24"})


class TestBitrixFieldMapping:
    """Сверено с официальной документацией crm.item.add/crm.item.update
    (entityTypeId=2 для сделки, camelCase-поля) — см. докстринг
    `integration.bitrix`."""

    def _deal(self, **overrides: object) -> Deal:
        deal = Deal(
            id=uuid.uuid4(),
            number="D-2026-000099",
            title="Продвижение курса «Python» МГУ",
            deal_type="b2b",
            amount=None,
            currency="RUB",
            source=None,
        )
        for key, value in overrides.items():
            setattr(deal, key, value)
        return deal

    def test_entity_type_id_is_deal(self) -> None:
        assert _DEAL_ENTITY_TYPE_ID == 2

    def test_uses_camel_case_fields_not_legacy_uppercase(self) -> None:
        fields = _deal_to_bitrix_fields(self._deal())
        assert "title" in fields
        assert "TITLE" not in fields
        assert "STAGE_ID" not in fields  # раздел «Что сознательно не отправляется»

    def test_does_not_invent_stage_or_assignee_ids(self) -> None:
        # Ни stageId, ни assignedById, ни companyId/contactId — это ID на
        # стороне портала, у нас для них нет таблицы соответствия.
        fields = _deal_to_bitrix_fields(self._deal())
        for forbidden in ("stageId", "assignedById", "companyId", "contactId"):
            assert forbidden not in fields

    def test_opportunity_and_currency_mapped(self) -> None:
        fields = _deal_to_bitrix_fields(self._deal(amount=150000))
        assert fields["opportunity"] == "150000"
        assert fields["currencyId"] == "RUB"

    def test_source_goes_to_description_not_source_id(self) -> None:
        fields = _deal_to_bitrix_fields(self._deal(source="cms"))
        assert fields["sourceId"] == "OTHER"
        assert "cms" in fields["sourceDescription"]
        assert fields["sourceDescription"].startswith("CRM #D-2026-000099")


class TestBitrixSecretRedaction:
    """Раздел «Аутентификация» докстринга `integration.bitrix`: секрет —
    весь URL вебхука целиком, без отдельного токена. `httpx.Response.
    raise_for_status()` вшивает `response.url` в текст `HTTPStatusError`
    (проверено на установленной версии httpx==0.28.1) — если бы
    `BitrixClient._call` пробрасывал `str(exc)` как есть, секрет утёк бы в
    `outbox_events.last_error` (`integration.tasks.sweep_outbox_events`) и
    оттуда — в ответ `GET /api/admin/integrations/outbox-events`
    (`integration.schemas.OutboxEventOut.last_error`). Здесь — не мок, а
    настоящий локальный HTTP-сервер, отвечающий 401: сценарий из аудита
    («вебхук отозван/переиздан → следующая доставка получает 401»)
    воспроизведён по-настоящему, а не подставлен."""

    @staticmethod
    def _serve(status: int, body: bytes) -> ThreadingHTTPServer:
        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 — имя метода фиксировано http.server
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args: object) -> None:
                pass  # не шуметь в тестовом выводе

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    async def test_401_response_does_not_leak_webhook_url_in_error(self) -> None:
        server = self._serve(401, b'{"error": "expired_token"}')
        secret_code = "SUPERSECRETWEBHOOKCODE12345"
        webhook_url = f"http://127.0.0.1:{server.server_address[1]}/rest/1/{secret_code}"
        try:
            client = BitrixClient(webhook_url)
            with pytest.raises(RuntimeError) as exc_info:
                await client.add_deal({"title": "x"})
        finally:
            server.shutdown()
            server.server_close()

        message = str(exc_info.value)
        assert secret_code not in message
        assert webhook_url not in message
        assert "127.0.0.1" not in message
        assert "401" in message

    async def test_connection_failure_does_not_leak_webhook_url_in_error(self) -> None:
        # Порт 9 (discard) в этом окружении уже используется как надёжный
        # «мгновенный отказ соединения» — см. tests/test_api_smoke.py.
        secret_code = "ANOTHERSECRETCODE99"
        webhook_url = f"http://127.0.0.1:9/rest/1/{secret_code}"
        client = BitrixClient(webhook_url)
        with pytest.raises(RuntimeError) as exc_info:
            await client.add_deal({"title": "x"})

        message = str(exc_info.value)
        assert secret_code not in message
        assert webhook_url not in message


class TestParseBitrixTime:
    def test_none_and_empty_return_none(self) -> None:
        assert _parse_bitrix_time(None) is None
        assert _parse_bitrix_time("") is None

    def test_parses_real_bitrix_iso_format(self) -> None:
        # Реальный формат `updatedTime` из crm.item.get (см. докстринг
        # `BitrixClient.get_deal`) — со смещением, не 'Z'.
        parsed = _parse_bitrix_time("2026-09-18T14:30:00+03:00")
        assert parsed == dt.datetime(
            2026, 9, 18, 14, 30, 0, tzinfo=dt.timezone(dt.timedelta(hours=3))
        )


class TestRejectIfBitrixMovedAhead:
    """Зеркало проверки `apply_inbound_change`'s stale-version отклонения
    (см. докстринг `integration.bitrix`), только для исходящего
    направления — push не должен вслепую затирать правку, сделанную прямо
    в Bitrix после нашей последней синхронизации. `crm.item.get` не имеет
    счётчика версий (только `updatedTime`/`createdTime`/`movedTime` — см.
    докстринг `BitrixClient.get_deal`), поэтому сравнение идёт по времени,
    а не по выдуманному полю."""

    class _FakeClient:
        def __init__(self, updated_time: str | None) -> None:
            self._updated_time = updated_time
            self.get_deal_calls = 0

        async def get_deal(self, bitrix_id: str) -> dict[str, object]:
            self.get_deal_calls += 1
            item: dict[str, object] = {"id": bitrix_id}
            if self._updated_time is not None:
                item["updatedTime"] = self._updated_time
            return {"result": {"item": item}}

    def _ref(self, last_synced_at: dt.datetime | None) -> ExternalRef:
        return ExternalRef(
            entity_type="deal",
            entity_id=uuid.uuid4(),
            source_code=IntegrationSourceCode.BITRIX24.value,
            external_id="42",
            synced_version=1,
            last_synced_at=last_synced_at,
            sync_direction=SyncDirection.OUTBOUND.value,
        )

    async def test_no_baseline_skips_check_without_calling_bitrix(self) -> None:
        client = self._FakeClient(updated_time="2026-09-18T14:30:00+03:00")
        ref = self._ref(last_synced_at=None)
        await _reject_if_bitrix_moved_ahead(client, ref)
        assert client.get_deal_calls == 0

    async def test_bitrix_unchanged_since_sync_does_not_raise(self) -> None:
        synced_at = dt.datetime(2026, 9, 18, 12, 0, 0, tzinfo=dt.UTC)
        client = self._FakeClient(updated_time="2026-09-18T12:00:00+00:00")
        ref = self._ref(last_synced_at=synced_at)
        await _reject_if_bitrix_moved_ahead(client, ref)  # не должно поднять исключение

    async def test_bitrix_changed_after_sync_raises_conflict(self) -> None:
        synced_at = dt.datetime(2026, 9, 18, 12, 0, 0, tzinfo=dt.UTC)
        client = self._FakeClient(updated_time="2026-09-18T15:00:00+00:00")
        ref = self._ref(last_synced_at=synced_at)
        with pytest.raises(RuntimeError, match="conflict"):
            await _reject_if_bitrix_moved_ahead(client, ref)

    async def test_missing_updated_time_in_response_does_not_raise(self) -> None:
        client = self._FakeClient(updated_time=None)
        ref = self._ref(last_synced_at=dt.datetime(2026, 9, 18, 12, 0, 0, tzinfo=dt.UTC))
        await _reject_if_bitrix_moved_ahead(client, ref)  # нечем доказать конфликт


class TestSchemas:
    def test_bitrix_webhook_requires_positive_version(self) -> None:
        with pytest.raises(ValidationError):
            BitrixWebhookRequest(external_id="1", bitrix_id="42", version=0, fields={})

    def test_bitrix_webhook_accepts_valid_payload(self) -> None:
        payload = BitrixWebhookRequest(
            external_id="evt-1", bitrix_id="42", version=2, fields={"title": "x"}
        )
        assert payload.version == 2

    def test_lms_progress_push_defaults_items_to_empty_list(self) -> None:
        payload = LmsProgressPushRequest(external_id="evt-1")
        assert payload.items == []


class TestIntegrationPermissions:
    def test_only_admin_has_integration_admin(self) -> None:
        assert has_permission(Role.ADMIN.value, Permission.INTEGRATION_ADMIN) is True
        for role in (Role.KAM.value, Role.HEAD.value, Role.AUDITOR.value):
            assert has_permission(role, Permission.INTEGRATION_ADMIN) is False

    def test_integration_role_has_ingest_and_deal_create(self) -> None:
        assert has_permission(Role.INTEGRATION.value, Permission.INTEGRATION_INGEST) is True
        assert has_permission(Role.INTEGRATION.value, Permission.DEAL_CREATE) is True
        assert has_permission(Role.INTEGRATION.value, Permission.DEAL_TRANSITION) is True

    def test_integration_role_cannot_administer_sources(self) -> None:
        assert has_permission(Role.INTEGRATION.value, Permission.INTEGRATION_ADMIN) is False
