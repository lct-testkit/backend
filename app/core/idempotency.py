"""Идемпотентность мутирующих запросов.

Раздел 2: клиент передаёт `Idempotency-Key`, сервер хранит ключ, хэш запроса
и ответ. Повтор с тем же ключом и телом возвращает сохранённый ответ; тот же
ключ с другим телом — это конфликт CRM-1003.

Быстрый путь — Redis (`idem:{key}`, TTL 24 часа), надёжный — таблица
`idempotency_keys`, которая переживает перезапуск Redis.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode
from app.core.redis_client import TTL_IDEMPOTENCY, get_redis, key_idempotency
from app.modules.admin.models import IdempotencyKey

logger = structlog.get_logger(__name__)


def request_hash(method: str, path: str, body: bytes) -> str:
    digest = hashlib.sha256()
    digest.update(method.upper().encode())
    digest.update(b"\x00")
    digest.update(path.encode())
    digest.update(b"\x00")
    digest.update(body)
    return digest.hexdigest()


def scoped_key(key: str, actor_id: uuid.UUID | None) -> str:
    """Ключ идемпотентности всегда живёт внутри своего актора.

    Без этого угаданный или подсмотренный `Idempotency-Key` возвращал бы
    чужое сохранённое тело ответа — прямая утечка данных между
    пользователями. Для анонимных вызовов (входящие вебхуки) скоупом
    выступает источник интеграции, который передаётся как `actor_id`.
    """
    prefix = str(actor_id) if actor_id else "anonymous"
    return f"{prefix}:{key}"


@dataclass(slots=True)
class StoredResponse:
    status: int
    body: dict[str, Any] | None


class IdempotencyGuard:
    """Проверяет ключ до выполнения операции и сохраняет ответ после.

    Ключ хранится вместе с идентификатором актора: повтор чужим
    пользователем с тем же значением заголовка не должен ни возвращать
    сохранённый ответ, ни блокировать операцию.
    """

    def __init__(self, session: AsyncSession, *, actor_id: uuid.UUID | None = None) -> None:
        self._session = session
        self._actor_id = actor_id

    async def lookup(
        self, *, key: str, method: str, path: str, body: bytes
    ) -> StoredResponse | None:
        key = scoped_key(key, self._actor_id)
        digest = request_hash(method, path, body)

        cached = await self._lookup_redis(key)
        if cached is not None:
            self._ensure_same_request(key, cached.get("request_hash"), digest)
            if cached.get("response_status") is None:
                # Запрос в обработке: повтор пришёл раньше, чем завершился первый.
                raise AppError(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "Запрос с этим Idempotency-Key ещё обрабатывается",
                    status=409,
                )
            return StoredResponse(
                status=int(cached["response_status"]), body=cached.get("response_body")
            )

        record = (
            await self._session.execute(select(IdempotencyKey).where(IdempotencyKey.key == key))
        ).scalar_one_or_none()
        if record is None:
            return None

        self._ensure_same_request(key, record.request_hash, digest)
        if record.response_status is None:
            raise AppError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "Запрос с этим Idempotency-Key ещё обрабатывается",
                status=409,
            )
        return StoredResponse(status=record.response_status, body=record.response_body)

    @staticmethod
    def _ensure_same_request(key: str, stored: str | None, current: str) -> None:
        if stored and stored != current:
            raise AppError(
                ErrorCode.IDEMPOTENCY_CONFLICT,
                "Idempotency-Key уже использовался с другим телом запроса",
                extra={"idempotency_key": key},
            )

    async def _lookup_redis(self, key: str) -> dict[str, Any] | None:
        try:
            raw = await get_redis().get(key_idempotency(key))
        except Exception:
            # Redis недоступен — идём в БД, это медленнее, но корректно.
            return None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    async def reserve(self, *, key: str, method: str, path: str, body: bytes) -> None:
        """Занимает ключ до выполнения операции, чтобы параллельный повтор не прошёл."""
        settings = get_settings()
        actor_id = self._actor_id
        key = scoped_key(key, actor_id)
        digest = request_hash(method, path, body)
        expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(
            seconds=settings.idempotency_ttl_seconds
        )

        stmt = (
            pg_insert(IdempotencyKey)
            .values(
                key=key,
                request_hash=digest,
                request_method=method.upper(),
                request_path=path,
                actor_id=actor_id,
                expires_at=expires_at,
            )
            .on_conflict_do_nothing(index_elements=[IdempotencyKey.key])
        )
        await self._session.execute(stmt)

        try:
            await get_redis().setex(
                key_idempotency(key),
                TTL_IDEMPOTENCY,
                json.dumps({"request_hash": digest, "response_status": None}),
            )
        except Exception:
            logger.warning("idempotency_redis_reserve_failed")

    async def store(self, *, key: str, status: int, body: dict[str, Any] | None) -> None:
        key = scoped_key(key, self._actor_id)
        record = (
            await self._session.execute(select(IdempotencyKey).where(IdempotencyKey.key == key))
        ).scalar_one_or_none()
        if record is not None:
            record.response_status = status
            record.response_body = body
            await self._session.flush()

        try:
            await get_redis().setex(
                key_idempotency(key),
                TTL_IDEMPOTENCY,
                json.dumps(
                    {
                        "request_hash": record.request_hash if record else None,
                        "response_status": status,
                        "response_body": body,
                    },
                    default=str,
                ),
            )
        except Exception:
            logger.warning("idempotency_redis_store_failed")
