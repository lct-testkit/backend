"""Сервисный интерфейс исходящих событий (outbox, раздел 5.8/3.6).

Полный модуль интеграций (outbox_events, доставка с backoff, мок-сервисы CMS
и LMS) делается в своём спринте (раздел 20, «Спринт 7 — интеграции»). Но
переход по статусу сделки обязан публиковать событие уже сейчас: new_spec
§4.9 п.6 описывает `INSERT outbox_events` как часть той же транзакции, что
обновляет сделку, а действие `integration_event` в DSL переходов (раздел 8)
ссылается ровно на этот механизм.

Кросс-модульные вызовы идут только через этот интерфейс: crm не знает ни
таблицы `outbox_events`, ни воркера доставки. До появления реализации
зарегистрирована заглушка, которая пишет событие в структурный лог — честнее,
чем тихо ничего не делать, и оставляет ровно одну точку, которую сprint 7
заменит на настоящую транзакционную запись.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol, runtime_checkable

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)


@runtime_checkable
class OutboxService(Protocol):
    async def publish(
        self,
        session: AsyncSession,
        *,
        aggregate_type: str,
        aggregate_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> None: ...


class LoggingOutboxService:
    """Заглушка на период, пока модуль интеграций не реализован."""

    async def publish(
        self,
        session: AsyncSession,
        *,
        aggregate_type: str,
        aggregate_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        logger.info(
            "outbox_event_stub",
            aggregate_type=aggregate_type,
            aggregate_id=str(aggregate_id),
            event_type=event_type,
        )


_service: OutboxService = LoggingOutboxService()


def register_outbox_service(service: OutboxService) -> None:
    """Вызывается модулем integration при инициализации приложения."""
    global _service
    _service = service


def get_outbox_service() -> OutboxService:
    return _service
