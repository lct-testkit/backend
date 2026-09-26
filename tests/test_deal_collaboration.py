"""Участники, комментарии, задачи и поля перехода: права и аудит.

Найдено внешним тестированием: состав участников мог менять любой, кто может править сделку
(в том числе «наблюдатель»); упоминания принимали любой UUID; руководитель удалял чужие
комментарии; правка комментария копировала весь текст в бессрочный журнал; задача, закрытая
через PATCH, не помнила, кто её закрыл; поля, заданные вместе с переходом, в аудит не попадали.
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


def _deal_owned_by(client, owner) -> dict[str, Any]:
    """Сделка, где `owner` — ответственный; заводит ADMIN (сам вызывающий остаётся в сессии)."""
    admin = login(client, "ADMIN")
    graph = create_published_workflow(client)
    deal = create_deal(client, graph["workflow"]["id"], owner_id=str(owner.id))
    return {"deal": deal, "graph": graph, "admin": admin}


def _add_participant(client, deal_id: str, user_id, role: str = "watcher"):
    return client.post(
        f"/api/deals/{deal_id}/participants", json={"user_id": str(user_id), "role_in_deal": role}
    )


def _audit_changes(client, entity_id: str, action: str) -> list[dict]:
    async def _load(entity: uuid.UUID, act: str) -> list[dict]:
        from sqlalchemy import select

        from app.core.db import session_scope
        from app.modules.audit.models import AuditLog

        async with session_scope() as session:
            rows = await session.scalars(
                select(AuditLog).where(AuditLog.entity_id == entity, AuditLog.action == act)
            )
            return [row.changes or {} for row in rows]

    return run(client, _load, uuid.UUID(entity_id), action)


class TestParticipants:
    def test_owner_manages_participants(self, client) -> None:
        owner = create_user(client, "KAM")
        colleague = create_user(client, "KAM")
        deal = _deal_owned_by(client, owner)["deal"]
        sign_in(client, owner)

        added = _add_participant(client, deal["id"], colleague.id)
        assert added.status_code == 201, added.text

        removed = client.delete(f"/api/deals/{deal['id']}/participants/{added.json()['id']}")
        assert removed.status_code == 200, removed.text

    def test_watcher_cannot_change_the_participant_list(self, client) -> None:
        owner = create_user(client, "KAM")
        watcher = create_user(client, "KAM")
        outsider = create_user(client, "KAM")
        deal = _deal_owned_by(client, owner)["deal"]
        sign_in(client, owner)
        link = _add_participant(client, deal["id"], watcher.id).json()

        sign_in(client, watcher)
        # Видимость у наблюдателя есть, а права менять состав — нет.
        assert client.get(f"/api/deals/{deal['id']}").status_code == 200
        assert _add_participant(client, deal["id"], outsider.id, "co_owner").status_code == 403
        assert (
            client.delete(f"/api/deals/{deal['id']}/participants/{link['id']}").status_code == 403
        )

    def test_admin_can_change_participants(self, client) -> None:
        owner = create_user(client, "KAM")
        colleague = create_user(client, "KAM")
        deal = _deal_owned_by(client, owner)["deal"]  # остаёмся под ADMIN

        assert _add_participant(client, deal["id"], colleague.id).status_code == 201

    def test_inactive_user_cannot_be_added(self, client) -> None:
        owner = create_user(client, "KAM")
        blocked = create_user(client, "KAM", status="blocked")
        deal = _deal_owned_by(client, owner)["deal"]
        sign_in(client, owner)

        response = _add_participant(client, deal["id"], blocked.id)

        assert response.status_code == 422, response.text

    def test_unknown_user_is_404(self, client) -> None:
        owner = create_user(client, "KAM")
        deal = _deal_owned_by(client, owner)["deal"]
        sign_in(client, owner)

        assert _add_participant(client, deal["id"], uuid.uuid4()).status_code == 404


class TestComments:
    def _comment(self, client, deal_id: str, **extra):
        return client.post(f"/api/deals/{deal_id}/comments", json={"body": "Текст", **extra})

    def test_mentions_are_deduplicated(self, client) -> None:
        owner = create_user(client, "KAM")
        colleague = create_user(client, "KAM")
        deal = _deal_owned_by(client, owner)["deal"]

        response = self._comment(
            client, deal["id"], mentions=[str(colleague.id), str(colleague.id)]
        )

        assert response.status_code == 201, response.text
        assert response.json()["mentions"] == [str(colleague.id)]

    def test_mention_of_unknown_or_blocked_user_is_rejected(self, client) -> None:
        owner = create_user(client, "KAM")
        blocked = create_user(client, "KAM", status="blocked")
        deal = _deal_owned_by(client, owner)["deal"]

        for user_id in (uuid.uuid4(), blocked.id):
            response = self._comment(client, deal["id"], mentions=[str(user_id)])
            assert response.status_code == 422, response.text
            assert response.json()["errors"][0]["field"] == "mentions"

    def test_too_many_mentions_are_rejected(self, client) -> None:
        owner = create_user(client, "KAM")
        deal = _deal_owned_by(client, owner)["deal"]

        response = self._comment(
            client, deal["id"], mentions=[str(uuid.uuid4()) for _ in range(21)]
        )

        assert response.status_code == 422, response.text

    def test_reply_to_a_deleted_comment_is_404(self, client) -> None:
        owner = create_user(client, "KAM")
        deal = _deal_owned_by(client, owner)["deal"]
        parent = self._comment(client, deal["id"]).json()
        assert client.request(
            "DELETE", f"/api/comments/{parent['id']}", json={"reason": "лишний"}
        ).is_success

        response = self._comment(client, deal["id"], parent_id=parent["id"])

        assert response.status_code == 404, response.text

    def test_head_cannot_delete_a_subordinates_comment(self, client) -> None:
        team = create_team(client)
        head = create_user(client, "HEAD", team_id=team)
        kam = create_user(client, "KAM", team_id=team)
        deal = _deal_owned_by(client, kam)["deal"]
        sign_in(client, kam)
        comment = self._comment(client, deal["id"]).json()

        sign_in(client, head)
        forbidden = client.request(
            "DELETE", f"/api/comments/{comment['id']}", json={"reason": "мешает"}
        )
        assert forbidden.status_code == 403, forbidden.text

        login(client, "ADMIN")
        allowed = client.request(
            "DELETE", f"/api/comments/{comment['id']}", json={"reason": "по 152-ФЗ"}
        )
        assert allowed.status_code == 200, allowed.text

    def test_audit_of_an_edit_does_not_copy_the_text(self, client) -> None:
        owner = create_user(client, "KAM")
        deal = _deal_owned_by(client, owner)["deal"]
        sign_in(client, owner)
        comment = self._comment(client, deal["id"], body="Старый секретный текст").json()

        edited = client.patch(f"/api/comments/{comment['id']}", json={"body": "Новый текст"})
        assert edited.status_code == 200, edited.text

        entries = _audit_changes(client, comment["id"], "COMMENT_UPDATED")
        assert len(entries) == 1
        dumped = str(entries[0])
        assert "секретный" not in dumped and "Новый текст" not in dumped
        assert entries[0]["body_length"] == {"old": len("Старый секретный текст"), "new": 11}


class TestTasks:
    def _task(self, client, deal_id: str, assignee_id, **extra):
        return client.post(
            "/api/tasks",
            json={"deal_id": deal_id, "title": "Позвонить", "assignee_id": str(assignee_id)}
            | extra,
        )

    def test_done_through_patch_remembers_who_closed_it(self, client) -> None:
        owner = create_user(client, "KAM")
        deal = _deal_owned_by(client, owner)["deal"]
        sign_in(client, owner)
        task = self._task(client, deal["id"], owner.id).json()

        done = client.patch(f"/api/tasks/{task['id']}", json={"status": "done"})

        assert done.status_code == 200, done.text
        assert done.json()["completed_by"] == str(owner.id)
        assert done.json()["completed_at"] is not None

    def test_reopening_clears_the_completion(self, client) -> None:
        owner = create_user(client, "KAM")
        deal = _deal_owned_by(client, owner)["deal"]
        sign_in(client, owner)
        task = self._task(client, deal["id"], owner.id).json()
        client.patch(f"/api/tasks/{task['id']}", json={"status": "done"})

        reopened = client.patch(f"/api/tasks/{task['id']}", json={"status": "open"})

        assert reopened.status_code == 200, reopened.text
        assert reopened.json()["completed_at"] is None
        assert reopened.json()["completed_by"] is None

    @pytest.mark.parametrize("field", ["title", "assignee_id", "priority", "status"])
    def test_null_in_a_required_field_is_422(self, client, field: str) -> None:
        owner = create_user(client, "KAM")
        deal = _deal_owned_by(client, owner)["deal"]
        sign_in(client, owner)
        task = self._task(client, deal["id"], owner.id).json()

        response = client.patch(f"/api/tasks/{task['id']}", json={field: None})

        assert response.status_code == 422, response.text

    def test_assignee_must_exist_and_be_active(self, client) -> None:
        owner = create_user(client, "KAM")
        blocked = create_user(client, "KAM", status="blocked")
        deal = _deal_owned_by(client, owner)["deal"]
        sign_in(client, owner)

        assert self._task(client, deal["id"], uuid.uuid4()).status_code == 404
        assert self._task(client, deal["id"], blocked.id).status_code == 422

        task = self._task(client, deal["id"], owner.id).json()
        response = client.patch(f"/api/tasks/{task['id']}", json={"assignee_id": str(blocked.id)})
        assert response.status_code == 422, response.text


class TestTransitionFieldsAudit:
    def test_fields_set_with_the_transition_are_audited(self, client) -> None:
        owner = create_user(client, "KAM")
        setup = _deal_owned_by(client, owner)
        deal, graph = setup["deal"], setup["graph"]
        codes = by_code(graph)

        response = transition_deal(
            client,
            deal,
            codes["work"]["id"],
            fields={"amount": "150000", "custom_fields.channel": "сайт"},
        )
        assert response.status_code == 200, response.text

        entries = _audit_changes(client, deal["id"], "DEAL_STATUS_CHANGED")
        assert len(entries) == 1
        changes = entries[0]
        assert changes["amount"]["new"] == "150000"
        assert changes["custom_fields.channel"] == {"old": None, "new": "сайт"}
        assert "status_id" in changes


class TestSignatureDoesNotCarryOver:
    def test_signed_document_of_the_previous_stage_is_not_a_signed_contract(self, client) -> None:
        async def _mark_signed(deal_id: uuid.UUID) -> None:
            from sqlalchemy import update

            from app.core.db import session_scope
            from app.modules.crm.models import Deal

            async with session_scope() as session:
                await session.execute(
                    update(Deal).where(Deal.id == deal_id).values(signature_status="signed")
                )

        owner = create_user(client, "KAM")
        setup = _deal_owned_by(client, owner)
        deal, codes = setup["deal"], by_code(setup["graph"])
        run(client, _mark_signed, uuid.UUID(deal["id"]))
        card = client.get(f"/api/deals/{deal['id']}").json()["deal"]
        assert card["signature_status"] == "signed"

        moved = transition_deal(client, card, codes["work"]["id"])

        assert moved.status_code == 200, moved.text
        assert moved.json()["deal"]["signature_status"] == "none"
