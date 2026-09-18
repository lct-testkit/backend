"""Тесты модуля уведомлений (спринт 7, spec.txt §5.7/§6.11, new_spec §7.7).

Как и `tests/test_registry.py`/`tests/test_signing.py`, здесь нет поднятых
PostgreSQL/Redis: покрываются чистые функции — рендеринг шаблонов
(`service.render_template`, реально запускает Jinja2), расчёт тихих часов
(`service._in_quiet_hours`), права (`core.permissions`) и содержимое сида
(`seed._DEFAULT_TEMPLATES`, чтобы каждый `TPL_*`, реально используемый в
7 модулях, имел хотя бы один активный шаблон — иначе тихо теряется текст
уведомления, см. докстринг `RealNotificationService._create_deliveries`).
Реальная запись `notifications`/`notification_deliveries`/эскалация
заблокированного получателя проверены вживую против настоящего Postgres в
этой же сессии (миграция 0008 применена, сид применён, 25 шаблонов
созданы) — того же типа верификация, что и для остального DB-слоя
репозитория, просто не как pytest-тест.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.core.permissions import Permission, has_permission
from app.modules.identity.models import Role
from app.modules.notification.models import UserNotificationPref
from app.modules.notification.seed import _DEFAULT_TEMPLATES
from app.modules.notification.service import (
    TPL_ACCOUNT_BLOCKED,
    TPL_ACCOUNT_UNBLOCKED,
    TPL_OFFBOARD_SUCCESSOR,
    TPL_PASSWORD_CHANGED,
    TPL_PASSWORD_RESET,
    TPL_ROLE_CHANGED,
    ChannelDeliveryError,
    LoggingChannelGateway,
    _in_quiet_hours,
    get_channel_gateway,
    render_template,
)


class TestRenderTemplate:
    def test_substitutes_payload_fields(self) -> None:
        body = render_template("Пароль изменён {{ changed_at }}", {"changed_at": "2026-09-18"})
        assert body == "Пароль изменён 2026-09-18"

    def test_missing_field_renders_empty_not_error(self) -> None:
        # payload реальных вызовов сильно варьируется (см. `_create_deliveries`
        # докстринг) — шаблон не должен падать на отсутствующем поле.
        body = render_template("IP: {{ ip }}.", {})
        assert body == "IP: ."

    def test_conditional_block_skips_absent_optional_field(self) -> None:
        body = render_template("Причина.{% if reason %} {{ reason }}{% endif %}", {})
        assert body == "Причина."
        body_with = render_template(
            "Причина.{% if reason %} {{ reason }}{% endif %}", {"reason": "истёк срок"}
        )
        assert body_with == "Причина. истёк срок"


class TestQuietHours:
    def _pref(self, start: dt.time | None, end: dt.time | None) -> UserNotificationPref:
        pref = UserNotificationPref(quiet_hours_start=start, quiet_hours_end=end)
        return pref

    def test_no_window_configured_never_quiet(self) -> None:
        assert _in_quiet_hours(self._pref(None, None), "Europe/Moscow") is False

    def test_same_day_window(self) -> None:
        pref = self._pref(dt.time(12, 0), dt.time(14, 0))
        assert _in_quiet_hours(pref, "Europe/Moscow", now_local=dt.time(13, 0)) is True

        pref_outside = self._pref(dt.time(22, 0), dt.time(23, 0))
        assert _in_quiet_hours(pref_outside, "Europe/Moscow", now_local=dt.time(13, 0)) is False

    def test_overnight_window_wraps_midnight(self) -> None:
        pref = self._pref(dt.time(22, 0), dt.time(8, 0))
        assert _in_quiet_hours(pref, "Europe/Moscow", now_local=dt.time(23, 30)) is True
        assert _in_quiet_hours(pref, "Europe/Moscow", now_local=dt.time(3, 0)) is True
        assert _in_quiet_hours(pref, "Europe/Moscow", now_local=dt.time(12, 0)) is False

    def test_unknown_timezone_falls_back_to_utc_instead_of_raising(self) -> None:
        pref = self._pref(dt.time(0, 0), dt.time(23, 59))
        # Без `now_local` реально идёт в `ZoneInfo(tz_name)` — не должно
        # бросать исключение даже с мусорной таймзоной.
        assert _in_quiet_hours(pref, "Not/ARealZone") in (True, False)


class TestChannelGateways:
    async def test_default_email_gateway_is_honest_stub_not_fake_success(self) -> None:
        gateway = get_channel_gateway("email")
        assert isinstance(gateway, LoggingChannelGateway)
        with pytest.raises(ChannelDeliveryError) as excinfo:
            await gateway.send(address_masked="i***@rt.ru", subject=None, body="x")
        assert excinfo.value.retryable is False

    async def test_default_telegram_gateway_is_also_a_stub(self) -> None:
        gateway = get_channel_gateway("telegram")
        assert isinstance(gateway, LoggingChannelGateway)
        with pytest.raises(ChannelDeliveryError):
            await gateway.send(address_masked=None, subject=None, body="x")

    def test_in_app_has_no_registered_gateway(self) -> None:
        # in_app не «отправляется» через шлюз — доставка это сама запись
        # `notifications` (см. `RealNotificationService._create_deliveries`).
        assert get_channel_gateway("in_app") is None


class TestSeedCoversAllReferencedTemplateCodes:
    """Каждый `TPL_*`, реально вызываемый в identity/crm/catalog/registry/
    signing (см. докстринг `service.py`), обязан иметь хотя бы один активный
    шаблон — иначе `notify_user` создаёт `notifications` без единой
    `notification_deliveries` и уведомление в API остаётся без текста."""

    @pytest.mark.parametrize(
        "code",
        [
            TPL_PASSWORD_CHANGED,
            TPL_PASSWORD_RESET,
            TPL_ACCOUNT_BLOCKED,
            TPL_ACCOUNT_UNBLOCKED,
            TPL_ROLE_CHANGED,
            TPL_OFFBOARD_SUCCESSOR,
            "SIGNATURE_REQUESTED",
            "SIGNATURE_DOCUMENT_SIGNED",
            "SIGNATURE_DOCUMENT_REJECTED",
            "SIGNATURE_DOCUMENT_EXPIRED",
            "SIGNATURE_OTP_LOCKED",
            "EDM_AGREEMENT_MISSING",
            "ORG_REQUISITES_DRIFT_DETECTED",
            "ORG_LIQUIDATION_DETECTED",
            "DEAL_SLA_WARNING",
            "DEAL_SLA_BREACHED",
            "ORG_DRIFT_APPLIED",
            "DEAL_REASSIGNED",
            "DEAL_EVENT",
        ],
    )
    def test_code_has_at_least_one_seeded_template(self, code: str) -> None:
        codes = {row[0] for row in _DEFAULT_TEMPLATES}
        assert code in codes

    def test_no_duplicate_code_channel_pairs(self) -> None:
        pairs = [(code, channel) for code, channel, _subject, _body in _DEFAULT_TEMPLATES]
        assert len(pairs) == len(set(pairs))

    def test_every_body_template_renders_without_error(self) -> None:
        for _code, _channel, _subject, body in _DEFAULT_TEMPLATES:
            # Пустой payload — худший случай (см. `TestRenderTemplate`).
            render_template(body, {})


class TestNotificationPermissions:
    def test_only_admin_manages_notification_templates(self) -> None:
        assert has_permission(Role.ADMIN.value, Permission.NOTIFICATION_TEMPLATE_MANAGE)
        assert not has_permission(Role.KAM.value, Permission.NOTIFICATION_TEMPLATE_MANAGE)
        assert not has_permission(Role.HEAD.value, Permission.NOTIFICATION_TEMPLATE_MANAGE)
        assert not has_permission(Role.AUDITOR.value, Permission.NOTIFICATION_TEMPLATE_MANAGE)
        assert not has_permission(Role.INTEGRATION.value, Permission.NOTIFICATION_TEMPLATE_MANAGE)
