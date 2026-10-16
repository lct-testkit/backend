"""B6-3: события `LOGIN_FAILED`/`BRUTE_FORCE_LOCK` из Keycloak (группа АУД, Функц.7).

Вход идёт на странице Keycloak (OIDC code flow) — BFF видит только успешный `callback`,
неверный пароль до CRM не доходит. Единственный способ узнать о попытках подбора без
собственного SPI-плагина в Keycloak (вариант A в бэклоге, не делаем: кастомный образ, 24–30 ч)
— периодически опрашивать `GET /admin/realms/{realm}/events` (вариант B).

Курсор — не параметр `dateFrom` (Keycloak 25.0 фильтрует по дням, слишком грубо), а Redis-ключ
по полю `time` события (мс) с накладкой `KEYCLOAK_EVENTS_OVERLAP_SECONDS`; дедупликация одного
и того же события при повторном пересечении окон — по sha256 от его полей, `SET NX EX 3600`.

Блокировка определяется НЕ по типу события (в Keycloak 25.0 не проверено, есть ли
`USER_DISABLED_BY_TEMPORARY_LOCKOUT`), а повторным запросом `brute_force_status` — так же,
как это сделал бы администратор, открыв профиль пользователя.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from dataclasses import dataclass, field

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.redis_client import get_redis
from app.modules.audit.actions import AuditAction
from app.modules.audit.models import AuditResult
from app.modules.audit.service import AuditService
from app.modules.identity.keycloak import KeycloakClient
from app.modules.identity.models import Role, SecurityEventType, Severity, User, UserStatus
from app.modules.identity.service import IdentityService
from app.modules.notification.models import NotificationPriority
from app.modules.notification.service import get_notification_service

logger = structlog.get_logger(__name__)

#: Коды ошибок Keycloak LOGIN_ERROR, которые считаются «неверный пароль/логин», а не сбоем
#: инфраструктуры (`invalid_client`, `expired_code` и т.п. — не это).
_LOGIN_FAILURE_ERRORS = frozenset(
    {"invalid_user_credentials", "user_not_found", "invalid_credentials"}
)

_CURSOR_KEY = "kc:events:cursor"
_LOOKBACK_MS_ON_FIRST_RUN = 10 * 60 * 1000
_DEDUP_TTL_SECONDS = 3600
_BRUTE_FORCE_LOCK_TTL_SECONDS = 900


def _dedup_key(event: dict) -> str:
    raw = "|".join(
        str(event.get(field_name, ""))
        for field_name in ("time", "type", "userId", "ipAddress", "error")
    )
    return f"kc:events:seen:{hashlib.sha256(raw.encode()).hexdigest()}"


def _username_hash(username: str) -> str:
    return hashlib.sha256(username.strip().casefold().encode()).hexdigest()[:16]


@dataclass(slots=True)
class _Batch:
    """Попытки входа одного субъекта с одного IP за один проход — раздел B6-3: «события
    сворачиваются по паре (субъект, IP) в одну запись на пачку», чтобы тысяча попыток не
    раздувала таблицу `security_events`."""

    ip: str | None
    user_id: uuid.UUID | None = None
    username_hash: str | None = None
    count: int = 0
    first_at: dt.datetime | None = None
    last_at: dt.datetime | None = None

    def add(self, when: dt.datetime) -> None:
        self.count += 1
        self.first_at = when if self.first_at is None else min(self.first_at, when)
        self.last_at = when if self.last_at is None else max(self.last_at, when)


@dataclass(slots=True)
class KeycloakEventSyncResult:
    fetched: int = 0
    batches: int = 0
    locked: int = 0
    errored: bool = False
    skipped: bool = False
    extra: dict = field(default_factory=dict)


class KeycloakEventSync:
    def __init__(self, session: AsyncSession, keycloak: KeycloakClient) -> None:
        self._session = session
        self._keycloak = keycloak
        self._identity = IdentityService(session)
        self._audit = AuditService(session)

    async def run(self) -> KeycloakEventSyncResult:
        settings = get_settings()
        if not settings.keycloak_events_poll_enabled:
            return KeycloakEventSyncResult(skipped=True)

        redis = get_redis()
        now_ms = int(dt.datetime.now(dt.UTC).timestamp() * 1000)
        cursor_raw = await redis.get(_CURSOR_KEY)
        since_ms = (
            now_ms - _LOOKBACK_MS_ON_FIRST_RUN
            if cursor_raw is None
            else int(cursor_raw) - settings.keycloak_events_overlap_seconds * 1000
        )

        try:
            events = await self._keycloak.list_events(
                types=["LOGIN_ERROR"], date_from_ms=since_ms, max_results=1000
            )
        except Exception as exc:  # noqa: BLE001 — сбой Keycloak не должен ронять тик воркера
            logger.warning("keycloak_events_fetch_failed", error=str(exc))
            return KeycloakEventSyncResult(errored=True)

        batches: dict[tuple, _Batch] = {}
        max_seen_ms = since_ms
        for event in events:
            event_time = int(event.get("time") or 0)
            max_seen_ms = max(max_seen_ms, event_time)
            if event.get("error") not in _LOGIN_FAILURE_ERRORS:
                continue

            dedup_key = _dedup_key(event)
            if not await redis.set(dedup_key, "1", nx=True, ex=_DEDUP_TTL_SECONDS):
                continue  # уже учтено прошлым проходом (накладка окон)

            keycloak_user_id = event.get("userId")
            user_id = None
            if keycloak_user_id:
                user_id = await self._session.scalar(
                    select(User.id).where(User.keycloak_id == keycloak_user_id)
                )
            username = ((event.get("details") or {}).get("username") or "").strip()
            ip = event.get("ipAddress")

            batch_key = (str(user_id) if user_id else None, username.casefold(), ip)
            batch = batches.setdefault(batch_key, _Batch(ip=ip))
            batch.user_id = user_id
            if username and user_id is None:
                batch.username_hash = _username_hash(username)
            when = dt.datetime.fromtimestamp(event_time / 1000, dt.UTC) if event_time else None
            batch.add(when or dt.datetime.now(dt.UTC))

        locked = 0
        for batch in batches.values():
            # Личное имя, пришедшее от Keycloak, в базу не пишется ни в каком виде (раздел
            # B6-3) — только счётчик, хэш логина для неизвестных и диапазон времени пачки.
            await self._identity.record_security_event(
                SecurityEventType.LOGIN_FAILED,
                user_id=batch.user_id,
                severity=Severity.WARNING,
                details={
                    "context": "keycloak_login",
                    "count": batch.count,
                    "first_at": batch.first_at.isoformat() if batch.first_at else None,
                    "last_at": batch.last_at.isoformat() if batch.last_at else None,
                    "username_hash": batch.username_hash,
                },
            )
            if await self._maybe_lock(batch):
                locked += 1

        if events:
            # Продвигаем курсор до максимума ВИДЕННОГО времени, не до "сейчас": событие,
            # случившееся между запросом и ответом Keycloak, иначе было бы потеряно.
            await redis.set(_CURSOR_KEY, str(max_seen_ms + 1))

        return KeycloakEventSyncResult(fetched=len(events), batches=len(batches), locked=locked)

    async def _maybe_lock(self, batch: _Batch) -> bool:
        """`True`, если в этом проходе зафиксирована НОВАЯ блокировка (для метрики вызывающей
        стороны); повторный проход в пределах `_BRUTE_FORCE_LOCK_TTL_SECONDS` — не повторяет."""
        if batch.user_id is None:
            return False
        user = await self._session.get(User, batch.user_id)
        if user is None or not user.keycloak_id:
            return False

        try:
            status = await self._keycloak.brute_force_status(user.keycloak_id)
        except Exception as exc:  # noqa: BLE001 — не должен ронять весь проход
            logger.warning("keycloak_brute_force_status_failed", error=str(exc))
            return False
        if not status or not status.get("disabled"):
            return False

        redis = get_redis()
        lock_key = f"bf:lock:{batch.user_id}"
        if not await redis.set(lock_key, "1", nx=True, ex=_BRUTE_FORCE_LOCK_TTL_SECONDS):
            return False  # уже зафиксировано недавно

        await self._identity.record_security_event(
            SecurityEventType.BRUTE_FORCE_LOCK,
            user_id=batch.user_id,
            severity=Severity.CRITICAL,
            details={"num_failures": status.get("numFailures"), "last_ip": batch.ip},
        )
        # Обычные LOGIN_FAILED в аудит не пишутся (при атаке они упёрлись бы в глобальный лок
        # цепочки аудита, раздел B6-3) — только сама блокировка, раз на окно.
        await self._audit.record(
            AuditAction.LOGIN_FAILED,
            entity_type="user",
            entity_id=batch.user_id,
            result=AuditResult.DENIED,
            changes={"reason": {"old": None, "new": "brute_force_lock"}},
        )
        await self._notify_admins(user)
        return True

    async def _notify_admins(self, locked_user: User) -> None:
        admins = (
            await self._session.execute(
                select(User).where(
                    User.role == Role.ADMIN.value, User.status == UserStatus.ACTIVE.value
                )
            )
        ).scalars()
        notifier = get_notification_service()
        for admin in admins:
            await notifier.notify_user(
                self._session,
                recipient_id=admin.id,
                template_code="SECURITY_BRUTE_FORCE_LOCK",
                payload={"locked_user_id": str(locked_user.id)},
                priority=NotificationPriority.CRITICAL,
                entity_type="user",
                entity_id=locked_user.id,
            )
