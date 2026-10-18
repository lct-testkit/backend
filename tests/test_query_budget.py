"""Бюджет SQL-запросов на списки (new_spec §6: «запрет N+1, тест на количество
запросов в CI»).

Идея: число запросов на страницу списка НЕ должно зависеть от числа строк.
Создаём 2 сделки, меряем запросы; докидываем ещё 20 — меряем снова. Если между
замерами число запросов выросло больше чем на константу-допуск, где-то ленивая
загрузка связей в цикле (классический N+1).

Нужна настоящая Postgres (`TEST_DATABASE_URL`, как у остальных сквозных тестов).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import event

from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

# Допуск на «дрожание»: разные ветки кэша прав/настроек могут дать 1–2 запроса.
TOLERANCE = 2
ROWS_SMALL = 2
ROWS_LARGE = 22


async def _seed_deals(count: int) -> None:
    from app.core.db import session_scope
    from app.modules.catalog.models import Organization
    from app.modules.crm.models import Deal
    from app.modules.identity.models import User
    from app.modules.workflow.models import Workflow, WorkflowStatus

    async with session_scope() as session:
        workflow = Workflow(
            code=f"wf-{uuid.uuid4().hex[:10]}",
            name="Воронка для бюджета запросов",
            deal_type="b2b",
            state="draft",
        )
        session.add(workflow)
        await session.flush()
        status = WorkflowStatus(workflow_id=workflow.id, code="new", name="Новая")
        session.add(status)
        owner = User(
            keycloak_id=str(uuid.uuid4()),
            email=f"{uuid.uuid4().hex[:8]}@rt-it-school.ru",
            full_name="Петров П.П.",
            role="KAM",
            status="active",
            consent_version="1.0",
        )
        session.add(owner)
        await session.flush()
        for _ in range(count):
            org = Organization(name=f"Вуз {uuid.uuid4().hex[:6]}", org_type="university")
            session.add(org)
            await session.flush()
            session.add(
                Deal(
                    number=f"D-{uuid.uuid4().hex[:10]}",
                    title="Сделка бюджета запросов",
                    deal_type="b2b",
                    workflow_id=workflow.id,
                    status_id=status.id,
                    organization_id=org.id,
                    owner_id=owner.id,
                )
            )
        await session.flush()


def _count_queries(client, path: str) -> int:
    from app.core.db import get_engine

    counter = {"n": 0}

    def _before(*_args: object, **_kwargs: object) -> None:
        counter["n"] += 1

    sync_engine = get_engine().sync_engine
    event.listen(sync_engine, "before_cursor_execute", _before)
    try:
        response = client.get(path)
    finally:
        event.remove(sync_engine, "before_cursor_execute", _before)
    assert response.status_code == 200, response.text
    return counter["n"]


def _count_queries_for_coro(client, func, *args: object) -> int:
    """Как `_count_queries`, но для произвольной корутины, запущенной в цикле `client`
    (`tests.conftest.run`) — нужна B5-9: `fan_out` не делает HTTP, только SQL."""
    from app.core.db import get_engine

    counter = {"n": 0}

    def _before(*_args: object, **_kwargs: object) -> None:
        counter["n"] += 1

    sync_engine = get_engine().sync_engine
    event.listen(sync_engine, "before_cursor_execute", _before)
    try:
        run(client, func, *args)
    finally:
        event.remove(sync_engine, "before_cursor_execute", _before)
    return counter["n"]


class TestListQueryBudget:
    @pytest.mark.parametrize("path", ["/api/deals?limit=100", "/api/organizations?limit=100"])
    def test_queries_do_not_grow_with_rows(self, client, path: str) -> None:
        admin = run(client, _make_user, "ADMIN")
        authenticate(client, admin)

        run(client, _seed_deals, ROWS_SMALL)
        _count_queries(client, path)  # прогрев кэшей (права, настройки)
        small = _count_queries(client, path)

        run(client, _seed_deals, ROWS_LARGE)
        large = _count_queries(client, path)

        assert small > 0, "счётчик запросов не сработал — тест ничего не проверяет"
        assert large <= small + TOLERANCE, (
            f"{path}: {small} запросов на {ROWS_SMALL}+ строк и {large} на "
            f"{ROWS_SMALL + ROWS_LARGE}+ — вероятен N+1 (ленивая загрузка связей в цикле). "
            "Загрузите связи явно: selectinload/joinedload."
        )


class TestWebhookFanOutQueryBudget:
    """B5-9: `fan_out` (`integration.webhooks`) с прогретым кэшем `cache:webhook_subs` не
    должен добавлять SELECT к публикации события — переход сделки стоит в бюджете 300 мс."""

    def test_publish_with_warm_cache_adds_no_extra_query(self, client) -> None:
        from tests.crm_helpers import login

        login(client, "ADMIN")
        created = client.post(
            "/api/admin/integrations/webhooks",
            json={
                "name": "budget",
                "url": "https://erp-budget.invalid/hooks/crm",
                "event_types": ["DEAL_CREATED"],
                "filters": {},
            },
        )
        assert created.status_code == 201, created.text

        async def _publish() -> None:
            import uuid as uuid_module

            from app.core.db import session_scope
            from app.modules.catalog.models import Organization
            from app.modules.crm.models import Deal
            from app.modules.identity.models import User
            from app.modules.integration.service import get_outbox_service
            from app.modules.workflow.models import Workflow, WorkflowStatus

            async with session_scope() as session:
                workflow = Workflow(
                    code=f"wf-budget-{uuid_module.uuid4().hex[:10]}",
                    name="Воронка бюджета webhook",
                    deal_type="b2b",
                    state="draft",
                )
                session.add(workflow)
                await session.flush()
                status = WorkflowStatus(workflow_id=workflow.id, code="new", name="Новая")
                org = Organization(name=f"Вуз {uuid_module.uuid4().hex[:6]}", org_type="university")
                owner = User(
                    keycloak_id=str(uuid_module.uuid4()),
                    email=f"{uuid_module.uuid4().hex[:8]}@rt-it-school.ru",
                    full_name="Петров П.П.",
                    role="KAM",
                    status="active",
                    consent_version="1.0",
                )
                session.add_all([status, org, owner])
                await session.flush()
                deal = Deal(
                    number=f"D-budget-{uuid_module.uuid4().hex[:10]}",
                    title="Сделка бюджета webhook",
                    deal_type="b2b",
                    workflow_id=workflow.id,
                    status_id=status.id,
                    organization_id=org.id,
                    owner_id=owner.id,
                )
                session.add(deal)
                await session.flush()
                await get_outbox_service().publish(
                    session,
                    aggregate_type="deal",
                    aggregate_id=deal.id,
                    event_type="DEAL_CREATED",
                    payload={"title": deal.title, "deal_type": "b2b"},
                )

        # Первый вызов прогревает кэш подписок (одна SELECT сверх обычного). Второй и третий —
        # с прогретым кэшем, число запросов не должно расти между ними.
        _count_queries_for_coro(client, _publish)
        warm_first = _count_queries_for_coro(client, _publish)
        warm_second = _count_queries_for_coro(client, _publish)

        assert warm_first > 0, "счётчик запросов не сработал — тест ничего не проверяет"
        assert warm_second <= warm_first + TOLERANCE, (
            f"{warm_first} запросов на публикацию с прогретым кэшем, {warm_second} на "
            "следующую — fan_out, похоже, делает SELECT подписок на каждый вызов."
        )


class TestOrgSuggestQueryBudget:
    """B4-11: `GET /api/org-lookup/suggest` проверяет скоуп каждой подсказки. Число запросов не
    должно расти с числом подсказок: автоподстановка вызывается на каждое нажатие клавиши."""

    @staticmethod
    async def _seed_orgs(owner_id: uuid.UUID, prefix: str, start: int, count: int) -> None:
        from app.core.db import session_scope
        from app.modules.catalog.models import Organization

        async with session_scope() as session:
            for index in range(start, start + count):
                session.add(
                    Organization(
                        name=f"Вуз подсказки {prefix}{index}",
                        org_type="university",
                        inn=f"{prefix}{index:04d}",
                        owner_id=owner_id,
                    )
                )
            await session.flush()

    def test_queries_do_not_grow_with_the_number_of_suggestions(self, client) -> None:
        kam = run(client, _make_user, "KAM")
        authenticate(client, kam)
        prefix = str(uuid.uuid4().int % 10**6).zfill(6)
        path = f"/api/org-lookup/suggest?q={prefix}&limit=20"

        run(client, self._seed_orgs, kam.id, prefix, 0, 2)
        _count_queries(client, path)  # прогрев кэшей (права, настройки)
        small = _count_queries(client, path)

        run(client, self._seed_orgs, kam.id, prefix, 2, 12)
        large = _count_queries(client, path)

        assert small > 0, "счётчик запросов не сработал — тест ничего не проверяет"
        assert (
            large <= small + TOLERANCE
        ), f"{small} запросов на 2 подсказки и {large} на 14 — вероятен N+1 при проверке скоупа"
