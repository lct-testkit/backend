"""Сквозные тесты доставки outbox и его админских ручек (`integration/tasks.py`,
`integration/router.py`) на настоящей PostgreSQL (`TEST_DATABASE_URL`) — см.
докстринг `tests/conftest.py`.

Сети нет: запросы к Bitrix24 перехватывает `httpx.MockTransport`, адрес вебхука
— зарезервированный домен `.invalid`, который не разрешается ни при каких
настройках. Сама доставка запускается в цикле событий `TestClient`
(`tests.conftest.run`) — там же, где живёт движок БД приложения.
"""

from __future__ import annotations

import functools
import itertools
import json
import random
import uuid

import httpx
import pytest

from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

_WEBHOOK_ENV = "BITRIX_TEST_WEBHOOK_URL"
_WEBHOOK_URL = "https://bitrix.invalid/rest/1/test-webhook-code"
# Идентификаторы «Bitrix24» уникальны между тестами: `external_refs` живёт в общей БД.
_BITRIX_IDS = itertools.count(random.randint(10**6, 10**9))


async def _prepare(*, source_active: bool, flag: bool | None) -> uuid.UUID:
    """Источник `bitrix24`, флаг `bitrix_connector` и одно событие `DEAL_CREATED`.

    `flag=None` — строки флага в `feature_flags` нет. Чужие ожидающие события
    закрываются, чтобы цикл доставки не подхватывал их вместе с нашим.
    """
    from sqlalchemy import delete, select, update

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

        source = (
            await session.execute(
                select(IntegrationSource).where(IntegrationSource.code == "bitrix24")
            )
        ).scalar_one_or_none()
        if source is None:
            source = IntegrationSource(code="bitrix24", name="Bitrix24")
            session.add(source)
        source.is_active = source_active
        source.credentials_ref = _WEBHOOK_ENV

        await session.execute(delete(FeatureFlag).where(FeatureFlag.code == "bitrix_connector"))
        if flag is not None:
            session.add(FeatureFlag(code="bitrix_connector", is_enabled=flag))

        workflow = Workflow(
            code=f"wf-{uuid.uuid4().hex[:10]}",
            name="Воронка outbox",
            deal_type="b2b",
            state="draft",
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
            target="bitrix24",
        )
        session.add(event)
        await session.flush()
        return event.id


def _prepare_event(client, *, source_active: bool, flag: bool | None) -> uuid.UUID:
    return run(client, functools.partial(_prepare, source_active=source_active, flag=flag))


async def _event_state(event_id: uuid.UUID) -> dict[str, object]:
    from app.core.db import session_scope
    from app.modules.integration.models import OutboxEvent

    async with session_scope() as session:
        event = await session.get(OutboxEvent, event_id)
        return {
            "status": event.status,
            "attempts": event.attempts,
            "last_error": event.last_error,
            "next_retry_at": event.next_retry_at,
        }


async def _sweep() -> dict[str, int]:
    from app.modules.integration.tasks import sweep_outbox_events

    return await sweep_outbox_events({})


def _connector(monkeypatch: pytest.MonkeyPatch, *, enabled: bool = True) -> list[dict]:
    """Включает коннектор (`BITRIX_CONNECTOR_ENABLED`) и подменяет сеть Bitrix24."""
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "bitrix_connector_enabled", enabled)
    monkeypatch.setenv(_WEBHOOK_ENV, _WEBHOOK_URL)

    calls: list[dict] = []
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append({"url": str(request.url), "body": json.loads(request.content)})
        item = {"id": next(_BITRIX_IDS), "updatedTime": "2026-09-25T10:00:00+03:00"}
        return httpx.Response(200, json={"result": {"item": item}})

    def client_with_mock_network(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_with_mock_network)
    return calls


class TestBitrixDeliveryConditions:
    """Доставка в Bitrix24 включена, только когда совпали три выключателя:
    `BITRIX_CONNECTOR_ENABLED`, `integration_sources.is_active` и флаг функции
    `bitrix_connector` (`feature_flags`)."""

    def test_all_three_on_delivers_the_event(self, client, monkeypatch) -> None:
        calls = _connector(monkeypatch)
        event_id = _prepare_event(client, source_active=True, flag=True)

        run(client, _sweep)

        assert run(client, _event_state, event_id)["status"] == "sent"
        assert [c["url"] for c in calls] == [f"{_WEBHOOK_URL}/crm.item.add.json"]

    def test_disabled_flag_holds_the_event_back(self, client, monkeypatch) -> None:
        calls = _connector(monkeypatch)
        event_id = _prepare_event(client, source_active=True, flag=False)

        run(client, _sweep)

        state = run(client, _event_state, event_id)
        assert state["status"] == "dead"
        assert state["last_error"] == "feature_flag_disabled"
        assert calls == []

    def test_a_flag_that_was_never_created_does_not_block(self, client, monkeypatch) -> None:
        # Флаги заводятся вручную (ручки создания нет): без строки выключать
        # нечем, доставку решают остальные два условия.
        _connector(monkeypatch)
        event_id = _prepare_event(client, source_active=True, flag=None)

        run(client, _sweep)

        assert run(client, _event_state, event_id)["status"] == "sent"

    def test_inactive_source_still_holds_the_event_back(self, client, monkeypatch) -> None:
        calls = _connector(monkeypatch)
        event_id = _prepare_event(client, source_active=False, flag=True)

        run(client, _sweep)

        state = run(client, _event_state, event_id)
        assert (state["status"], state["last_error"]) == ("dead", "source_inactive")
        assert calls == []

    def test_disabled_connector_still_holds_the_event_back(self, client, monkeypatch) -> None:
        calls = _connector(monkeypatch, enabled=False)
        event_id = _prepare_event(client, source_active=True, flag=True)

        run(client, _sweep)

        state = run(client, _event_state, event_id)
        assert (state["status"], state["last_error"]) == ("dead", "source_inactive")
        assert calls == []


class TestBitrixSourceId:
    """`sourceId` — код из справочника источников конкретного портала;
    задаётся `BITRIX_SOURCE_ID`, по умолчанию `OTHER`."""

    def test_default_source_id_is_other(self, client, monkeypatch) -> None:
        calls = _connector(monkeypatch)
        _prepare_event(client, source_active=True, flag=True)

        run(client, _sweep)

        assert calls[0]["body"]["fields"]["sourceId"] == "OTHER"

    def test_source_id_comes_from_settings(self, client, monkeypatch) -> None:
        from app.core.config import get_settings

        calls = _connector(monkeypatch)
        monkeypatch.setattr(get_settings(), "bitrix_source_id", "UC_WEB")
        _prepare_event(client, source_active=True, flag=True)

        run(client, _sweep)

        assert calls[0]["body"]["fields"]["sourceId"] == "UC_WEB"


def _admin(client) -> None:
    csrf = authenticate(client, run(client, _make_user, "ADMIN"))
    client.headers["X-CSRF-Token"] = csrf


async def _set_status(event_id: uuid.UUID, status: str, attempts: int = 8) -> None:
    from app.core.db import session_scope
    from app.modules.integration.models import OutboxEvent

    async with session_scope() as session:
        event = await session.get(OutboxEvent, event_id)
        event.status = status
        event.attempts = attempts
        event.last_error = "bitrix24 crm.item.add http error: 500 Internal Server Error"


async def _audit_entries(event_id: uuid.UUID) -> list[dict]:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.audit.models import AuditLog

    async with session_scope() as session:
        rows = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.action == "INTEGRATION_OUTBOX_RETRIED", AuditLog.entity_id == event_id
                )
            )
        ).scalars()
        return [{"actor_id": row.actor_id, "changes": row.changes} for row in rows]


class TestRetryOutboxEvent:
    """`POST /admin/integrations/outbox-events/{id}/retry`: dead-letter не
    приходилось бы править SQL-ом."""

    def _retry(self, client, event_id):
        return client.post(f"/api/admin/integrations/outbox-events/{event_id}/retry")

    @pytest.mark.parametrize("status", ["dead", "failed"])
    def test_failed_and_dead_events_go_back_to_the_queue(self, client, status: str) -> None:
        _admin(client)
        event_id = _prepare_event(client, source_active=True, flag=True)
        run(client, _set_status, event_id, status)

        response = self._retry(client, event_id)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["id"] == str(event_id)
        assert (body["status"], body["attempts"], body["last_error"]) == ("pending", 0, None)
        assert body["next_retry_at"] is not None
        state = run(client, _event_state, event_id)
        assert (state["status"], state["attempts"], state["last_error"]) == ("pending", 0, None)

    def test_retry_is_written_to_the_audit_log(self, client) -> None:
        _admin(client)
        event_id = _prepare_event(client, source_active=True, flag=True)
        run(client, _set_status, event_id, "dead")

        assert self._retry(client, event_id).status_code == 200

        (entry,) = run(client, _audit_entries, event_id)
        assert entry["actor_id"] is not None
        assert entry["changes"]["status"] == {"old": "dead", "new": "pending"}
        assert entry["changes"]["attempts"] == {"old": 8, "new": 0}

    @pytest.mark.parametrize("status", ["pending", "sent"])
    def test_other_statuses_are_refused(self, client, status: str) -> None:
        _admin(client)
        event_id = _prepare_event(client, source_active=True, flag=True)
        run(client, _set_status, event_id, status, 1)

        response = self._retry(client, event_id)

        assert response.status_code == 409, response.text
        assert response.json()["code"] == "CRM-1704"
        assert run(client, _event_state, event_id)["status"] == status

    def test_unknown_event_is_404(self, client) -> None:
        _admin(client)

        assert self._retry(client, uuid.uuid4()).status_code == 404

    def test_only_integration_admins_may_retry(self, client) -> None:
        event_id = _prepare_event(client, source_active=True, flag=True)
        run(client, _set_status, event_id, "dead")
        csrf = authenticate(client, run(client, _make_user, "KAM"))
        client.headers["X-CSRF-Token"] = csrf

        response = self._retry(client, event_id)

        assert response.status_code == 403, response.text
        assert run(client, _event_state, event_id)["status"] == "dead"

    def test_retried_event_is_delivered_by_the_next_sweep(self, client, monkeypatch) -> None:
        calls = _connector(monkeypatch)
        _admin(client)
        # Источник был выключен: событие ушло в dead-letter и ждёт повтора.
        event_id = _prepare_event(client, source_active=False, flag=True)
        run(client, _sweep)
        assert run(client, _event_state, event_id)["status"] == "dead"

        async def _activate_source() -> None:
            from sqlalchemy import update

            from app.core.db import session_scope
            from app.modules.integration.models import IntegrationSource

            async with session_scope() as session:
                await session.execute(
                    update(IntegrationSource)
                    .where(IntegrationSource.code == "bitrix24")
                    .values(is_active=True)
                )

        run(client, _activate_source)
        assert self._retry(client, event_id).status_code == 200
        run(client, _sweep)

        assert run(client, _event_state, event_id)["status"] == "sent"
        assert len(calls) == 1
