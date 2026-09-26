"""Очередь отчётов: без двойной генерации, без текста исключений клиенту, с аудитом выдачи.

Настоящая PostgreSQL (`TEST_DATABASE_URL`); хранилище и сборка отчёта подменяются.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import functools
import threading
import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


async def _clear_queue() -> None:
    """Чужие незавершённые задания закрываются: тик очереди берёт всё, что стоит в очереди, и
    считает `processing` как занятые места (тесты других файлов оставляют такие строки)."""
    from sqlalchemy import update

    from app.core.db import session_scope
    from app.modules.reporting.models import ReportJob

    async with session_scope() as session:
        await session.execute(
            update(ReportJob)
            .where(ReportJob.status.in_(["queued", "processing"]))
            .values(status="failed")
        )


async def _queued_job(owner_id: uuid.UUID) -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.reporting.models import ReportJob

    async with session_scope() as session:
        job = ReportJob(
            template_code="deal_funnel", format="xlsx", requested_by=owner_id, status="queued"
        )
        session.add(job)
        await session.flush()
        return job.id


async def _job(job_id: uuid.UUID):
    from app.core.db import session_scope
    from app.modules.reporting.models import ReportJob

    async with session_scope() as session:
        job = await session.get(ReportJob, job_id)
        session.expunge(job)
        return job


class TestQueue:
    def test_the_error_shown_to_the_client_hides_the_exception_text(
        self, client, monkeypatch
    ) -> None:
        from app.modules.reporting.service import ReportJobService
        from app.modules.reporting.tasks import sweep_report_jobs

        async def boom(self, job, principal):
            raise RuntimeError('relation "secret_table" does not exist: SELECT password FROM x')

        monkeypatch.setattr(ReportJobService, "generate", boom)
        owner = run(client, _make_user, "KAM")
        run(client, _clear_queue)
        job_id = run(client, _queued_job, owner.id)

        result = run(client, sweep_report_jobs, {})

        job = run(client, _job, job_id)
        assert result["failed"] == 1
        assert job.status == "failed"
        assert "secret_table" not in job.error and "SELECT" not in job.error
        assert "Код ошибки" in job.error

    def test_a_job_held_by_another_tick_is_skipped(self, client, monkeypatch) -> None:
        from sqlalchemy import select

        from app.core.db import get_session_factory
        from app.modules.reporting.models import ReportJob
        from app.modules.reporting.service import ReportJobService
        from app.modules.reporting.tasks import sweep_report_jobs

        generated: list[uuid.UUID] = []

        async def fake_generate(self, job, principal):
            generated.append(job.id)
            job.status = "completed"
            await self._session.flush()

        monkeypatch.setattr(ReportJobService, "generate", fake_generate)
        owner = run(client, _make_user, "KAM")
        run(client, _clear_queue)
        job_id = run(client, _queued_job, owner.id)

        async def _scenario() -> tuple[int, int]:
            async with get_session_factory()() as holder:
                # Другой тик уже взял задание: строка под замком, статус в БД всё ещё `queued`.
                await holder.execute(
                    select(ReportJob).where(ReportJob.id == job_id).with_for_update()
                )
                skipped = await sweep_report_jobs({})
                await holder.rollback()
            taken = await sweep_report_jobs({})
            return skipped["processed"], taken["processed"]

        assert run(client, _scenario) == (0, 1)
        assert generated == [job_id]

    def test_overlapping_ticks_build_a_report_once(self, client, monkeypatch) -> None:
        from app.modules.reporting.service import ReportJobService
        from app.modules.reporting.tasks import sweep_report_jobs

        generated: list[uuid.UUID] = []

        async def slow_generate(self, job, principal):
            generated.append(job.id)
            await asyncio.sleep(0.3)  # отчёт считается дольше, чем до следующего тика
            job.status = "completed"
            await self._session.flush()

        monkeypatch.setattr(ReportJobService, "generate", slow_generate)
        owner = run(client, _make_user, "KAM")
        run(client, _clear_queue)
        job_id = run(client, _queued_job, owner.id)

        async def _two_ticks() -> None:
            await asyncio.gather(sweep_report_jobs({}), sweep_report_jobs({}))

        run(client, _two_ticks)

        assert generated == [job_id]
        assert run(client, _job, job_id).status == "completed"


class TestRenderOffTheLoop:
    def test_the_report_is_rendered_in_a_worker_thread(self, client, monkeypatch) -> None:
        import app.modules.reporting.service as reporting_service

        async def ensure_bucket(bucket: str) -> None:
            return None

        async def upload(*, bucket: str, key: str, body: bytes, content_type: str) -> None:
            return None

        seen: list[int] = []

        def fake_render(dataset, *, format: str, template_code: str) -> bytes:
            seen.append(threading.get_ident())
            return b"rendered"

        monkeypatch.setattr(reporting_service, "ensure_bucket", ensure_bucket)
        monkeypatch.setattr(reporting_service, "upload_object_bytes", upload)
        monkeypatch.setattr(reporting_service, "render_report", fake_render)

        async def _loop_thread() -> int:
            return threading.get_ident()

        async def _template() -> None:
            from sqlalchemy import select

            from app.core.db import session_scope
            from app.modules.reporting.models import ReportTemplate

            async with session_scope() as session:
                if not await session.scalar(
                    select(ReportTemplate.id).where(ReportTemplate.code == "sla_compliance")
                ):
                    session.add(
                        ReportTemplate(
                            code="sla_compliance",
                            name="sla_compliance",
                            query_def={"kind": "sla_compliance"},
                            allowed_roles=[],
                            default_params={},
                            output_formats=["xlsx", "pdf"],
                            is_active=True,
                        )
                    )

        run(client, _template)
        client.headers["X-CSRF-Token"] = authenticate(client, run(client, _make_user, "KAM"))

        response = client.post(
            "/api/reports", json={"template_code": "sla_compliance", "format": "xlsx"}
        )

        assert response.status_code == 201, response.text
        assert len(seen) == 1
        assert seen[0] != run(client, _loop_thread)


class TestDownload:
    @staticmethod
    async def _completed(owner_id: uuid.UUID, *, allowed_roles: list[str]) -> uuid.UUID:
        from app.core.db import session_scope
        from app.modules.files.models import File
        from app.modules.reporting.models import ReportJob, ReportTemplate

        code = f"tpl-{uuid.uuid4().hex[:10]}"
        async with session_scope() as session:
            session.add(
                ReportTemplate(
                    code=code,
                    name=code,
                    query_def={"kind": "deal_funnel"},
                    allowed_roles=allowed_roles,
                    default_params={},
                    output_formats=["xlsx"],
                    is_active=True,
                )
            )
            file = File(
                storage_key=f"{uuid.uuid4()}/report.xlsx",
                bucket="reports",
                original_filename="Отчёт.xlsx",
                mime_type="application/vnd.ms-excel",
                size_bytes=10,
                status="ready",
            )
            session.add(file)
            await session.flush()
            job = ReportJob(
                template_code=code,
                format="xlsx",
                requested_by=owner_id,
                status="completed",
                file_id=file.id,
                expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=1),
            )
            session.add(job)
            await session.flush()
            return job.id

    @staticmethod
    async def _actions(job_id: uuid.UUID) -> list[tuple[str, str | None]]:
        from sqlalchemy import select

        from app.core.db import session_scope
        from app.modules.audit.models import AuditLog

        async with session_scope() as session:
            rows = await session.execute(
                select(AuditLog.action, AuditLog.entity_type).where(AuditLog.entity_id == job_id)
            )
            return [(row[0], row[1]) for row in rows]

    @pytest.fixture(autouse=True)
    def _storage(self, monkeypatch):
        import app.modules.reporting.service as reporting_service

        async def presigned(**kwargs) -> str:
            return "https://storage.invalid/get"

        monkeypatch.setattr(reporting_service, "generate_presigned_get", presigned)

    def test_issuing_the_link_is_written_to_the_audit_log(self, client) -> None:
        owner = run(client, _make_user, "KAM")
        client.headers["X-CSRF-Token"] = authenticate(client, owner)
        job_id = run(client, functools.partial(self._completed, owner.id, allowed_roles=[]))

        response = client.get(f"/api/reports/{job_id}/download")

        assert response.status_code == 200, response.text
        assert ("FILE_DOWNLOADED", "report_job") in run(client, self._actions, job_id)

    def test_a_role_that_lost_access_to_the_template_gets_no_link(self, client) -> None:
        # Шаблон открыт только для HEAD; автор отчёта (когда-то HEAD) теперь KAM.
        owner = run(client, _make_user, "KAM")
        client.headers["X-CSRF-Token"] = authenticate(client, owner)
        job_id = run(client, functools.partial(self._completed, owner.id, allowed_roles=["HEAD"]))

        response = client.get(f"/api/reports/{job_id}/download")

        assert response.status_code == 403, response.text
        assert ("FILE_DOWNLOADED", "report_job") not in run(client, self._actions, job_id)
