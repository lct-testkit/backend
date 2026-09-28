"""Прикладные метрики: SLA, отчёты, импорт, кэш графа воронки.

Без БД: сессии — заглушки, значения читаются из реестра `prometheus_client`. Счётчики
процесса общие для всех тестов, поэтому сравниваются приращения (или уникальные метки).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import fakeredis.aioredis
import pytest
from prometheus_client import REGISTRY
from sqlalchemy.dialects import postgresql

from app.core import redis_client
from app.core.errors import AppError, ErrorCode
from app.modules.crm import tasks as crm_tasks
from app.modules.crm.models import Deal, SlaState
from app.modules.imports.models import ImportJob, ImportRowResult, ImportRowStatus
from app.modules.imports.service import ImportService
from app.modules.reporting import tasks as reporting_tasks
from app.modules.workflow.models import Workflow
from app.modules.workflow.service import get_cached_published_graph


def _sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _fake_scope(session: object) -> Callable[[], Any]:
    @asynccontextmanager
    async def scope() -> AsyncIterator[object]:
        yield session

    return scope


def _sql(statement: object) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))  # type: ignore[attr-defined]


# --- SLA --------------------------------------------------------------------


def _snapshots(
    workflow_id: uuid.UUID,
    status_id: uuid.UUID,
    codes: tuple[str, str] | None,
) -> crm_tasks._SlaSnapshots:
    key = (workflow_id, str(status_id))
    return crm_tasks._SlaSnapshots(
        # Без каналов уведомлять некого: сервис уведомлений в тесте не нужен.
        rules={key: {**crm_tasks._DEFAULT_RULE, "channels": []}},
        codes={key: codes} if codes else {},
    )


def _deal(workflow_id: uuid.UUID, status_id: uuid.UUID, *, elapsed: float, state: str) -> Deal:
    """Сделка, у которой израсходовано `elapsed` от срока SLA (1.2 — 120%)."""
    now = dt.datetime.now(dt.UTC)
    entered = now - dt.timedelta(minutes=100 * elapsed)
    return Deal(
        workflow_id=workflow_id,
        status_id=status_id,
        owner_id=uuid.uuid4(),
        sla_state=state,
        status_changed_at=entered,
        sla_due_at=entered + dt.timedelta(minutes=100),
    )


async def _process(deal: Deal, snapshots: crm_tasks._SlaSnapshots) -> dict[str, int]:
    counters = {"warned": 0, "breached": 0, "escalated": 0}
    session = SimpleNamespace(get=AsyncMock(return_value=None))
    await crm_tasks._process_deal(
        session,  # type: ignore[arg-type]
        deal,
        dt.datetime.now(dt.UTC),
        snapshots,
        counters,
    )
    return counters


async def test_breach_increments_violations_by_workflow_and_status_code() -> None:
    workflow_id, status_id = uuid.uuid4(), uuid.uuid4()
    workflow_code = f"wf-{uuid.uuid4().hex[:8]}"
    labels = {"workflow": workflow_code, "status": "negotiation"}
    snapshots = _snapshots(workflow_id, status_id, (workflow_code, "negotiation"))
    deal = _deal(workflow_id, status_id, elapsed=1.2, state=SlaState.OK.value)

    counters = await _process(deal, snapshots)

    assert counters["breached"] == 1
    assert deal.sla_state == SlaState.BREACHED.value
    assert _sample("crm_sla_violations_total", **labels) == 1


async def test_already_breached_deal_is_not_counted_again() -> None:
    workflow_id, status_id = uuid.uuid4(), uuid.uuid4()
    workflow_code = f"wf-{uuid.uuid4().hex[:8]}"
    labels = {"workflow": workflow_code, "status": "negotiation"}
    snapshots = _snapshots(workflow_id, status_id, (workflow_code, "negotiation"))
    deal = _deal(workflow_id, status_id, elapsed=1.2, state=SlaState.OK.value)

    await _process(deal, snapshots)
    await _process(deal, snapshots)  # следующий проход: состояние уже `breached`

    assert _sample("crm_sla_violations_total", **labels) == 1


async def test_warning_is_not_a_violation() -> None:
    workflow_id, status_id = uuid.uuid4(), uuid.uuid4()
    workflow_code = f"wf-{uuid.uuid4().hex[:8]}"
    snapshots = _snapshots(workflow_id, status_id, (workflow_code, "negotiation"))
    deal = _deal(workflow_id, status_id, elapsed=0.8, state=SlaState.OK.value)

    counters = await _process(deal, snapshots)

    assert counters == {"warned": 1, "breached": 0, "escalated": 0}
    assert (
        REGISTRY.get_sample_value(
            "crm_sla_violations_total", {"workflow": workflow_code, "status": "negotiation"}
        )
        is None
    )


async def test_status_missing_from_snapshot_is_labelled_unknown() -> None:
    workflow_id, status_id = uuid.uuid4(), uuid.uuid4()
    labels = {"workflow": "unknown", "status": "unknown"}
    before = _sample("crm_sla_violations_total", **labels)
    snapshots = _snapshots(workflow_id, status_id, None)
    deal = _deal(workflow_id, status_id, elapsed=1.2, state=SlaState.OK.value)

    await _process(deal, snapshots)

    assert _sample("crm_sla_violations_total", **labels) == before + 1


async def test_sla_snapshots_take_rules_and_codes_from_the_published_graph() -> None:
    workflow_id, other_id = uuid.uuid4(), uuid.uuid4()
    status_id = uuid.uuid4()
    graph = {
        "workflow_code": "ignored-in-favour-of-column",
        "statuses": [{"id": str(status_id), "code": "negotiation", "name": "Переговоры"}],
        "sla_rules": [{"status_id": str(status_id), "warn_threshold_pct": 50}],
    }
    rows = [(workflow_id, "b2b-sales", graph), (other_id, "draft-only", None)]
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(all=lambda: rows)),
    )

    snapshots = await crm_tasks._sla_snapshots(session, {workflow_id, other_id})  # type: ignore[arg-type]

    assert snapshots.codes == {(workflow_id, str(status_id)): ("b2b-sales", "negotiation")}
    rule = snapshots.rules[(workflow_id, str(status_id))]
    assert rule["warn_threshold_pct"] == 50
    assert rule["escalate_threshold_pct"] == crm_tasks._DEFAULT_RULE["escalate_threshold_pct"]
    assert len(snapshots.rules) == 1


def _gauge(state: str) -> float | None:
    return REGISTRY.get_sample_value("crm_sla_breaching_deals", {"sla_state": state})


async def test_publish_sla_gauges_counts_open_deals_per_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executed: list[object] = []

    async def execute(statement: object) -> object:
        executed.append(statement)
        return SimpleNamespace(
            tuples=lambda: SimpleNamespace(all=lambda: [("breached", 4), ("ok", 10)])
        )

    monkeypatch.setattr(crm_tasks, "session_scope", _fake_scope(SimpleNamespace(execute=execute)))

    await crm_tasks._publish_sla_gauges()

    assert {state.value: _gauge(state.value) for state in SlaState} == {
        "ok": 10,
        "warning": 0,
        "breached": 4,
        "paused": 0,
    }
    sql = _sql(executed[0])
    assert "GROUP BY deals.sla_state" in sql
    assert "deals.closed_at IS NULL" in sql
    assert "deals.deleted_at IS NULL" in sql


async def test_publish_sla_gauges_resets_states_that_disappeared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counts: list[tuple[str, int]] = [("breached", 4)]

    async def execute(statement: object) -> object:
        return SimpleNamespace(tuples=lambda: SimpleNamespace(all=lambda: list(counts)))

    monkeypatch.setattr(crm_tasks, "session_scope", _fake_scope(SimpleNamespace(execute=execute)))
    await crm_tasks._publish_sla_gauges()
    assert _gauge("breached") == 4

    counts.clear()  # нарушения разобрали — состояния в выборке больше нет
    await crm_tasks._publish_sla_gauges()

    assert _gauge("breached") == 0


async def test_publish_sla_gauges_failure_keeps_old_values_and_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def execute(statement: object) -> object:
        return SimpleNamespace(tuples=lambda: SimpleNamespace(all=lambda: [("breached", 2)]))

    monkeypatch.setattr(crm_tasks, "session_scope", _fake_scope(SimpleNamespace(execute=execute)))
    await crm_tasks._publish_sla_gauges()

    @asynccontextmanager
    async def broken_scope() -> AsyncIterator[object]:
        raise ConnectionError("БД недоступна")
        yield  # pragma: no cover

    monkeypatch.setattr(crm_tasks, "session_scope", broken_scope)
    await crm_tasks._publish_sla_gauges()

    assert _gauge("breached") == 2


async def test_sweep_publishes_gauges_and_is_tracked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Сверху скан ничего не нашёл, но метрика всё равно обновляется — нарушенные и уже
    эскалированные сделки скан не читает, а в метрике они нужны."""
    session = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=list),
                tuples=lambda: SimpleNamespace(all=lambda: [("breached", 3)]),
            )
        )
    )
    monkeypatch.setattr(crm_tasks, "session_scope", _fake_scope(session))
    before = _sample("crm_background_tasks_total", task="sweep_sla_breaches", result="success")

    result = await crm_tasks.sweep_sla_breaches({})

    assert result == {"warned": 0, "breached": 0, "escalated": 0}
    assert _gauge("breached") == 3
    after = _sample("crm_background_tasks_total", task="sweep_sla_breaches", result="success")
    assert after == before + 1


# --- Отчёты -----------------------------------------------------------------


def _in_progress() -> float:
    return _sample("crm_reports_in_progress")


class _RenderProbe:
    """Подставной `ReportJobService`: запоминает, сколько отчётов считалось одновременно."""

    seen: list[float]
    fail: bool

    def __init__(self, session: object) -> None:
        pass

    async def generate(self, job: object, principal: object) -> None:
        type(self).seen.append(_in_progress())
        await asyncio.sleep(0.01)
        if type(self).fail:
            raise RuntimeError("не удалось построить отчёт")

    async def mark_failed(self, job: object, message: str) -> None:
        pass


@pytest.fixture
def render_probe(monkeypatch: pytest.MonkeyPatch) -> type[_RenderProbe]:
    _RenderProbe.seen = []
    _RenderProbe.fail = False
    session = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4())),
        rollback=AsyncMock(),
    )
    monkeypatch.setattr(reporting_tasks, "session_scope", _fake_scope(session))
    monkeypatch.setattr(
        reporting_tasks,
        "_claim_queued",
        AsyncMock(return_value=SimpleNamespace(requested_by=uuid.uuid4())),
    )
    monkeypatch.setattr(reporting_tasks, "_principal_for_worker", lambda user: object())
    monkeypatch.setattr(reporting_tasks, "ReportJobService", _RenderProbe)
    return _RenderProbe


async def test_report_in_progress_is_raised_only_while_rendering(
    render_probe: type[_RenderProbe],
) -> None:
    before = _in_progress()

    assert await reporting_tasks._generate_one(uuid.uuid4()) is True

    assert render_probe.seen == [before + 1]
    assert _in_progress() == before


async def test_report_in_progress_is_released_when_rendering_fails(
    render_probe: type[_RenderProbe],
) -> None:
    render_probe.fail = True
    before = _in_progress()

    assert await reporting_tasks._generate_one(uuid.uuid4()) is False

    assert render_probe.seen == [before + 1]
    assert _in_progress() == before


async def test_report_in_progress_does_not_exceed_reports_max_concurrent(
    render_probe: type[_RenderProbe], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Тик берёт `reports_max_concurrent` за вычетом уже обрабатываемых, и метрика видит ровно
    столько одновременных рендеров."""
    job_ids = [uuid.uuid4() for _ in range(7)]
    statements: list[object] = []

    async def execute(statement: object) -> object:
        statements.append(statement)
        limit = statement._limit_clause.value  # type: ignore[attr-defined]
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: job_ids[:limit]))

    session = SimpleNamespace(
        scalar=AsyncMock(return_value=1),
        execute=execute,
        get=AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4())),
        rollback=AsyncMock(),
    )
    monkeypatch.setattr(reporting_tasks, "session_scope", _fake_scope(session))
    monkeypatch.setattr(
        reporting_tasks, "get_settings", lambda: SimpleNamespace(reports_max_concurrent=3)
    )
    _RenderProbe.seen = []
    before = _in_progress()

    result = await reporting_tasks.sweep_report_jobs({})

    assert result == {"processed": 2, "failed": 0}
    assert "LIMIT 2" in str(statements[0].compile(compile_kwargs={"literal_binds": True}))  # type: ignore[attr-defined]
    assert max(render_probe.seen) == before + 2
    assert _in_progress() == before


# --- Импорт -----------------------------------------------------------------


@asynccontextmanager
async def _savepoint() -> AsyncIterator[None]:
    yield


def _import_session(rows: list[ImportRowResult]) -> SimpleNamespace:
    return SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))
        ),
        begin_nested=_savepoint,
        flush=AsyncMock(),
        scalar=AsyncMock(return_value=0),
    )


def _import_service(rows: list[ImportRowResult]) -> ImportService:
    service = ImportService(_import_session(rows))  # type: ignore[arg-type]
    service._audit = MagicMock(record=AsyncMock())  # type: ignore[assignment]
    service._set_actor = AsyncMock()  # type: ignore[method-assign]
    return service


def _job(**overrides: Any) -> ImportJob:
    fields: dict[str, Any] = {
        "entity_type": "organization",
        "mode": "insert",
        "processed_rows": 0,
        "ok_rows": 3,
        "warn_rows": 0,
        "error_rows": 0,
    }
    return ImportJob(**{**fields, **overrides})


def _row(number: int, status: str = "ok", entity_id: uuid.UUID | None = None) -> ImportRowResult:
    return ImportRowResult(row_number=number, status=status, errors=[], entity_id=entity_id)


def _rows_total(status: str) -> float:
    return _sample("crm_import_rows_total", entity_type="organization", status=status)


async def test_apply_batch_counts_applied_skipped_and_failed_rows() -> None:
    rows = [_row(1), _row(2), _row(3)]
    service = _import_service(rows)

    async def apply_row(job: ImportJob, row: ImportRowResult) -> None:
        if row.row_number == 1:
            row.entity_id = uuid.uuid4()
        elif row.row_number == 2:
            row.status = ImportRowStatus.SKIPPED.value
        else:
            raise AppError(ErrorCode.VALIDATION, "Некорректная строка")

    service._apply_organization_row = apply_row  # type: ignore[method-assign]
    before = {status: _rows_total(status) for status in ("applied", "skipped", "error")}

    processed = await service.apply_batch(_job(), batch_size=500)

    assert processed == 3
    assert {status: _rows_total(status) - before[status] for status in before} == {
        "applied": 1,
        "skipped": 1,
        "error": 1,
    }


async def test_rollback_batch_counts_rolled_back_and_blocked_rows() -> None:
    rows = [_row(1, entity_id=uuid.uuid4()), _row(2, entity_id=uuid.uuid4())]
    service = _import_service(rows)

    async def rollback_row(job: ImportJob, row: ImportRowResult) -> None:
        if row.row_number == 1:
            row.status = ImportRowStatus.ROLLED_BACK.value
        else:
            raise RuntimeError("откат строки не удался")

    service._rollback_row = rollback_row  # type: ignore[method-assign]
    before = {status: _rows_total(status) for status in ("rolled_back", "rollback_blocked")}

    await service.rollback_batch(_job(), batch_size=500)

    assert {status: _rows_total(status) - before[status] for status in before} == {
        "rolled_back": 1,
        "rollback_blocked": 1,
    }


async def test_finalize_apply_observes_job_duration() -> None:
    service = _import_service([])
    labels = {"entity_type": "organization", "phase": "apply"}
    count_before = _sample("crm_import_duration_seconds_count", **labels)
    sum_before = _sample("crm_import_duration_seconds_sum", **labels)
    job = _job(started_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=90))

    assert await service.finalize_apply_if_done(job) is True

    assert _sample("crm_import_duration_seconds_count", **labels) == count_before + 1
    observed = _sample("crm_import_duration_seconds_sum", **labels) - sum_before
    assert 90 <= observed < 120


async def test_unfinished_apply_is_not_observed() -> None:
    service = _import_service([])
    service._session.scalar = AsyncMock(return_value=5)  # type: ignore[attr-defined]
    labels = {"entity_type": "organization", "phase": "apply"}
    count_before = _sample("crm_import_duration_seconds_count", **labels)

    assert await service.finalize_apply_if_done(_job()) is False

    assert _sample("crm_import_duration_seconds_count", **labels) == count_before


async def test_dry_run_observes_validation_duration() -> None:
    service = _import_service([])
    job = _job()
    service._dry_run = AsyncMock(return_value=job)  # type: ignore[method-assign]
    labels = {"entity_type": "organization", "phase": "validate"}
    before = _sample("crm_import_duration_seconds_count", **labels)

    assert await service.dry_run(job) is job

    assert _sample("crm_import_duration_seconds_count", **labels) == before + 1


async def test_failed_dry_run_is_not_observed() -> None:
    service = _import_service([])
    service._dry_run = AsyncMock(  # type: ignore[method-assign]
        side_effect=AppError(ErrorCode.VALIDATION, "Сначала сохраните маппинг колонок")
    )
    labels = {"entity_type": "organization", "phase": "validate"}
    before = _sample("crm_import_duration_seconds_count", **labels)

    with pytest.raises(AppError):
        await service.dry_run(_job())

    assert _sample("crm_import_duration_seconds_count", **labels) == before


# --- Кэш графа воронки ------------------------------------------------------


def _cache(result: str) -> float:
    return _sample("crm_cache_requests_total", cache="workflow_graph", result=result)


def _workflow(graph_hash: str = "h1") -> Workflow:
    return Workflow(
        id=uuid.uuid4(),
        code="b2b-sales",
        state="published",
        published_graph={"statuses": [], "transitions": []},
        graph_hash=graph_hash,
    )


async def test_workflow_graph_cache_counts_miss_then_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        redis_client, "_client", fakeredis.aioredis.FakeRedis(decode_responses=True)
    )
    workflow = _workflow()
    hits, misses = _cache("hit"), _cache("miss")

    await get_cached_published_graph(workflow)
    assert (_cache("hit"), _cache("miss")) == (hits, misses + 1)

    await get_cached_published_graph(workflow)
    assert (_cache("hit"), _cache("miss")) == (hits + 1, misses + 1)


async def test_workflow_graph_cache_entry_with_stale_hash_is_a_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        redis_client, "_client", fakeredis.aioredis.FakeRedis(decode_responses=True)
    )
    workflow = _workflow(graph_hash="h1")
    await get_cached_published_graph(workflow)  # кладёт запись с хэшем h1
    workflow.graph_hash = "h2"  # воронку опубликовали заново
    hits, misses = _cache("hit"), _cache("miss")

    await get_cached_published_graph(workflow)

    assert (_cache("hit"), _cache("miss")) == (hits, misses + 1)


async def test_workflow_graph_cache_counts_miss_when_redis_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broken = MagicMock()
    broken.get = AsyncMock(side_effect=ConnectionError("Redis недоступен"))
    broken.setex = AsyncMock(side_effect=ConnectionError("Redis недоступен"))
    monkeypatch.setattr(redis_client, "_client", broken)
    hits, misses = _cache("hit"), _cache("miss")

    graph = await get_cached_published_graph(_workflow())

    assert graph == {"statuses": [], "transitions": []}
    assert (_cache("hit"), _cache("miss")) == (hits, misses + 1)
