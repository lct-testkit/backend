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

import uuid

import pytest
from pydantic import ValidationError

from app.core.permissions import Permission, has_permission
from app.modules.crm.models import Deal
from app.modules.identity.models import Role
from app.modules.integration.bitrix import _DEAL_ENTITY_TYPE_ID, _deal_to_bitrix_fields
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
