"""B4-7: AUDITOR читает комментарии сделки (`comment:audit_read`) — `GET
/api/admin/audit/deals/{deal_id}/comments`. Настоящая Postgres (`TEST_DATABASE_URL`, см.
докстринг `tests/conftest.py`).

Матрица прав `new_spec` (часть 5) даёт AUDITOR «чтение» комментариев, а код его не пускал —
несоответствие из `ACCOMPANYING-DOCUMENTATION.md`. Решение — отдельная ручка с отдельным
правом (`comment:audit_read`, не `deal:read`): сделка для этой роли остаётся вне скоупа
целиком (раздел 4, `DealScope.NONE`), доступна только переписка, найденная через запись
`COMMENT_CREATED` в журнале аудита, и только с замаскированным текстом.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import create_deal, create_published_workflow, create_user, login, sign_in

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _audit_url(deal_id: str) -> str:
    return f"/api/admin/audit/deals/{deal_id}/comments"


def _setup_deal(client) -> dict[str, Any]:
    login(client, "ADMIN")
    graph = create_published_workflow(client)
    return create_deal(client, graph["workflow"]["id"])


def _add_comment(client, deal: dict[str, Any], *, body: str, is_internal: bool = False) -> Any:
    response = client.post(
        f"/api/deals/{deal['id']}/comments", json={"body": body, "is_internal": is_internal}
    )
    assert response.status_code == 201, response.text
    return response.json()


def _delete_comment(client, comment_id: str) -> None:
    response = client.request(
        "DELETE", f"/api/comments/{comment_id}", json={"reason": "тест B4-7"}
    )
    assert response.status_code == 200, response.text


async def _insert_system_comment(deal_id: uuid.UUID, body: str) -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.crm.models import DealComment

    async with session_scope() as session:
        comment = DealComment(deal_id=deal_id, author_id=None, body=body, is_system=True)
        session.add(comment)
        await session.flush()
        return comment.id


async def _insert_comments(deal_id: uuid.UUID, count: int) -> None:
    from app.core.db import session_scope
    from app.modules.crm.models import DealComment

    async with session_scope() as session:
        session.add_all(
            DealComment(deal_id=deal_id, author_id=None, body=f"Комментарий {i}", is_system=True)
            for i in range(count)
        )


async def _soft_delete_deal(deal_id: uuid.UUID) -> None:
    from app.core.db import session_scope
    from app.modules.crm.models import Deal

    async with session_scope() as session:
        deal = await session.get(Deal, deal_id)
        assert deal is not None
        deal.deleted_at = dt.datetime.now(dt.UTC)


async def _denied_count(actor_id: uuid.UUID) -> int:
    from sqlalchemy import func, select

    from app.core.db import session_scope
    from app.modules.audit.models import AuditLog

    async with session_scope() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "ACCESS_DENIED", AuditLog.actor_id == actor_id)
            )
            or 0
        )


async def _viewed_count(deal_id: uuid.UUID) -> int:
    from sqlalchemy import func, select

    from app.core.db import session_scope
    from app.modules.audit.models import AuditLog

    async with session_scope() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(
                    AuditLog.action == "DEAL_COMMENTS_VIEWED",
                    AuditLog.entity_type == "deal",
                    AuditLog.entity_id == deal_id,
                )
            )
            or 0
        )


class TestAuditorSeesTheComments:
    def test_auditor_sees_plain_internal_system_and_masked_text(self, client) -> None:
        deal = _setup_deal(client)
        plain = _add_comment(
            client, deal, body="Звонил Иванову, телефон +7 999 123-45-12, email i@vuz.ru"
        )
        internal = _add_comment(client, deal, body="Внутренняя заметка", is_internal=True)
        system_id = run(client, _insert_system_comment, uuid.UUID(deal["id"]), "Системное событие")
        to_delete = _add_comment(client, deal, body="Это удалят")
        _delete_comment(client, to_delete["id"])

        auditor = create_user(client, "AUDITOR")
        sign_in(client, auditor)

        response = client.get(_audit_url(deal["id"]))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["deal"] == {"id": deal["id"], "number": deal["number"], "deleted": False}

        by_id = {item["id"]: item for item in body["items"]}
        assert "+7999" not in by_id[plain["id"]]["body"]
        assert "i@vuz.ru" not in by_id[plain["id"]]["body"]
        assert by_id[plain["id"]]["is_deleted"] is False

        assert by_id[internal["id"]]["is_internal"] is True
        assert by_id[internal["id"]]["body"] == "Внутренняя заметка"

        assert by_id[str(system_id)]["is_system"] is True

        deleted_item = by_id[to_delete["id"]]
        assert deleted_item["is_deleted"] is True
        assert deleted_item["body"] is None
        assert deleted_item["deleted_at"] is not None

    def test_auditor_still_cannot_read_the_deal_itself(self, client) -> None:
        deal = _setup_deal(client)
        _add_comment(client, deal, body="Комментарий")

        auditor = create_user(client, "AUDITOR")
        sign_in(client, auditor)

        assert client.get(f"/api/deals/{deal['id']}").status_code == 403
        assert client.get(f"/api/deals/{deal['id']}/comments").status_code == 403
        # Но новая ручка работает:
        assert client.get(_audit_url(deal["id"])).status_code == 200

    def test_me_has_the_new_scope_but_not_deal_read(self, client) -> None:
        auditor = create_user(client, "AUDITOR")
        sign_in(client, auditor)
        scopes = client.get("/api/me").json()["scopes"]
        assert "comment:audit_read" in scopes
        assert "deal:read" not in scopes

    def test_each_call_records_exactly_one_deal_comments_viewed(self, client) -> None:
        deal = _setup_deal(client)
        _add_comment(client, deal, body="Комментарий")
        deal_id = uuid.UUID(deal["id"])

        auditor = create_user(client, "AUDITOR")
        sign_in(client, auditor)

        assert run(client, _viewed_count, deal_id) == 0
        assert client.get(_audit_url(deal["id"])).status_code == 200
        assert run(client, _viewed_count, deal_id) == 1
        assert client.get(_audit_url(deal["id"])).status_code == 200
        assert run(client, _viewed_count, deal_id) == 2

    def test_soft_deleted_deal_is_still_readable(self, client) -> None:
        deal = _setup_deal(client)
        _add_comment(client, deal, body="Комментарий до удаления")
        run(client, _soft_delete_deal, uuid.UUID(deal["id"]))

        auditor = create_user(client, "AUDITOR")
        sign_in(client, auditor)

        response = client.get(_audit_url(deal["id"]))
        assert response.status_code == 200, response.text
        assert response.json()["deal"]["deleted"] is True
        assert len(response.json()["items"]) == 1

    def test_nonexistent_deal_is_404(self, client) -> None:
        auditor = create_user(client, "AUDITOR")
        sign_in(client, auditor)
        response = client.get(_audit_url(str(uuid.uuid4())))
        assert response.status_code == 404, response.text

    def test_pagination_three_pages_without_overlap(self, client) -> None:
        deal = _setup_deal(client)
        run(client, _insert_comments, uuid.UUID(deal["id"]), 120)

        auditor = create_user(client, "AUDITOR")
        sign_in(client, auditor)

        seen: set[str] = set()
        cursor: str | None = None
        pages = 0
        while True:
            params: dict[str, Any] = {"limit": 50}
            if cursor:
                params["cursor"] = cursor
            response = client.get(_audit_url(deal["id"]), params=params)
            assert response.status_code == 200, response.text
            body = response.json()
            ids = {item["id"] for item in body["items"]}
            assert not (ids & seen), "страницы не должны повторяться"
            seen |= ids
            pages += 1
            cursor = body["next_cursor"]
            if cursor is None:
                break
        assert len(seen) == 120
        assert pages == 3


class TestOtherRolesAreDenied:
    @pytest.mark.parametrize("role", ["KAM", "HEAD", "INTEGRATION"])
    def test_role_gets_403_and_is_logged(self, client, role: str) -> None:
        deal = _setup_deal(client)
        _add_comment(client, deal, body="Комментарий")

        user = create_user(client, role)
        sign_in(client, user)

        assert run(client, _denied_count, user.id) == 0
        response = client.get(_audit_url(deal["id"]))
        assert response.status_code == 403, response.text
        assert run(client, _denied_count, user.id) == 1
