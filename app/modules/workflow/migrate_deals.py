"""B2-3: перенос открытых сделок с одной воронки (по коду) на другую.

Запуск внутри контейнера (не `tools/`: `Dockerfile` копирует в образ только `app`,
`migrations` и `alembic.ini` — каталога `tools/` там нет)::

    python -m app.modules.workflow.migrate_deals --from b2b_university_v1 --to b2b_university_v2 \\
        [--map-file map.json] [--apply] [--batch 100] [--sla-mode recalculate]

Без `--apply` — сухой прогон (по умолчанию): печатает таблицу «из статуса → в статус →
сделок» и ничего не пишет в БД. `--map-file` — JSON-объект `{"код_v1": "код_v2", ...}`,
переопределяющий/дополняющий встроенную таблицу `DEFAULT_V1_TO_V2` (коды статусов v1 и v2 —
по разделу 2 плана спринта, сверены с `app/modules/workflow/seed.py`).

Правило: соответствие статусов статично (код → код), без условных правил по полям сделки —
это решает `B-7`/`mapping_rules` (спринт B9, отдельная задача, здесь не реализуется).

Безопасно запускать повторно: партия берёт только сделки, у которых `workflow_id` ещё равен
исходной воронке — уже перенесённые не попадут в выборку снова. Строки, занятые параллельной
транзакцией, партия пропускает (`SKIP LOCKED`) — после `--apply` скрипт сверяет остаток и, если
часть сделок осталась на исходной воронке не из-за открытой подписи, завершается кодом 3: запуск
нужно повторить. Сам скрипт не проверяет адрес базы — ответственность за то, что `--apply`
запускается на нужной базе, на том, кто его запускает.

Кэш карточки сделки (`cache:deal:{id}:v{version}`) вручную не сбрасывается: ключ версионный, а
`claim_version` в партии поднимает `version` каждой перенесённой сделки.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
import uuid
from collections import Counter
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session_factory
from app.core.errors import AppError
from app.core.optimistic import claim_version
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.crm.models import (
    Deal,
    DealComment,
    DealEvent,
    DealEventType,
    DealStatusHistory,
    HistoryReason,
)
from app.modules.crm.service import apply_sla
from app.modules.workflow.models import Workflow, WorkflowStatus
from app.modules.workflow.service import get_cached_published_graph

#: Раздел 2 плана спринта: соответствие кодов статусов v1 → v2, статично (код → код).
#: Проверено по `app/modules/workflow/seed.py` — коды v2 совпадают буквально.
DEFAULT_V1_TO_V2: dict[str, str] = {
    "identification": "contact_search",
    "first_contact": "communication",
    "qualification": "communication",
    "meeting_scheduled": "meeting",
    "meeting_held": "meeting",
    "requirements": "documents_exchange",
    "kp_preparation": "documents_exchange",
    "kp_approval": "documents_exchange",
    "legal_approval": "documents_exchange",
    "contract_signing": "signing",
    "lms_transfer": "materials_transfer",
    "training_launch": "teacher_training",
    "monitoring": "classes",
    "closing_prolongation": "execution_control",
}

#: Сделки с открытой подписью не переносятся: их нужно дождаться или аннулировать подпись
#: вручную — перенос статуса сделки, ожидающей подписи по старой воронке, рассинхронизирует
#: `document.requested_in_status_id`/`on_signed` с новым графом.
_OPEN_SIGNATURE_STATUSES = frozenset({"pending", "partially_signed"})


class MigrationError(Exception):
    """Ошибка конфигурации/данных, не позволяющая начать перенос — ничего не записано."""


async def _load_workflow(session: AsyncSession, code: str) -> Workflow:
    workflow = await session.scalar(select(Workflow).where(Workflow.code == code))
    if workflow is None:
        raise MigrationError(f"Воронка с кодом {code!r} не найдена")
    return workflow


async def _build_status_map(
    session: AsyncSession,
    from_workflow: Workflow,
    graph: dict[str, Any],
    overrides: dict[str, str],
) -> tuple[dict[uuid.UUID, dict[str, Any]], dict[uuid.UUID, WorkflowStatus]]:
    """Возвращает `{status_id(v1): статус v2 из ОПУБЛИКОВАННОГО СНИМКА}` для статусов, у
    которых есть пара, и словарь `{status_id(v1): WorkflowStatus(v1)}` (для имён/кодов в
    истории и в отчёте). Пара ищется среди статусов снимка `graph["statuses"]`, а не живой
    таблицы целевой воронки: черновые правки после публикации не должны участвовать в
    переносе — сделки всё равно продолжат жить по снимку (см. `get_cached_published_graph`).
    Статус без пары просто не попадает в первый словарь — вызывающая сторона отдельно
    проверяет, что сделок на таких статусах нет."""
    from_statuses = (
        (
            await session.execute(
                select(WorkflowStatus).where(WorkflowStatus.workflow_id == from_workflow.id)
            )
        )
        .scalars()
        .all()
    )
    to_by_code: dict[str, dict[str, Any]] = {s["code"]: s for s in graph["statuses"]}
    from_by_id = {s.id: s for s in from_statuses}
    mapping: dict[uuid.UUID, dict[str, Any]] = {}
    for status in from_statuses:
        target_code = overrides.get(status.code) or DEFAULT_V1_TO_V2.get(status.code, status.code)
        target = to_by_code.get(target_code)
        if target is not None:
            mapping[status.id] = target
    return mapping, from_by_id


async def _validate_before_start(
    session: AsyncSession,
    from_workflow: Workflow,
    status_map: dict[uuid.UUID, dict[str, Any]],
    from_by_id: dict[uuid.UUID, WorkflowStatus],
) -> list[str]:
    """Коды статусов v1, у которых есть живые сделки, но нет пары в v2 — пустой список
    означает «можно начинать», непустой — скрипт завершается кодом 2 без единой записи."""
    status_ids_with_deals = (
        (
            await session.execute(
                select(Deal.status_id)
                .where(Deal.workflow_id == from_workflow.id, Deal.deleted_at.is_(None))
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    unresolved = sorted(
        {
            from_by_id[status_id].code
            for status_id in status_ids_with_deals
            if status_id not in status_map and status_id in from_by_id
        }
    )
    return unresolved


async def _migrate_one_batch(
    session: AsyncSession,
    *,
    from_workflow: Workflow,
    to_workflow: Workflow,
    status_map: dict[uuid.UUID, dict[str, Any]],
    from_by_id: dict[uuid.UUID, WorkflowStatus],
    graph: dict[str, Any],
    sla_mode: str,
    batch_size: int,
    cursor: uuid.UUID | None,
    apply: bool,
) -> tuple[list[uuid.UUID], Counter[tuple[str, str]], list[uuid.UUID], uuid.UUID | None, int]:
    """Одна партия. Возвращает (id мигрированных сделок; счётчик «из статуса -> в статус» ->
    число сделок; id сделок с открытой подписью, пропущенных в этой партии — для отчёта; курсор
    следующей партии; число сделок, фактически прочитанных в этой партии). `None` в качестве
    курсора — начало выборки."""
    stmt = (
        select(Deal)
        .where(Deal.workflow_id == from_workflow.id, Deal.deleted_at.is_(None))
        .order_by(Deal.id)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )
    if cursor is not None:
        stmt = stmt.where(Deal.id > cursor)
    deals = (await session.execute(stmt)).scalars().all()

    migrated: list[uuid.UUID] = []
    counts: Counter[tuple[str, str]] = Counter()
    skipped_signature_ids: list[uuid.UUID] = []
    now = dt.datetime.now(dt.UTC)

    for deal in deals:
        if deal.signature_status in _OPEN_SIGNATURE_STATUSES:
            skipped_signature_ids.append(deal.id)
            continue
        target = status_map.get(deal.status_id)
        if target is None:
            # Валидация перед первой партией уже должна была это отловить — защитная ветка
            # на случай, если сделка сменила статус между валидацией и этой партией.
            continue
        from_status = from_by_id.get(deal.status_id)
        from_status_id = deal.status_id
        is_closed = deal.closed_at is not None
        target_id = uuid.UUID(target["id"])

        await claim_version(session, deal)
        deal.workflow_id = to_workflow.id
        deal.status_id = target_id
        deal.status_changed_at = now
        deal.sla_escalated_at = None

        if not is_closed:
            if sla_mode == "recalculate":
                await apply_sla(session, deal, graph, target, now)
            elif sla_mode == "reset":
                deal.sla_due_at = None
                deal.sla_state = "ok"
            # "keep" — sla_due_at/sla_state переносятся как есть (дефолт).
        # Закрытые (won/lost/parked по равенству кода) — closed_at и SLA не трогаем вовсе.

        from_name = from_status.name if from_status else str(from_status_id)
        session.add(
            DealStatusHistory(
                deal_id=deal.id,
                from_status_id=from_status_id,
                to_status_id=target_id,
                changed_by=None,
                reason=HistoryReason.WORKFLOW_MIGRATION.value,
                comment=(
                    f"Перенос на воронку «{to_workflow.name}»: «{from_name}» → «{target['name']}»"
                ),
                changed_at=now,
            )
        )
        session.add(
            DealEvent(
                deal_id=deal.id,
                event_type=DealEventType.WORKFLOW_MIGRATION.value,
                actor_id=None,
                payload={
                    "from_workflow_id": str(from_workflow.id),
                    "to_workflow_id": str(to_workflow.id),
                    "from_status_id": str(from_status_id),
                    "to_status_id": str(target_id),
                },
            )
        )
        session.add(
            DealComment(
                deal_id=deal.id,
                author_id=None,
                is_system=True,
                body=(
                    f"Сделка перенесена с воронки «{from_workflow.name}» на "
                    f"«{to_workflow.name}»: статус «{from_name}» → «{target['name']}»."
                ),
            )
        )
        migrated.append(deal.id)
        counts[(from_status.code if from_status else "?", target["code"])] += 1

    # Аудит пишется только при настоящей записи: на сухом прогоне эта партия всё равно
    # откатывается целиком, а `AuditService.record` сверх этого берёт advisory-лок цепочки
    # аудита (см. `AuditService._lock_chain`) — захватывать его просто чтобы отменить ход
    # лишняя точка контакта с параллельными писателями аудита.
    if apply and migrated:
        await AuditService(session).record(
            AuditAction.WORKFLOW_DEALS_MIGRATED,
            entity_type="workflow",
            entity_id=to_workflow.id,
            changes={
                "from_workflow": {"old": None, "new": from_workflow.code},
                "to_workflow": {"old": None, "new": to_workflow.code},
                "count": {"old": None, "new": len(migrated)},
            },
        )
    await session.flush()
    next_cursor = deals[-1].id if deals else None
    return migrated, counts, skipped_signature_ids, next_cursor, len(deals)


async def run_migration(
    *,
    from_code: str,
    to_code: str,
    overrides: dict[str, str],
    apply: bool,
    batch_size: int,
    sla_mode: str,
) -> int:
    """Возвращает код выхода (0 — успех/сухой прогон, 2 — есть статус без пары, 3 — после
    `--apply` часть сделок осталась на исходной воронке не из-за открытой подписи: их держала
    параллельная транзакция, запуск нужно повторить)."""
    factory = get_session_factory()

    async with factory() as probe_session:
        from_workflow = await _load_workflow(probe_session, from_code)
        to_workflow = await _load_workflow(probe_session, to_code)
        try:
            graph = await get_cached_published_graph(to_workflow)
        except AppError as exc:
            print(f"Ошибка: {exc.detail}", file=sys.stderr)
            return 2
        status_map, from_by_id = await _build_status_map(
            probe_session, from_workflow, graph, overrides
        )
        unresolved = await _validate_before_start(
            probe_session, from_workflow, status_map, from_by_id
        )
        if unresolved:
            print("Статусы без пары в целевой воронке (ничего не изменено):", file=sys.stderr)
            for code in unresolved:
                print(f"  - {code}", file=sys.stderr)
            return 2

    total_counts: Counter[tuple[str, str]] = Counter()
    total_skipped_signature_ids: list[uuid.UUID] = []
    cursor: uuid.UUID | None = None

    while True:
        async with factory() as session:
            # Транзакция открывается неявно первым запросом (autobegin) — `with_for_update`
            # держит замок строк до явного commit/rollback ниже, без отдельного `session.begin()`.
            (
                _migrated_ids,
                counts,
                skipped_signature_ids,
                next_cursor,
                rows_seen,
            ) = await _migrate_one_batch(
                session,
                from_workflow=from_workflow,
                to_workflow=to_workflow,
                status_map=status_map,
                from_by_id=from_by_id,
                graph=graph,
                sla_mode=sla_mode,
                batch_size=batch_size,
                cursor=cursor,
                apply=apply,
            )
            if apply:
                await session.commit()
            else:
                # Сухой прогон: партия обрабатывается и считается как при настоящем переносе
                # (включая проверку статуса/подписи), но транзакция откатывается — ничего не
                # записано. Локи строк снимаются вместе с откатом.
                await session.rollback()

        total_counts.update(counts)
        total_skipped_signature_ids.extend(skipped_signature_ids)
        # `LIMIT` считает уже после `SKIP LOCKED`: неполная партия — это конец выборки, а не
        # «часть строк занята». Занятые строки остаются позади курсора — их найдёт сверка ниже.
        if rows_seen < batch_size or next_cursor is None:
            break
        cursor = next_cursor

    _print_report(total_counts, total_skipped_signature_ids, apply=apply)
    if apply:
        async with factory() as session:
            left = await session.scalar(
                select(func.count())
                .select_from(Deal)
                .where(Deal.workflow_id == from_workflow.id, Deal.deleted_at.is_(None))
            )
        not_moved = int(left or 0) - len(total_skipped_signature_ids)
        if not_moved > 0:
            print(
                f"Внимание: на исходной воронке осталось {not_moved} сделок, не перенесённых "
                "не из-за открытой подписи (их держала параллельная транзакция или статус "
                "изменился после проверки) — повторите запуск.",
                file=sys.stderr,
            )
            return 3
    return 0


def _print_report(
    counts: Counter[tuple[str, str]],
    skipped_signature_ids: list[uuid.UUID],
    *,
    apply: bool,
) -> None:
    total = sum(counts.values())
    print("# из статуса             → в статус                  сделок")
    for (from_status, to_status), count in sorted(counts.items()):
        print(f"# {from_status:<20} → {to_status:<24} {count}")
    print(
        f"# итого {total}, без пары 0, с открытой подписью "
        f"{len(skipped_signature_ids)} (пропускаются)."
    )
    if skipped_signature_ids:
        print("# Сделки с открытой подписью (не перенесены, дождитесь подписи или снимите её):")
        for deal_id in skipped_signature_ids:
            print(f"  - {deal_id}")
    if not apply:
        print("# Для записи добавьте --apply")


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="B2-3: перенос открытых сделок на другую воронку (по коду статуса)."
    )
    parser.add_argument("--from", dest="from_code", required=True, help="Код исходной воронки")
    parser.add_argument("--to", dest="to_code", required=True, help="Код целевой воронки")
    parser.add_argument(
        "--map-file",
        dest="map_file",
        default=None,
        help="JSON {код_v1: код_v2}, переопределяет встроенную таблицу DEFAULT_V1_TO_V2",
    )
    parser.add_argument(
        "--apply", action="store_true", help="Записать изменения (без флага — сухой прогон)"
    )
    parser.add_argument("--batch", type=int, default=100, help="Размер партии (по умолчанию 100)")
    parser.add_argument(
        "--sla-mode",
        choices=("recalculate", "reset", "keep"),
        default="recalculate",
        help="Как считать SLA нового статуса для ОТКРЫТЫХ сделок (закрытые не трогаются)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    overrides: dict[str, str] = {}
    if args.map_file:
        with open(args.map_file, encoding="utf-8") as fh:
            overrides = json.load(fh)
    try:
        return asyncio.run(
            run_migration(
                from_code=args.from_code,
                to_code=args.to_code,
                overrides=overrides,
                apply=args.apply,
                batch_size=args.batch,
                sla_mode=args.sla_mode,
            )
        )
    except MigrationError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
