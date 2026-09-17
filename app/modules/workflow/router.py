"""Ручки конструктора воронок (раздел 6.5).

`WORKFLOW_WRITE` покрывает создание черновика и правку графа, `WORKFLOW_PUBLISH`
— публикацию и архивирование статуса: это необратимее правки черновика и по
матрице прав (раздел 5) должно требовать отдельного права, а не совпадать с
обычной записью один в один.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status

from app.core.deps import DbSession, IfMatch, Pagination, require_permission
from app.core.pagination import Page, keyset_before
from app.core.permissions import Permission
from app.core.security import Principal
from app.modules.workflow.models import Workflow
from app.modules.workflow.schemas import (
    GraphIn,
    GraphOut,
    PublishResponse,
    SlaRuleOut,
    StatusArchiveRequest,
    StatusArchiveResponse,
    StatusImpactResponse,
    StatusOut,
    TransitionOut,
    ValidateResponse,
    WorkflowCreateRequest,
    WorkflowListResponse,
    WorkflowOut,
)
from app.modules.workflow.service import Graph, WorkflowFilters, WorkflowService

router = APIRouter(prefix="/workflows", tags=["workflows"])

WorkflowRead = Annotated[Principal, Depends(require_permission(Permission.WORKFLOW_READ))]
WorkflowWrite = Annotated[Principal, Depends(require_permission(Permission.WORKFLOW_WRITE))]
WorkflowPublish = Annotated[Principal, Depends(require_permission(Permission.WORKFLOW_PUBLISH))]


def _graph_out(graph: Graph) -> GraphOut:
    return GraphOut(
        workflow=WorkflowOut.model_validate(graph.workflow),
        statuses=[StatusOut.model_validate(s) for s in graph.statuses],
        transitions=[TransitionOut.model_validate(t) for t in graph.transitions],
        sla_rules=[SlaRuleOut.from_model(r) for r in graph.sla_rules],
    )


@router.get(
    "",
    summary="Список воронок",
    description="Фильтры: deal_type, state, q. Роль: чтение воронок.",
    response_model=WorkflowListResponse,
)
async def list_workflows(
    session: DbSession,
    page: Pagination,
    _: WorkflowRead,
    deal_type: Annotated[str | None, Query()] = None,
    workflow_state: Annotated[str | None, Query(alias="state")] = None,
    q: Annotated[str | None, Query()] = None,
) -> WorkflowListResponse:
    service = WorkflowService(session)
    stmt = service.list_query(
        WorkflowFilters(deal_type=deal_type, state=workflow_state, q=q)
    ).order_by(Workflow.created_at.desc(), Workflow.id.desc())

    cursor = page.decoded_cursor
    if cursor:
        stmt = stmt.where(keyset_before(Workflow.created_at, Workflow.id, cursor))

    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())
    built: Page = Page.build(rows, limit=page.limit, serializer=WorkflowOut.model_validate)
    return WorkflowListResponse(items=built.items, next_cursor=built.next_cursor)


@router.post(
    "",
    summary="Создать черновик воронки",
    description="Тело: code, name, deal_type, is_default. Роль: запись воронок.",
    response_model=WorkflowOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_workflow(
    payload: WorkflowCreateRequest,
    session: DbSession,
    principal: WorkflowWrite,
) -> WorkflowOut:
    workflow = await WorkflowService(session).create_draft(
        code=payload.code,
        name=payload.name,
        deal_type=payload.deal_type,
        is_default=payload.is_default,
        principal=principal,
    )
    return WorkflowOut.model_validate(workflow)


@router.get(
    "/{workflow_id}",
    summary="Граф воронки",
    description=(
        "Статусы (включая архивные), переходы, SLA-правила и состояние "
        "публикации. Роль: чтение воронок."
    ),
    response_model=GraphOut,
)
async def get_workflow_graph(
    session: DbSession,
    _: WorkflowRead,
    workflow_id: Annotated[uuid.UUID, Path()],
) -> GraphOut:
    service = WorkflowService(session)
    workflow = await service.get_or_404(workflow_id)
    return _graph_out(await service.get_graph(workflow))


@router.put(
    "/{workflow_id}/graph",
    summary="Сохранить черновик графа",
    description=(
        "Тело — полный снимок графа: statuses, transitions, sla_rules. Статусы "
        "и переходы, не попавшие в тело, удаляются. Архивные статусы этой "
        "ручкой не редактируются. Обязателен If-Match. Роль: запись воронок."
    ),
    response_model=GraphOut,
)
async def put_workflow_graph(
    payload: GraphIn,
    session: DbSession,
    _: WorkflowWrite,
    if_match: IfMatch,
    workflow_id: Annotated[uuid.UUID, Path()],
) -> GraphOut:
    service = WorkflowService(session)
    workflow = await service.get_or_404(workflow_id)
    graph = await service.save_graph(workflow, payload, expected_version=if_match)
    return _graph_out(graph)


@router.post(
    "/{workflow_id}/validate",
    summary="Провалидировать граф",
    description=(
        "Ровно один initial, минимум один терминальный статус, достижимость, "
        "отсутствие ловушек, корректность условий и действий, путь при отказе "
        "подписи. Роль: чтение воронок."
    ),
    response_model=ValidateResponse,
)
async def validate_workflow(
    session: DbSession,
    _: WorkflowRead,
    workflow_id: Annotated[uuid.UUID, Path()],
) -> ValidateResponse:
    service = WorkflowService(session)
    workflow = await service.get_or_404(workflow_id)
    errors, warnings = await service.validate(workflow)
    return ValidateResponse(ok=not errors, errors=errors, warnings=warnings)


@router.post(
    "/{workflow_id}/publish",
    summary="Опубликовать воронку",
    description=(
        "Публикация возможна только при пустом списке ошибок валидации. "
        "Снимок графа и его хэш фиксируются, кэш инвалидируется. Обязателен "
        "If-Match. Роль: публикация воронок."
    ),
    response_model=PublishResponse,
)
async def publish_workflow(
    session: DbSession,
    principal: WorkflowPublish,
    if_match: IfMatch,
    workflow_id: Annotated[uuid.UUID, Path()],
) -> PublishResponse:
    service = WorkflowService(session)
    workflow = await service.get_or_404(workflow_id)
    published, _warnings = await service.publish(workflow, principal, expected_version=if_match)
    return PublishResponse(
        workflow=WorkflowOut.model_validate(published), graph_hash=published.graph_hash or ""
    )


@router.get(
    "/{workflow_id}/statuses/{status_id}/impact",
    summary="Предпросмотр архивирования статуса",
    description=(
        "Число активных сделок, проблемные сделки, предложения целевых "
        "статусов. Роль: чтение воронок."
    ),
    response_model=StatusImpactResponse,
)
async def status_impact(
    session: DbSession,
    _: WorkflowRead,
    workflow_id: Annotated[uuid.UUID, Path()],
    status_id: Annotated[uuid.UUID, Path()],
) -> StatusImpactResponse:
    service = WorkflowService(session)
    workflow = await service.get_or_404(workflow_id)
    target_status = await service.get_status_or_404(workflow_id, status_id)
    workload, candidates = await service.status_impact(workflow, target_status)

    warnings: list[str] = []
    if not workload.supported:
        warnings.append(
            "Модуль сделок ещё не подключён: реальное число сделок в статусе показать нельзя"
        )
    return StatusImpactResponse(
        supported=workload.supported,
        active_count=workload.active_count,
        problem_deals=workload.problem_deals,
        sla_affected=workload.sla_affected,
        suggested_targets=[StatusOut.model_validate(s) for s in candidates],
        warnings=warnings,
    )


@router.post(
    "/{workflow_id}/statuses/{status_id}/archive",
    summary="Архивировать статус с переносом сделок",
    description=(
        "Запускает мастер сопоставления: сделки переносятся батчами на "
        "target_status_id (или fallback_status_id), статус помечается "
        "archived только после завершения переноса. Обязателен If-Match. "
        "Роль: публикация воронок."
    ),
    response_model=StatusArchiveResponse,
)
async def archive_status(
    payload: StatusArchiveRequest,
    session: DbSession,
    principal: WorkflowPublish,
    if_match: IfMatch,
    workflow_id: Annotated[uuid.UUID, Path()],
    status_id: Annotated[uuid.UUID, Path()],
) -> StatusArchiveResponse:
    service = WorkflowService(session)
    workflow = await service.get_or_404(workflow_id)
    target_status = await service.get_status_or_404(workflow_id, status_id)

    archived_status, job = await service.archive_status(
        workflow,
        target_status,
        principal,
        target_status_id=payload.target_status_id,
        fallback_status_id=payload.fallback_status_id,
        mapping_rules=payload.mapping_rules,
        sla_mode=payload.sla_mode,
        expected_version=if_match,
    )

    warnings: list[str] = []
    if job.report and job.report.get("supported") is False:
        warnings.append(
            "Модуль сделок ещё не подключён: статус архивирован без переноса, "
            "переносить было нечего"
        )

    return StatusArchiveResponse(
        status=StatusOut.model_validate(archived_status),
        job_id=job.id,
        job_status=job.status,
        affected_count=job.affected_count,
        warnings=warnings,
    )
