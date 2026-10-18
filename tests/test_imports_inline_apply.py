"""B3-12: небольшое задание (`IMPORT_INLINE_MAX_ROWS` строк, по умолчанию 500) применяется сразу
в `POST /api/imports/{id}/apply`, большее — уходит в фоновую обработку как раньше."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.core.errors import ErrorCode
from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import create_user, login, sign_in
from tests.import_helpers import make_inn, start_import, token, xlsx_bytes

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

ORG_HEADERS = ["Наименование", "ИНН"]


def _org_rows(count: int, tok: str) -> list[list[str]]:
    return [[f"Организация {tok}-{i}", make_inn()] for i in range(count)]


class TestInlineApply:
    def test_small_job_completes_within_the_apply_request(self, client, monkeypatch) -> None:
        tok = token()
        head = login(client, "HEAD")
        content = xlsx_bytes(ORG_HEADERS, _org_rows(10, tok))
        started = start_import(
            client,
            monkeypatch,
            head,
            entity_type="organization",
            content=content,
            source_format="xlsx",
        )
        assert started["dry"]["error_rows"] == 0

        response = client.post(f"/api/imports/{started['id']}/apply")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] in ("completed", "completed_with_errors")
        assert body["processed_rows"] == 10
        assert body["finished_at"] is not None

    def test_large_job_stays_applying_for_the_background_task(self, client, monkeypatch) -> None:
        tok = token()
        head = login(client, "HEAD")
        content = xlsx_bytes(ORG_HEADERS, _org_rows(600, tok))
        started = start_import(
            client,
            monkeypatch,
            head,
            entity_type="organization",
            content=content,
            source_format="xlsx",
        )
        assert started["dry"]["error_rows"] == 0

        response = client.post(f"/api/imports/{started['id']}/apply")
        assert response.status_code == 200, response.text
        body = response.json()
        # Без фоновой задачи (не запущена в тесте) задание остаётся в `applying`, не завершается
        # внутри самого запроса — выше лимита инлайн-применения.
        assert body["status"] == "applying"
        assert body["finished_at"] is None


async def _audit_actors(job_id: str, action: str) -> list[object]:
    """`actor_id` записей журнала с данным действием по заданию (в порядке записи)."""
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.audit.models import AuditLog

    async with session_scope() as session:
        rows = await session.execute(
            select(AuditLog.actor_id)
            .where(AuditLog.entity_id == uuid.UUID(job_id), AuditLog.action == action)
            .order_by(AuditLog.created_at)
        )
        return list(rows.scalars().all())


class TestInlineApplyGuards:
    def test_audit_actor_is_the_user_who_applies_not_the_initiator(
        self, client, monkeypatch
    ) -> None:
        tok = token()
        head = login(client, "HEAD")
        content = xlsx_bytes(ORG_HEADERS, _org_rows(3, tok))
        started = start_import(
            client,
            monkeypatch,
            head,
            entity_type="organization",
            content=content,
            source_format="xlsx",
        )
        admin = create_user(client, "ADMIN")
        sign_in(client, admin)

        response = client.post(f"/api/imports/{started['id']}/apply")

        assert response.status_code == 200, response.text
        assert response.json()["status"] in ("completed", "completed_with_errors")
        # Применяет ADMIN, загружал HEAD: события «начато»/«применено» принадлежат тому, кто нажал
        # кнопку, а не инициатору задания (в фоновой задаче актора запроса нет, там — инициатор).
        assert run(client, _audit_actors, started["id"], "IMPORT_STARTED") == [admin.id]
        assert run(client, _audit_actors, started["id"], "IMPORT_APPLIED") == [admin.id]

    def test_concurrent_apply_of_the_same_job_is_applied_once(self, client, monkeypatch) -> None:
        tok = token()
        head = login(client, "HEAD")
        content = xlsx_bytes(ORG_HEADERS, _org_rows(5, tok))
        started = start_import(
            client,
            monkeypatch,
            head,
            entity_type="organization",
            content=content,
            source_format="xlsx",
        )
        job_id = uuid.UUID(started["id"])

        async def race() -> list[object]:
            from app.core.db import session_scope
            from app.core.errors import AppError
            from app.modules.imports.service import ImportService

            first_applied = asyncio.Event()

            async def apply(*, hold: bool) -> object:
                async with session_scope() as session:
                    service = ImportService(session)
                    job = await service.get_or_404(job_id)
                    if not hold:
                        # Второй запрос прочитал задание, пока первый ещё не закоммитился.
                        await first_applied.wait()
                        job = await service.get_or_404(job_id)
                    try:
                        job = await service.start_apply(job)
                    except AppError as exc:
                        return exc.code
                    if hold:
                        first_applied.set()
                        await asyncio.sleep(0.5)  # транзакция открыта: коммит — на выходе
                    return job.status

            return list(await asyncio.gather(apply(hold=True), apply(hold=False)))

        first, second = run(client, race)

        assert first in ("completed", "completed_with_errors")
        assert second == ErrorCode.IMPORT_NOT_APPLICABLE
        assert len(run(client, _audit_actors, started["id"], "IMPORT_APPLIED")) == 1
