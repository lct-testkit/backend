"""Сервис аудита.

Ключевое требование раздела 1: запись аудита идёт в той же транзакции, что
и бизнес-изменение. Поэтому сервис принимает уже открытую сессию и никогда
не коммитит сам — коммит делает владелец транзакции.

Цепочка хэшей: каждая запись хранит `prev_hash` предыдущей и свой `hash`.
Чтобы цепочка не рвалась при параллельных вставках, голова цепочки берётся
под транзакционным advisory-локом.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import structlog
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import get_actor, get_client, get_request_id
from app.core.masking import mask_mapping
from app.core.metrics import audit_records_total
from app.modules.audit.actions import AuditAction
from app.modules.audit.models import AuditLog, AuditResult

logger = structlog.get_logger(__name__)

# Произвольная, но фиксированная константа для advisory-лока цепочки аудита.
_AUDIT_CHAIN_LOCK_ID = 0x4352_4D41  # "CRMA"

GENESIS_HASH = "0" * 64


def compute_hash(
    *,
    prev_hash: str | None,
    created_at: str,
    actor_id: str | None,
    action: str,
    entity_type: str | None,
    entity_id: str | None,
    changes: dict[str, Any] | None,
    result: str,
    request_id: str | None,
) -> str:
    """Канонизация и SHA-256. Порядок полей фиксирован, иначе хэш невоспроизводим."""
    payload = {
        "prev_hash": prev_hash or GENESIS_HASH,
        "created_at": created_at,
        "actor_id": actor_id,
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "changes": changes,
        "result": result,
        "request_id": request_id,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def diff_changes(
    before: dict[str, Any] | None, after: dict[str, Any] | None
) -> dict[str, dict[str, Any]]:
    """Готовит поле `changes`: только изменившиеся поля, в формате old/new."""
    before = before or {}
    after = after or {}
    changes: dict[str, dict[str, Any]] = {}
    for key in set(before) | set(after):
        old = before.get(key)
        new = after.get(key)
        if old != new:
            changes[key] = {"old": old, "new": new}
    return mask_mapping(changes)  # type: ignore[return-value]


class AuditService:
    """Пишет аудит в переданную сессию. Не коммитит и не открывает транзакции."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _chain_head(self) -> str | None:
        # Лок транзакционный: освободится вместе с коммитом или откатом.
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": _AUDIT_CHAIN_LOCK_ID},
        )
        result = await self._session.execute(
            select(AuditLog.hash).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(1)
        )
        return result.scalar_one_or_none()

    async def record(
        self,
        action: AuditAction | str,
        *,
        entity_type: str | None = None,
        entity_id: uuid.UUID | None = None,
        changes: dict[str, Any] | None = None,
        result: AuditResult = AuditResult.SUCCESS,
        actor_id: uuid.UUID | None = None,
        actor_role: str | None = None,
    ) -> AuditLog:
        actor = get_actor()
        client = get_client()
        request_id = get_request_id()

        resolved_actor_id = actor_id or (actor.user_id if actor else None)
        resolved_role = actor_role or (actor.role if actor else None)
        masked_changes = mask_mapping(changes) if changes else None

        prev_hash = await self._chain_head()

        entry = AuditLog(
            actor_id=resolved_actor_id,
            actor_role=resolved_role,
            impersonated_by=actor.impersonated_by if actor else None,
            action=str(action),
            entity_type=entity_type,
            entity_id=entity_id,
            changes=masked_changes,  # type: ignore[arg-type]
            result=str(result),
            ip=client.ip if client else None,
            user_agent=client.user_agent if client else None,
            request_id=request_id,
            prev_hash=prev_hash,
        )
        # created_at нужен до вставки: он входит в хэш и в первичный ключ.
        now = await self._session.scalar(text("SELECT now()"))
        entry.created_at = now  # type: ignore[assignment]
        entry.hash = compute_hash(
            prev_hash=prev_hash,
            created_at=now.isoformat(),  # type: ignore[union-attr]
            actor_id=str(resolved_actor_id) if resolved_actor_id else None,
            action=str(action),
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id else None,
            changes=masked_changes,  # type: ignore[arg-type]
            result=str(result),
            request_id=request_id,
        )

        self._session.add(entry)
        await self._session.flush()

        audit_records_total.labels(action=str(action), result=str(result)).inc()
        logger.info(
            "audit",
            action=str(action),
            entity_type=entity_type,
            entity_id=str(entity_id) if entity_id else None,
            result=str(result),
        )
        return entry

    async def record_denied(
        self,
        action: AuditAction | str,
        *,
        entity_type: str | None = None,
        entity_id: uuid.UUID | None = None,
        reason: str | None = None,
    ) -> AuditLog:
        """Отказы в доступе тоже пишутся в аудит (раздел «Эндпоинты аудита»)."""
        return await self.record(
            action,
            entity_type=entity_type,
            entity_id=entity_id,
            changes={"reason": reason} if reason else None,
            result=AuditResult.DENIED,
        )

    async def verify_chain(self, *, limit: int = 1000) -> dict[str, Any]:
        """Проверяет целостность хвоста цепочки. Используется админкой и тестами."""
        rows = (
            (
                await self._session.execute(
                    select(AuditLog)
                    .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        entries = list(reversed(rows))
        broken: list[str] = []
        for index, entry in enumerate(entries):
            expected = compute_hash(
                prev_hash=entry.prev_hash,
                created_at=entry.created_at.isoformat(),
                actor_id=str(entry.actor_id) if entry.actor_id else None,
                action=entry.action,
                entity_type=entry.entity_type,
                entity_id=str(entry.entity_id) if entry.entity_id else None,
                changes=entry.changes,
                result=entry.result,
                request_id=entry.request_id,
            )
            if expected != entry.hash:
                broken.append(f"{entry.id}: хэш записи не совпадает")
            if index > 0 and entry.prev_hash != entries[index - 1].hash:
                broken.append(f"{entry.id}: разрыв цепочки с предыдущей записью")

        return {"checked": len(entries), "ok": not broken, "problems": broken}


async def record_out_of_band(
    action: AuditAction | str,
    *,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    changes: dict[str, Any] | None = None,
    result: AuditResult = AuditResult.DENIED,
) -> None:
    """Пишет аудит в собственной транзакции.

    Обычные бизнес-события пишутся в транзакции изменения данных. Но отказ в
    доступе завершается исключением, и эта транзакция откатывается — вместе с
    записью аудита. Поэтому такие события фиксируются отдельно, чтобы
    требование «отказы в доступе также записываются» реально выполнялось.
    """
    from app.core.db import session_scope

    try:
        async with session_scope() as session:
            await AuditService(session).record(
                action,
                entity_type=entity_type,
                entity_id=entity_id,
                changes=changes,
                result=result,
            )
    except Exception:  # noqa: BLE001
        # Невозможность записать отказ не должна подменять исходную ошибку прав.
        logger.exception("audit_out_of_band_failed", action=str(action))
