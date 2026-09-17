"""Кэш профиля и прав пользователя.

Раздел 16: `cache:perm:{user_id}` — развёрнутый набор прав, TTL 5 минут,
инвалидация при смене роли, команды, блокировке, выходе и обезличивании.

Кэш не является источником истины: любая операция, меняющая роль, статус
или эпоху прав, обязана вызвать `invalidate_principal`. Поэтому блокировка
пользователя срабатывает немедленно, а не через пять минут.

Вторая карта `cache:kcid:{sub}` хранит только соответствие субъекта токена
локальному идентификатору: оно стабильно и живёт дольше.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass

import structlog

from app.core.redis_client import TTL_PERMISSIONS, get_redis, key_permissions

logger = structlog.get_logger(__name__)

# Соответствие sub → user_id меняется только при обезличивании.
TTL_KEYCLOAK_MAP = 3600


def key_keycloak_map(keycloak_id: str) -> str:
    return f"cache:kcid:{keycloak_id}"


@dataclass(slots=True)
class CachedPrincipal:
    """Проекция пользователя, которой достаточно для проверки доступа."""

    user_id: str
    keycloak_id: str
    role: str
    status: str
    email: str | None
    full_name: str
    team_id: str | None
    manager_id: str | None
    perm_epoch: int
    consent_version: str | None
    must_change_password: bool


async def get_principal_cache(keycloak_id: str) -> CachedPrincipal | None:
    try:
        redis = get_redis()
        user_id = await redis.get(key_keycloak_map(keycloak_id))
        if not user_id:
            return None
        raw = await redis.get(key_permissions(user_id))
        if not raw:
            return None
        return CachedPrincipal(**json.loads(raw))
    except Exception:  # noqa: BLE001 — кэш не источник истины
        return None


async def set_principal_cache(entry: CachedPrincipal) -> None:
    try:
        redis = get_redis()
        pipe = redis.pipeline()
        pipe.setex(key_keycloak_map(entry.keycloak_id), TTL_KEYCLOAK_MAP, entry.user_id)
        pipe.setex(key_permissions(entry.user_id), TTL_PERMISSIONS, json.dumps(asdict(entry)))
        await pipe.execute()
    except Exception:  # noqa: BLE001
        logger.warning("principal_cache_write_failed")


async def invalidate_principal(
    user_id: uuid.UUID | str, *, keycloak_id: str | None = None
) -> None:
    """Вызывается при смене роли, команды, статуса, эпохи прав и выходе."""
    try:
        redis = get_redis()
        keys = [key_permissions(user_id)]
        if keycloak_id:
            keys.append(key_keycloak_map(keycloak_id))
        await redis.delete(*keys)
    except Exception:  # noqa: BLE001
        logger.warning("principal_cache_invalidate_failed", user_id=str(user_id))
