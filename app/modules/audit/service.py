"""Сервис аудита.

Ключевое требование раздела 1: запись аудита идёт в той же транзакции, что
и бизнес-изменение. Поэтому сервис принимает уже открытую сессию и никогда
не коммитит сам — коммит делает владелец транзакции.

Цепочка хэшей: каждая запись хранит `prev_hash` предыдущей и свой `hash`.
Чтобы цепочка не рвалась при параллельных вставках, голова цепочки берётся
под транзакционным advisory-локом — из `audit_chain_head` (синглтон-таблица,
`app.modules.audit.models.AuditChainHead`), не пересчётом по `audit_log`.

Состав хэшируемых полей версионируется (`hash_version`): записи версии 1 (всё, что
было до расширения) проверяются по-старому, новые — версии 2, где в хэш входят и роль
актора, подмена личности, IP и User-Agent. Иначе эти поля можно было бы поправить в БД,
и цепочка этого не заметила бы.

Версия 3 — тот же состав, что у версии 2, но вместо голого SHA-256 берётся HMAC-SHA256 с
серверным ключом `AUDIT_HMAC_KEY`. Голый SHA-256 может пересчитать любой, у кого есть запись в
БД: правит запись и переписывает хэши всех последующих. Ключ лежит вне БД, и без него такую
подмену не скрыть. Пока ключ не задан, пишется версия 2, как раньше.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import Select, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.context import (
    get_actor,
    get_client,
    get_request_id,
    set_actor,
    set_client,
    set_request_id,
)
from app.core.errors import AppError, ErrorCode
from app.core.masking import mask_mapping
from app.core.metrics import audit_records_total
from app.modules.audit.actions import AuditAction
from app.modules.audit.models import AuditChainHead, AuditLog, AuditResult

logger = structlog.get_logger(__name__)

# Произвольная, но фиксированная константа для advisory-лока цепочки аудита.
_AUDIT_CHAIN_LOCK_ID = 0x4352_4D41  # "CRMA"

# Сколько запись аудита ждёт лок цепочки. Лок один на всю систему и держится до коммита
# транзакции-владельца: без предела зависший запрос с открытой транзакцией останавливал бы
# все аудируемые изменения в системе. Отказ по таймауту — 503 «повторите», а не вечное ожидание.
_AUDIT_LOCK_TIMEOUT_MS = 15_000
_PG_LOCK_NOT_AVAILABLE = "55P03"

GENESIS_HASH = "0" * 64

#: Версия состава хэшируемых полей для новых записей без ключа. 1 — исходный состав (без роли,
#: IP и UA), 2 — расширенный (SHA-256).
AUDIT_HASH_VERSION = 2
#: Расширенный состав, хэш — HMAC-SHA256 с `AUDIT_HMAC_KEY`. Пишется, когда ключ задан.
AUDIT_HASH_VERSION_HMAC = 3
_KNOWN_HASH_VERSIONS = frozenset({1, 2, AUDIT_HASH_VERSION_HMAC})


def audit_hmac_key() -> bytes | None:
    """Ключ HMAC записей аудита из настроек; `None` — ключ не задан."""
    secret = get_settings().audit_hmac_key
    value = secret.get_secret_value() if secret is not None else ""
    return value.encode("utf-8") if value else None


@dataclass(slots=True)
class AuditFilters:
    """Фильтры выборки журнала (раздел 6.12)."""

    actor_id: uuid.UUID | None = None
    action: str | None = None
    entity_type: str | None = None
    entity_id: uuid.UUID | None = None
    result: str | None = None
    request_id: str | None = None
    date_from: dt.datetime | None = None
    date_to: dt.datetime | None = None
    # Скоуп: None — без ограничения, [] — пустой доступ.
    actor_ids: list[uuid.UUID] | None = None


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
    version: int = 1,
    actor_role: str | None = None,
    impersonated_by: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    hmac_key: bytes | None = None,
) -> str:
    """Канонизация и SHA-256 (версия 3 — HMAC-SHA256). Порядок полей фиксирован, иначе хэш
    невоспроизводим.

    Версия 1 хэширует девять исходных полей — записи, сделанные до расширения, обязаны
    проверяться именно так, поэтому её состав менять нельзя. Версия 2 добавляет роль
    актора, `impersonated_by`, IP и User-Agent и саму версию (чтобы запись версии 2 нельзя
    было выдать за версию 1 с теми же девятью полями). Версия 3 хэширует тот же состав, что и
    версия 2 (с `v` = 3, чтобы запись нельзя было выдать за другую версию), но как HMAC с
    `hmac_key`: без ключа она не вычисляется, вызов без ключа — ошибка программиста, а не
    тихий откат на SHA-256, который выдал бы подделываемый хэш за защищённый."""
    if version not in _KNOWN_HASH_VERSIONS:
        raise ValueError(f"неизвестная версия хэша аудита: {version}")

    payload: dict[str, Any] = {
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
    if version >= 2:
        payload.update(
            {
                "v": version,
                "actor_role": actor_role,
                "impersonated_by": impersonated_by,
                "ip": ip,
                "user_agent": user_agent,
            }
        )
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    if version >= AUDIT_HASH_VERSION_HMAC:
        if not hmac_key:
            raise ValueError("для хэша аудита версии 3 нужен ключ HMAC")
        return hmac.new(hmac_key, canonical.encode("utf-8"), hashlib.sha256).hexdigest()
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
        """Берёт голову цепочки под advisory-локом.

        Лок транзакционный: освободится вместе с коммитом или откатом.
        Важно: вложенная транзакция, которая берёт этот же лок, пока его
        держит транзакция того же запроса, даёт взаимную блокировку. Поэтому
        записи, не принадлежащие транзакции запроса, откладываются до её
        завершения (`defer_audit`), а не пишутся «рядом».

        Ожидание лока ограничено `_AUDIT_LOCK_TIMEOUT_MS` (`lock_timeout` действует только на
        время захвата и потом возвращается прежним): владелец лока, застрявший на медленном
        внешнем вызове, не должен замораживать запись аудита у всей системы.
        """
        previous = await self._session.scalar(text("SELECT current_setting('lock_timeout')"))
        await self._session.execute(
            text("SELECT set_config('lock_timeout', :value, true)"),
            {"value": f"{_AUDIT_LOCK_TIMEOUT_MS}ms"},
        )
        try:
            await self._session.execute(
                text("SELECT pg_advisory_xact_lock(:lock_id)"),
                {"lock_id": _AUDIT_CHAIN_LOCK_ID},
            )
        except DBAPIError as exc:
            if getattr(exc.orig, "pgcode", None) == _PG_LOCK_NOT_AVAILABLE:
                logger.error("audit_chain_lock_timeout", timeout_ms=_AUDIT_LOCK_TIMEOUT_MS)
                raise AppError(
                    ErrorCode.DEPENDENCY_UNAVAILABLE,
                    "Журнал аудита занят другими операциями, повторите запрос",
                    headers={"Retry-After": "1"},
                ) from exc
            raise
        await self._session.execute(
            text("SELECT set_config('lock_timeout', :value, true)"), {"value": previous or "0"}
        )
        # `audit_chain_head` — синглтон-таблица, не `ORDER BY ... LIMIT 1` по партиционированному
        # audit_log (перф-диагностика 27.09, см. докстринг AuditChainHead в audit/models.py):
        # то же самое значение, но без constraint exclusion по всем партициям на каждый вызов.
        return await self._session.scalar(select(AuditChainHead.hash))

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
        # С ключом запись подписывается HMAC (версия 3), без ключа — как раньше (версия 2).
        hmac_key = audit_hmac_key()
        hash_version = AUDIT_HASH_VERSION_HMAC if hmac_key else AUDIT_HASH_VERSION

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
            hash_version=hash_version,
        )
        # created_at нужен до вставки: он входит в хэш и в первичный ключ. Берётся именно
        # `clock_timestamp()` и именно ПОСЛЕ захвата лока цепочки: `now()` — это время начала
        # транзакции. Долгая транзакция, начавшаяся раньше, но получившая лок позже, писала бы
        # запись «в прошлое» с `prev_hash` более поздней, а следующая находила бы голову по
        # `created_at` уже не там, где она в цепочке: цепочка ветвилась и рвалась (26 разрывов и
        # 7 ветвлений на 96 параллельных записях). Время под локом растёт в порядке захвата
        # лока — порядок по `created_at` и порядок хэшей совпадают.
        now = await self._session.scalar(text("SELECT clock_timestamp()"))
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
            version=hash_version,
            actor_role=resolved_role,
            impersonated_by=str(entry.impersonated_by) if entry.impersonated_by else None,
            ip=entry.ip,
            user_agent=entry.user_agent,
            hmac_key=hmac_key,
        )

        self._session.add(entry)
        await self._session.flush()
        # Указатель обновляется в той же транзакции и под тем же локом: если транзакция
        # откатится, откатится и он, а `_chain_head()` следующего писателя снова увидит
        # прежнюю голову — цепочка не заметит несостоявшуюся запись.
        await self._session.execute(update(AuditChainHead).values(hash=entry.hash))

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

    def query(self, filters: AuditFilters) -> Select[tuple[AuditLog]]:
        """Выборка журнала с фильтрами раздела 6.12.

        Скоуп применяется вызывающей стороной через `actor_ids`: аудитор и
        администратор видят всё, руководитель — только свою команду.
        """
        stmt = select(AuditLog)
        if filters.actor_ids is not None:
            # Пустой список означает пустой скоуп, а не «без фильтра».
            stmt = stmt.where(AuditLog.actor_id.in_(filters.actor_ids))
        if filters.actor_id:
            stmt = stmt.where(AuditLog.actor_id == filters.actor_id)
        if filters.action:
            stmt = stmt.where(AuditLog.action == filters.action)
        if filters.entity_type:
            stmt = stmt.where(AuditLog.entity_type == filters.entity_type)
        if filters.entity_id:
            stmt = stmt.where(AuditLog.entity_id == filters.entity_id)
        if filters.result:
            stmt = stmt.where(AuditLog.result == filters.result)
        if filters.request_id:
            stmt = stmt.where(AuditLog.request_id == filters.request_id)
        if filters.date_from:
            stmt = stmt.where(AuditLog.created_at >= filters.date_from)
        if filters.date_to:
            stmt = stmt.where(AuditLog.created_at < filters.date_to)
        return stmt

    async def verify_chain(self, *, limit: int = 1000) -> dict[str, Any]:
        """Проверяет целостность хвоста цепочки. Используется админкой и тестами.

        Каждая запись проверяется по своей версии хэша. Записи версии 3 требуют
        `AUDIT_HMAC_KEY`: без него они не «подделаны», а просто непроверяемы, и отчёт говорит
        именно это. Неверный ключ от подделки отличить нельзя (хэш не сходится в обоих
        случаях), поэтому в сообщении названы оба объяснения."""
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
        hmac_key = audit_hmac_key()
        broken: list[str] = []
        unverifiable: list[uuid.UUID] = []
        # Самая высокая версия, уже встреченная в хвосте цепочки (записи идут по времени).
        highest_version = 0
        for index, entry in enumerate(entries):
            # Запись проверяется по тому составу полей, с которым она была хэширована.
            version = entry.hash_version or 1
            if version not in _KNOWN_HASH_VERSIONS:
                broken.append(f"{entry.id}: неизвестная версия хэша {version}")
            elif version >= AUDIT_HASH_VERSION_HMAC and hmac_key is None:
                unverifiable.append(entry.id)
            else:
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
                    version=version,
                    actor_role=entry.actor_role,
                    impersonated_by=str(entry.impersonated_by) if entry.impersonated_by else None,
                    ip=entry.ip,
                    user_agent=entry.user_agent,
                    hmac_key=hmac_key,
                )
                if expected != entry.hash:
                    reason = (
                        "хэш записи не совпадает (запись изменена либо задан другой "
                        "AUDIT_HMAC_KEY)"
                        if version >= AUDIT_HASH_VERSION_HMAC
                        else "хэш записи не совпадает"
                    )
                    broken.append(f"{entry.id}: {reason}")
            # Понижение версии после HMAC-записи: подмена, переписавшая записи голым SHA-256,
            # либо компонент, работающий без ключа. Проверяется только при заданном ключе:
            # без него отчёт и так сообщает, что записи версии 3 не проверены.
            if hmac_key is not None and version < highest_version:
                broken.append(
                    f"{entry.id}: запись версии {version} после версии {highest_version} "
                    "(понижение версии хэша либо компонент без AUDIT_HMAC_KEY)"
                )
            highest_version = max(highest_version, version)
            if index > 0 and entry.prev_hash != entries[index - 1].hash:
                broken.append(f"{entry.id}: разрыв цепочки с предыдущей записью")

        if unverifiable:
            broken.append(
                f"{unverifiable[0]}: не задан AUDIT_HMAC_KEY — записи версии 3 не проверены "
                f"(всего таких записей: {len(unverifiable)})"
            )
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

    Вызывать можно только там, где нет открытой транзакции запроса: иначе
    advisory-лок цепочки даёт взаимную блокировку. Штатное применение —
    фоновые задачи и CLI. Внутри запроса для отказов используется
    `record_denied_and_commit`.
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


def defer_denied_audit(
    session: AsyncSession,
    action: AuditAction | str = AuditAction.ACCESS_DENIED,
    *,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    reason: str | None = None,
) -> None:
    """Отказ на объектном уровне (чужая сделка, нет права на запись): в журнал он попадает
    отдельной закоммиченной записью ПОСЛЕ отката транзакции запроса.

    Проверка объекта завершается исключением (403/404), и транзакция откатывается вместе с
    всем, что запрос успел записать, — в том числе с аудитом. Писать отказ «рядом» нельзя:
    запись встала бы за advisory-локом цепочки, который, возможно, держит эта же транзакция.
    Поэтому запись откладывается до отката (`run_after_rollback`): к тому моменту лок
    освобождён, и отказ пишется собственной короткой транзакцией. Если запрос всё же
    завершился успехом (исключение перехвачено выше), отложенная запись отбрасывается.

    Актор, IP и request_id запоминаются сразу: к моменту отката контекст запроса может уже
    сбрасываться."""
    from app.core.db import run_after_rollback

    actor = get_actor()
    client = get_client()
    request_id = get_request_id()
    changes: dict[str, Any] = {}
    if reason:
        changes["reason"] = reason
    if actor is not None and actor.role:
        changes["role"] = actor.role

    async def _write() -> None:
        # Контекст мог быть сброшен: возвращаем, чтобы запись получила актора и адрес.
        if get_actor() is None and actor is not None:
            set_actor(actor)
        if get_client() is None and client is not None:
            set_client(client)
        if get_request_id() is None and request_id is not None:
            set_request_id(request_id)
        await record_out_of_band(
            action,
            entity_type=entity_type,
            entity_id=entity_id,
            changes=changes or None,
            result=AuditResult.DENIED,
        )

    run_after_rollback(session, _write)


async def record_denied_and_commit(
    session: AsyncSession,
    action: AuditAction | str,
    *,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    changes: dict[str, Any] | None = None,
) -> None:
    """Пишет отказ в доступе и сразу фиксирует его.

    Отказ завершается исключением, а оно откатывает транзакцию запроса —
    вместе с записью аудита. Писать «рядом», второй транзакцией, нельзя:
    она встала бы в очередь за advisory-локом цепочки, который держит
    первая, и запрос завис бы до таймаута.

    Поэтому запись фиксируется в той же транзакции немедленным коммитом.
    Это безопасно: проверка прав выполняется до любых бизнес-изменений, так
    что фиксируется только аутентификация и сам отказ. Лок освобождается
    вместе с коммитом.
    """
    try:
        await AuditService(session).record(
            action,
            entity_type=entity_type,
            entity_id=entity_id,
            changes=changes,
            result=AuditResult.DENIED,
        )
        await session.commit()
    except Exception:  # noqa: BLE001
        # Невозможность записать отказ не должна подменять исходную ошибку прав.
        logger.exception("audit_denied_record_failed", action=str(action))
        await session.rollback()
