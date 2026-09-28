"""Регрессия для переноса `_audit.record()` в конец `DealService.transition` и
`CommentService.create` (`app/modules/crm/service.py`).

Контекст (перф-диагностика 27.09, живой нагрузочный тест): `AuditService._chain_head()`
берёт единственный на всю систему advisory-лок (`_AUDIT_CHAIN_LOCK_ID`,
`app/modules/audit/service.py`) транзакционным (`pg_advisory_xact_lock`) — держится до
коммита владельца транзакции, а не до возврата из `record()`. Сам лок дешёвый (медиана
захвата — доли миллисекунды), но раньше `record()` вызывался ПЕРВЫМ в обоих местах: всё,
что шло после него (publish в outbox, `_run_actions` — задачи/LMS-события/запрос подписи —
в `transition`; уведомления упомянутым в `CommentService.create`), исполнялось уже держа
этот глобальный лок, и каждый параллельный переход/комментарий стоял в очереди не за сам
аудит, а за весь этот хвост. На гейте 50 concurrent p95 `transition` дошёл до ~1400мс вместо
целевых 300 при нулевых ошибках — чистая задержка под локом.

Тесты ниже упали бы на коде до переноса: там `record()` шёл первым, а не последним.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import (
    by_code,
    create_deal,
    create_published_workflow,
    create_user,
    login,
    transition_deal,
)

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


class TestTransitionRecordsAuditLast:
    def test_outbox_publish_and_actions_run_before_the_audit_record(
        self, client, monkeypatch
    ) -> None:
        """Порядок вызовов внутри одного `transition()`: раньше был record → publish →
        _run_actions, теперь — publish → _run_actions → record."""
        from app.modules.audit.service import AuditService
        from app.modules.crm.service import DealService
        from app.modules.integration.service import RealOutboxService

        login(client)
        graph = create_published_workflow(client)
        deal = create_deal(client, graph["workflow"]["id"])
        work_status_id = by_code(graph)["work"]["id"]

        order: list[str] = []
        original_publish = RealOutboxService.publish
        original_run_actions = DealService._run_actions
        original_record = AuditService.record

        async def spy_publish(self, session, **kwargs):
            order.append("outbox_publish")
            return await original_publish(self, session, **kwargs)

        async def spy_run_actions(self, deal, actions, principal, now):
            order.append("run_actions")
            return await original_run_actions(self, deal, actions, principal, now)

        async def spy_record(self, action, **kwargs):
            order.append(f"audit_record:{action}")
            return await original_record(self, action, **kwargs)

        monkeypatch.setattr(RealOutboxService, "publish", spy_publish)
        monkeypatch.setattr(DealService, "_run_actions", spy_run_actions)
        monkeypatch.setattr(AuditService, "record", spy_record)

        # Настройка (создание пользователя/воронки/сделки) уже прогнала свой аудит —
        # список чист и содержит только вызовы этого перехода.
        response = transition_deal(client, deal, work_status_id)
        assert response.status_code == 200, response.text

        assert order == ["outbox_publish", "run_actions", "audit_record:DEAL_STATUS_CHANGED"], order

    def test_two_slow_transitions_no_longer_serialize_behind_the_audit_lock(
        self, client, monkeypatch
    ) -> None:
        """Раньше `_run_actions` исполнялся, уже держа глобальный лок цепочки аудита: две
        параллельные транзакции с медленным `_run_actions` (1с каждая) фактически шли
        последовательно (~2с суммарно — вторая ждёт лока, который первая держит все свои
        1с). После переноса лок держится только на саму запись — обе задержки идут
        параллельно (~1с).

        Искусственная задержка нарочно взята большой (1с, не 0.3с, как было в первой
        версии этого теста): на self-hosted раннере эта сюита гоняется одновременно с
        другими PR'ами на той же машине (3 параллельных инстанса, один Docker-демон), и
        при 0.3с/порог 0.5с обычный шум планировщика/диска под чужой нагрузкой уже
        достаточен, чтобы параллельный прогон (истинно ~0.3с) изредка дополз до 0.5-0.6с
        и тест ложно покраснел — поймано вживую в первую же ночь на реальном раннере.
        При 1с/порог 1.6с тот же абсолютный шум остаётся тем же (десятки-сотни мс), но его
        доля от сигнала на порядок меньше — а разрыв между «правда параллельно» (~1с) и
        «правда последовательно» (~2с) по-прежнему в 2 раза, есть что отличать."""
        from app.modules.crm.service import DealService

        async def slow_run_actions(self, deal, actions, principal, now) -> None:
            await asyncio.sleep(1.0)

        monkeypatch.setattr(DealService, "_run_actions", slow_run_actions)

        login(client)
        graph_a = create_published_workflow(client)
        deal_a = create_deal(client, graph_a["workflow"]["id"])
        work_a = by_code(graph_a)["work"]["id"]
        graph_b = create_published_workflow(client)
        deal_b = create_deal(client, graph_b["workflow"]["id"])
        work_b = by_code(graph_b)["work"]["id"]

        elapsed = run(
            client,
            _two_concurrent_transitions,
            uuid.UUID(deal_a["id"]),
            deal_a["version"],
            uuid.UUID(work_a),
            uuid.UUID(deal_b["id"]),
            deal_b["version"],
            uuid.UUID(work_b),
        )

        assert elapsed < 1.6, (
            f"два параллельных перехода заняли {elapsed:.2f}с — похоже, второй ждёт лок "
            "цепочки аудита, который первый держит все время своего _run_actions"
        )


async def _two_concurrent_transitions(
    deal_a_id: uuid.UUID,
    version_a: int,
    to_a: uuid.UUID,
    deal_b_id: uuid.UUID,
    version_b: int,
    to_b: uuid.UUID,
) -> float:
    from app.core.db import session_scope
    from app.core.security import Principal
    from app.modules.crm.models import Deal
    from app.modules.crm.service import DealService

    def _principal(owner_id: uuid.UUID) -> Principal:
        return Principal(
            user_id=owner_id,
            keycloak_id="k",
            role="ADMIN",
            status="active",
            email=None,
            full_name="x",
            team_id=None,
            manager_id=None,
            perm_epoch=1,
            session_id=None,
            consent_version="1.0",
            must_change_password=False,
            claims=None,  # type: ignore[arg-type]
        )

    async def go(deal_id: uuid.UUID, version: int, to_status_id: uuid.UUID) -> None:
        async with session_scope() as session:
            deal = await session.get(Deal, deal_id)
            assert deal is not None
            await DealService(session).transition(
                deal,
                _principal(deal.owner_id),
                to_status_id=to_status_id,
                comment=None,
                fields={},
                expected_version=version,
            )

    t0 = time.monotonic()
    await asyncio.gather(
        go(deal_a_id, version_a, to_a),
        go(deal_b_id, version_b, to_b),
    )
    return time.monotonic() - t0


class TestCommentRecordsAuditLast:
    def test_mentions_are_notified_before_the_audit_record(self, client, monkeypatch) -> None:
        """Порядок вызовов внутри `CommentService.create`: раньше был record → уведомления,
        теперь — уведомления → record."""
        from app.modules.audit.service import AuditService
        from app.modules.notification.service import RealNotificationService

        colleague = create_user(client, "KAM")
        login(client, "ADMIN")
        graph = create_published_workflow(client)
        deal = create_deal(client, graph["workflow"]["id"])

        order: list[str] = []
        original_notify = RealNotificationService.notify_user
        original_record = AuditService.record

        async def spy_notify(self, session, **kwargs):
            order.append("notify_user")
            return await original_notify(self, session, **kwargs)

        async def spy_record(self, action, **kwargs):
            order.append(f"audit_record:{action}")
            return await original_record(self, action, **kwargs)

        monkeypatch.setattr(RealNotificationService, "notify_user", spy_notify)
        monkeypatch.setattr(AuditService, "record", spy_record)

        response = client.post(
            f"/api/deals/{deal['id']}/comments",
            json={"body": "Посмотри", "mentions": [str(colleague.id)]},
        )
        assert response.status_code == 201, response.text

        assert order == ["notify_user", "audit_record:COMMENT_CREATED"], order
