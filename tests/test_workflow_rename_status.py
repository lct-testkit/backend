"""B2-5: `PATCH /workflows/{id}/statuses/{status_id}` — переименование статуса Руководителем.

Требует `TEST_DATABASE_URL` — настоящая Postgres (см. докстринг `tests/conftest.py`).
"""

from __future__ import annotations

import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import create_published_workflow, get_graph, login

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _rename(client, workflow_id: str, status_id: str, name: str, version: int):
    return client.patch(
        f"/api/workflows/{workflow_id}/statuses/{status_id}",
        json={"name": name},
        headers={"If-Match": str(version)},
    )


def _scene(client):
    login(client, "ADMIN")
    graph = create_published_workflow(client)
    status = next(s for s in graph["statuses"] if s["code"] == "work")
    return graph, status


async def _audit_changes(workflow_status_id: str) -> list[dict]:
    import uuid

    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.audit.models import AuditLog

    async with session_scope() as session:
        rows = await session.execute(
            select(AuditLog.changes).where(
                AuditLog.action == "WORKFLOW_STATUS_RENAMED",
                AuditLog.entity_id == uuid.UUID(workflow_status_id),
            )
        )
        return list(rows.scalars())


class TestHeadRenamesStatus:
    def test_head_renames_and_the_snapshot_follows(self, client) -> None:
        graph, status = _scene(client)
        login(client, "HEAD")

        response = _rename(
            client,
            graph["workflow"]["id"],
            status["id"],
            "В процессе",
            graph["workflow"]["version"],
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["name"] == "В процессе"
        assert body["code"] == "work"  # код не меняется

        updated = get_graph(client, graph["workflow"]["id"])
        live = next(s for s in updated["statuses"] if s["id"] == status["id"])
        assert live["name"] == "В процессе"
        # Снимок переписан тем же именем — не только черновик, поэтому флаг не включается
        # только из-за переименования.
        assert updated["workflow"]["has_unpublished_changes"] is False
        assert updated["workflow"]["version"] == graph["workflow"]["version"] + 1

    def test_kam_is_forbidden(self, client) -> None:
        graph, status = _scene(client)
        login(client, "KAM")

        response = _rename(
            client, graph["workflow"]["id"], status["id"], "Новое имя", graph["workflow"]["version"]
        )

        assert response.status_code == 403, response.text
        assert response.json()["code"] == "CRM-1102"

    def test_stale_version_is_a_conflict(self, client) -> None:
        graph, status = _scene(client)
        login(client, "HEAD")

        response = _rename(
            client,
            graph["workflow"]["id"],
            status["id"],
            "Новое имя",
            graph["workflow"]["version"] + 5,
        )

        assert response.status_code == 409, response.text
        assert response.json()["code"] == "CRM-1002"

    def test_blank_name_is_rejected(self, client) -> None:
        graph, status = _scene(client)
        login(client, "HEAD")

        response = _rename(
            client, graph["workflow"]["id"], status["id"], "   ", graph["workflow"]["version"]
        )

        assert response.status_code == 422, response.text

    def test_archived_status_is_rejected(self, client) -> None:
        import datetime as dt
        import uuid

        async def _archive(status_id: str) -> None:
            from app.core.db import session_scope
            from app.modules.workflow.models import WorkflowStatus

            async with session_scope() as session:
                row = await session.get(WorkflowStatus, uuid.UUID(status_id))
                row.is_archived = True
                row.archived_at = dt.datetime.now(dt.UTC)

        graph, status = _scene(client)
        run(client, _archive, status["id"])
        login(client, "HEAD")

        response = _rename(
            client, graph["workflow"]["id"], status["id"], "Новое имя", graph["workflow"]["version"]
        )

        assert response.status_code == 422, response.text

    def test_renaming_to_the_same_name_does_not_bump_the_version(self, client) -> None:
        graph, status = _scene(client)
        login(client, "HEAD")

        response = _rename(
            client,
            graph["workflow"]["id"],
            status["id"],
            status["name"],
            graph["workflow"]["version"],
        )

        assert response.status_code == 200, response.text
        updated = get_graph(client, graph["workflow"]["id"])
        assert updated["workflow"]["version"] == graph["workflow"]["version"]

    def test_audit_has_old_and_new_name(self, client) -> None:
        graph, status = _scene(client)
        login(client, "HEAD")

        response = _rename(
            client,
            graph["workflow"]["id"],
            status["id"],
            "Переименованный статус",
            graph["workflow"]["version"],
        )
        assert response.status_code == 200, response.text

        (changes,) = run(client, _audit_changes, status["id"])
        assert changes["name"]["old"] == status["name"]
        assert changes["name"]["new"] == "Переименованный статус"
