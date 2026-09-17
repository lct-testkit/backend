"""Сервисный интерфейс модуля подписания.

Модуль ПЭП реализуется в своём спринте, но identity обязан дёргать его уже
сейчас: смена пароля аннулирует незавершённые запросы подписи, сброс пароля
делает это безусловно и помечает свежие подписи как оспоренные, а
обезличивание блокируется наличием подписей (dop §10.7).

Кросс-модульные вызовы идут только через этот интерфейс: identity не знает
ни таблиц, ни репозиториев signing. До появления реализации зарегистрирована
заглушка, которая честно возвращает нули и пишет это в лог.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Protocol, runtime_checkable

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

# Причины аннулирования из dop §10.7.
VOID_REASON_CREDENTIALS_CHANGED = "credentials_changed"
VOID_REASON_KEY_COMPROMISED = "key_compromised"
VOID_REASON_OFFBOARDED = "signer_offboarded"


@runtime_checkable
class SigningService(Protocol):
    """Контракт, который реализует модуль signing."""

    async def void_pending_for_user(
        self, session: AsyncSession, user_id: uuid.UUID, *, reason: str
    ) -> int:
        """Аннулирует незавершённые запросы подписи пользователя."""

    async def mark_disputed_since(
        self, session: AsyncSession, user_id: uuid.UUID, *, since: dt.datetime
    ) -> int:
        """Помечает подписи за период как оспоренные (компрометация ключа)."""

    async def count_signatures(self, session: AsyncSession, user_id: uuid.UUID) -> int:
        """Сколько подписей поставил пользователь: блокер обезличивания."""

    async def count_pending_requests(self, session: AsyncSession, user_id: uuid.UUID) -> int:
        """Незакрытые задачи на подпись: показываются в мастере передачи дел."""

    async def reassign_pending(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        *,
        successor_id: uuid.UUID | None,
    ) -> int:
        """Переназначает незакрытые запросы при увольнении (dop §10.7)."""


class NullSigningService:
    """Заглушка на период, пока модуль ПЭП не реализован."""

    async def void_pending_for_user(
        self, session: AsyncSession, user_id: uuid.UUID, *, reason: str
    ) -> int:
        logger.debug("signing_stub_void", user_id=str(user_id), reason=reason)
        return 0

    async def mark_disputed_since(
        self, session: AsyncSession, user_id: uuid.UUID, *, since: dt.datetime
    ) -> int:
        return 0

    async def count_signatures(self, session: AsyncSession, user_id: uuid.UUID) -> int:
        return 0

    async def count_pending_requests(self, session: AsyncSession, user_id: uuid.UUID) -> int:
        return 0

    async def reassign_pending(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        *,
        successor_id: uuid.UUID | None,
    ) -> int:
        return 0


_service: SigningService = NullSigningService()


def register_signing_service(service: SigningService) -> None:
    """Вызывается модулем signing при инициализации приложения."""
    global _service
    _service = service


def get_signing_service() -> SigningService:
    return _service
