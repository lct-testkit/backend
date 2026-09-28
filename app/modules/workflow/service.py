"""Сервис конструктора воронок (раздел 6.5, new_spec §4.11).

Три операции здесь заслуживают отдельного пояснения:

* **`save_graph` — полная замена живой части графа.** Тело `PUT .../graph`
  описывает весь граф целиком, поэтому статусы и переходы, не попавшие в
  запрос, удаляются — это ожидаемое поведение редактора, а не потеря данных.
  Статусы и переходы с `id` обновляются на месте: на них ссылаются сделки и
  их история. Убранный, но уже используемый статус или переход — 409
  (CRM-1208/CRM-1209), а не 500.
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
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import run_after_commit
from app.core.errors import AppError, ErrorCode, FieldError, NotFoundError, ValidationError
from app.core.metrics import record_cache
from app.core.optimistic import claim_version
from app.core.redis_client import TTL_WORKFLOW_GRAPH, get_redis, key_workflow_graph
from app.core.security import Principal
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.identity.models import Role, User
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
from app.modules.workflow.schemas import GraphIn, TransitionIn

MAPPING_BATCH_SIZE = 100

# Значения полей эскалации SLA у правила, снимок которого сделан до их появления.
_SLA_RULE_SNAPSHOT_DEFAULTS: dict[str, Any] = {
    "escalate_threshold_pct": 150,
    "escalate_to_role": None,
    "escalate_to_user_id": None,
    "channels": ["in_app"],
}


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


#: Значение метки `cache` в `crm_cache_requests_total`.
WORKFLOW_GRAPH_CACHE = "workflow_graph"


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
    и заполняет кэш. Публикация сбрасывает ключ после коммита (`invalidate_workflow_cache`),
    а запись в кэше несёт хэш графа и принимается, только если он совпадает с `graph_hash`
    воронки из БД: так запись, которую читатель успел положить между коммитом публикации и
    сбросом ключа, не оживляет устаревший снимок на час.
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
            entry = json.loads(cached)
            if isinstance(entry, dict) and entry.get("h") == workflow.graph_hash and "g" in entry:
                record_cache(WORKFLOW_GRAPH_CACHE, hit=True)
                return entry["g"]
    except Exception:  # noqa: BLE001 — кэш не источник истины
        pass

    # Устаревшая по хэшу запись и недоступный Redis — тоже промах: граф пришлось брать из БД.
    record_cache(WORKFLOW_GRAPH_CACHE, hit=False)
    graph = workflow.published_graph
    try:
        await get_redis().setex(
            key_workflow_graph(workflow.id),
            TTL_WORKFLOW_GRAPH,
            json.dumps({"h": workflow.graph_hash, "g": graph}, default=str),
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

    async def get_mapping_job_or_404(
        self, workflow_id: uuid.UUID, job_id: uuid.UUID
    ) -> StatusMappingJob:
        job = await self._session.get(StatusMappingJob, job_id)
        if job is None or job.workflow_id != workflow_id:
            raise NotFoundError("Задача сопоставления", job_id)
        return job

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

    async def unpublished_flags(self, workflows: Sequence[Workflow]) -> dict[uuid.UUID, bool]:
        """`has_unpublished_changes` для списка воронок: три запроса на всех, а не три на каждую."""
        ids = [workflow.id for workflow in workflows]
        if not ids:
            return {}
        statuses: dict[uuid.UUID, list[WorkflowStatus]] = defaultdict(list)
        transitions: dict[uuid.UUID, list[WorkflowTransition]] = defaultdict(list)
        sla_rules: dict[uuid.UUID, list[SlaRule]] = defaultdict(list)
        for status in (
            await self._session.execute(
                select(WorkflowStatus).where(WorkflowStatus.workflow_id.in_(ids))
            )
        ).scalars():
            statuses[status.workflow_id].append(status)
        for transition in (
            await self._session.execute(
                select(WorkflowTransition).where(WorkflowTransition.workflow_id.in_(ids))
            )
        ).scalars():
            transitions[transition.workflow_id].append(transition)
        for rule in (
            await self._session.execute(select(SlaRule).where(SlaRule.workflow_id.in_(ids)))
        ).scalars():
            sla_rules[rule.workflow_id].append(rule)
        return {
            workflow.id: has_unpublished_changes(
                workflow, statuses[workflow.id], transitions[workflow.id], sla_rules[workflow.id]
            )
            for workflow in workflows
        }

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

    # --- Метаданные воронки ----------------------------------------------------

    async def _demote_other_defaults(self, workflow: Workflow) -> None:
        """Воронка по умолчанию среди опубликованных на тип сделки одна (уникальный индекс):
        прежняя уступает раньше, чем новая её займёт — в одной пачке порядок UPDATE не
        гарантирован."""
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
        await self._session.flush()

    async def update(self, workflow: Workflow, payload: Any, *, expected_version: int) -> Workflow:
        """`PATCH /workflows/{id}`: имя и воронка по умолчанию. Граф и опубликованный снимок не
        трогает; версия растёт, как у любой правки воронки."""
        self._check_version(workflow, expected_version)
        self._ensure_not_archived(workflow)

        data = payload.model_dump(exclude_unset=True)
        changes: dict[str, dict[str, Any]] = {}
        for key in ("name", "is_default"):
            value = data.get(key)
            if value is not None and value != getattr(workflow, key):
                changes[key] = {"old": getattr(workflow, key), "new": value}
        if not changes:
            return workflow

        if (
            changes.get("is_default", {}).get("new")
            and workflow.state == WorkflowState.PUBLISHED.value
        ):
            await self._demote_other_defaults(workflow)
        for key, change in changes.items():
            setattr(workflow, key, change["new"])
        await self._claim_version(workflow, expected_version)
        await self._session.flush()
        await self._audit.record(
            AuditAction.WORKFLOW_UPDATED,
            entity_type="workflow",
            entity_id=workflow.id,
            changes=changes,
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

    async def _claim_version(self, workflow: Workflow, expected_version: int) -> None:
        """Проверка версии воронки и её атомарный захват (`UPDATE ... WHERE version = :v`).

        Проверка в Python ловит только запоздавший запрос; два запроса с одной версией проходили
        её оба, и правки графа затирали друг друга."""
        self._check_version(workflow, expected_version)
        await claim_version(
            self._session, workflow, expected_version, conflicting={"name": workflow.name}
        )

    def _ensure_not_archived(self, workflow: Workflow) -> None:
        if workflow.state == WorkflowState.ARCHIVED.value:
            raise AppError(ErrorCode.VALIDATION, "Архивная воронка недоступна для изменений")

    async def save_graph(
        self, workflow: Workflow, payload: GraphIn, *, expected_version: int
    ) -> Graph:
        await self._claim_version(workflow, expected_version)
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
        #
        # Статус из опубликованного снимка удалять нельзя вовсе, даже пока сделок в нём нет:
        # сделки идут по снимку, и после удаления строки переход в этот статус падал бы на
        # внешнем ключе уже в работе, а не при сохранении. Убрать такой статус можно только
        # архивацией (без сделок она мгновенная и правит снимок).
        published_status_ids = {
            s["id"] for s in ((workflow.published_graph or {}).get("statuses") or [])
        }
        for sid, row in existing_statuses.items():
            if sid not in kept_ids and not row.is_archived:
                name = row.name
                if str(sid) in published_status_ids:
                    raise AppError(
                        ErrorCode.WORKFLOW_STATUS_IN_USE,
                        f"Статус «{name}» входит в опубликованную воронку и не удаляется: "
                        "архивируйте его через мастер сопоставления",
                        errors=[
                            FieldError(field="statuses", reason=f"статус «{name}» опубликован")
                        ],
                        extra={"status_id": str(sid)},
                    )
                try:
                    async with self._session.begin_nested():
                        await self._session.delete(row)
                        await self._session.flush()
                except IntegrityError:
                    raise AppError(
                        ErrorCode.WORKFLOW_STATUS_IN_USE,
                        f"Статус «{name}» используется в сделках или истории: "
                        "сначала архивируйте его через мастер сопоставления",
                        errors=[
                            FieldError(field="statuses", reason=f"статус «{name}» нельзя удалить")
                        ],
                        extra={"status_id": str(sid)},
                    ) from None

        def resolve(ref: str, *, where: str) -> uuid.UUID:
            resolved = id_map.get(ref)
            if resolved is None:
                raise ValidationError(
                    f"Статус {ref!r} не найден в теле запроса графа",
                    [FieldError(field=where, reason=f"неизвестный статус {ref!r}")],
                )
            return resolved

        # Переходы обновляются на месте, а не пересоздаются: на их id ссылается история сделок
        # (FK RESTRICT), и «удалить всё и создать заново» падало, как только хотя бы одна сделка
        # прошла по переходу. Строку ищут по `id` из тела, а у перехода без `id` (новый на
        # холсте) — по паре статусов; не нашедшееся создаётся, оставшееся без пары — удаляется.
        rows_by_id = {
            row.id: row
            for row in await self._load_transitions(workflow.id)
            if row.from_status_id not in archived_ids and row.to_status_id not in archived_ids
        }  # переходы через архивный статус — история, их не редактируют

        items: list[tuple[TransitionIn, uuid.UUID, uuid.UUID]] = []
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
            items.append((item, from_id, to_id))

        claimed: dict[uuid.UUID, WorkflowTransition] = {}
        for item, _from_id, _to_id in items:
            if item.id is None:
                continue
            row = rows_by_id.get(item.id)
            if row is None:
                raise NotFoundError("Переход воронки", item.id)
            if row.id in claimed:
                raise ValidationError(
                    f"Переход {item.id} указан в теле запроса дважды",
                    [FieldError(field="transitions", reason=item.name)],
                )
            claimed[row.id] = row
        free_by_pair = {
            (row.from_status_id, row.to_status_id): row
            for row in rows_by_id.values()
            if row.id not in claimed
        }
        matched: list[WorkflowTransition | None] = [
            claimed[item.id] if item.id is not None else free_by_pair.pop((from_id, to_id), None)
            for item, from_id, to_id in items
        ]

        # Убранные с холста переходы удаляются до правок: их пару статусов может занять другой
        # переход. Если по ним уже проходили сделки, БД остановит это внешним ключом — вместо 500
        # пользователь получает отказ.
        kept_transition_ids = {row.id for row in matched if row is not None}
        for transition_id, row in rows_by_id.items():
            if transition_id in kept_transition_ids:
                continue
            name = row.name
            try:
                async with self._session.begin_nested():
                    await self._session.delete(row)
                    await self._session.flush()
            except IntegrityError:
                raise AppError(
                    ErrorCode.WORKFLOW_TRANSITION_IN_USE,
                    f"Переход «{name}» уже использован в истории сделок и не может быть удалён",
                    errors=[
                        FieldError(field="transitions", reason=f"переход «{name}» использован")
                    ],
                    extra={"transition_id": str(transition_id)},
                ) from None

        for (item, from_id, to_id), row in zip(items, matched, strict=True):
            if row is None:
                row = WorkflowTransition(workflow_id=workflow.id)
                self._session.add(row)
            row.from_status_id = from_id
            row.to_status_id = to_id
            row.name = item.name
            row.allowed_roles = item.allowed_roles
            row.conditions = item.conditions
            row.actions = item.actions
            row.requires_comment = item.requires_comment
            row.sort_order = item.sort_order
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

            await self._validate_escalation_target(item)
            self._session.add(
                SlaRule(
                    workflow_id=workflow.id,
                    status_id=status_id,
                    max_duration=dt.timedelta(hours=item.max_duration_hours),
                    warn_threshold_pct=item.warn_threshold_pct,
                    escalate_threshold_pct=item.escalate_threshold_pct,
                    escalate_to_role=item.escalate_to_role,
                    escalate_to_user_id=item.escalate_to_user_id,
                    channels=item.channels,
                    count_business_days=item.count_business_days,
                    is_active=item.is_active,
                )
            )

        await self._session.flush()
        return await self.get_graph(workflow)

    async def _validate_escalation_target(self, item: Any) -> None:
        """Кому эскалировать нарушение SLA: существующая роль и активный сотрудник. Раньше в
        правило писалась любая строка и любой UUID, а эскалация молча уходила в пустоту."""
        if item.escalate_to_role is not None and item.escalate_to_role not in {
            role.value for role in Role
        }:
            raise ValidationError(
                f"Неизвестная роль эскалации {item.escalate_to_role!r}",
                [FieldError(field="sla_rules.escalate_to_role", reason="неизвестная роль")],
            )
        if item.escalate_to_user_id is not None:
            user = await self._session.get(User, item.escalate_to_user_id)
            if user is None or not user.is_active:
                raise ValidationError(
                    "Получатель эскалации не найден или неактивен",
                    [
                        FieldError(
                            field="sla_rules.escalate_to_user_id",
                            reason="нужен активный сотрудник",
                        )
                    ],
                )

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
        await self._claim_version(workflow, expected_version)
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
        digest = snapshot_digest(snapshot)

        if workflow.is_default:
            await self._demote_other_defaults(workflow)

        now = dt.datetime.now(dt.UTC)
        workflow.published_graph = snapshot
        workflow.graph_hash = digest
        workflow.published_at = now
        workflow.published_by = principal.user_id
        workflow.state = WorkflowState.PUBLISHED.value
        await self._session.flush()

        # После коммита, не сейчас: до него параллельный читатель прогрел бы кэш старым снимком,
        # который уже некому сбросить.
        run_after_commit(self._session, lambda: invalidate_workflow_cache(workflow.id))
        await self._audit.record(
            AuditAction.WORKFLOW_PUBLISHED,
            entity_type="workflow",
            entity_id=workflow.id,
            changes={"graph_hash": {"old": None, "new": digest}},
        )
        return workflow, warnings

    # --- Архивирование статуса --------------------------------------------

    async def status_impact(
        self,
        workflow: Workflow,
        status: WorkflowStatus,
        target_status_id: uuid.UUID | None = None,
    ) -> tuple[Any, list[WorkflowStatus]]:
        # Импорт отложен: с спринта 3 `crm.service` сам импортирует
        # `get_cached_published_graph` из этого модуля (переход по статусу
        # работает со снимком графа), а этот модуль — `get_deal_status_service`
        # из `crm.service`. Импорт на уровне модуля с обеих сторон был бы
        # циклическим; вызов внутри метода срабатывает уже после того, как оба
        # модуля полностью загружены.
        from app.modules.crm.service import get_deal_status_service

        if target_status_id is not None:
            # Те же условия, что у архивирования: цель — другой живой статус этой воронки.
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

        workload = await get_deal_status_service().status_workload(
            self._session, status.id, target_status_id
        )
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
        # С целевым статусом: заодно считаются сделки без его обязательных полей — их перенести
        # можно только в резервный статус.
        workload = await deal_service.status_workload(self._session, status.id, target.id)
        if fallback is None and workload.problem_count > 0:
            # Раньше такие сделки молча оставались в архивируемом статусе, а он всё равно
            # архивировался — сделки застревали в статусе без выходов. Отказываем заранее.
            raise ValidationError(
                f"У {workload.problem_count} сделок нет обязательных полей статуса "
                f"«{target.name}»: укажите резервный статус или заполните поля",
                [
                    FieldError(
                        field="fallback_status_id",
                        reason="нужен резервный статус для сделок без обязательных полей",
                    )
                ],
            )

        await self._claim_version(workflow, expected_version)
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
            await self._finish_archive(workflow, status, target, job, when=now)
            return status, job

        job.status = MappingJobStatus.RUNNING.value
        job.started_at = now
        await self.advance_mapping_job(job)
        # Если сделок больше партии, задача остаётся `running`: следующие партии докручивает
        # периодический воркер (`app/modules/workflow/tasks.py`), а не эта операция — см.
        # docstring модуля.
        await self._session.flush()
        return status, job

    async def advance_mapping_job(self, job: StatusMappingJob) -> None:
        """Одна партия переноса и, если просмотр дошёл до конца, завершение задачи.

        Общий шаг для HTTP-запроса архивации и воркера. Курсор (`cursor` в `mapping_rules`)
        двигается по id сделок: сделка, которую перенести нельзя, остаётся в статусе, и без
        курсора каждая партия читала бы одни и те же строки, а задача не кончалась. Дойдя до
        конца, задача сверяет, что в статусе никого не осталось:

        * пусто — статус архивируется;
        * не пусто, но проход что-то перенёс — новый проход (сделки могли прийти в статус, пока
          шёл перенос);
        * не пусто и проход ничего не перенёс — оставшиеся застряли: задача `failed`, статус НЕ
          архивируется (раньше он архивировался вместе со сделками, у которых не было выхода)."""
        from app.modules.crm.service import get_deal_status_service

        deal_service = get_deal_status_service()
        rules = dict(job.mapping_rules)
        target_id = uuid.UUID(rules["target_status_id"])
        fallback_raw = rules.get("fallback_status_id")
        cursor_raw = rules.get("cursor")

        result = await deal_service.migrate_batch(
            self._session,
            from_status_id=job.from_status_id,
            target_status_id=target_id,
            fallback_status_id=uuid.UUID(fallback_raw) if fallback_raw else None,
            sla_mode=rules.get("sla_mode", "recalculate"),
            batch_size=MAPPING_BATCH_SIZE,
            after_id=uuid.UUID(cursor_raw) if cursor_raw else None,
        )
        job.processed_count += result.processed
        rules["pass_processed"] = int(rules.get("pass_processed", 0)) + result.processed
        rules["pass_failed"] = int(rules.get("pass_failed", 0)) + result.failed
        job.failed_count = rules["pass_failed"]

        if result.has_more:
            rules["cursor"] = str(result.last_id) if result.last_id else None
            job.mapping_rules = rules
            return

        status = await self._session.get(WorkflowStatus, job.from_status_id)
        target = await self._session.get(WorkflowStatus, target_id)
        workflow = await self._session.get(Workflow, job.workflow_id)
        remaining = (
            await deal_service.status_workload(self._session, job.from_status_id)
        ).active_count
        if remaining == 0 and status is not None and target is not None and workflow is not None:
            job.failed_count = 0
            job.mapping_rules = rules
            job.status = MappingJobStatus.COMPLETED.value
            job.finished_at = dt.datetime.now(dt.UTC)
            job.report = {"processed": job.processed_count, "failed": 0}
            await self._finish_archive(workflow, status, target, job, when=job.finished_at)
            return

        if rules["pass_processed"] > 0:
            # Что-то ещё переносилось: остаток мог прийти в статус во время переноса. Новый проход.
            rules.update({"cursor": None, "pass_processed": 0, "pass_failed": 0})
            job.mapping_rules = rules
            return

        job.mapping_rules = rules
        job.failed_count = remaining
        job.status = MappingJobStatus.FAILED.value
        job.finished_at = dt.datetime.now(dt.UTC)
        job.error = (
            f"{remaining} сделок не перенесены: у них нет обязательных полей целевого статуса, "
            "а резервный статус не задан. Статус не архивирован — заполните поля или задайте "
            "резервный статус и повторите архивацию."
        )
        job.report = {
            "processed": job.processed_count,
            "failed": remaining,
            "needs_attention": True,
        }
        await self._session.flush()
        await self._audit.record(
            AuditAction.STATUS_MAPPING_FAILED,
            entity_type="status_mapping_job",
            entity_id=job.id,
            changes={"processed": job.processed_count, "failed": remaining},
        )

    async def _finish_archive(
        self,
        workflow: Workflow,
        status: WorkflowStatus,
        target: WorkflowStatus,
        job: StatusMappingJob,
        *,
        when: dt.datetime,
    ) -> None:
        self._archive_status_row(status, target, when)
        await self._session.flush()
        await self.republish_after_archive(workflow, status)
        await self._audit.record(
            AuditAction.STATUS_ARCHIVED,
            entity_type="workflow_status",
            entity_id=status.id,
            changes={"replaced_by": {"old": None, "new": str(target.id)}},
        )
        await self._complete_mapping_audit(job)

    def _archive_status_row(
        self, status: WorkflowStatus, target: WorkflowStatus, when: dt.datetime
    ) -> None:
        status.is_archived = True
        status.archived_at = when
        status.replaced_by_status_id = target.id

    async def republish_after_archive(
        self, workflow: Workflow, archived: WorkflowStatus | None = None
    ) -> None:
        """Убирает архивированный статус из `published_graph` и пересчитывает `graph_hash`.

        Переходы сделок работают исключительно со снимком, поэтому одной пометки
        `is_archived` на живой строке мало: без правки снимка архивный статус оставался бы
        живым для переходов. Обе точки завершения (`archive_status` и воркер) зовут это.

        Снимок получается из УЖЕ ОПУБЛИКОВАННОГО графа минус архивный статус, его переходы и
        SLA-правила — а не пересборкой из живых строк черновика. Раньше пересборка молча
        публиковала всё, что администратор успел набросать в черновике (в том числе
        невалидное, без проверок публикации): архивация статуса выпускала чужие
        неопубликованные правки. Теперь черновик остаётся черновиком до явной публикации.

        `published_at`/`published_by`/`version` не трогаются: это не публикация человеком."""
        published = workflow.published_graph
        if published is None:
            return  # воронку ещё не публиковали: снимка нет, сделки живут только на публикации
        archived_ids = {
            str(s.id) for s in (await self._load_statuses(workflow.id)).values() if s.is_archived
        }
        if archived is not None:
            archived_ids.add(str(archived.id))
        snapshot = _without_statuses(published, archived_ids)
        digest = snapshot_digest(snapshot)
        workflow.published_graph = snapshot
        workflow.graph_hash = digest
        await self._session.flush()
        workflow_id = workflow.id
        run_after_commit(self._session, lambda: invalidate_workflow_cache(workflow_id))

    async def _complete_mapping_audit(self, job: StatusMappingJob) -> None:
        await self._audit.record(
            AuditAction.STATUS_MAPPING_COMPLETED,
            entity_type="status_mapping_job",
            entity_id=job.id,
            changes={"processed": job.processed_count, "failed": job.failed_count},
        )


def _without_statuses(snapshot: dict[str, Any], status_ids: set[str]) -> dict[str, Any]:
    """Снимок без перечисленных статусов, их переходов и SLA-правил."""
    return {
        **snapshot,
        "statuses": [s for s in snapshot.get("statuses", []) if s["id"] not in status_ids],
        "transitions": [
            t
            for t in snapshot.get("transitions", [])
            if t["from_status_id"] not in status_ids and t["to_status_id"] not in status_ids
        ],
        "sla_rules": [r for r in snapshot.get("sla_rules", []) if r["status_id"] not in status_ids],
    }


def snapshot_structure_warnings(snapshot: dict[str, Any]) -> list[str]:
    """Структурные проблемы снимка после архивации статуса: недостижимые статусы и ловушки.

    Предупреждения, а не отказ: архивация убирает статус вместе с его переходами, и
    администратору остаётся пересобрать пути в черновике и опубликовать заново."""
    statuses = {s["id"]: s for s in snapshot.get("statuses", [])}
    initials = [sid for sid, s in statuses.items() if s["type"] == StatusType.INITIAL.value]
    forward: dict[Any, list[Any]] = defaultdict(list)
    backward: dict[Any, list[Any]] = defaultdict(list)
    for t in snapshot.get("transitions", []):
        forward[t["from_status_id"]].append(t["to_status_id"])
        backward[t["to_status_id"]].append(t["from_status_id"])
    warnings: list[str] = []
    if len(initials) == 1:
        reachable = _bfs(initials[0], forward)
        lost_names = sorted(s["name"] for sid, s in statuses.items() if sid not in reachable)
        if lost_names:
            warnings.append(f"Недостижимые из начального статусы: {', '.join(lost_names)}")
    terminal_values = {t.value for t in TERMINAL_TYPES}
    terminals = {sid for sid, s in statuses.items() if s["type"] in terminal_values}
    if terminals:
        can_finish = _bfs_multi_source(terminals, backward)
        traps = sorted(s["name"] for sid, s in statuses.items() if sid not in can_finish)
        if traps:
            warnings.append(f"Статусы без пути в закрывающий статус: {', '.join(traps)}")
    return warnings


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
        errors.append("Воронка должна иметь хотя бы один терминальный статус (won/lost)")

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
        # `parked` без исходящих переходов — не ловушка-ошибка, а предупреждение ниже: заморозка
        # без выхода не мешает публикации (так работали все воронки до того, как parked перестал
        # быть терминальным), но администратор должен знать, что возобновить сделку нельзя.
        traps = {
            i
            for i in live_ids - can_reach_terminal
            if by_id[i].type != StatusType.PARKED.value or forward.get(i)
        }
        if traps:
            codes = sorted(by_id[i].code for i in traps)
            errors.append(f"Статусы без пути в терминальный статус (ловушки): {', '.join(codes)}")

    # `parked` — пауза, а не закрытие: сделка должна уметь из неё выйти. Без исходящих переходов
    # заморозка необратима (раньше она была терминальной и «закрывала» сделку).
    for parked in (s for s in live if s.type == StatusType.PARKED.value):
        if not forward.get(parked.id):
            warnings.append(
                f"Статус «{parked.name}» (parked) без исходящих переходов: "
                "сделку из него нельзя возобновить"
            )

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


def transition_snapshot_entry(t: WorkflowTransition) -> dict[str, Any]:
    """Переход в том виде, в каком он лежит в `published_graph`."""
    return {
        "id": str(t.id),
        "from_status_id": str(t.from_status_id),
        "to_status_id": str(t.to_status_id),
        "name": t.name,
        "allowed_roles": t.allowed_roles,
        "conditions": t.conditions,
        "actions": t.actions,
        "requires_comment": t.requires_comment,
    }


def snapshot_digest(snapshot: dict[str, Any]) -> str:
    """`graph_hash` снимка: sha256 канонического JSON."""
    return hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


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
            transition_snapshot_entry(t)
            for t in transitions
            if t.from_status_id in live_ids and t.to_status_id in live_ids
        ],
        "sla_rules": [
            {
                "status_id": str(r.status_id),
                "max_duration_seconds": r.max_duration.total_seconds(),
                "warn_threshold_pct": r.warn_threshold_pct,
                "count_business_days": r.count_business_days,
                # Эскалация и каналы живут в снимке: сделки идут по нему, а не по живым правилам
                # черновика, и воркер SLA должен знать, кого и когда уведомлять.
                "escalate_threshold_pct": r.escalate_threshold_pct,
                "escalate_to_role": r.escalate_to_role,
                "escalate_to_user_id": str(r.escalate_to_user_id)
                if r.escalate_to_user_id
                else None,
                "channels": list(r.channels or []),
            }
            for r in sla_rules
            if r.status_id in live_ids and r.is_active
        ],
    }


def has_unpublished_changes(
    workflow: Workflow,
    statuses: Sequence[WorkflowStatus],
    transitions: Sequence[WorkflowTransition],
    sla_rules: Sequence[SlaRule],
) -> bool:
    """Черновик графа отличается от опубликованного снимка. Сравнивается содержимое, а не
    `graph_hash`: порядок строк из БД не задан, и хэш одного и того же графа мог бы
    разойтись. Поэтому оба снимка приводятся к одному порядку списков."""
    published = workflow.published_graph
    if published is None:
        return True
    draft = _build_snapshot(workflow, list(statuses), list(transitions), list(sla_rules))
    return _canonical_snapshot(draft) != _canonical_snapshot(published)


def _canonical_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Списки снимка в одном порядке. Ключи берутся через `.get`: снимки старых публикаций
    бывают неполными, и это не повод отвечать 500 на чтение воронки."""
    return {
        **snapshot,
        "statuses": sorted(snapshot.get("statuses", []), key=lambda item: item["id"]),
        "transitions": sorted(snapshot.get("transitions", []), key=lambda item: item["id"]),
        "sla_rules": sorted(
            (
                # Правила старых публикаций без полей эскалации приравниваются к правилу с
                # умолчаниями: иначе каждая воронка с SLA показывала бы «есть неопубликованные
                # изменения» только из-за того, что снимок появился раньше этих полей.
                {**_SLA_RULE_SNAPSHOT_DEFAULTS, **rule}
                for rule in snapshot.get("sla_rules", [])
            ),
            key=lambda item: item["status_id"],
        ),
    }
