"""B2-7: `TransitionRequest.attachments` и условия `step_attachments.<категория>`.

До этой задачи поле `attachments` принималось и ничего не делало (комментарий в коде это
признавал), а DSL не умел различать «файл приложен на этой сделке» от «файл приложен именно
на этом шаге». Воронка для тестов — минимальный граф с одним переходом под условием
`step_attachments.contract exists`, а не полный сид v2: так тест не зависит от решений B2-1
про конкретные шаги задания и проверяет именно механизм привязки.

Требует `TEST_DATABASE_URL` — настоящая Postgres, как и остальные сквозные тесты
(см. докстринг `tests/conftest.py`).
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import by_code, create_deal, create_published_workflow, login, transition_deal

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _step_graph_body() -> dict:
    """Шаг 4 (initial) -> шаг 6 (won), переход под условием `step_attachments.contract exists`.
    Терминальный тип `won`, а не второй `intermediate`: граф должен иметь терминальный статус,
    и тесту не нужен отдельный переход в него."""
    return {
        "statuses": [
            {"code": "step4", "name": "Шаг 4: обмен документами", "type": "initial", "sort_order": 10},
            {"code": "step6", "name": "Шаг 6: подписание", "type": "won", "sort_order": 20},
        ],
        "transitions": [
            {
                "from_status": "step4",
                "to_status": "step6",
                "name": "К подписанию",
                "conditions": {"field": "step_attachments.contract", "op": "exists"},
            }
        ],
        "sla_rules": [],
    }


def _scene(client):
    """Пользователь, опубликованная воронка (шаг4 -> шаг6) и сделка на шаге 4. ADMIN — у неё
    есть все права (workflow:write, deal:*), не нужно переключаться между ролями."""
    owner = login(client, "ADMIN")
    graph = create_published_workflow(client, _step_graph_body(), deal_type="b2b")
    statuses = by_code(graph)
    deal = create_deal(client, graph["workflow"]["id"])
    return owner, graph, statuses, deal


def _upload_attachment(client, deal_id: str, *, category: str = "contract") -> str:
    """Создаёт готовый файл напрямую в БД (S3 в тестах недоступен — тот же приём, что
    `tests/test_files_access.py::_file`) и привязывает его к сделке через настоящее API
    `POST /api/attachments`, чтобы проверить весь путь B2-7/B2-8, а не только сервис."""

    async def _file() -> uuid.UUID:
        from app.core.db import session_scope
        from app.modules.files.models import File

        async with session_scope() as session:
            file = File(
                storage_key=f"{uuid.uuid4()}/dogovor.pdf",
                bucket="files",
                original_filename="dogovor.pdf",
                mime_type="application/pdf",
                size_bytes=184320,
                sha256=uuid.uuid4().hex + uuid.uuid4().hex,
                status="ready",
            )
            session.add(file)
            await session.flush()
            return file.id

    file_id = run(client, _file)
    response = client.post(
        "/api/attachments",
        json={
            "file_id": str(file_id),
            "entity_type": "deal",
            "entity_id": deal_id,
            "category": category,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


class TestStepAttachmentsCondition:
    def test_transition_without_the_required_category_is_422(self, client) -> None:
        _, _, statuses, deal = _scene(client)
        response = transition_deal(client, deal, statuses["step6"]["id"])
        assert response.status_code == 422, response.text
        body = response.json()
        assert body["code"] == "CRM-1201"
        assert any(u["field"] == "step_attachments.contract" for u in body["unmet"])

    def test_transition_with_attached_file_succeeds_and_binds_history(self, client) -> None:
        _, _, statuses, deal = _scene(client)
        attachment_id = _upload_attachment(client, deal["id"])

        response = transition_deal(
            client, deal, statuses["step6"]["id"], attachments=[attachment_id]
        )

        assert response.status_code == 200, response.text
        new_deal = response.json()["deal"]
        assert new_deal["status_id"] == statuses["step6"]["id"]

        async def _attachment_state() -> dict:
            from app.core.db import session_scope
            from app.modules.files.models import Attachment

            async with session_scope() as session:
                row = await session.get(Attachment, uuid.UUID(attachment_id))
                return {
                    "status_id": str(row.status_id) if row.status_id else None,
                    "has_history": row.status_history_id is not None,
                }

        state = run(client, _attachment_state)
        assert state["status_id"] == statuses["step4"]["id"]
        assert state["has_history"] is True

    def test_wrong_category_does_not_satisfy_condition(self, client) -> None:
        _, _, statuses, deal = _scene(client)
        attachment_id = _upload_attachment(client, deal["id"], category="act")

        response = transition_deal(
            client, deal, statuses["step6"]["id"], attachments=[attachment_id]
        )

        assert response.status_code == 422, response.text
        assert response.json()["code"] == "CRM-1201"

    def test_foreign_attachment_id_is_rejected(self, client) -> None:
        _, _, statuses, deal = _scene(client)
        other_deal = create_deal(client, deal["workflow_id"])
        foreign_attachment = _upload_attachment(client, other_deal["id"])

        response = transition_deal(
            client, deal, statuses["step6"]["id"], attachments=[foreign_attachment]
        )

        assert response.status_code == 422, response.text
        assert response.json()["code"] == "CRM-1001"

    def test_duplicate_attachment_id_in_the_same_request_is_rejected(self, client) -> None:
        _, _, statuses, deal = _scene(client)
        attachment_id = _upload_attachment(client, deal["id"])

        response = transition_deal(
            client,
            deal,
            statuses["step6"]["id"],
            attachments=[attachment_id, attachment_id],
        )

        assert response.status_code == 422, response.text
        assert response.json()["code"] == "CRM-1001"

    def test_rejected_transition_does_not_leave_the_attachment_bound(self, client) -> None:
        """Переход без комментария к той же паре, но с явно нарушенными условиями (чужая
        категория) не должен привязать вложение к записи истории — её попросту не будет."""
        _, _, statuses, deal = _scene(client)
        attachment_id = _upload_attachment(client, deal["id"], category="act")

        response = transition_deal(
            client, deal, statuses["step6"]["id"], attachments=[attachment_id]
        )
        assert response.status_code == 422, response.text

        async def _attachment_state() -> dict:
            from app.core.db import session_scope
            from app.modules.files.models import Attachment

            async with session_scope() as session:
                row = await session.get(Attachment, uuid.UUID(attachment_id))
                return {"status_history_id": row.status_history_id}

        state = run(client, _attachment_state)
        assert state["status_history_id"] is None

    def test_attachment_from_a_previous_step_counts_in_attachments_not_step_attachments(
        self, client
    ) -> None:
        """Файл, приложенный на предыдущем шаге, засчитывается `attachments.contract`
        (вся сделка), но не `step_attachments.contract` (только текущий шаг) — условие
        перехода проверяет именно `step_attachments.*`."""
        owner, graph, statuses, deal = _scene(client)
        attachment_id = _upload_attachment(client, deal["id"])

        async def _move_attachment_to_other_status() -> None:
            from app.core.db import session_scope
            from app.modules.files.models import Attachment

            async with session_scope() as session:
                row = await session.get(Attachment, uuid.UUID(attachment_id))
                row.status_id = statuses["step6"]["id"]

        run(client, _move_attachment_to_other_status)

        response = transition_deal(
            client, deal, statuses["step6"]["id"], attachments=[]
        )
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "CRM-1201"
