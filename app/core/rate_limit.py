"""Ограничение частоты запросов на Redis.

Раздел 16 спецификации задаёт ключ `rl:{ip}:{route}`. Здесь живёт общий
счётчик с фиксированным окном: его хватает и для публичных страниц
подписания (10/мин), и для автоподстановки по ИНН (30/мин на пользователя),
и для защиты формы смены пароля (new_spec §4.4: 5 попыток → блок на 15 мин).

Если Redis недоступен, лимит по умолчанию не применяется: для обычных счётчиков это
осознанный компромисс в пользу доступности (раздел 16 требует жёсткого отказа только для
локов, сессий и идемпотентности). Но у ограничений, которые и есть защита от перебора
(одноразовый код подписи, ссылка подписания, ссылка-приглашение, смена пароля), «нет счётчика»
означало бы «нет защиты»: атакующему достаточно дождаться (или вызвать) сбоя Redis. Для них
вызывающий код передаёт `fail_closed=True` — при недоступном Redis запрос получает 503, а не
пропускается без лимита.
"""

from __future__ import annotations

import structlog

from app.core.errors import AppError, ErrorCode
from app.core.redis_client import get_redis, key_rate_limit

logger = structlog.get_logger(__name__)


class RateLimitResult:
    __slots__ = ("allowed", "counter", "retry_after")

    def __init__(self, *, allowed: bool, counter: int, retry_after: int) -> None:
        self.allowed = allowed
        self.counter = counter
        self.retry_after = retry_after


async def hit(
    subject: str, route: str, *, limit: int, window_seconds: int, fail_closed: bool = False
) -> RateLimitResult:
    """Инкрементирует счётчик окна и сообщает, не превышен ли лимит.

    `fail_closed` — при недоступном Redis бросить 503 вместо «лимит не применён»."""
    key = key_rate_limit(subject, route)
    try:
        redis = get_redis()
        pipe = redis.pipeline()
        pipe.incr(key)
        pipe.ttl(key)
        counter, ttl = await pipe.execute()
        counter = int(counter)
        if counter == 1 or int(ttl) < 0:
            await redis.expire(key, window_seconds)
            ttl = window_seconds
        return RateLimitResult(
            allowed=counter <= limit, counter=counter, retry_after=max(int(ttl), 1)
        )
    except Exception as exc:  # noqa: BLE001 — счётчик не должен ронять запрос
        logger.warning("rate_limit_unavailable", route=route, fail_closed=fail_closed)
        if fail_closed:
            raise AppError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "Проверка частоты запросов временно недоступна, повторите попытку позже",
                headers={"Retry-After": "5"},
            ) from exc
        return RateLimitResult(allowed=True, counter=0, retry_after=0)


async def enforce(
    subject: str,
    route: str,
    *,
    limit: int,
    window_seconds: int,
    detail: str = "Слишком много запросов, повторите позже",
    fail_closed: bool = False,
) -> None:
    """Бросает 429 при превышении лимита, проставляя `Retry-After`; при `fail_closed` и
    недоступном Redis — 503 (см. докстринг модуля)."""
    result = await hit(
        subject, route, limit=limit, window_seconds=window_seconds, fail_closed=fail_closed
    )
    if result.allowed:
        return
    raise AppError(
        ErrorCode.RATE_LIMITED,
        detail,
        headers={"Retry-After": str(result.retry_after)},
        extra={"limit": limit, "window_seconds": window_seconds},
    )


async def reset(subject: str, route: str) -> None:
    """Сбрасывает счётчик — например, после успешного ввода пароля."""
    try:
        await get_redis().delete(key_rate_limit(subject, route))
    except Exception:  # noqa: BLE001
        logger.warning("rate_limit_reset_failed", route=route)
