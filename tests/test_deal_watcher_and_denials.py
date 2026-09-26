"""Наблюдатель (`watcher`) в сделке только читает, а отказы на объектном уровне попадают в аудит.

Найдено внешним тестированием: скоуп KAM — «владелец или любой участник», а права записи
проверялись лишь правом роли и скоупом, поэтому наблюдатель мог править, переводить по воронке и
закрывать сделку. Отказы доступа к объекту (чужая сделка, запись наблюдателем) откатывали
транзакцию запроса вместе с записью аудита и следов не оставляли.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import (
    by_code,
    create_deal,
    create_published_workflow,
    create_team,
    create_user,
    login,
    sign_in,
    transition_deal,
)

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _setup(client, owner) -> dict[str, Any]:
    admin = login(client, "ADMIN")
    graph = create_published_workflow(client)
    deal = create_deal(client, graph["workflow"]["id"], owner_id=str(owner.id))
    return {"deal": deal, "graph": graph, "admin": admin}


def _join(client, deal_id: str, user, role: str) -> None:
    response = client.post(
        f"/api/deals/{deal_id}/participants", json={"user_id": str(user.id), "role_in_deal": role}
    )
    assert response.status_code == 201, response.text


def _denials(client, entity_id: str, actor_id: uuid.UUID) -> list[dict]:
    async def _load(entity: uuid.UUID, actor: uuid.UUID) -> list[dict]:
        from sqlalchemy import select

        from app.core.db import session_scope
        from app.modules.audit.models import AuditLog

        async with session_scope() as session:
            rows = await session.scalars(
                select(AuditLog).where(
                    AuditLog.entity_id == entity,
                    AuditLog.action == "ACCESS_DENIED",
                    AuditLog.result == "denied",
                    AuditLog.actor_id == actor,
                )
            )
            return [{"changes": row.changes or {}, "ip": row.ip} for row in rows]

    return run(client, _load, uuid.UUID(entity_id), actor_id)


class TestWatcherIsReadOnly:
    def test_watcher_sees_the_deal_but_cannot_change_it(self, client) -> None:
        owner = create_user(client, "KAM")
        watcher = create_user(client, "KAM")
        setup = _setup(client, owner)
        deal, codes = setup["deal"], by_code(setup["graph"])
        _join(client, deal["id"], watcher, "watcher")
        sign_in(client, watcher)

        card = client.get(f"/api/deals/{deal['id']}")
        assert card.status_code == 200, card.text
        version = str(card.json()["deal"]["version"])

        patched = client.patch(
            f"/api/deals/{deal['id']}", json={"title": "Взлом"}, headers={"If-Match": version}
        )
        assert patched.status_code == 403, patched.text
        moved = transition_deal(client, card.json()["deal"], codes["work"]["id"])
        assert moved.status_code == 403, moved.text
        products = client.put(
            f"/api/deals/{deal['id']}/products",
            json={"items": []},
            headers={"If-Match": version},
        )
        assert products.status_code == 403, products.text
        comment = client.post(f"/api/deals/{deal['id']}/comments", json={"body": "Привет"})
        assert comment.status_code == 403, comment.text
        task = client.post(
            "/api/tasks",
            json={"deal_id": deal["id"], "title": "Задача", "assignee_id": str(watcher.id)},
        )
        assert task.status_code == 403, task.text

        # Ничего не изменилось.
        sign_in(client, owner)
        unchanged = client.get(f"/api/deals/{deal['id']}").json()["deal"]
        assert unchanged["title"] == deal["title"]
        assert unchanged["status_id"] == deal["status_id"]
        assert unchanged["version"] == deal["version"]

    @pytest.mark.parametrize("role", ["co_owner", "lawyer", "methodist"])
    def test_working_participant_can_change_the_deal(self, client, role: str) -> None:
        owner = create_user(client, "KAM")
        colleague = create_user(client, "KAM")
        setup = _setup(client, owner)
        deal = setup["deal"]
        _join(client, deal["id"], colleague, role)
        sign_in(client, colleague)

        response = client.patch(
            f"/api/deals/{deal['id']}",
            json={"title": "Правка соисполнителя"},
            headers={"If-Match": str(deal["version"])},
        )

        assert response.status_code == 200, response.text

    def test_owner_who_is_also_a_watcher_row_keeps_write_access(self, client) -> None:
        owner = create_user(client, "KAM")
        setup = _setup(client, owner)
        deal = setup["deal"]
        _join(client, deal["id"], owner, "watcher")
        sign_in(client, owner)

        response = client.patch(
            f"/api/deals/{deal['id']}",
            json={"title": "Владелец правит"},
            headers={"If-Match": str(deal["version"])},
        )

        assert response.status_code == 200, response.text

    def test_head_of_the_team_still_writes(self, client) -> None:
        team = create_team(client)
        head = create_user(client, "HEAD", team_id=team)
        kam = create_user(client, "KAM", team_id=team)
        setup = _setup(client, kam)
        deal = setup["deal"]
        sign_in(client, head)

        response = client.patch(
            f"/api/deals/{deal['id']}",
            json={"title": "Правка руководителя"},
            headers={"If-Match": str(deal["version"])},
        )

        assert response.status_code == 200, response.text


class TestObjectLevelDenialsAreAudited:
    def test_write_attempt_by_a_watcher_leaves_a_denied_entry(self, client) -> None:
        owner = create_user(client, "KAM")
        watcher = create_user(client, "KAM")
        setup = _setup(client, owner)
        deal = setup["deal"]
        _join(client, deal["id"], watcher, "watcher")
        sign_in(client, watcher)

        response = client.patch(
            f"/api/deals/{deal['id']}",
            json={"title": "Взлом"},
            headers={"If-Match": str(deal["version"])},
        )
        assert response.status_code == 403

        entries = _denials(client, deal["id"], watcher.id)
        assert len(entries) == 1, entries
        assert entries[0]["changes"]["reason"] == "watcher_read_only"
        assert entries[0]["changes"]["role"] == "KAM"

    def test_reading_a_foreign_deal_is_a_404_and_is_audited(self, client) -> None:
        owner = create_user(client, "KAM")
        outsider = create_user(client, "KAM")
        setup = _setup(client, owner)
        deal = setup["deal"]
        sign_in(client, outsider)

        response = client.get(f"/api/deals/{deal['id']}")
        assert response.status_code == 404

        entries = _denials(client, deal["id"], outsider.id)
        assert len(entries) == 1, entries
        assert entries[0]["changes"]["reason"] == "out_of_scope"

    def test_missing_deal_is_a_404_without_an_audit_entry(self, client) -> None:
        # Несуществующий id ничего не раскрывает и журнал не засоряет.
        outsider = create_user(client, "KAM")
        sign_in(client, outsider)
        missing = str(uuid.uuid4())

        assert client.get(f"/api/deals/{missing}").status_code == 404
        assert _denials(client, missing, outsider.id) == []

    def test_foreign_comment_edit_is_audited(self, client) -> None:
        owner = create_user(client, "KAM")
        colleague = create_user(client, "KAM")
        setup = _setup(client, owner)
        deal = setup["deal"]
        _join(client, deal["id"], colleague, "co_owner")
        sign_in(client, owner)
        comment = client.post(f"/api/deals/{deal['id']}/comments", json={"body": "Текст"}).json()
        sign_in(client, colleague)

        response = client.patch(f"/api/comments/{comment['id']}", json={"body": "Чужое"})
        assert response.status_code == 403

        entries = _denials(client, comment["id"], colleague.id)
        assert entries and entries[0]["changes"]["reason"] == "edit_foreign_comment"

    def test_a_successful_request_leaves_no_denial(self, client) -> None:
        owner = create_user(client, "KAM")
        setup = _setup(client, owner)
        deal = setup["deal"]
        sign_in(client, owner)

        assert client.get(f"/api/deals/{deal['id']}").status_code == 200
        assert _denials(client, deal["id"], owner.id) == []
