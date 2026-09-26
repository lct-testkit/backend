"""Заготовки для сквозных тестов воронок и сделок: пользователь, команда,
опубликованная воронка, организация, контакт, сделка, переход.

Всё, что можно, идёт через настоящее API (тесты проверяют именно его
поведение); напрямую в БД пишутся пользователи и команды (Keycloak в тестах
не участвует), организация и контакт — их создание через API тянет ИНН,
согласия и дедупликацию, тестам ненужные. Требуют `TEST_DATABASE_URL`, как и
остальные сквозные тесты (`tests/conftest.py`).
"""

from __future__ import annotations

import uuid
from typing import Any

from tests.conftest import authenticate, run


def create_team(client) -> uuid.UUID:
    async def _create() -> uuid.UUID:
        from app.core.db import session_scope
        from app.modules.identity.models import Team

        async with session_scope() as session:
            team = Team(name=f"Команда {uuid.uuid4().hex[:6]}")
            session.add(team)
            await session.flush()
            return team.id

    return run(client, _create)


def create_user(
    client, role: str = "KAM", *, team_id: uuid.UUID | None = None, status: str = "active"
):
    """Пользователь в БД, минуя Keycloak (как `conftest._make_user`, но с командой)."""

    async def _create():
        from app.core.db import session_scope
        from app.modules.identity.models import User

        async with session_scope() as session:
            user = User(
                keycloak_id=str(uuid.uuid4()),
                email=f"{uuid.uuid4().hex[:12]}@rt-it-school.ru",
                full_name="Иванов Иван Иванович",
                role=role,
                status=status,
                team_id=team_id,
                consent_version="1.0",
            )
            session.add(user)
            await session.flush()
            session.expunge(user)
            return user

    return run(client, _create)


def sign_in(client, user) -> None:
    """Открывает сессию и CSRF под `user`; следующие запросы клиента идут от его имени."""
    client.headers["X-CSRF-Token"] = authenticate(client, user)


def login(client, role: str = "ADMIN"):
    """Заводит пользователя роли `role` и входит под ним."""
    user = create_user(client, role)
    sign_in(client, user)
    return user


def unique_code(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def graph_body(
    *,
    statuses: list[dict[str, Any]] | None = None,
    transitions: list[dict[str, Any]] | None = None,
    sla_rules: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Тело `PUT /graph`. По умолчанию — три статуса `new → work → won` и SLA на `work`."""
    return {
        "statuses": statuses
        if statuses is not None
        else [
            {"code": "new", "name": "Новая", "type": "initial", "sort_order": 10},
            {"code": "work", "name": "В работе", "type": "intermediate", "sort_order": 20},
            {"code": "won", "name": "Закрыта", "type": "won", "sort_order": 30},
        ],
        "transitions": transitions
        if transitions is not None
        else [
            {"from_status": "new", "to_status": "work", "name": "В работу"},
            {"from_status": "work", "to_status": "won", "name": "Закрыть"},
        ],
        "sla_rules": sla_rules
        if sla_rules is not None
        else [{"status": "work", "max_duration_hours": 24, "count_business_days": False}],
    }


def create_draft_workflow(
    client, *, deal_type: str = "b2b", is_default: bool = False
) -> dict[str, Any]:
    response = client.post(
        "/api/workflows",
        json={
            "code": unique_code("wf"),
            "name": "Воронка для теста",
            "deal_type": deal_type,
            "is_default": is_default,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def put_graph(client, workflow: dict[str, Any], body: dict[str, Any]):
    """`PUT /graph` с текущей версией воронки; возвращает сырой ответ."""
    return client.put(
        f"/api/workflows/{workflow['id']}/graph",
        json=body,
        headers={"If-Match": str(workflow["version"])},
    )


def create_published_workflow(
    client,
    body: dict[str, Any] | None = None,
    *,
    deal_type: str = "b2b",
    is_default: bool = False,
) -> dict[str, Any]:
    """Черновик → граф → публикация. Возвращает `GraphOut` опубликованной воронки."""
    workflow = create_draft_workflow(client, deal_type=deal_type, is_default=is_default)
    saved = put_graph(client, workflow, body or graph_body())
    assert saved.status_code == 200, saved.text
    version = saved.json()["workflow"]["version"]
    published = client.post(
        f"/api/workflows/{workflow['id']}/publish", headers={"If-Match": str(version)}
    )
    assert published.status_code == 200, published.text
    return get_graph(client, workflow["id"])


def get_graph(client, workflow_id: str) -> dict[str, Any]:
    response = client.get(f"/api/workflows/{workflow_id}")
    assert response.status_code == 200, response.text
    return response.json()


def by_code(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {status["code"]: status for status in graph["statuses"]}


def body_from_graph(graph: dict[str, Any]) -> dict[str, Any]:
    """`GraphOut` → тело `PUT /graph` с теми же `id`: так граф правит редактор.
    Архивные статусы и их переходы редактор не присылает."""
    live = {s["id"] for s in graph["statuses"] if not s["is_archived"]}
    return {
        "statuses": [
            {
                key: status[key]
                for key in ("id", "code", "name", "type", "color", "sort_order", "required_fields")
            }
            for status in graph["statuses"]
            if status["id"] in live
        ],
        "transitions": [
            {
                "id": t["id"],
                "from_status": t["from_status_id"],
                "to_status": t["to_status_id"],
                **{
                    key: t[key]
                    for key in (
                        "name",
                        "allowed_roles",
                        "conditions",
                        "actions",
                        "requires_comment",
                        "sort_order",
                    )
                },
            }
            for t in graph["transitions"]
            if t["from_status_id"] in live and t["to_status_id"] in live
        ],
        "sla_rules": [
            {
                "status": rule["status_id"],
                **{
                    key: rule[key]
                    for key in (
                        "max_duration_hours",
                        "warn_threshold_pct",
                        "escalate_to_role",
                        "escalate_to_user_id",
                        "channels",
                        "count_business_days",
                        "is_active",
                    )
                },
            }
            for rule in graph["sla_rules"]
            if rule["status_id"] in live
        ],
    }


def create_organization(client, name: str | None = None) -> str:
    async def _create() -> str:
        from app.core.db import session_scope
        from app.modules.catalog.models import Organization

        async with session_scope() as session:
            org = Organization(name=name or f"Вуз {uuid.uuid4().hex[:6]}", org_type="university")
            session.add(org)
            await session.flush()
            return str(org.id)

    return run(client, _create)


def create_contact(
    client,
    last_name: str = "Петров",
    first_name: str = "Пётр",
    middle_name: str | None = "Петрович",
) -> str:
    async def _create() -> str:
        from app.core.db import session_scope
        from app.modules.catalog.models import Contact

        async with session_scope() as session:
            contact = Contact(first_name=first_name, last_name=last_name, middle_name=middle_name)
            session.add(contact)
            await session.flush()
            return str(contact.id)

    return run(client, _create)


def create_deal(client, workflow_id: str, **overrides: Any) -> dict[str, Any]:
    payload = {
        "title": "Сделка для теста",
        "deal_type": "b2b",
        "workflow_id": workflow_id,
        "organization_id": create_organization(client),
        **overrides,
    }
    response = client.post("/api/deals", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _release_transition_lock(client, deal_id: str) -> None:
    """Redis-лок перехода снимает Lua-скрипт, а `fakeredis` без `lupa` его не исполняет: лок
    оставался бы до конца TTL (30 с) и второй переход той же сделки получал бы 503. Ключ
    удаляется вручную — на поведение самого перехода это не влияет."""

    async def _delete() -> None:
        from app.core.redis_client import get_redis, key_lock

        await get_redis().delete(key_lock(f"deal:{deal_id}:transition"))

    run(client, _delete)


def transition_deal(client, deal: dict[str, Any], to_status_id: str, **body: Any):
    response = client.post(
        f"/api/deals/{deal['id']}/transition",
        json={"to_status_id": to_status_id, **body},
        headers={"If-Match": str(deal["version"])},
    )
    _release_transition_lock(client, deal["id"])
    return response
