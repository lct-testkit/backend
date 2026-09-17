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
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

import structlog

from app.core.config import get_settings
from app.core.masking import mask_token
from app.core.redis_client import key_session, key_user_sessions, require_redis

logger = structlog.get_logger(__name__)


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
        redis = await require_redis()
        raw = await redis.get(key_session(sid))
        if not raw:
            return None
        try:
            return SessionData(**json.loads(raw))
        except (TypeError, ValueError):
            # Формат сессии изменился между версиями — считаем её невалидной.
            await redis.delete(key_session(sid))
            return None

    async def touch(self, session: SessionData) -> None:
        """Обновляет last_seen_at и продлевает TTL простоя."""
        settings = get_settings()
        redis = await require_redis()
        session.last_seen_at = dt.datetime.now(dt.UTC).isoformat()
        await redis.setex(
            key_session(session.sid), settings.session_ttl, json.dumps(asdict(session))
        )

    async def update(self, session: SessionData) -> None:
        settings = get_settings()
        redis = await require_redis()
        await redis.setex(
            key_session(session.sid), settings.session_ttl, json.dumps(asdict(session))
        )

    async def delete(self, sid: str) -> SessionData | None:
        redis = await require_redis()
        session = await self.get(sid)
        pipe = redis.pipeline()
        pipe.delete(key_session(sid))
        if session:
            pipe.srem(key_user_sessions(session.user_id), sid)
        await pipe.execute()
        if session:
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
