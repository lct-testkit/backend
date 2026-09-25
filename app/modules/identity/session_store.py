"""Серверные сессии в Redis (BFF-паттерн).

Раздел «Аутентификация»: браузер не получает токены в JavaScript-контексте.
В cookie уходит только идентификатор сессии, а сами токены лежат в Redis.
Индекс `user_sessions:{user_id}` нужен, чтобы уметь завершить все сессии
пользователя при смене пароля, блокировке или backchannel-logout.
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

import structlog

from app.core.config import get_settings
from app.core.errors import AppError, UnauthenticatedError
from app.core.masking import mask_token
from app.core.redis_client import (
    distributed_lock,
    key_session,
    key_user_sessions,
    require_redis,
)

logger = structlog.get_logger(__name__)

# Как часто реально писать `last_seen_at` в Redis.
_TOUCH_THROTTLE_SECONDS = 60


def new_session_id() -> str:
    # 32 байта энтропии: идентификатор сессии является секретом.
    return secrets.token_urlsafe(32)


@dataclass(slots=True)
class SessionData:
    sid: str
    user_id: str
    keycloak_id: str
    access_token: str
    refresh_token: str | None = None
    id_token: str | None = None
    kc_session_state: str | None = None
    created_at: str = ""
    last_seen_at: str = ""
    ip: str | None = None
    user_agent: str | None = None
    device: str | None = None
    access_expires_at: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def public_view(self) -> dict[str, Any]:
        """Для GET /api/me/sessions: без токенов."""
        return {
            "sid": self.sid,
            "device": self.device,
            "ip": self.ip,
            "user_agent": self.user_agent,
            "created_at": self.created_at,
            "last_seen_at": self.last_seen_at,
        }

    def log_view(self) -> dict[str, Any]:
        return {"sid": mask_token(self.sid), "user_id": self.user_id, "ip": self.ip}


class SessionStore:
    """Хранилище сессий. Недоступность Redis — это 503, а не тихий вход без сессии."""

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        keycloak_id: str,
        access_token: str,
        refresh_token: str | None,
        id_token: str | None,
        kc_session_state: str | None,
        ip: str | None,
        user_agent: str | None,
        access_expires_at: int | None = None,
    ) -> SessionData:
        settings = get_settings()
        redis = await require_redis()
        now = dt.datetime.now(dt.UTC).isoformat()

        session = SessionData(
            sid=new_session_id(),
            user_id=str(user_id),
            keycloak_id=keycloak_id,
            access_token=access_token,
            refresh_token=refresh_token,
            id_token=id_token,
            kc_session_state=kc_session_state,
            created_at=now,
            last_seen_at=now,
            ip=ip,
            user_agent=user_agent,
            device=_device_from_user_agent(user_agent),
            access_expires_at=access_expires_at,
        )

        pipe = redis.pipeline()
        pipe.setex(key_session(session.sid), settings.session_ttl, json.dumps(asdict(session)))
        pipe.sadd(key_user_sessions(user_id), session.sid)
        pipe.expire(key_user_sessions(user_id), settings.session_ttl)
        await pipe.execute()

        logger.info("session_created", **session.log_view())
        return session

    async def get(self, sid: str) -> SessionData | None:
        """Возвращает сессию, если она жива и не простаивала дольше лимита.

        Абсолютный TTL держит Redis, а idle-таймаут (new_spec §3.1) —
        проверка `last_seen_at`: без неё забытая открытой вкладка остаётся
        валидным доступом все 12 часов.
        """
        redis = await require_redis()
        raw = await redis.get(key_session(sid))
        if not raw:
            return None
        try:
            session = SessionData(**json.loads(raw))
        except (TypeError, ValueError):
            # Формат сессии изменился между версиями — считаем её невалидной.
            await redis.delete(key_session(sid))
            return None

        if self._is_idle_expired(session):
            logger.info("session_idle_expired", **session.log_view())
            # Именно `_purge`, а не `delete`: `delete` читает сессию через
            # `get`, и пара вызвала бы друг друга бесконечно.
            await self._purge(session)
            return None
        return session

    async def _purge(self, session: SessionData) -> None:
        """Удаляет уже прочитанную сессию вместе с записью в индексе."""
        redis = await require_redis()
        pipe = redis.pipeline()
        pipe.delete(key_session(session.sid))
        pipe.srem(key_user_sessions(session.user_id), session.sid)
        await pipe.execute()

    @staticmethod
    def _is_idle_expired(session: SessionData) -> bool:
        idle_limit = get_settings().session_idle_timeout
        if idle_limit <= 0 or not session.last_seen_at:
            return False
        try:
            last_seen = dt.datetime.fromisoformat(session.last_seen_at)
        except ValueError:
            return False
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=dt.UTC)
        return (dt.datetime.now(dt.UTC) - last_seen).total_seconds() > idle_limit

    async def touch(self, session: SessionData) -> None:
        """Обновляет `last_seen_at` и продлевает TTL.

        Запись в Redis на каждый запрос — лишний round-trip, поэтому
        обновляем не чаще, чем раз в `_TOUCH_THROTTLE_SECONDS`; на точность
        idle-таймаута это влияет в пределах той же минуты.
        """
        now = dt.datetime.now(dt.UTC)
        if session.last_seen_at:
            try:
                last_seen = dt.datetime.fromisoformat(session.last_seen_at)
                if last_seen.tzinfo is None:
                    last_seen = last_seen.replace(tzinfo=dt.UTC)
                if (now - last_seen).total_seconds() < _TOUCH_THROTTLE_SECONDS:
                    return
            except ValueError:
                pass
        session.last_seen_at = now.isoformat()
        await self.update(session)

    async def rotate_sid(self, session: SessionData) -> SessionData:
        """Переиздаёт сессию с новым `sid` — защита от session fixation.

        Вызывается после смены пароля (new_spec §4.4, последствие 2).
        """
        settings = get_settings()
        redis = await require_redis()
        old_sid = session.sid
        session.sid = new_session_id()
        session.last_seen_at = dt.datetime.now(dt.UTC).isoformat()

        pipe = redis.pipeline()
        pipe.setex(key_session(session.sid), settings.session_ttl, json.dumps(asdict(session)))
        pipe.sadd(key_user_sessions(session.user_id), session.sid)
        pipe.expire(key_user_sessions(session.user_id), settings.session_ttl)
        pipe.delete(key_session(old_sid))
        pipe.srem(key_user_sessions(session.user_id), old_sid)
        await pipe.execute()

        logger.info("session_rotated", **session.log_view())
        return session

    async def update(self, session: SessionData) -> None:
        settings = get_settings()
        redis = await require_redis()
        await redis.setex(
            key_session(session.sid), settings.session_ttl, json.dumps(asdict(session))
        )

    async def delete(self, sid: str) -> SessionData | None:
        session = await self.get(sid)
        if session is None:
            # Сессии нет или она уже погашена по простою внутри `get`;
            # ключ всё равно удаляем — вдруг он битый и не разобрался.
            redis = await require_redis()
            await redis.delete(key_session(sid))
            return None
        await self._purge(session)
        logger.info("session_deleted", **session.log_view())
        return session

    async def list_for_user(self, user_id: uuid.UUID | str) -> list[SessionData]:
        redis = await require_redis()
        sids = await redis.smembers(key_user_sessions(user_id))
        if not sids:
            return []
        sessions: list[SessionData] = []
        stale: list[str] = []
        for sid in sids:
            session = await self.get(sid)
            if session:
                sessions.append(session)
            else:
                stale.append(sid)
        if stale:
            # Подчищаем индекс от сессий, истёкших по TTL.
            await redis.srem(key_user_sessions(user_id), *stale)
        sessions.sort(key=lambda s: s.created_at, reverse=True)
        return sessions

    async def delete_all_for_user(
        self, user_id: uuid.UUID | str, *, except_sid: str | None = None
    ) -> int:
        """Используется при смене пароля, блокировке и увольнении."""
        redis = await require_redis()
        sids = await redis.smembers(key_user_sessions(user_id))
        targets = [sid for sid in sids if sid != except_sid]
        if not targets:
            return 0
        pipe = redis.pipeline()
        for sid in targets:
            pipe.delete(key_session(sid))
        pipe.srem(key_user_sessions(user_id), *targets)
        await pipe.execute()
        logger.info("sessions_terminated", user_id=str(user_id), count=len(targets))
        return len(targets)

    async def delete_by_kc_session_state(self, kc_session_state: str, user_id: str) -> int:
        """Backchannel-logout приходит с `sid` из Keycloak, а не с нашим."""
        sessions = await self.list_for_user(user_id)
        removed = 0
        for session in sessions:
            if session.kc_session_state == kc_session_state:
                await self.delete(session.sid)
                removed += 1
        return removed


async def ensure_fresh_access_token(session: SessionData) -> str:
    """Возвращает живой access-токен сессии, обновляя его при необходимости.

    Время жизни access-токена — 5 минут, серверной сессии — до 12 часов
    (new_spec §3.1). Без обновления по refresh пользователь получал бы 401
    через пять минут после входа, хотя сессия жива.

    Обновление идёт под коротким локом: Keycloak ротирует refresh-токен, и
    две параллельные попытки обновления убили бы сессию срабатыванием
    reuse-detection.
    """
    from app.modules.identity.keycloak import keycloak_client

    if not _needs_refresh(session):
        return session.access_token
    if not session.refresh_token:
        # Сессия без refresh-токена (например, создана сервисным вызовом):
        # продлить нечего, пусть истекает честно.
        return session.access_token

    async with distributed_lock(f"session:{session.sid}:refresh", ttl=15) as acquired:
        if not acquired:
            return session.access_token

        # Победитель гонки уже мог обновить токен, пока мы ждали лок.
        current = await session_store.get(session.sid)
        if current is None:
            raise UnauthenticatedError("Сессия истекла или была завершена")
        if not _needs_refresh(current):
            session.access_token = current.access_token
            session.refresh_token = current.refresh_token
            session.access_expires_at = current.access_expires_at
            return current.access_token

        try:
            tokens = await keycloak_client.refresh(current.refresh_token or "")
        except AppError:
            # Refresh отозван или просрочен: сессия больше не действительна.
            logger.info("session_refresh_failed", **current.log_view())
            await session_store.delete(current.sid)
            raise UnauthenticatedError("Сессия истекла: требуется повторный вход") from None

        current.access_token = tokens.access_token
        current.refresh_token = tokens.refresh_token or current.refresh_token
        current.id_token = tokens.id_token or current.id_token
        current.access_expires_at = tokens.access_expires_at
        current.last_seen_at = dt.datetime.now(dt.UTC).isoformat()
        await session_store.update(current)

        session.access_token = current.access_token
        session.refresh_token = current.refresh_token
        session.access_expires_at = current.access_expires_at
        logger.info("session_token_refreshed", **current.log_view())
        return current.access_token


def _needs_refresh(session: SessionData) -> bool:
    if session.access_expires_at is None:
        return False
    leeway = get_settings().access_token_refresh_leeway
    return time.time() >= (session.access_expires_at - leeway)


def _device_from_user_agent(user_agent: str | None) -> str | None:
    """Грубое определение устройства для списка сессий — без внешних зависимостей."""
    if not user_agent:
        return None
    ua = user_agent.lower()
    if "android" in ua:
        return "Android"
    if "iphone" in ua or "ipad" in ua:
        return "iOS"
    if "windows" in ua:
        return "Windows"
    if "macintosh" in ua or "mac os" in ua:
        return "macOS"
    if "linux" in ua:
        return "Linux"
    if any(bot in ua for bot in ("curl", "python", "httpx", "postman")):
        return "API-клиент"
    return "Неизвестное устройство"


session_store = SessionStore()
