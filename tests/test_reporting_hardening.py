"""Очередь отчётов: без двойной генерации, без текста исключений клиенту, с аудитом выдачи.

Настоящая PostgreSQL (`TEST_DATABASE_URL`); хранилище и сборка отчёта подменяются.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import functools
import threading
import time
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
    """B8-11: `sweep_report_jobs` больше не рендерит сама (это теперь делает задача arq
    `render_report_job`, поставленная `ReportJobService.create` сразу после коммита) — тик
    остаётся добором зависших `processing` и потерянных постановок. Эти тесты проверяют
    безопасность и конкурентность на уровне `render_report_job` напрямую: атомарный захват
    (`UPDATE ... WHERE status='queued' RETURNING`) даёт те же гарантии «взято ровно один раз»,
    что раньше давал `FOR UPDATE SKIP LOCKED`, но без необходимости держать замок на всё время
    рендера."""

    def test_the_error_shown_to_the_client_hides_the_exception_text(
        self, client, monkeypatch
    ) -> None:
        from app.modules.reporting.service import ReportJobService
        from app.modules.reporting.tasks import render_report_job

        async def boom(self, job, principal):
            raise RuntimeError('relation "secret_table" does not exist: SELECT password FROM x')

        monkeypatch.setattr(ReportJobService, "generate", boom)
        owner = run(client, _make_user, "KAM")
        run(client, _clear_queue)
        job_id = run(client, _queued_job, owner.id)

        result = run(client, render_report_job, {}, str(job_id))

        job = run(client, _job, job_id)
        assert result is False
        assert job.status == "failed"
        assert "secret_table" not in job.error and "SELECT" not in job.error
        assert "Код ошибки" in job.error

    def test_only_one_concurrent_claim_renders_the_same_job(self, client, monkeypatch) -> None:
        """Два параллельных вызова `render_report_job` на одно и то же задание (повторная
        постановка, перекрытие добора и прямой постановки) — атомарный `UPDATE ... RETURNING`
        отдаёт строку только одному; второй получает `None` и ничего не рендерит."""
        from app.modules.reporting.service import ReportJobService
        from app.modules.reporting.tasks import render_report_job

        generated: list[uuid.UUID] = []

        async def fake_generate(self, job, principal):
            generated.append(job.id)
            job.status = "completed"
            await self._session.flush()

        monkeypatch.setattr(ReportJobService, "generate", fake_generate)
        owner = run(client, _make_user, "KAM")
        run(client, _clear_queue)
        job_id = run(client, _queued_job, owner.id)

        async def _scenario() -> tuple[bool | None, bool | None]:
            return await asyncio.gather(
                render_report_job({}, str(job_id)), render_report_job({}, str(job_id))
            )

        results = run(client, _scenario)
        assert sorted(results, key=lambda r: r is None) == [True, None]
        assert generated == [job_id]
        assert run(client, _job, job_id).status == "completed"

    def test_two_different_jobs_render_concurrently_not_one_by_one(
        self, client, monkeypatch
    ) -> None:
        """Раздел 4.13: «конкурентность воркеров ограничена семафором (10)» — семафор по
        умолчанию (10) пропускает оба задания разом: если бы `render_report_job` сериализовал
        их (ждал освобождения семафора одно за другим), два задания по 0.3с заняли бы ~0.6с
        суммарно; конкурентно — внахлёст, и обе задачи укладываются в ~0.3с."""
        from app.modules.reporting.service import ReportJobService
        from app.modules.reporting.tasks import render_report_job

        started: list[float] = []

        async def overlapping_generate(self, job, principal):
            started.append(asyncio.get_running_loop().time())
            await asyncio.sleep(0.3)
            job.status = "completed"
            await self._session.flush()

        monkeypatch.setattr(ReportJobService, "generate", overlapping_generate)
        owner = run(client, _make_user, "KAM")
        run(client, _clear_queue)
        job_ids = [run(client, _queued_job, owner.id) for _ in range(2)]

        async def _scenario() -> list[bool | None]:
            return await asyncio.gather(*(render_report_job({}, str(jid)) for jid in job_ids))

        t0 = time.monotonic()
        results = run(client, _scenario)
        elapsed = time.monotonic() - t0

        assert results == [True, True]
        assert all(run(client, _job, jid).status == "completed" for jid in job_ids)
        # И оба рендера стартовали до того, как первый успел завершиться — не строго
        # последовательно (иначе второй начался бы не раньше t=0.3с).
        assert max(started) - min(started) < 0.3
        assert elapsed < 0.55, f"рендер занял {elapsed:.2f}с — похоже, задания шли одно за другим"

    def test_sweep_requeues_processing_stuck_longer_than_the_stale_threshold(
        self, client, monkeypatch
    ) -> None:
        """B8-11: воркер «упал» посреди рендера — `processing` со старым `started_at` тик
        возвращает в `queued`, чтобы его подхватил следующий добор/постановка."""
        from app.core.db import session_scope
        from app.modules.reporting.models import ReportJob
        from app.modules.reporting.tasks import sweep_report_jobs

        owner = run(client, _make_user, "KAM")
        run(client, _clear_queue)
        job_id = run(client, _queued_job, owner.id)

        async def _mark_stale_processing() -> None:
            async with session_scope() as session:
                job = await session.get(ReportJob, job_id)
                job.status = "processing"
                job.started_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=16)

        run(client, _mark_stale_processing)

        result = run(client, sweep_report_jobs, {})

        assert result["requeued"] == 1
        job = run(client, _job, job_id)
        assert job.status == "queued"
        assert job.started_at is None

    def test_sweep_does_not_requeue_processing_within_the_threshold(
        self, client, monkeypatch
    ) -> None:
        from app.core.db import session_scope
        from app.modules.reporting.models import ReportJob
        from app.modules.reporting.tasks import sweep_report_jobs

        owner = run(client, _make_user, "KAM")
        run(client, _clear_queue)
        job_id = run(client, _queued_job, owner.id)

        async def _mark_fresh_processing() -> None:
            async with session_scope() as session:
                job = await session.get(ReportJob, job_id)
                job.status = "processing"
                job.started_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)

        run(client, _mark_fresh_processing)

        result = run(client, sweep_report_jobs, {})

        assert result["requeued"] == 0
        assert run(client, _job, job_id).status == "processing"


class TestRenderOffTheLoop:
    def test_the_report_is_rendered_in_a_worker_thread(self, client, monkeypatch) -> None:
        import app.modules.reporting.service as reporting_service

        async def ensure_bucket(bucket: str) -> None:
            return None

        async def upload(*, bucket: str, key: str, body: bytes, content_type: str) -> None:
            return None

        seen: list[int] = []

        def fake_render(dataset, *, format: str, template_code: str, **kwargs: object) -> bytes:
            # B1-4/B1-5: `render_report` принимает `job_id`/`params`/`tz_name` (нужны только
            # формату json) — стаб должен принимать их же, иначе любой вызов падает `TypeError`
            # независимо от формата конкретного задания теста.
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
