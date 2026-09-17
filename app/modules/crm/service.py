"""Сервисный интерфейс CRM-домена для администрирования пользователей.

Мастер передачи дел (new_spec §4.7) и проверка блокеров обезличивания
(§4.8.3) обязаны знать, что именно держит пользователь: сделки, задачи,
импорты, отчёты. Эти сущности живут в модулях crm/reporting и появятся в
своих спринтах, поэтому identity обращается к ним только через контракт.

Заглушка возвращает пустую нагрузку. Важно, что она возвращает именно
`supported=False`: мастер тогда честно пишет «модуль сделок ещё не
подключён», а не делает вид, что передавать нечего.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(slots=True)
class UserWorkload:
    """Что держит пользователь на момент увольнения или удаления."""

    supported: bool = False
    active_deals: int = 0
    critical_deals: list[dict[str, Any]] = field(default_factory=list)
    open_tasks: int = 0
    running_imports: int = 0
    running_reports: int = 0

    @property
    def is_empty(self) -> bool:
        return not (
            self.active_deals or self.open_tasks or self.running_imports or self.running_reports
        )


@runtime_checkable
class OwnershipService(Protocol):
    async def collect_workload(
        self, session: AsyncSession, user_id: uuid.UUID
    ) -> UserWorkload: ...

    async def reassign_all(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        *,
        successor_id: uuid.UUID,
        reason: str,
    ) -> list[uuid.UUID]:
        """Меняет владельца всех сделок, возвращая их идентификаторы."""

    async def mark_owner_unavailable(
        self, session: AsyncSession, user_id: uuid.UUID, *, unavailable: bool
    ) -> int:
        """Ставит или снимает `owner_unavailable` при блокировке (§4.5)."""


class NullOwnershipService:
    async def collect_workload(self, session: AsyncSession, user_id: uuid.UUID) -> UserWorkload:
        return UserWorkload(supported=False)

    async def reassign_all(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        *,
        successor_id: uuid.UUID,
        reason: str,
    ) -> list[uuid.UUID]:
        return []

    async def mark_owner_unavailable(
        self, session: AsyncSession, user_id: uuid.UUID, *, unavailable: bool
    ) -> int:
        return 0


_service: OwnershipService = NullOwnershipService()


def register_ownership_service(service: OwnershipService) -> None:
    global _service
    _service = service


def get_ownership_service() -> OwnershipService:
    return _service
