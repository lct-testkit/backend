"""Сервисный интерфейс CRM-домена для identity и workflow.

Мастер передачи дел (new_spec §4.7) и проверка блокеров обезличивания
(§4.8.3) обязаны знать, что именно держит пользователь: сделки, задачи,
импорты, отчёты. Конструктор воронок (раздел 6.5) обязан знать, сколько
живых сделок стоит в статусе, который администратор хочет архивировать.
Эти сущности живут в модуле crm и появятся в своём спринте, поэтому
обращение к ним идёт только через контракт.

Заглушка возвращает пустую нагрузку. Важно, что она возвращает именно
`supported=False`: мастер и предпросмотр архивирования тогда честно пишут
«модуль сделок ещё не подключён», а не делают вид, что переносить нечего.
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


@dataclass(slots=True)
class StatusWorkload:
    """Что стоит в статусе воронки на момент архивирования (раздел 6.5).

    `problem_deals` — сделки, которым не хватает обязательных полей целевого
    статуса: мастер сопоставления обязан показать их отдельно, а не только
    общее число, иначе администратор не поймёт, что чинить руками.
    """

    supported: bool = False
    active_count: int = 0
    problem_deals: list[dict[str, Any]] = field(default_factory=list)
    sla_affected: int = 0


@dataclass(slots=True)
class MappingBatchResult:
    """Итог одной партии переноса сделок в `status_mapping_jobs`."""

    processed: int = 0
    failed: int = 0
    has_more: bool = False


@runtime_checkable
class DealStatusService(Protocol):
    """Контракт между конструктором воронок и сделками (появятся в спринте 3)."""

    async def status_workload(
        self, session: AsyncSession, status_id: uuid.UUID
    ) -> StatusWorkload: ...

    async def migrate_batch(
        self,
        session: AsyncSession,
        *,
        from_status_id: uuid.UUID,
        target_status_id: uuid.UUID,
        fallback_status_id: uuid.UUID | None,
        sla_mode: str,
        batch_size: int,
    ) -> MappingBatchResult:
        """Переносит одну партию сделок из статуса.

        Возвращает `has_more=True`, пока в статусе остаются необработанные
        сделки — вызывающий код (фоновая задача) повторяет вызов батчами.
        """


class NullDealStatusService:
    async def status_workload(
        self, session: AsyncSession, status_id: uuid.UUID
    ) -> StatusWorkload:
        return StatusWorkload(supported=False)

    async def migrate_batch(
        self,
        session: AsyncSession,
        *,
        from_status_id: uuid.UUID,
        target_status_id: uuid.UUID,
        fallback_status_id: uuid.UUID | None,
        sla_mode: str,
        batch_size: int,
    ) -> MappingBatchResult:
        return MappingBatchResult(has_more=False)


_deal_status_service: DealStatusService = NullDealStatusService()


def register_deal_status_service(service: DealStatusService) -> None:
    global _deal_status_service
    _deal_status_service = service


def get_deal_status_service() -> DealStatusService:
    return _deal_status_service
