"""Сервис конструктора воронок (раздел 6.5, new_spec §4.11).

Три операции здесь заслуживают отдельного пояснения:

* **`save_graph` — полная замена живой части графа.** Тело `PUT .../graph`
  описывает весь граф целиком, поэтому статусы и переходы, не попавшие в
  запрос, удаляются — это ожидаемое поведение редактора, а не потеря данных.
  Исключение — архивные статусы и переходы между ними: они история, и этот
  эндпоинт их не видит и не трогает.
* **`publish` проверяет граф тем же валидатором, что и `POST /validate`.**
  Разного кода для «просто проверить» и «проверить перед публикацией» быть
  не должно — иначе они разойдутся, и администратор увидит «ok» там, где
  публикация откажет.
* **`archive_status` не ждёт мастер целиком в одном HTTP-запросе.** Первая
  партия сделок переносится синхронно (это и есть быстрый путь «сделок было
  немного»), а статус помечается `archived` только после того, как перенесены
  все. Если сделок больше одной партии, задача остаётся `running`, и её
  докручивает периодический воркер (`app/modules/workflow/tasks.py`), а не
  сама операция — постановка продолжения в очередь сразу после создания
  записи означала бы, что воркер может прочитать её раньше, чем закоммитится
  транзакция HTTP-запроса, в которой эта запись создана.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, ErrorCode, FieldError, NotFoundError, ValidationError
from app.core.redis_client import TTL_WORKFLOW_GRAPH, get_redis, key_workflow_graph
from app.core.security import Principal
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.workflow import dsl
from app.modules.workflow.models import (
    TERMINAL_TYPES,
    MappingJobStatus,
    SlaRule,
    StatusMappingJob,
    StatusType,
    Workflow,
    WorkflowState,
    WorkflowStatus,
    WorkflowTransition,
)
from app.modules.workflow.schemas import GraphIn

MAPPING_BATCH_SIZE = 100


@dataclass(slots=True)
class WorkflowFilters:
    deal_type: str | None = None
    state: str | None = None
    q: str | None = None


@dataclass(slots=True)
class Graph:
    workflow: Workflow
    statuses: list[WorkflowStatus]
    transitions: list[WorkflowTransition]
    sla_rules: list[SlaRule]


async def invalidate_workflow_cache(workflow_id: uuid.UUID) -> None:
    try:
        await get_redis().delete(key_workflow_graph(workflow_id))
    except Exception:  # noqa: BLE001 — кэш не источник истины
        pass


async def get_cached_published_graph(workflow: Workflow) -> dict[str, Any]:
    """Снимок опубликованного графа для перехода по статусу (new_spec §4.9).

    Сделки живут по `published_graph` — снимку на момент публикации, а не по
    живым таблицам `workflow_statuses`/`workflow_transitions`: черновая правка
    после публикации не должна немедленно менять правила для сделок, уже
    идущих по воронке (см. docstring `app/modules/workflow/models.py`).
    Поэтому переход читает исключительно эту JSON-структуру: `id`,
    `from_status_id`, `to_status_id`, `conditions`, `actions`, `allowed_roles`
    в ней — те же значения, что были в живых таблицах на момент публикации.

    Читает `cache:wf:{id}` (раздел 16), при промахе — `workflow.published_graph`
    и заполняет кэш. Публикация инвалидирует ключ явно (`invalidate_workflow_cache`).
    """
    if workflow.state != WorkflowState.PUBLISHED.value or workflow.published_graph is None:
        raise AppError(
            ErrorCode.VALIDATION,
            "Воронка не опубликована: переходы недоступны",
            extra={"workflow_id": str(workflow.id)},
        )

    try:
        cached = await get_redis().get(key_workflow_graph(workflow.id))
        if cached:
            return json.loads(cached)
    except Exception:  # noqa: BLE001 — кэш не источник истины
        pass

    graph = workflow.published_graph
    try:
        await get_redis().setex(
            key_workflow_graph(workflow.id),
            TTL_WORKFLOW_GRAPH,
            json.dumps(graph, default=str),
        )
    except Exception:  # noqa: BLE001
        pass
    return graph


class WorkflowService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    # --- Список и карточка -------------------------------------------------

    def list_query(self, filters: WorkflowFilters) -> Select[tuple[Workflow]]:
        stmt = select(Workflow)
        if filters.deal_type:
            stmt = stmt.where(Workflow.deal_type == filters.deal_type)
        if filters.state:
            stmt = stmt.where(Workflow.state == filters.state)
        if filters.q:
            pattern = f"%{filters.q.strip()}%"
            stmt = stmt.where(Workflow.name.ilike(pattern) | Workflow.code.ilike(pattern))
        return stmt

    async def get_or_404(self, workflow_id: uuid.UUID) -> Workflow:
        workflow = await self._session.get(Workflow, workflow_id)
        if workflow is None:
            raise NotFoundError("Воронка", workflow_id)
        return workflow

    async def get_status_or_404(
        self, workflow_id: uuid.UUID, status_id: uuid.UUID
    ) -> WorkflowStatus:
        status = await self._session.get(WorkflowStatus, status_id)
        if status is None or status.workflow_id != workflow_id:
            raise NotFoundError("Статус воронки", status_id)
        return status

    async def get_graph(self, workflow: Workflow) -> Graph:
        statuses = await self._load_statuses(workflow.id)
        transitions = await self._load_transitions(workflow.id)
        sla_rules = await self._load_sla_rules(workflow.id)
        return Graph(
            workflow=workflow,
            statuses=sorted(statuses.values(), key=lambda s: s.sort_order),
            transitions=sorted(transitions, key=lambda t: t.sort_order),
            sla_rules=sla_rules,
        )

    async def _load_statuses(self, workflow_id: uuid.UUID) -> dict[uuid.UUID, WorkflowStatus]:
        rows = (
            (
                await self._session.execute(
                    select(WorkflowStatus).where(WorkflowStatus.workflow_id == workflow_id)
                )
            )
            .scalars()
            .all()
        )
        return {row.id: row for row in rows}

    async def _load_transitions(self, workflow_id: uuid.UUID) -> list[WorkflowTransition]:
        return list(
            (
                await self._session.execute(
                    select(WorkflowTransition).where(WorkflowTransition.workflow_id == workflow_id)
                )
            )
            .scalars()
            .all()
        )

    async def _load_sla_rules(self, workflow_id: uuid.UUID) -> list[SlaRule]:
        return list(
            (await self._session.execute(select(SlaRule).where(SlaRule.workflow_id == workflow_id)))
            .scalars()
            .all()
        )

    # --- Создание ------------------------------------------------------------

    async def create_draft(
        self,
        *,
        code: str,
        name: str,
        deal_type: str,
        is_default: bool,
        principal: Principal,
    ) -> Workflow:
        existing = (
            await self._session.execute(select(Workflow).where(Workflow.code == code))
        ).scalar_one_or_none()
        if existing is not None:
            raise AppError(
                ErrorCode.DUPLICATE,
                f"Воронка с кодом {code!r} уже существует",
                extra={"workflow_id": str(existing.id)},
            )

        workflow = Workflow(
            code=code,
            name=name,
            deal_type=deal_type,
            is_default=is_default,
            state=WorkflowState.DRAFT.value,
        )
        self._session.add(workflow)
        await self._session.flush()

        await self._audit.record(
            AuditAction.WORKFLOW_CREATED,
            entity_type="workflow",
            entity_id=workflow.id,
            changes={
                "code": {"old": None, "new": code},
                "deal_type": {"old": None, "new": deal_type},
            },
        )
        return workflow

    # --- Удаление черновика (П4) ---------------------------------------------

    async def delete_draft(self, workflow: Workflow, principal: Principal) -> None:
        """Раздел 4/П4: удалить можно ТОЛЬКО черновик, который никогда не
        публиковался — опубликованную воронку (с историей и, возможно, живыми
        сделками) удалять нельзя ни при каких условиях, только архивировать
        статусы по одному (раздел 4.11, `archive_status`). Проверка `state`
        уже отсекает подавляющее большинство случаев, но сделка технически
        могла быть заведена на черновик (например, интеграцией, до того как
        воронку успели опубликовать) — второй, defensive-проверкой по
        `Deal.workflow_id` не полагаемся только на `state`.
        """
        if workflow.state != WorkflowState.DRAFT.value:
            raise AppError(
                ErrorCode.WORKFLOW_NOT_DRAFT,
                "Удалить можно только черновик воронки, который не публиковался — "
                "опубликованную воронку можно только архивировать статусы по одному",
            )

        from app.modules.crm.models import Deal

        has_deals = await self._session.scalar(
            select(Deal.id).where(Deal.workflow_id == workflow.id).limit(1)
        )
        if has_deals is not None:
            raise AppError(
                ErrorCode.WORKFLOW_NOT_DRAFT,
                "У воронки уже есть сделки — удаление невозможно",
            )

        workflow_id, code = workflow.id, workflow.code
        await self._session.delete(workflow)
        await self._session.flush()
        await self._audit.record(
            AuditAction.WORKFLOW_DELETED,
            entity_type="workflow",
            entity_id=workflow_id,
            changes={"code": {"old": code, "new": None}},
        )

    # --- Сохранение черновика графа ------------------------------------------

    def _check_version(self, workflow: Workflow, expected_version: int) -> None:
        if workflow.version != expected_version:
            raise AppError(
                ErrorCode.VERSION_CONFLICT,
                "Воронка была изменена другим пользователем. Обновите граф и повторите.",
                extra={"current_version": workflow.version},
            )

    def _ensure_not_archived(self, workflow: Workflow) -> None:
        if workflow.state == WorkflowState.ARCHIVED.value:
            raise AppError(ErrorCode.VALIDATION, "Архивная воронка недоступна для изменений")

    async def save_graph(
        self, workflow: Workflow, payload: GraphIn, *, expected_version: int
    ) -> Graph:
        self._check_version(workflow, expected_version)
        self._ensure_not_archived(workflow)

        existing_statuses = await self._load_statuses(workflow.id)
        archived_ids = {sid for sid, row in existing_statuses.items() if row.is_archived}

        for item in payload.statuses:
            if item.id is not None and item.id in archived_ids:
                raise ValidationError(
                    "Архивный статус нельзя редактировать через сохранение графа",
                    [FieldError(field="statuses", reason=f"статус {item.id} архивирован")],
                )

        id_map: dict[str, uuid.UUID] = {}
        kept_ids: set[uuid.UUID] = set(archived_ids)

        for item in payload.statuses:
            if item.id is not None:
                row = existing_statuses.get(item.id)
                if row is None:
                    raise NotFoundError("Статус воронки", item.id)
                row.code = item.code
                row.name = item.name
                row.type = item.type
                row.color = item.color
                row.sort_order = item.sort_order
                row.required_fields = item.required_fields
                status_id = row.id
            else:
                row = WorkflowStatus(
                    workflow_id=workflow.id,
                    code=item.code,
                    name=item.name,
                    type=item.type,
                    color=item.color,
                    sort_order=item.sort_order,
                    required_fields=item.required_fields,
                )
                self._session.add(row)
                await self._session.flush()
                status_id = row.id

            id_map[item.code] = status_id
            id_map[str(status_id)] = status_id
            kept_ids.add(status_id)

        # Статусы, убранные с холста, физически удаляются — если на них
        # ссылаются сделки, БД остановит это внешним ключом (RESTRICT), и
        # такой статус нужно сначала архивировать через мастер сопоставления.
        for sid, row in existing_statuses.items():
            if sid not in kept_ids and not row.is_archived:
                try:
                    async with self._session.begin_nested():
                        await self._session.delete(row)
                        await self._session.flush()
                except IntegrityError:
                    raise ValidationError(
                        f"Статус «{row.name}» используется в сделках или истории: "
                        "сначала архивируйте его через мастер сопоставления",
                        [
                            FieldError(
                                field="statuses", reason=f"статус «{row.name}» нельзя удалить"
                            )
                        ],
                    ) from None

        def resolve(ref: str, *, where: str) -> uuid.UUID:
            resolved = id_map.get(ref)
            if resolved is None:
                raise ValidationError(
                    f"Статус {ref!r} не найден в теле запроса графа",
                    [FieldError(field=where, reason=f"неизвестный статус {ref!r}")],
                )
            return resolved

        # Переходы сопоставляются по паре статусов и обновляются на месте: на их id ссылается
        # история сделок (FK RESTRICT), поэтому «удалить всё и создать заново» падало с 500,
        # как только хотя бы одна сделка прошла по переходу.
        reusable: dict[tuple[uuid.UUID, uuid.UUID], WorkflowTransition] = {}
        for row in await self._load_transitions(workflow.id):
            if row.from_status_id in archived_ids or row.to_status_id in archived_ids:
                continue  # исторические переходы не редактируются
            reusable[(row.from_status_id, row.to_status_id)] = row

        seen_pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()
        for item in payload.transitions:
            from_id = resolve(item.from_status, where="transitions.from_status")
            to_id = resolve(item.to_status, where="transitions.to_status")
            if from_id in archived_ids or to_id in archived_ids:
                raise ValidationError(
                    "Переход не может ссылаться на архивный статус",
                    [FieldError(field="transitions", reason=item.name)],
                )
            if from_id == to_id:
                raise ValidationError(
                    f"Переход «{item.name}» не может вести из статуса в самого себя",
                    [FieldError(field="transitions", reason=item.name)],
                )
            if (from_id, to_id) in seen_pairs:
                raise ValidationError(
                    f"Дублирующийся переход между теми же статусами: «{item.name}»",
                    [FieldError(field="transitions", reason=item.name)],
                )
            seen_pairs.add((from_id, to_id))

            row = reusable.pop((from_id, to_id), None)
            if row is not None:
                row.name = item.name
                row.allowed_roles = item.allowed_roles
                row.conditions = item.conditions
                row.actions = item.actions
                row.requires_comment = item.requires_comment
                row.sort_order = item.sort_order
            else:
                self._session.add(
                    WorkflowTransition(
                        workflow_id=workflow.id,
                        from_status_id=from_id,
                        to_status_id=to_id,
                        name=item.name,
                        allowed_roles=item.allowed_roles,
                        conditions=item.conditions,
                        actions=item.actions,
                        requires_comment=item.requires_comment,
                        sort_order=item.sort_order,
                    )
                )

        # Убранные с холста переходы удаляются; если по ним уже проходили сделки — БД остановит
        # это внешним ключом, и пользователь получит понятный отказ вместо 500.
        for row in reusable.values():
            try:
                async with self._session.begin_nested():
                    await self._session.delete(row)
                    await self._session.flush()
            except IntegrityError:
                raise ValidationError(
                    f"Переход «{row.name}» уже использован в истории сделок и не может быть удалён",
                    [FieldError(field="transitions", reason=f"переход «{row.name}» использован")],
                ) from None
        await self._session.flush()

        existing_sla = await self._load_sla_rules(workflow.id)
        for rule in existing_sla:
            if rule.status_id in archived_ids:
                continue
            await self._session.delete(rule)
        await self._session.flush()

        seen_active_status: set[uuid.UUID] = set()
        for item in payload.sla_rules:
            status_id = resolve(item.status, where="sla_rules.status")
            if status_id in archived_ids:
                raise ValidationError(
                    "SLA-правило не может ссылаться на архивный статус",
                    [FieldError(field="sla_rules", reason=item.status)],
                )
            if item.is_active:
                if status_id in seen_active_status:
                    raise ValidationError(
                        "На статус может ссылаться только одно активное SLA-правило",
                        [FieldError(field="sla_rules", reason=item.status)],
                    )
                seen_active_status.add(status_id)

            self._session.add(
                SlaRule(
                    workflow_id=workflow.id,
                    status_id=status_id,
                    max_duration=dt.timedelta(hours=item.max_duration_hours),
                    warn_threshold_pct=item.warn_threshold_pct,
                    escalate_to_role=item.escalate_to_role,
                    escalate_to_user_id=item.escalate_to_user_id,
                    channels=item.channels,
                    count_business_days=item.count_business_days,
                    is_active=item.is_active,
                )
            )

        workflow.version += 1
        await self._session.flush()
        return await self.get_graph(workflow)

    # --- Валидация -------------------------------------------------------

    async def validate(self, workflow: Workflow) -> tuple[list[str], list[str]]:
        statuses = list((await self._load_statuses(workflow.id)).values())
        transitions = await self._load_transitions(workflow.id)
        errors, warnings = _validate_graph_data(statuses, transitions)

        await self._audit.record(
            AuditAction.WORKFLOW_VALIDATED,
            entity_type="workflow",
            entity_id=workflow.id,
            changes={"errors": len(errors), "warnings": len(warnings)},
        )
        return errors, warnings

    # --- Публикация ------------------------------------------------------

    async def publish(
        self, workflow: Workflow, principal: Principal, *, expected_version: int
    ) -> tuple[Workflow, list[str]]:
        self._check_version(workflow, expected_version)
        self._ensure_not_archived(workflow)

        statuses = list((await self._load_statuses(workflow.id)).values())
        transitions = await self._load_transitions(workflow.id)
        sla_rules = await self._load_sla_rules(workflow.id)

        errors, warnings = _validate_graph_data(statuses, transitions)
        if errors:
            raise ValidationError(
                "Граф воронки не прошёл валидацию, публикация отменена",
                [FieldError(field="graph", reason=item) for item in errors],
            )

        snapshot = _build_snapshot(workflow, statuses, transitions, sla_rules)
        digest = hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()

        if workflow.is_default:
            others = (
                (
                    await self._session.execute(
                        select(Workflow).where(
                            Workflow.deal_type == workflow.deal_type,
                            Workflow.id != workflow.id,
                            Workflow.is_default.is_(True),
                            Workflow.state == WorkflowState.PUBLISHED.value,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for other in others:
                other.is_default = False

        now = dt.datetime.now(dt.UTC)
        workflow.published_graph = snapshot
        workflow.graph_hash = digest
        workflow.published_at = now
        workflow.published_by = principal.user_id
        workflow.state = WorkflowState.PUBLISHED.value
        workflow.version += 1
        await self._session.flush()

        await invalidate_workflow_cache(workflow.id)
        await self._audit.record(
            AuditAction.WORKFLOW_PUBLISHED,
            entity_type="workflow",
            entity_id=workflow.id,
            changes={"graph_hash": {"old": None, "new": digest}},
        )
        return workflow, warnings

    # --- Архивирование статуса --------------------------------------------

    async def status_impact(
        self, workflow: Workflow, status: WorkflowStatus
    ) -> tuple[Any, list[WorkflowStatus]]:
        # Импорт отложен: с спринта 3 `crm.service` сам импортирует
        # `get_cached_published_graph` из этого модуля (переход по статусу
        # работает со снимком графа), а этот модуль — `get_deal_status_service`
        # из `crm.service`. Импорт на уровне модуля с обеих сторон был бы
        # циклическим; вызов внутри метода срабатывает уже после того, как оба
        # модуля полностью загружены.
        from app.modules.crm.service import get_deal_status_service

        workload = await get_deal_status_service().status_workload(self._session, status.id)
        candidates = [
            row
            for row in (await self._load_statuses(workflow.id)).values()
            if row.id != status.id and not row.is_archived
        ]
        return workload, sorted(candidates, key=lambda s: s.sort_order)

    async def archive_status(
        self,
        workflow: Workflow,
        status: WorkflowStatus,
        principal: Principal,
        *,
        target_status_id: uuid.UUID,
        fallback_status_id: uuid.UUID | None,
        mapping_rules: dict[str, Any],
        sla_mode: str,
        expected_version: int,
    ) -> tuple[WorkflowStatus, StatusMappingJob]:
        self._check_version(workflow, expected_version)
        self._ensure_not_archived(workflow)

        if status.is_archived:
            raise AppError(ErrorCode.VALIDATION, "Статус уже архивирован")
        if status.type == StatusType.INITIAL.value:
            raise AppError(
                ErrorCode.VALIDATION, "Начальный статус нельзя архивировать: воронке нужен вход"
            )
        if target_status_id == status.id:
            raise ValidationError(
                "Целевой статус должен отличаться от архивируемого",
                [
                    FieldError(
                        field="target_status_id", reason="совпадает со статусом архивирования"
                    )
                ],
            )
        target = await self.get_status_or_404(workflow.id, target_status_id)
        if target.is_archived:
            raise ValidationError(
                "Целевой статус архивирован",
                [FieldError(field="target_status_id", reason="статус недоступен")],
            )
        fallback: WorkflowStatus | None = None
        if fallback_status_id is not None:
            fallback = await self.get_status_or_404(workflow.id, fallback_status_id)
            if fallback.is_archived:
                raise ValidationError(
                    "Резервный статус архивирован",
                    [FieldError(field="fallback_status_id", reason="статус недоступен")],
                )

        from app.modules.crm.service import get_deal_status_service  # см. status_impact()

        deal_service = get_deal_status_service()
        workload = await deal_service.status_workload(self._session, status.id)

        job = StatusMappingJob(
            workflow_id=workflow.id,
            from_status_id=status.id,
            mapping_rules={
                "rules": mapping_rules,
                "target_status_id": str(target.id),
                "fallback_status_id": str(fallback.id) if fallback else None,
                "sla_mode": sla_mode,
            },
            affected_count=workload.active_count,
            initiated_by=principal.user_id,
        )
        self._session.add(job)
        workflow.version += 1
        await self._session.flush()

        await self._audit.record(
            AuditAction.STATUS_MAPPING_STARTED,
            entity_type="status_mapping_job",
            entity_id=job.id,
            changes={
                "from_status": status.code,
                "target_status": target.code,
                "affected_count": workload.active_count,
            },
        )

        now = dt.datetime.now(dt.UTC)
        if not workload.supported or workload.active_count == 0:
            job.status = MappingJobStatus.COMPLETED.value
            job.started_at = now
            job.finished_at = now
            job.report = {"processed": 0, "failed": 0, "supported": workload.supported}
            self._archive_status_row(status, target, now)
            await self._session.flush()
            await self.republish_after_archive(workflow)
            await self._audit.record(
                AuditAction.STATUS_ARCHIVED,
                entity_type="workflow_status",
                entity_id=status.id,
                changes={"replaced_by": {"old": None, "new": str(target.id)}},
            )
            await self._complete_mapping_audit(job)
            return status, job

        job.status = MappingJobStatus.RUNNING.value
        job.started_at = now
        result = await deal_service.migrate_batch(
            self._session,
            from_status_id=status.id,
            target_status_id=target.id,
            fallback_status_id=fallback.id if fallback else None,
            sla_mode=sla_mode,
            batch_size=MAPPING_BATCH_SIZE,
        )
        job.processed_count += result.processed
        job.failed_count += result.failed

        if not result.has_more:
            job.status = MappingJobStatus.COMPLETED.value
            job.finished_at = dt.datetime.now(dt.UTC)
            job.report = {"processed": job.processed_count, "failed": job.failed_count}
            self._archive_status_row(status, target, job.finished_at)
            await self._session.flush()
            await self.republish_after_archive(workflow)
            await self._audit.record(
                AuditAction.STATUS_ARCHIVED,
                entity_type="workflow_status",
                entity_id=status.id,
                changes={"replaced_by": {"old": None, "new": str(target.id)}},
            )
            await self._complete_mapping_audit(job)
        # Иначе задача остаётся `running`: следующие партии докручивает
        # периодический воркер (`app/modules/workflow/tasks.py`), а не эта
        # операция — см. docstring модуля.

        await self._session.flush()
        return status, job

    def _archive_status_row(
        self, status: WorkflowStatus, target: WorkflowStatus, when: dt.datetime
    ) -> None:
        status.is_archived = True
        status.archived_at = when
        status.replaced_by_status_id = target.id

    async def republish_after_archive(self, workflow: Workflow) -> None:
        """Пересобирает `published_graph`/`graph_hash` после того, как статус
        реально архивирован (обе точки завершения — синхронная в этом же
        методе и асинхронная докрутка в `workflow.tasks._process_one_batch`
        — обязаны звать это).

        Раньше `archive_status` только выставлял `is_archived = true` на
        живой строке и звал `invalidate_workflow_cache`, но переходы по
        сделкам работают исключительно со снимком `published_graph`
        (`get_cached_published_graph`) — сброс кэша без пересборки снимка
        означал, что следующее чтение просто заново прогревало кэш ТЕМ ЖЕ
        устаревшим снимком с архивным статусом внутри. Архивный статус
        оставался живым для переходов сколь угодно долго — раздел 4.11
        «запрет удаления без миграции» на практике не работал. Полная
        `_validate_graph_data` здесь намеренно не перезапускается: граф уже
        прошёл её на публикации, а архивирование — не повторная ручная
        публикация человеком (не трогаем `published_at`/`published_by`,
        `workflow.version` тоже не бампаем второй раз — это уже сделал
        вызывающий код).
        """
        statuses = list((await self._load_statuses(workflow.id)).values())
        transitions = await self._load_transitions(workflow.id)
        sla_rules = await self._load_sla_rules(workflow.id)
        snapshot = _build_snapshot(workflow, statuses, transitions, sla_rules)
        digest = hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str).encode()
        ).hexdigest()
        workflow.published_graph = snapshot
        workflow.graph_hash = digest
        await self._session.flush()
        await invalidate_workflow_cache(workflow.id)

    async def _complete_mapping_audit(self, job: StatusMappingJob) -> None:
        await self._audit.record(
            AuditAction.STATUS_MAPPING_COMPLETED,
            entity_type="status_mapping_job",
            entity_id=job.id,
            changes={"processed": job.processed_count, "failed": job.failed_count},
        )


# --- Валидация графа (чистая функция, без сессии) ---------------------------


def _bfs(start: uuid.UUID, forward: dict[uuid.UUID, list[uuid.UUID]]) -> set[uuid.UUID]:
    seen = {start}
    stack = [start]
    while stack:
        current = stack.pop()
        for nxt in forward.get(current, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def _has_field_condition(node: Any, field_name: str) -> bool:
    if not isinstance(node, dict):
        return False
    if "all" in node:
        return any(_has_field_condition(branch, field_name) for branch in node["all"])
    if "any" in node:
        return any(_has_field_condition(branch, field_name) for branch in node["any"])
    return node.get("field") == field_name


def _validate_graph_data(
    statuses: list[WorkflowStatus], transitions: list[WorkflowTransition]
) -> tuple[list[str], list[str]]:
    """Раздел 4.11: единственный начальный статус, хотя бы один терминальный,
    достижимость, отсутствие ловушек, корректные условия и действия, путь
    при отклонении подписи. Список ошибок закрыт этим перечнем — деловые
    рекомендации (обязательная причина для lost и т.п.) идут в `warnings` и
    публикацию не блокируют.
    """
    errors: list[str] = []
    warnings: list[str] = []

    live = [s for s in statuses if not s.is_archived]
    live_ids = {s.id for s in live}
    by_id = {s.id: s for s in live}
    live_transitions = [
        t for t in transitions if t.from_status_id in live_ids and t.to_status_id in live_ids
    ]

    initials = [s for s in live if s.type == StatusType.INITIAL.value]
    if len(initials) != 1:
        errors.append(
            f"Воронка должна иметь ровно один статус типа initial, найдено {len(initials)}"
        )

    terminals = [s for s in live if s.type in {t.value for t in TERMINAL_TYPES}]
    if not terminals:
        errors.append("Воронка должна иметь хотя бы один терминальный статус (won/lost/parked)")

    forward: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    backward: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for t in live_transitions:
        forward[t.from_status_id].append(t.to_status_id)
        backward[t.to_status_id].append(t.from_status_id)

    if len(initials) == 1:
        reachable = _bfs(initials[0].id, forward)
        unreachable = live_ids - reachable
        if unreachable:
            codes = sorted(by_id[i].code for i in unreachable)
            errors.append(f"Недостижимые из initial статусы: {', '.join(codes)}")

    if terminals:
        can_reach_terminal = _bfs_multi_source({s.id for s in terminals}, backward)
        traps = live_ids - can_reach_terminal
        if traps:
            codes = sorted(by_id[i].code for i in traps)
            errors.append(f"Статусы без пути в терминальный статус (ловушки): {', '.join(codes)}")

    live_codes = {s.code for s in live}
    for t in live_transitions:
        label = t.name or f"{t.from_status_id}->{t.to_status_id}"
        errors.extend(dsl.validate_condition(t.conditions, path=f"transitions[{label}].conditions"))
        errors.extend(dsl.validate_actions(t.actions, path=f"transitions[{label}].actions"))

        for action in dsl.signature_actions(t.actions):
            on_rejected = action.get("on_rejected") or "previous_status"
            if on_rejected != "previous_status" and on_rejected not in live_codes:
                errors.append(
                    f"transitions[{label}].actions.request_signature.on_rejected: "
                    f"статус {on_rejected!r} не найден в воронке"
                )

        to_status = by_id.get(t.to_status_id)
        if to_status is None:
            continue
        if to_status.type == StatusType.LOST.value and not (
            t.requires_comment and _has_field_condition(t.conditions, "loss_reason_id")
        ):
            warnings.append(
                f"Переход «{t.name}» в lost обычно требует комментарий и условие "
                "по loss_reason_id (new_spec §4.9)"
            )
        if to_status.type == StatusType.WON.value and not (
            _has_field_condition(t.conditions, "amount")
            and _has_field_condition(t.conditions, "expected_close_date")
        ):
            warnings.append(
                f"Переход «{t.name}» в won обычно требует условие по сумме и дате закрытия"
            )

    return errors, warnings


def _bfs_multi_source(
    starts: set[uuid.UUID], forward: dict[uuid.UUID, list[uuid.UUID]]
) -> set[uuid.UUID]:
    seen = set(starts)
    stack = list(starts)
    while stack:
        current = stack.pop()
        for nxt in forward.get(current, ()):
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return seen


def _build_snapshot(
    workflow: Workflow,
    statuses: list[WorkflowStatus],
    transitions: list[WorkflowTransition],
    sla_rules: list[SlaRule],
) -> dict[str, Any]:
    """Снимок для `published_graph`/`graph_hash`. Архивные статусы и связанные
    с ними переходы в снимок не входят — они не участвуют в переходах."""
    live_ids = {s.id for s in statuses if not s.is_archived}
    return {
        "workflow_code": workflow.code,
        "deal_type": workflow.deal_type,
        "statuses": [
            {
                "id": str(s.id),
                "code": s.code,
                "name": s.name,
                "type": s.type,
                "sort_order": s.sort_order,
                "required_fields": s.required_fields,
            }
            for s in statuses
            if not s.is_archived
        ],
        "transitions": [
            {
                "id": str(t.id),
                "from_status_id": str(t.from_status_id),
                "to_status_id": str(t.to_status_id),
                "name": t.name,
                "allowed_roles": t.allowed_roles,
                "conditions": t.conditions,
                "actions": t.actions,
                "requires_comment": t.requires_comment,
            }
            for t in transitions
            if t.from_status_id in live_ids and t.to_status_id in live_ids
        ],
        "sla_rules": [
            {
                "status_id": str(r.status_id),
                "max_duration_seconds": r.max_duration.total_seconds(),
                "warn_threshold_pct": r.warn_threshold_pct,
                "count_business_days": r.count_business_days,
            }
            for r in sla_rules
            if r.status_id in live_ids and r.is_active
        ],
    }
