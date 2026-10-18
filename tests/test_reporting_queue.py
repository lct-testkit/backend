"""B8-11: отчёты и импорт — постановка в очередь arq сразу после коммита, личный и
общий лимиты, видимость `processing` без ожидания cron.

Настоящая Postgres обязательна (`TEST_DATABASE_URL`); пул arq подменяется — реального
Redis не поднимаем (см. `tests/conftest.py`: `REDIS_URL` в тестах указывает на
`fakeredis`, а не настоящий сервер, поэтому `get_arq_pool()` подключиться не сможет).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _kam(client):
    kam = run(client, _make_user, "KAM")
    client.headers["X-CSRF-Token"] = authenticate(client, kam)
    return kam


def _stub_s3(monkeypatch) -> None:
    import app.modules.reporting.service as reporting_service

    async def fake_ensure_bucket(bucket: str) -> None:
        return None

    async def fake_upload(*, bucket: str, key: str, body: bytes, content_type: str) -> None:
        return None

    monkeypatch.setattr(reporting_service, "ensure_bucket", fake_ensure_bucket)
    monkeypatch.setattr(reporting_service, "upload_object_bytes", fake_upload)


def _ensure_template(client, code: str, *, kind: str | None = None) -> None:
    from sqlalchemy import select as sa_select

    from app.core.db import session_scope
    from app.modules.reporting.models import ReportTemplate

    async def _ensure() -> None:
        async with session_scope() as session:
            existing = await session.scalar(
                sa_select(ReportTemplate.id).where(ReportTemplate.code == code)
            )
            if existing is None:
                session.add(
                    ReportTemplate(
                        code=code,
                        name=code,
                        query_def={"kind": kind or code},
                        allowed_roles=[],
                        default_params={},
                        output_formats=["xlsx", "pdf"],
                        is_active=True,
                    )
                )

    run(client, _ensure)


def _force_heavy(monkeypatch) -> None:
    """Любой `kind` с оценщиком, подменённым на заведомо большую оценку строк — тот же
    приём, что и настоящие тяжёлые отчёты (`stuck_deals`/`deal_register`), без генерации
    тысяч строк в тесте."""
    import app.modules.reporting.service as reporting_service

    async def huge_estimate(session: Any, principal: Any, params: dict[str, Any]) -> int:
        return 999_999

    monkeypatch.setattr(reporting_service, "REPORT_ESTIMATORS", {"stuck_deals": huge_estimate})


class _FakeArqPool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], str | None]] = []

    async def enqueue_job(self, function: str, *args: Any, _job_id: str | None = None) -> None:
        self.calls.append((function, args, _job_id))


class TestHeavyReportEnqueue:
    def test_heavy_report_is_enqueued_only_after_commit(self, client, monkeypatch) -> None:
        import app.core.queue as queue_module

        _stub_s3(monkeypatch)
        _force_heavy(monkeypatch)
        _ensure_template(client, "stuck_deals")
        _kam(client)

        pool = _FakeArqPool()

        async def fake_get_pool() -> _FakeArqPool:
            return pool

        monkeypatch.setattr(queue_module, "get_arq_pool", fake_get_pool)

        response = client.post(
            "/api/reports", json={"template_code": "stuck_deals", "format": "xlsx"}
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["status"] == "queued"

        assert len(pool.calls) == 1
        function, args, job_id = pool.calls[0]
        assert function == "render_report_job"
        assert args == (body["id"],)
        assert job_id == f"report:{body['id']}"

    def test_enqueue_is_not_called_when_the_request_is_rejected(self, client, monkeypatch) -> None:
        """Лимит отбивает запрос ДО создания задания — значит и до постановки в очередь:
        `enqueue_after_commit` висит на коммите транзакции, а здесь транзакции не будет."""
        import app.core.queue as queue_module

        _stub_s3(monkeypatch)
        _force_heavy(monkeypatch)
        _ensure_template(client, "stuck_deals")
        kam = _kam(client)

        from app.core.db import session_scope
        from app.modules.reporting.models import ReportJob

        async def _fill_quota() -> None:
            async with session_scope() as session:
                for _ in range(10):
                    session.add(
                        ReportJob(
                            template_code="stuck_deals",
                            format="xlsx",
                            requested_by=kam.id,
                            status="queued",
                        )
                    )

        run(client, _fill_quota)

        pool = _FakeArqPool()

        async def fake_get_pool() -> _FakeArqPool:
            return pool

        monkeypatch.setattr(queue_module, "get_arq_pool", fake_get_pool)

        response = client.post(
            "/api/reports", json={"template_code": "stuck_deals", "format": "xlsx"}
        )
        assert response.status_code == 429, response.text
        assert pool.calls == []

    def test_started_at_is_committed_even_when_the_render_itself_fails(
        self, client, monkeypatch
    ) -> None:
        """B8-11: захват (`UPDATE ... RETURNING`, `status='processing', started_at=now()`) —
        своя, более ранняя транзакция, отдельная от самого рендера. Если бы `started_at`
        проставлялся вместе с рендером одной транзакцией (как раньше с `processing`), сбой
        рендера откатил бы и его — здесь он остаётся, даже когда `generate()` падает сразу."""
        import app.modules.reporting.tasks as reporting_tasks
        from app.modules.reporting.service import ReportJobService

        _stub_s3(monkeypatch)
        _ensure_template(client, "sla_compliance")
        kam = _kam(client)

        from app.core.db import session_scope
        from app.modules.reporting.models import ReportJob

        async def _insert_queued() -> str:
            async with session_scope() as session:
                job = ReportJob(
                    template_code="sla_compliance",
                    format="xlsx",
                    requested_by=kam.id,
                    status="queued",
                )
                session.add(job)
                await session.flush()
                return str(job.id)

        job_id = run(client, _insert_queued)

        async def boom(self, job, principal):
            raise RuntimeError("boom")

        monkeypatch.setattr(ReportJobService, "generate", boom)

        result = run(client, reporting_tasks.render_report_job, {}, job_id)
        assert result is False

        async def _reload() -> ReportJob:
            async with session_scope() as session:
                job = await session.get(ReportJob, uuid.UUID(job_id))
                session.expunge(job)
                return job

        job = run(client, _reload)
        assert job.status == "failed"
        assert job.started_at is not None


class TestQueueMaxLimit:
    def test_global_queue_limit_refuses_with_retry_after(self, client, monkeypatch) -> None:
        """Общий потолок очереди — маленькое число через monkeypatch, а не реальная заливка
        сотен строк: тесты делят одну БД без откатывающей транзакции на тест (см. докстринг
        модуля), и сотни чужих `queued`-записей пережили бы этот тест и мешали бы следующим."""
        import app.modules.reporting.service as reporting_service

        _stub_s3(monkeypatch)
        _ensure_template(client, "sla_compliance")
        _kam(client)

        from app.core.db import session_scope
        from app.modules.reporting.models import ReportJob

        async def _insert_one_queued() -> uuid.UUID:
            async with session_scope() as session:
                other = await _make_user()
                job = ReportJob(
                    template_code="sla_compliance",
                    format="xlsx",
                    requested_by=other.id,
                    status="queued",
                )
                session.add(job)
                await session.flush()
                return job.id

        run(client, _insert_one_queued)

        real_settings = reporting_service.get_settings()
        patched = real_settings.model_copy(update={"reports_queue_max": 1})
        monkeypatch.setattr(reporting_service, "get_settings", lambda: patched)

        response = client.post(
            "/api/reports", json={"template_code": "sla_compliance", "format": "xlsx"}
        )
        assert response.status_code == 429, response.text
        assert response.json()["code"] == "CRM-1601"
        assert response.headers["Retry-After"] == "30"


class TestLightReportRateLimit:
    def test_31st_light_report_in_a_minute_is_rate_limited(self, client, monkeypatch) -> None:
        _stub_s3(monkeypatch)
        _ensure_template(client, "sla_compliance")
        _kam(client)

        request = {"template_code": "sla_compliance", "format": "xlsx"}
        for _ in range(30):
            response = client.post("/api/reports", json=request)
            assert response.status_code == 201, response.text

        refused = client.post("/api/reports", json=request)
        assert refused.status_code == 429, refused.text
        assert refused.json()["code"] == "CRM-8429"


class TestImportEnqueue:
    def test_apply_enqueues_run_import_job_after_commit(self, client, monkeypatch) -> None:
        import app.core.queue as queue_module

        admin = run(client, _make_user, "ADMIN")
        client.headers["X-CSRF-Token"] = authenticate(client, admin)

        from app.core.config import get_settings
        from app.core.db import session_scope
        from app.modules.imports.models import ImportJob, ImportJobStatus

        # B3-12: задание не больше `IMPORT_INLINE_MAX_ROWS` строк `start_apply` применяет сразу, в
        # очередь оно не попадает (статус уже терминальный). В очередь уходит только большое —
        # поэтому `total_rows` здесь выше лимита (раньше поле оставалось 0, и тест проверял
        # путь, которым такое задание больше не идёт).
        total_rows = get_settings().import_inline_max_rows + 1

        async def _validated_job() -> str:
            async with session_scope() as session:
                job = ImportJob(
                    entity_type="product",
                    source_format="xlsx",
                    file_id=uuid.uuid4(),
                    initiated_by=admin.id,
                    status=ImportJobStatus.VALIDATED.value,
                    mode="upsert",
                    total_rows=total_rows,
                    ok_rows=total_rows,
                    warn_rows=0,
                    error_rows=0,
                )
                session.add(job)
                await session.flush()
                return str(job.id)

        job_id = run(client, _validated_job)

        pool = _FakeArqPool()

        async def fake_get_pool() -> _FakeArqPool:
            return pool

        monkeypatch.setattr(queue_module, "get_arq_pool", fake_get_pool)

        response = client.post(f"/api/imports/{job_id}/apply")
        assert response.status_code == 200, response.text

        assert len(pool.calls) == 1
        function, args, job_key = pool.calls[0]
        assert function == "run_import_job"
        assert args == (job_id, "apply")
        assert job_key == f"import:{job_id}:apply"
        assert response.json()["status"] == "applying"

    def test_small_job_applied_inline_is_not_enqueued(self, client, monkeypatch) -> None:
        import app.core.queue as queue_module

        admin = run(client, _make_user, "ADMIN")
        client.headers["X-CSRF-Token"] = authenticate(client, admin)

        from app.core.db import session_scope
        from app.modules.imports.models import ImportJob, ImportJobStatus

        async def _small_job() -> str:
            async with session_scope() as session:
                job = ImportJob(
                    entity_type="product",
                    source_format="xlsx",
                    file_id=uuid.uuid4(),
                    initiated_by=admin.id,
                    status=ImportJobStatus.VALIDATED.value,
                    mode="upsert",
                    total_rows=1,
                    ok_rows=1,
                    warn_rows=0,
                    error_rows=0,
                )
                session.add(job)
                await session.flush()
                return str(job.id)

        job_id = run(client, _small_job)

        pool = _FakeArqPool()

        async def fake_get_pool() -> _FakeArqPool:
            return pool

        monkeypatch.setattr(queue_module, "get_arq_pool", fake_get_pool)

        response = client.post(f"/api/imports/{job_id}/apply")
        assert response.status_code == 200, response.text

        # Применено в самом запросе (в тесте у задания нет строк результата — нечего обрабатывать,
        # статус сразу терминальный); фоновая задача не нужна.
        assert response.json()["status"] in ("completed", "completed_with_errors")
        assert pool.calls == []
