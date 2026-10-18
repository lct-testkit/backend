"""B5-9: универсальные webhook-подписки на исходящие события — CRUD, раздача (`fan_out`) и
доставка (`sweep_webhook_deliveries`).

Сквозные тесты на настоящей PostgreSQL (`TEST_DATABASE_URL`); сеть подменяет `Network`
(`tests/lms_helpers.py`), адрес подписки — зарезервированный домен `.invalid`.
"""

from __future__ import annotations

import functools
import uuid
from typing import Any

import httpx
import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import create_user, login, sign_in
from tests.lms_helpers import Network

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

_URL = "/api/admin/integrations/webhooks"
_HOOK_URL = "https://erp.invalid/hooks/crm"


async def _isolate_subscription(subscription_id: uuid.UUID) -> None:
    """Деактивирует все ОСТАЛЬНЫЕ подписки и инвалидирует кэш раздачи: тесты этого файла делят
    БД и Redis, и без изоляции `publish()` фанаутится ещё и на подписки, оставшиеся активными от
    предыдущих тестов (у всех один `_HOOK_URL` и события `DEAL_CREATED`/`DEAL_STATUS_CHANGED`)."""
    from sqlalchemy import update

    from app.core.db import session_scope
    from app.modules.integration.models import WebhookSubscription
    from app.modules.integration.webhooks import _invalidate_cache

    async with session_scope() as session:
        await session.execute(
            update(WebhookSubscription)
            .where(WebhookSubscription.id != subscription_id)
            .values(is_active=False)
        )
    await _invalidate_cache()


async def _close_other_deliveries() -> None:
    """Общая БД тестов может нести доставки от других подписок (этого и других файлов): тик
    `sweep_webhook_deliveries` подхватывает все `pending`/`failed` без разбора, поэтому
    закрываем чужие перед собственным прогоном — тот же приём, что `test_integration_outbox.py`
    уже применяет к `outbox_events`."""
    from sqlalchemy import update

    from app.core.db import session_scope
    from app.modules.integration.models import WebhookDelivery

    async with session_scope() as session:
        await session.execute(
            update(WebhookDelivery)
            .where(WebhookDelivery.status.in_(["pending", "failed"]))
            .values(status="sent")
        )


def _create_subscription(
    client, *, event_types: list[str], filters: dict[str, Any] | None = None
) -> dict[str, Any]:
    response = client.post(
        _URL,
        json={
            "name": "1С: смена статуса",
            "url": _HOOK_URL,
            "event_types": event_types,
            "filters": filters or {},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _seed_deal() -> dict[str, Any]:
    from app.core.db import session_scope
    from app.modules.catalog.models import Organization
    from app.modules.crm.models import Deal
    from app.modules.identity.models import User
    from app.modules.workflow.models import Workflow, WorkflowStatus

    async with session_scope() as session:
        workflow = Workflow(
            code=f"wf-wh-{uuid.uuid4().hex[:10]}",
            name="Воронка webhook",
            deal_type="b2b",
            state="draft",
        )
        session.add(workflow)
        await session.flush()
        status_a = WorkflowStatus(workflow_id=workflow.id, code="new", name="Новая")
        status_b = WorkflowStatus(workflow_id=workflow.id, code="qualified", name="Квалифицирована")
        org = Organization(name=f"Вуз {uuid.uuid4().hex[:6]}", org_type="university")
        owner = User(
            keycloak_id=str(uuid.uuid4()),
            email=f"{uuid.uuid4().hex[:8]}@rt-it-school.ru",
            full_name="Петров П.П.",
            role="KAM",
            status="active",
            consent_version="1.0",
        )
        session.add_all([status_a, status_b, owner, org])
        await session.flush()
        deal = Deal(
            number=f"D-{uuid.uuid4().hex[:10]}",
            title="Сделка webhook",
            deal_type="b2b",
            workflow_id=workflow.id,
            status_id=status_a.id,
            organization_id=org.id,
            owner_id=owner.id,
        )
        session.add(deal)
        await session.flush()
        return {"deal_id": deal.id, "deal_number": deal.number}


async def _publish_deal_created(deal_id: uuid.UUID, deal_type: str = "b2b") -> None:
    from app.core.db import session_scope
    from app.modules.integration.service import get_outbox_service

    async with session_scope() as session:
        await get_outbox_service().publish(
            session,
            aggregate_type="deal",
            aggregate_id=deal_id,
            event_type="DEAL_CREATED",
            payload={"title": "x", "deal_type": deal_type},
        )


async def _deliveries_for_subscription(subscription_id: uuid.UUID) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.integration.models import WebhookDelivery

    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(WebhookDelivery).where(
                        WebhookDelivery.subscription_id == subscription_id
                    )
                )
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": row.id,
                "status": row.status,
                "attempts": row.attempts,
                "last_error": row.last_error,
                "next_retry_at": row.next_retry_at,
            }
            for row in rows
        ]


async def _sweep() -> dict[str, int]:
    from app.modules.integration.tasks import sweep_webhook_deliveries

    return await sweep_webhook_deliveries({})


async def _force_lease_elapsed(delivery_id: uuid.UUID) -> None:
    """Аренда доставки истекла — следующий тик может взять её заново, не дожидаясь реальных
    десяти минут."""
    import datetime as dt

    from app.core.db import session_scope
    from app.modules.integration.models import WebhookDelivery

    async with session_scope() as session:
        delivery = await session.get(WebhookDelivery, delivery_id)
        assert delivery is not None
        delivery.next_retry_at = dt.datetime.now(dt.UTC)


async def _subscription_state(subscription_id: uuid.UUID) -> dict[str, Any]:
    from app.core.db import session_scope
    from app.modules.integration.models import WebhookSubscription

    async with session_scope() as session:
        subscription = await session.get(WebhookSubscription, subscription_id)
        assert subscription is not None
        return {
            "is_active": subscription.is_active,
            "disabled_reason": subscription.disabled_reason,
            "consecutive_failures": subscription.consecutive_failures,
            "secret_encrypted": subscription.secret_encrypted,
        }


class TestSubscriptionCrudAndPermissions:
    def test_admin_creates_subscription_and_secret_is_shown_once(self, client) -> None:
        login(client, "ADMIN")

        created = _create_subscription(client, event_types=["DEAL_STATUS_CHANGED"])

        assert created["secret"].startswith("whsec_")
        listed = client.get(_URL)
        assert listed.status_code == 200
        assert "secret" not in listed.text
        assert created["secret"] not in listed.text

    def test_kam_and_auditor_are_forbidden(self, client) -> None:
        kam = create_user(client, "KAM")
        sign_in(client, kam)
        response = client.post(
            _URL,
            json={"name": "x", "url": _HOOK_URL, "event_types": ["DEAL_CREATED"], "filters": {}},
        )
        assert response.status_code == 403

        auditor = create_user(client, "AUDITOR")
        sign_in(client, auditor)
        response = client.get(_URL)
        assert response.status_code == 403

    def test_unknown_event_type_is_rejected(self, client) -> None:
        login(client, "ADMIN")
        response = client.post(
            _URL,
            json={
                "name": "x",
                "url": _HOOK_URL,
                "event_types": ["NOT_A_REAL_EVENT"],
                "filters": {},
            },
        )
        assert response.status_code == 422

    def test_internal_address_is_rejected(self, client) -> None:
        login(client, "ADMIN")
        response = client.post(
            _URL,
            json={
                "name": "x",
                "url": "http://169.254.169.254/",
                "event_types": ["DEAL_CREATED"],
                "filters": {},
            },
        )
        assert response.status_code == 422

    def test_secret_never_reaches_audit_log(self, client) -> None:
        login(client, "ADMIN")
        created = _create_subscription(client, event_types=["DEAL_CREATED"])

        async def _audit_text() -> str:
            from sqlalchemy import select

            from app.core.db import session_scope
            from app.modules.audit.models import AuditLog

            async with session_scope() as session:
                rows = (
                    await session.execute(
                        select(AuditLog.changes).where(
                            AuditLog.entity_type == "webhook_subscription",
                            AuditLog.entity_id == uuid.UUID(created["id"]),
                        )
                    )
                ).all()
                return str(rows)

        text = run(client, _audit_text)
        assert created["secret"] not in text

    def test_rotate_secret_returns_new_secret_once(self, client) -> None:
        login(client, "ADMIN")
        created = _create_subscription(client, event_types=["DEAL_CREATED"])

        rotated = client.post(f"{_URL}/{created['id']}/rotate-secret")

        assert rotated.status_code == 200
        assert rotated.json()["secret"].startswith("whsec_")
        assert rotated.json()["secret"] != created["secret"]

    def test_delete_removes_subscription(self, client) -> None:
        login(client, "ADMIN")
        created = _create_subscription(client, event_types=["DEAL_CREATED"])

        deleted = client.delete(f"{_URL}/{created['id']}")
        assert deleted.status_code == 204
        assert client.get(f"{_URL}/{created['id']}/deliveries").status_code == 404


class TestFanOut:
    def test_matching_subscription_gets_a_delivery(self, client) -> None:
        login(client, "ADMIN")
        created = _create_subscription(client, event_types=["DEAL_CREATED"])
        scene = run(client, _seed_deal)

        run(client, functools.partial(_publish_deal_created, scene["deal_id"], "b2b"))

        deliveries = run(client, _deliveries_for_subscription, uuid.UUID(created["id"]))
        assert len(deliveries) == 1
        assert deliveries[0]["status"] == "pending"

    def test_subscription_with_other_event_type_gets_nothing(self, client) -> None:
        login(client, "ADMIN")
        created = _create_subscription(client, event_types=["DOCUMENT_SIGNED"])
        scene = run(client, _seed_deal)

        run(client, functools.partial(_publish_deal_created, scene["deal_id"], "b2b"))

        deliveries = run(client, _deliveries_for_subscription, uuid.UUID(created["id"]))
        assert deliveries == []

    def test_filter_on_deal_type_excludes_non_matching_events(self, client) -> None:
        login(client, "ADMIN")
        created = _create_subscription(
            client, event_types=["DEAL_CREATED"], filters={"deal_type": "b2c"}
        )
        scene = run(client, _seed_deal)

        run(client, functools.partial(_publish_deal_created, scene["deal_id"], "b2b"))

        assert run(client, _deliveries_for_subscription, uuid.UUID(created["id"])) == []


class TestDelivery:
    def test_successful_delivery_is_signed_and_marked_sent(
        self, client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        login(client, "ADMIN")
        created = _create_subscription(client, event_types=["DEAL_CREATED"])
        secret = created["secret"]
        run(client, functools.partial(_isolate_subscription, uuid.UUID(created["id"])))
        run(client, _close_other_deliveries)
        scene = run(client, _seed_deal)
        run(client, functools.partial(_publish_deal_created, scene["deal_id"], "b2b"))

        network = Network()
        # `Network`'s own MockTransport handler already records every request in
        # `network.calls`; `respond` только формирует ответ — свой список здесь нужен лишь
        # затем, что `network.calls` хранит тело уже РАЗОБРАННЫМ как JSON, а подпись проверяется
        # по сырым байтам (`request.content`).
        captured: list[httpx.Request] = []

        def ok(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json={})

        network.respond = ok
        network.install(monkeypatch)

        result = run(client, _sweep)

        assert result["sent"] == 1
        assert len(network.calls) == 1
        assert len(captured) == 1
        sent_request = captured[0]
        assert str(sent_request.url) == _HOOK_URL
        assert sent_request.headers["x-crm-event"] == "DEAL_CREATED"
        from app.modules.integration.security import verify_signature

        assert verify_signature(
            secret,
            sent_request.content,
            sent_request.headers["x-signature"],
            timestamp=sent_request.headers["x-timestamp"],
        )
        deliveries = run(client, _deliveries_for_subscription, uuid.UUID(created["id"]))
        assert deliveries[0]["status"] == "sent"

    def test_failing_delivery_backs_off_then_dies_after_eight_attempts(
        self, client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        login(client, "ADMIN")
        created = _create_subscription(client, event_types=["DEAL_CREATED"])
        run(client, functools.partial(_isolate_subscription, uuid.UUID(created["id"])))
        run(client, _close_other_deliveries)
        scene = run(client, _seed_deal)
        run(client, functools.partial(_publish_deal_created, scene["deal_id"], "b2b"))

        network = Network()
        network.respond = lambda request: httpx.Response(500)
        network.install(monkeypatch)

        for _ in range(8):
            run(client, _sweep)
            deliveries = run(client, _deliveries_for_subscription, uuid.UUID(created["id"]))
            delivery_id = deliveries[0]["id"]
            if deliveries[0]["status"] != "dead":
                run(client, functools.partial(_force_lease_elapsed, delivery_id))

        deliveries = run(client, _deliveries_for_subscription, uuid.UUID(created["id"]))
        assert deliveries[0]["status"] == "dead"
        assert deliveries[0]["attempts"] == 8
        assert "erp.invalid" not in (deliveries[0]["last_error"] or "")

    def test_manual_retry_returns_dead_delivery_to_the_queue(
        self, client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        login(client, "ADMIN")
        created = _create_subscription(client, event_types=["DEAL_CREATED"])
        run(client, functools.partial(_isolate_subscription, uuid.UUID(created["id"])))
        run(client, _close_other_deliveries)
        scene = run(client, _seed_deal)
        run(client, functools.partial(_publish_deal_created, scene["deal_id"], "b2b"))

        network = Network()
        network.respond = lambda request: httpx.Response(500)
        network.install(monkeypatch)
        for _ in range(8):
            run(client, _sweep)
            deliveries = run(client, _deliveries_for_subscription, uuid.UUID(created["id"]))
            if deliveries[0]["status"] != "dead":
                run(client, functools.partial(_force_lease_elapsed, deliveries[0]["id"]))
        deliveries = run(client, _deliveries_for_subscription, uuid.UUID(created["id"]))
        assert deliveries[0]["status"] == "dead"

        retried = client.post(f"{_URL}/deliveries/{deliveries[0]['id']}/retry")

        assert retried.status_code == 200
        assert retried.json()["status"] == "pending"
        assert retried.json()["attempts"] == 0

    def test_http_410_disables_the_subscription(
        self, client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        login(client, "ADMIN")
        created = _create_subscription(client, event_types=["DEAL_CREATED"])
        run(client, functools.partial(_isolate_subscription, uuid.UUID(created["id"])))
        run(client, _close_other_deliveries)
        scene = run(client, _seed_deal)
        run(client, functools.partial(_publish_deal_created, scene["deal_id"], "b2b"))

        network = Network()
        network.respond = lambda request: httpx.Response(410)
        network.install(monkeypatch)

        run(client, _sweep)

        state = run(client, _subscription_state, uuid.UUID(created["id"]))
        assert state["is_active"] is False
        assert state["disabled_reason"] == "gone_410"

    def test_secret_never_appears_in_last_error(
        self, client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        login(client, "ADMIN")
        created = _create_subscription(client, event_types=["DEAL_CREATED"])
        run(client, functools.partial(_isolate_subscription, uuid.UUID(created["id"])))
        run(client, _close_other_deliveries)
        scene = run(client, _seed_deal)
        run(client, functools.partial(_publish_deal_created, scene["deal_id"], "b2b"))

        network = Network()
        network.respond = lambda request: httpx.Response(500)
        network.install(monkeypatch)
        run(client, _sweep)

        deliveries = run(client, _deliveries_for_subscription, uuid.UUID(created["id"]))
        assert created["secret"] not in (deliveries[0]["last_error"] or "")
        assert "erp.invalid" not in (deliveries[0]["last_error"] or "")

    def test_secret_is_stored_encrypted_when_a_key_is_configured(
        self, client, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from cryptography.fernet import Fernet

        from app.modules.admin.setting_crypto import reset_secrets_config

        monkeypatch.setenv("SETTINGS_ENCRYPTION_KEY", Fernet.generate_key().decode())
        reset_secrets_config()
        try:
            login(client, "ADMIN")
            created = _create_subscription(client, event_types=["DEAL_CREATED"])

            state = run(client, _subscription_state, uuid.UUID(created["id"]))

            assert created["secret"] not in str(state["secret_encrypted"])
            assert state["secret_encrypted"]["__enc"] == "fernet:v1"
        finally:
            reset_secrets_config()
