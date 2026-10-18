"""B1-14 (backend-часть — только `reporting/seed.py`, этап деплоя `deploy/seed/reports.mjs`
не входит в этот репозиторий): `seed_demo_report_history` больше не заводит записи истории
отчётов без файла — только убирает пустышки, оставленные её прежней версией. Настоящая
Postgres обязательна (`tests/conftest.py`)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select

from app.modules.reporting.seed import seed_demo_report_history
from tests.conftest import TEST_DATABASE_URL, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


async def _make_user() -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.identity.models import User

    async with session_scope() as session:
        user = User(
            keycloak_id=str(uuid.uuid4()),
            email=f"seed-demo-{uuid.uuid4().hex[:10]}@rt-it-school.ru",
            full_name="Сидоров С. С.",
            role="ADMIN",
            status="active",
            consent_version="1.0",
        )
        session.add(user)
        await session.flush()
        return user.id


async def _make_job(
    requester_id: uuid.UUID, file_id: uuid.UUID | None, seed_marker: str | None
) -> uuid.UUID:
    """`file_id` — настоящая строка `files` (у `report_jobs.file_id` есть FK), не произвольный
    UUID: без неё `INSERT` падает `ForeignKeyViolationError`, не тихо игнорирует отсутствующую
    ссылку."""
    from app.core.db import session_scope
    from app.modules.files.models import File
    from app.modules.reporting.models import ReportJob

    params: dict = {}
    if seed_marker is not None:
        params["_seed"] = seed_marker
    async with session_scope() as session:
        if file_id is not None:
            session.add(
                File(
                    id=file_id,
                    storage_key=f"{uuid.uuid4()}/report.xlsx",
                    bucket="reports",
                    original_filename="Отчёт.xlsx",
                    mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    size_bytes=1,
                    status="ready",
                    uploaded_by=requester_id,
                    refcount=1,
                )
            )
            await session.flush()
        job = ReportJob(
            template_code="sla_compliance",
            format="xlsx",
            requested_by=requester_id,
            status="completed",
            row_count=0,
            params=params,
            file_id=file_id,
            finished_at=dt.datetime.now(dt.UTC),
        )
        session.add(job)
        await session.flush()
        return job.id


async def _job_exists(job_id: uuid.UUID) -> bool:
    from app.core.db import session_scope
    from app.modules.reporting.models import ReportJob

    async with session_scope() as session:
        return (
            await session.scalar(select(ReportJob.id).where(ReportJob.id == job_id))
        ) is not None


class TestSeedDemoReportHistory:
    def test_removes_a_completed_stub_without_a_file(self, client) -> None:
        requester_id = run(client, _make_user)
        job_id = run(client, _make_job, requester_id, None, "demo")

        async def _run() -> int:
            from app.core.db import session_scope

            async with session_scope() as session:
                return await seed_demo_report_history(session)

        removed = run(client, _run)
        assert removed >= 1
        assert run(client, _job_exists, job_id) is False

    def test_keeps_a_completed_job_that_has_a_real_file(self, client) -> None:
        requester_id = run(client, _make_user)
        file_id = uuid.uuid4()
        job_id = run(client, _make_job, requester_id, file_id, "demo")

        async def _run() -> int:
            from app.core.db import session_scope

            async with session_scope() as session:
                return await seed_demo_report_history(session)

        run(client, _run)
        assert run(client, _job_exists, job_id) is True

    def test_keeps_jobs_without_the_demo_marker_even_without_a_file(self, client) -> None:
        """Усечённый скоуп удаления — только `_seed=demo`, не любое `completed` без файла (у
        записи, чей файл удалил ретеншен, `_seed` нет вовсе, не только у неё, трогать её
        нельзя — ретеншен уже сделал своё дело правильно)."""
        requester_id = run(client, _make_user)
        job_id = run(client, _make_job, requester_id, None, None)

        async def _run() -> int:
            from app.core.db import session_scope

            async with session_scope() as session:
                return await seed_demo_report_history(session)

        run(client, _run)
        assert run(client, _job_exists, job_id) is True

    def test_is_idempotent(self, client) -> None:
        requester_id = run(client, _make_user)
        run(client, _make_job, requester_id, None, "demo")

        async def _run() -> int:
            from app.core.db import session_scope

            async with session_scope() as session:
                return await seed_demo_report_history(session)

        first = run(client, _run)
        assert first >= 1
        second = run(client, _run)
        assert second == 0
