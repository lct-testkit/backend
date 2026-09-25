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
