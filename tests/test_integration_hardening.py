"""Интеграции: порядок проверок вебхуков, метка времени, адреса внешних систем, outbox без дублей.

Чистые проверки идут без БД; сквозные — на PostgreSQL (`TEST_DATABASE_URL`), сеть подменена.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import functools
import hashlib
import hmac
import json
import random
import time
import uuid
from types import SimpleNamespace

import httpx
import pytest

from app.modules.integration.security import (
    check_outbound_url,
    check_outbound_url_resolved,
    same_host,
    signing_secret_ref,
    verify_signature,
)
from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

needs_db = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

SECRET = "hardening-secret"


def _sig(body: bytes, secret: str = SECRET, ts: str | None = None) -> str:
    signed = body if ts is None else ts.encode() + b"." + body
    return "sha256=" + hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()


class TestSignatureWithTimestamp:
    BODY = b'{"a": 1}'

    def test_without_a_timestamp_the_old_scheme_still_works(self) -> None:
        assert verify_signature(SECRET, self.BODY, _sig(self.BODY))

    def test_a_fresh_timestamp_is_part_of_the_signed_message(self) -> None:
        now = int(time.time())
        assert verify_signature(SECRET, self.BODY, _sig(self.BODY, ts=str(now)), timestamp=str(now))

    def test_a_signature_over_the_body_only_is_refused_when_a_timestamp_is_given(self) -> None:
        now = str(int(time.time()))
        assert not verify_signature(SECRET, self.BODY, _sig(self.BODY), timestamp=now)

    def test_a_timestamp_cannot_be_swapped_after_signing(self) -> None:
        now = int(time.time())
        signature = _sig(self.BODY, ts=str(now))
        assert not verify_signature(SECRET, self.BODY, signature, timestamp=str(now + 1))

    @pytest.mark.parametrize("shift", [-301, 301, -86400, 86400])
    def test_a_stale_or_future_timestamp_is_refused(self, shift: int) -> None:
        stamp = str(int(time.time()) + shift)
        assert not verify_signature(SECRET, self.BODY, _sig(self.BODY, ts=stamp), timestamp=stamp)

    def test_the_window_edges_are_accepted(self) -> None:
        moment = 1_800_000_000.0
        for shift in (-299, 299):
            stamp = str(int(moment) + shift)
            assert verify_signature(
                SECRET, self.BODY, _sig(self.BODY, ts=stamp), timestamp=stamp, now=moment
            )

    @pytest.mark.parametrize("stamp", ["", "abc", "1.5", "12 34", "١٢٣"])
    def test_a_garbage_timestamp_is_refused(self, stamp: str) -> None:
        assert not verify_signature(SECRET, self.BODY, _sig(self.BODY), timestamp=stamp)

    def test_a_source_can_insist_on_a_timestamp(self) -> None:
        assert not verify_signature(SECRET, self.BODY, _sig(self.BODY), require_timestamp=True)
        now = str(int(time.time()))
        assert verify_signature(
            SECRET, self.BODY, _sig(self.BODY, ts=now), timestamp=now, require_timestamp=True
        )


class TestSigningSecretRef:
    def test_the_dedicated_ref_wins_over_credentials_ref(self) -> None:
        source = SimpleNamespace(credentials_ref="URL_VAR", config={"signing_secret_ref": " KEY "})
        assert signing_secret_ref(source) == "KEY"

    def test_credentials_ref_and_then_the_fallback_are_used_without_it(self) -> None:
        assert (
            signing_secret_ref(SimpleNamespace(credentials_ref="URL_VAR", config={})) == "URL_VAR"
        )
        assert signing_secret_ref(SimpleNamespace(credentials_ref=None, config={}), "ENV") == "ENV"
        assert signing_secret_ref(SimpleNamespace(credentials_ref=None, config=None)) is None
        assert (
            signing_secret_ref(
                SimpleNamespace(credentials_ref="X", config={"signing_secret_ref": 5})
            )
            == "X"
        )


class TestOutboundUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://lms.example.org",
            "https://lms.example.org/api/v1",
            "http://lms.example.org:8080",
            "  https://lms.example.org  ",
            "http://lms-mock:8080",
            "https://93.184.216.34/",
        ],
    )
    def test_ordinary_addresses_pass(self, url: str) -> None:
        assert check_outbound_url(url, strict="lms-mock" not in url)

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "   ",
            "ftp://lms.example.org",
            "file:///etc/passwd",
            "gopher://lms.example.org",
            "javascript:alert(1)",
            "lms.example.org",
            "//lms.example.org",
            "https://user:secret@lms.example.org",
            "https://user@lms.example.org",
            "https://:secret@lms.example.org",
            "http://169.254.169.254/latest/meta-data",
            "http://[fe80::1]/",
            "http://0.0.0.0/",
            "http://224.0.0.1/",
            "https://lms.example.org/a b",
            "https://lms.example.org/\r\nHost: x",
            "http://lms.example.org:0",
            "http://lms.example.org:99999",
            "https:///path",
            "https://" + "a" * 600 + ".example.org",
        ],
    )
    def test_dangerous_or_malformed_addresses_are_refused_everywhere(self, url: str) -> None:
        with pytest.raises(ValueError):
            check_outbound_url(url, strict=False)

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/",
            "http://127.0.0.1:9000/",
            "http://10.0.0.5/",
            "http://192.168.1.10:8080",
            "http://172.16.0.1/",
            "http://100.64.0.1/",
            "http://[::1]/",
            "http://[::ffff:127.0.0.1]/",
            "http://[fc00::1]/",
            "http://localhost:8080",
            "http://LOCALHOST./",
            "http://svc.internal/",
            "http://printer.local/",
            "http://admin.localhost/",
        ],
    )
    def test_internal_addresses_are_refused_in_the_strict_profile(self, url: str) -> None:
        with pytest.raises(ValueError):
            check_outbound_url(url, strict=True)

    @pytest.mark.parametrize(
        "url",
        ["http://127.0.0.1:9000/", "http://10.0.0.5/", "http://localhost:8080", "http://svc/"],
    )
    def test_dev_and_demo_may_use_internal_addresses(self, url: str) -> None:
        assert check_outbound_url(url, strict=False)

    def test_a_name_that_resolves_to_a_private_address_is_refused(self, monkeypatch) -> None:
        import socket

        from app.modules.integration import security

        def resolves_to(address: str):
            def getaddrinfo(host, port, family=0, type=0, *args):
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

            return getaddrinfo

        monkeypatch.setattr(security.socket, "getaddrinfo", resolves_to("127.0.0.1"))
        with pytest.raises(ValueError):
            asyncio.run(check_outbound_url_resolved("https://sneaky.example.org", strict=True))

        monkeypatch.setattr(security.socket, "getaddrinfo", resolves_to("93.184.216.34"))
        assert asyncio.run(check_outbound_url_resolved("https://fine.example.org", strict=True))

    def test_an_unresolvable_name_is_refused_in_the_strict_profile_only(self, monkeypatch) -> None:
        from app.modules.integration import security

        def failing(*args, **kwargs):
            raise OSError("no such host")

        monkeypatch.setattr(security.socket, "getaddrinfo", failing)
        with pytest.raises(ValueError):
            asyncio.run(check_outbound_url_resolved("https://nowhere.example.org", strict=True))
        # Вне prod имя не резолвится вовсе (docker-имена, `.invalid` в тестах).
        assert asyncio.run(check_outbound_url_resolved("https://nowhere.example.org", strict=False))

    def test_same_host_compares_host_and_effective_port(self) -> None:
        assert same_host("https://LMS.example.org/api", "https://lms.example.org:443/x")
        assert same_host("http://lms.example.org/a", "http://lms.example.org:80")
        assert not same_host("https://lms.example.org", "https://other.example.org")
        assert not same_host("https://lms.example.org:8443", "https://lms.example.org")
        assert not same_host("https://lms.example.org", "http://lms.example.org")
        assert not same_host("", "https://lms.example.org")


# --- сквозные: вебхуки ---------------------------------------------------------------------


async def _configure(code: str, *, ref: str, config: dict | None = None) -> None:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.integration.models import IntegrationSource

    async with session_scope() as session:
        source = (
            await session.execute(select(IntegrationSource).where(IntegrationSource.code == code))
        ).scalar_one_or_none()
        if source is None:
            source = IntegrationSource(code=code, name=code)
            session.add(source)
        source.is_active = True
        source.credentials_ref = ref
        source.config = config or {}


async def _reset_sources() -> None:
    """Настройки источников — общие для всех тестов БД: `config` (метка времени, свой ключ
    подписи) не должен пережить тест, иначе вебхуки в чужих тестах начнут отвечать 401."""
    from sqlalchemy import update

    from app.core.db import session_scope
    from app.modules.integration.models import IntegrationSource

    async with session_scope() as session:
        await session.execute(update(IntegrationSource).values(config={}))


async def _messages(source_code: str, external_id: str | None = None) -> list[dict]:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.integration.models import InboundMessage

    stmt = select(InboundMessage).where(InboundMessage.source_code == source_code)
    if external_id is not None:
        stmt = stmt.where(InboundMessage.external_id == external_id)
    async with session_scope() as session:
        rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "id": row.id,
                "external_id": row.external_id,
                "status": row.status,
                "signature_valid": row.signature_valid,
                "error": row.error,
            }
            for row in rows
        ]


@needs_db
class TestPushWebhooks:
    ENV = "HARDENING_PUSH_SECRET"
    LMS = "/api/v1/integrations/lms/progress"
    BITRIX = "/api/v1/integrations/bitrix/webhook"

    @pytest.fixture(autouse=True)
    def _env(self, client, monkeypatch):
        monkeypatch.setenv(self.ENV, SECRET)
        self.client = client
        run(client, _reset_sources)
        for code in ("lms", "bitrix24"):
            run(client, functools.partial(_configure, code, ref=self.ENV))
        yield
        run(client, _reset_sources)

    def _lms_body(self, key: str) -> bytes:
        return json.dumps({"external_id": key, "items": []}).encode()

    def _bitrix_body(self, key: str) -> bytes:
        return json.dumps(
            {"external_id": key, "bitrix_id": f"b-{uuid.uuid4().hex}", "version": 1, "fields": {}}
        ).encode()

    def _post(self, path: str, body: bytes, signature: str | None, **headers):
        all_headers = {"Content-Type": "application/json", **headers}
        if signature is not None:
            all_headers["X-Signature"] = signature
        return self.client.post(path, content=body, headers=all_headers)

    @pytest.mark.parametrize("path", [LMS, BITRIX])
    def test_a_wrong_signature_does_not_take_the_senders_key(self, path: str) -> None:
        key = f"delivery-{uuid.uuid4().hex}"
        body = self._lms_body(key) if path == self.LMS else self._bitrix_body(key)
        source = "lms" if path == self.LMS else "bitrix24"

        forged = self._post(path, body, _sig(body, secret="wrong"))
        real = self._post(path, body, _sig(body))

        assert forged.status_code == 401, forged.text
        assert real.status_code == 200, real.text
        assert real.json()["status"] in ("processed", "duplicate")
        stored = run(self.client, functools.partial(_messages, source, key))
        assert [m["status"] for m in stored] in (["processed"], ["duplicate"])
        # Улика неверной подписи осталась — под собственным ключом.
        evidence = [
            m
            for m in run(self.client, functools.partial(_messages, source))
            if m["external_id"].startswith("invalid:") and not m["signature_valid"]
        ]
        assert evidence

    @pytest.mark.parametrize("path", [LMS, BITRIX])
    def test_a_missing_signature_is_refused(self, path: str) -> None:
        body = self._lms_body("x") if path == self.LMS else self._bitrix_body("x")

        assert self._post(path, body, None).status_code == 401

    @pytest.mark.parametrize("path", [LMS, BITRIX])
    def test_a_repeated_delivery_returns_the_first_result(self, path: str) -> None:
        key = f"delivery-{uuid.uuid4().hex}"
        body = self._lms_body(key) if path == self.LMS else self._bitrix_body(key)

        first = self._post(path, body, _sig(body))
        second = self._post(path, body, _sig(body))

        assert first.status_code == second.status_code == 200
        (stored,) = run(
            self.client,
            functools.partial(_messages, "lms" if path == self.LMS else "bitrix24", key),
        )
        assert second.json()["inbound_message_id"] == str(stored["id"])

    def test_a_failed_record_under_the_key_is_retried_not_returned(self) -> None:
        """Запись `failed` под ключом (её создавала прежняя логика чужим запросом) — не итог."""
        from app.core.db import session_scope
        from app.modules.integration.models import InboundMessage

        key = f"delivery-{uuid.uuid4().hex}"

        async def _legacy() -> uuid.UUID:
            async with session_scope() as session:
                message = InboundMessage(
                    source_code="lms",
                    external_id=key,
                    message_type="progress_push",
                    raw_payload={},
                    signature_valid=False,
                    status="failed",
                    error="invalid_signature",
                )
                session.add(message)
                await session.flush()
                return message.id

        legacy_id = run(self.client, _legacy)
        body = self._lms_body(key)

        response = self._post(self.LMS, body, _sig(body))

        assert response.status_code == 200, response.text
        (stored,) = run(self.client, functools.partial(_messages, "lms", key))
        assert stored["id"] == legacy_id
        assert (stored["status"], stored["signature_valid"], stored["error"]) == (
            "processed",
            True,
            None,
        )

    def test_a_body_over_the_limit_is_refused_before_the_signature(self) -> None:
        big = b'{"external_id": "x", "items": [' + b'{"a": 1},' * 300_000 + b"{}]}"

        response = self._post(self.LMS, big, _sig(big))

        assert response.status_code == 413, response.status_code

    def test_a_body_that_does_not_match_the_schema_is_kept_as_evidence(self) -> None:
        body = b'{"items": []}'  # нет external_id

        response = self._post(self.LMS, body, _sig(body))

        assert response.status_code == 422, response.text
        assert any(
            m["external_id"].startswith("malformed:")
            for m in run(self.client, functools.partial(_messages, "lms"))
        )

    def test_a_fresh_timestamp_is_accepted_and_a_stale_one_is_not(self) -> None:
        body = self._lms_body(f"delivery-{uuid.uuid4().hex}")
        now = str(int(time.time()))
        old = str(int(time.time()) - 3600)

        ok = self._post(self.LMS, body, _sig(body, ts=now), **{"X-Timestamp": now})
        stale = self._post(self.LMS, body, _sig(body, ts=old), **{"X-Timestamp": old})
        mismatch = self._post(self.LMS, body, _sig(body), **{"X-Timestamp": now})

        assert ok.status_code == 200, ok.text
        assert stale.status_code == 401
        assert mismatch.status_code == 401

    def test_a_source_that_requires_a_timestamp_refuses_plain_signatures(self) -> None:
        run(
            self.client,
            functools.partial(_configure, "lms", ref=self.ENV, config={"require_timestamp": True}),
        )
        body = self._lms_body(f"delivery-{uuid.uuid4().hex}")
        now = str(int(time.time()))

        plain = self._post(self.LMS, body, _sig(body))
        stamped = self._post(self.LMS, body, _sig(body, ts=now), **{"X-Timestamp": now})

        assert plain.status_code == 401
        assert stamped.status_code == 200, stamped.text

    def test_the_signing_secret_can_live_in_its_own_variable(self, monkeypatch) -> None:
        monkeypatch.setenv("HARDENING_SIGNING_ONLY", "another-secret")
        run(
            self.client,
            functools.partial(
                _configure,
                "bitrix24",
                ref="HARDENING_WEBHOOK_URL",  # секрет-адрес исходящего вызова, не ключ подписи
                config={"signing_secret_ref": "HARDENING_SIGNING_ONLY"},
            ),
        )
        body = self._bitrix_body(f"delivery-{uuid.uuid4().hex}")

        ok = self._post(self.BITRIX, body, _sig(body, secret="another-secret"))
        old_way = self._post(self.BITRIX, body, _sig(body))

        assert ok.status_code == 200, ok.text
        assert old_way.status_code == 401

    def test_the_cms_webhook_honours_the_timestamp_too(self, monkeypatch) -> None:
        run(
            self.client,
            functools.partial(_configure, "cms", ref=self.ENV, config={"require_timestamp": True}),
        )
        body = json.dumps({"email": "x@example.ru", "first_name": "Ы", "last_name": "Ы"}).encode()

        response = self.client.post(
            "/api/v1/integrations/cms/leads",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Signature": _sig(body),
                "Idempotency-Key": f"k-{uuid.uuid4().hex}",
            },
        )

        assert response.status_code == 401, response.text


# --- сквозные: PATCH источника -------------------------------------------------------------


@needs_db
class TestSourceUpdate:
    @pytest.fixture(autouse=True)
    def _admin(self, client):
        self.client = client
        client.headers["X-CSRF-Token"] = authenticate(client, run(client, _make_user, "ADMIN"))
        run(client, functools.partial(_configure, "lms", ref="LMS_ENV_X"))
        yield

        async def _clear_url() -> None:
            from sqlalchemy import update

            from app.core.db import session_scope
            from app.modules.integration.models import IntegrationSource

            async with session_scope() as session:
                await session.execute(
                    update(IntegrationSource)
                    .where(IntegrationSource.code == "lms")
                    .values(base_url=None)
                )

        run(client, _clear_url)

    def _patch(self, **body):
        return self.client.patch("/api/admin/integrations/sources/lms", json=body)

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "ftp://lms.example.org",
            "https://user:pw@lms.example.org",
            "http://169.254.169.254/latest/meta-data",
            "http://lms.example.org/a b",
        ],
    )
    def test_a_dangerous_base_url_is_refused(self, url: str) -> None:
        response = self._patch(base_url=url)

        assert response.status_code == 422, response.text
        assert response.json()["errors"][0]["field"] == "base_url"

    def test_an_ordinary_base_url_is_saved(self) -> None:
        response = self._patch(base_url="https://lms.example.org/api")

        assert response.status_code == 200, response.text
        assert response.json()["base_url"] == "https://lms.example.org/api"

    def test_a_private_address_is_refused_only_in_the_prod_profile(self, monkeypatch) -> None:
        from app.core.config import get_settings

        assert self._patch(base_url="http://10.1.2.3:8080").status_code == 200
        monkeypatch.setattr(get_settings(), "app_profile", "prod")

        response = self._patch(base_url="http://10.1.2.4:8080")

        assert response.status_code == 422, response.text

    def test_clearing_the_url_is_allowed(self) -> None:
        assert self._patch(base_url=None).status_code == 200

    @pytest.mark.parametrize("ref", ["bad name!", "1STARTS_WITH_DIGIT", "with-dash", "x" * 129])
    def test_credentials_ref_must_be_an_environment_variable_name(self, ref: str) -> None:
        assert self._patch(credentials_ref=ref).status_code == 422

    def test_overlong_values_are_a_422_not_a_500(self) -> None:
        assert self._patch(base_url="https://a.example.org/" + "x" * 600).status_code == 422
        assert self._patch(auth_type="x" * 40).status_code == 422


# --- сквозные: outbox ----------------------------------------------------------------------

_WEBHOOK_ENV = "HARDENING_BITRIX_URL"
_WEBHOOK_URL = "https://bitrix.invalid/rest/1/hardening"


async def _outbox_event(*, target: str = "bitrix24", attempts: int = 0, **fields) -> uuid.UUID:
    """Ожидающее событие. Чужие ожидающие закрываются: тик берёт всё, что стоит в очереди."""
    from sqlalchemy import update

    from app.core.db import session_scope
    from app.modules.admin.models import FeatureFlag
    from app.modules.catalog.models import Organization
    from app.modules.crm.models import Deal
    from app.modules.identity.models import User
    from app.modules.integration.models import IntegrationSource, OutboxEvent
    from app.modules.workflow.models import Workflow, WorkflowStatus

    async with session_scope() as session:
        await session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.status.in_(["pending", "failed"]))
            .values(status="sent")
        )
        from sqlalchemy import delete, select

        source = (
            await session.execute(select(IntegrationSource).where(IntegrationSource.code == target))
        ).scalar_one_or_none()
        if source is None:
            source = IntegrationSource(code=target, name=target)
            session.add(source)
        source.is_active = True
        source.credentials_ref = _WEBHOOK_ENV
        await session.execute(delete(FeatureFlag).where(FeatureFlag.code == "bitrix_connector"))

        workflow = Workflow(
            code=f"wf-{uuid.uuid4().hex[:10]}", name="Воронка", deal_type="b2b", state="draft"
        )
        session.add(workflow)
        await session.flush()
        status = WorkflowStatus(workflow_id=workflow.id, code="new", name="Новая")
        org = Organization(name=f"Вуз {uuid.uuid4().hex[:6]}", org_type="university")
        owner = User(
            keycloak_id=str(uuid.uuid4()),
            email=f"{uuid.uuid4().hex[:8]}@rt-it-school.ru",
            full_name="Петров П.П.",
            role="KAM",
            status="active",
            consent_version="1.0",
        )
        session.add_all([status, org, owner])
        await session.flush()
        deal = Deal(
            number=f"D-{uuid.uuid4().hex[:10]}",
            title="Сделка для Битрикса",
            deal_type="b2b",
            workflow_id=workflow.id,
            status_id=status.id,
            organization_id=org.id,
            owner_id=owner.id,
        )
        session.add(deal)
        await session.flush()
        event = OutboxEvent(
            aggregate_type="deal",
            aggregate_id=deal.id,
            event_type="DEAL_CREATED",
            payload={"title": deal.title},
            target=target,
            attempts=attempts,
            **fields,
        )
        session.add(event)
        await session.flush()
        return event.id


async def _event(event_id: uuid.UUID):
    from app.core.db import session_scope
    from app.modules.integration.models import OutboxEvent

    async with session_scope() as session:
        event = await session.get(OutboxEvent, event_id)
        session.expunge(event)
        return event


@needs_db
class TestOutboxDelivery:
    @pytest.fixture(autouse=True)
    def _bitrix_on(self, client, monkeypatch):
        # `client` — чтобы приложение уже стартовало: оно пересоздаёт настройки, и флаг,
        # включённый раньше, терялся. В CI коннектор выключен (`.env.example`), локально — нет.
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "bitrix_connector_enabled", True)
        monkeypatch.setenv(_WEBHOOK_ENV, _WEBHOOK_URL)

    def _network(self, monkeypatch, handler) -> list[dict]:
        calls: list[dict] = []
        real_client = httpx.AsyncClient

        def wrapped(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content) if request.content else {}
            calls.append({"url": str(request.url), "body": body, "headers": dict(request.headers)})
            return handler(request, body)

        def client_with_mock(*args: object, **kwargs: object) -> httpx.AsyncClient:
            return real_client(*args, transport=httpx.MockTransport(wrapped), **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", client_with_mock)
        return calls

    def test_overlapping_ticks_deliver_an_event_once(self, client, monkeypatch) -> None:
        from app.modules.integration import tasks

        delivered: list[uuid.UUID] = []

        async def slow_deliver(session, event, source) -> None:
            delivered.append(event.id)
            await asyncio.sleep(0.4)  # доставка идёт дольше, чем до следующего тика

        monkeypatch.setattr(tasks, "_deliver", slow_deliver)
        event_id = run(client, _outbox_event)

        async def _two_ticks() -> list[dict]:
            return await asyncio.gather(
                tasks.sweep_outbox_events({}), tasks.sweep_outbox_events({})
            )

        results = run(client, _two_ticks)

        assert delivered == [event_id]
        assert sum(r["sent"] for r in results) == 1
        stored = run(client, _event, event_id)
        assert (stored.status, stored.attempts) == ("sent", 1)
        assert stored.next_retry_at is None

    def test_a_claimed_event_is_leased_and_counted_at_once(self, client) -> None:
        from app.modules.integration import tasks

        event_id = run(client, _outbox_event)

        async def _claim_twice():
            settings = SimpleNamespace(bitrix_connector_enabled=True)
            first = await tasks._claim_events(settings, {"sent": 0, "skipped": 0})
            second = await tasks._claim_events(settings, {"sent": 0, "skipped": 0})
            return first, second

        first, second = run(client, _claim_twice)

        assert [item[0] for item in first] == [event_id]
        assert second == []
        stored = run(client, _event, event_id)
        assert stored.attempts == 1
        assert stored.next_retry_at > dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)
        assert stored.status == "pending"

    def test_a_claim_that_lost_its_lease_delivers_nothing(self, client, monkeypatch) -> None:
        from app.modules.integration import tasks

        called: list[uuid.UUID] = []

        async def fake_deliver(session, event, source) -> None:
            called.append(event.id)

        monkeypatch.setattr(tasks, "_deliver", fake_deliver)
        event_id = run(client, _outbox_event)

        async def _stale() -> None:
            stale = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
            await tasks._deliver_event(event_id, stale, {"sent": 0, "failed": 0, "dead": 0})

        run(client, _stale)

        assert called == []
        assert run(client, _event, event_id).status == "pending"

    def test_a_failure_backs_off_and_the_attempt_is_not_counted_twice(
        self, client, monkeypatch
    ) -> None:
        from app.modules.integration import tasks

        async def failing(session, event, source) -> None:
            raise RuntimeError("bitrix24 crm.item.add http error: 500")

        monkeypatch.setattr(tasks, "_deliver", failing)
        event_id = run(client, _outbox_event)

        result = run(client, tasks.sweep_outbox_events, {})

        stored = run(client, _event, event_id)
        assert result["failed"] == 1
        assert (stored.status, stored.attempts) == ("failed", 1)
        assert "http error" in stored.last_error
        # Первый шаг backoff — одна секунда: повтор назначен близко, а не на аренду в 10 минут.
        assert stored.next_retry_at <= dt.datetime.now(dt.UTC) + dt.timedelta(seconds=2)

    def test_the_last_attempt_ends_in_the_dead_letter(self, client, monkeypatch) -> None:
        from app.modules.integration import tasks

        async def failing(session, event, source) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(tasks, "_deliver", failing)
        event_id = run(client, functools.partial(_outbox_event, attempts=tasks._MAX_ATTEMPTS - 1))

        result = run(client, tasks.sweep_outbox_events, {})

        stored = run(client, _event, event_id)
        assert result["dead"] == 1
        assert (stored.status, stored.attempts) == ("dead", tasks._MAX_ATTEMPTS)

    def test_an_event_whose_lease_ran_out_is_taken_again(self, client, monkeypatch) -> None:
        from app.modules.integration import tasks

        delivered: list[int] = []

        async def fake_deliver(session, event, source) -> None:
            delivered.append(event.attempts)

        monkeypatch.setattr(tasks, "_deliver", fake_deliver)
        # Воркер упал посреди доставки: попытка посчитана, аренда истекла.
        past = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
        event_id = run(client, functools.partial(_outbox_event, attempts=1, next_retry_at=past))

        run(client, tasks.sweep_outbox_events, {})

        assert delivered == [2]
        assert run(client, _event, event_id).status == "sent"

    def test_a_retry_finds_the_deal_a_lost_attempt_already_created(
        self, client, monkeypatch
    ) -> None:
        from sqlalchemy import select

        from app.core.db import session_scope
        from app.modules.integration import tasks
        from app.modules.integration.models import ExternalRef

        past = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
        event_id = run(client, functools.partial(_outbox_event, attempts=1, next_retry_at=past))
        event = run(client, _event, event_id)
        bitrix_id = random.randint(10**9, 10**12)  # external_refs живёт в общей БД

        def handler(request: httpx.Request, body: dict) -> httpx.Response:
            if request.url.path.endswith("crm.item.list.json"):
                item = {"id": bitrix_id, "originId": str(event.aggregate_id), "updatedTime": None}
                return httpx.Response(200, json={"result": {"items": [item]}})
            item = {"id": bitrix_id + 1, "updatedTime": None}
            return httpx.Response(200, json={"result": {"item": item}})

        calls = self._network(monkeypatch, handler)

        run(client, tasks.sweep_outbox_events, {})

        assert [call["url"].rsplit("/", 1)[-1] for call in calls] == ["crm.item.list.json"]
        assert run(client, _event, event_id).status == "sent"

        async def _ref() -> str:
            async with session_scope() as session:
                return await session.scalar(
                    select(ExternalRef.external_id).where(
                        ExternalRef.entity_id == event.aggregate_id
                    )
                )

        assert run(client, _ref) == str(bitrix_id)

    def test_a_lookup_that_returns_a_stranger_is_not_trusted(self, client, monkeypatch) -> None:
        from app.modules.integration import tasks

        past = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
        run(client, functools.partial(_outbox_event, attempts=1, next_retry_at=past))

        def handler(request: httpx.Request, body: dict) -> httpx.Response:
            if request.url.path.endswith("crm.item.list.json"):
                # Портал проигнорировал фильтр и вернул чужую сделку.
                return httpx.Response(200, json={"result": {"items": [{"id": 1, "originId": "x"}]}})
            return httpx.Response(
                200, json={"result": {"item": {"id": random.randint(10**9, 10**12)}}}
            )

        calls = self._network(monkeypatch, handler)

        run(client, tasks.sweep_outbox_events, {})

        assert [call["url"].rsplit("/", 1)[-1] for call in calls] == [
            "crm.item.list.json",
            "crm.item.add.json",
        ]
        assert calls[1]["body"]["fields"]["originatorId"] == "rtk-crm"

    def test_the_first_attempt_makes_no_extra_call(self, client, monkeypatch) -> None:
        from app.modules.integration import tasks

        run(client, _outbox_event)

        def handler(request: httpx.Request, body: dict) -> httpx.Response:
            return httpx.Response(
                200, json={"result": {"item": {"id": random.randint(10**9, 10**12)}}}
            )

        calls = self._network(monkeypatch, handler)

        run(client, tasks.sweep_outbox_events, {})

        assert [call["url"].rsplit("/", 1)[-1] for call in calls] == ["crm.item.add.json"]


class TestLmsDelivery:
    def test_the_event_id_travels_as_the_idempotency_key(self, monkeypatch) -> None:
        from app.modules.integration.lms import LmsClient

        seen: list[dict[str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(dict(request.headers))
            return httpx.Response(200, json={})

        real = httpx.AsyncClient
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda *a, **k: real(*a, transport=httpx.MockTransport(handler), **k),
        )

        async def _push() -> None:
            await LmsClient("https://lms.invalid", "tok").push_enrollment(
                {"deal_id": "d"}, idempotency_key="evt-1"
            )
            await LmsClient("https://lms.invalid", "tok").push_enrollment({"deal_id": "d"})

        asyncio.run(_push())

        assert seen[0]["idempotency-key"] == "evt-1"
        assert seen[0]["authorization"] == "Bearer tok"
        assert "idempotency-key" not in seen[1]

    def _deliver(self, monkeypatch, *, base_url: str, lms_base_url: str | None, strict=False):
        from app.core.config import get_settings
        from app.modules.integration import tasks

        settings = get_settings()
        monkeypatch.setattr(settings, "lms_base_url", lms_base_url)
        monkeypatch.setattr(settings, "app_profile", "prod" if strict else "dev")
        calls: list[str] = []

        async def push(self, payload, *, idempotency_key=None):
            calls.append(idempotency_key or "")
            return {}

        monkeypatch.setattr(tasks.lms.LmsClient, "push_enrollment", push)
        event = SimpleNamespace(
            id=uuid.uuid4(),
            target="lms",
            aggregate_id=uuid.uuid4(),
            aggregate_type="signature",
            event_type="X",
            payload={},
        )
        source = SimpleNamespace(base_url=base_url)
        return asyncio.run(tasks._deliver(None, event, source)), calls, event

    def test_a_metadata_address_never_gets_the_token(self, monkeypatch) -> None:
        with pytest.raises(RuntimeError, match="rejected"):
            self._deliver(monkeypatch, base_url="http://169.254.169.254", lms_base_url=None)

    def test_a_host_other_than_the_configured_one_never_gets_the_token(self, monkeypatch) -> None:
        with pytest.raises(RuntimeError, match="differs from LMS_BASE_URL"):
            self._deliver(
                monkeypatch,
                base_url="https://attacker.example.org",
                lms_base_url="https://lms.example.org",
            )

    def test_the_configured_host_gets_the_event_with_a_stable_key(self, monkeypatch) -> None:
        _, calls, event = self._deliver(
            monkeypatch,
            base_url="https://lms.example.org/api",
            lms_base_url="https://lms.example.org",
        )

        assert calls == [str(event.id)]
