"""Сервисный интерфейс исходящих событий (outbox, раздел 3.6/7.8) и
администрирование источников интеграций (раздел 5: «Настройки интеграций»).

До этого спринта модуль состоял из протокола `OutboxService` и его
логирующей заглушки — `crm.service._run_actions` (действие DSL
`integration_event`) и `signing.service` уже вызывали
`get_outbox_service().publish(...)`, но событие уходило только в
структурный лог, а таблицы `outbox_events` не существовало. Этот спринт
заводит таблицу (`integration.models.OutboxEvent`) и подключает
`RealOutboxService` тем же приёмом, что `notification.service.
RealNotificationService`/`signing.service.RealSigningService` уже применили
к своим заглушкам: регистрация в `app/main.py`/`app/worker/main.py`, ноль
изменений в вызывающем коде (см. `register_outbox_service`).

`get_integration_principal`/`pick_least_loaded_owner` живут здесь, а не в
`cms.py`, — они не специфичны одному источнику: `lms.py`/`bitrix.py` для
входящих потоков используют ту же логику при необходимости атрибуции.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol, runtime_checkable

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import ActorContext, set_actor
from app.core.errors import AppError, ErrorCode, NotFoundError
from app.core.security import Principal, TokenClaims
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.crm.models import Deal
from app.modules.identity.models import Role, User
from app.modules.integration.models import IntegrationSource, OutboxEvent

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
        target: str | None = None,
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
        target: str | None = None,
    ) -> None:
        logger.info(
            "outbox_event_stub",
            aggregate_type=aggregate_type,
            aggregate_id=str(aggregate_id),
            event_type=event_type,
        )


class RealOutboxService:
    """Пишет `OutboxEvent` в той же транзакции, что и вызывающий код
    (раздел 3.6: «в одной транзакции с бизнес-изменением»). Доставку
    (раздел 4.14/7.8) делает отдельно `integration.tasks.
    sweep_outbox_events` — паблишер никогда не делает исходящих HTTP-вызовов
    сам, иначе переход по статусу сделки (бюджет 300 мс, раздел 4.9) ждал бы
    ответа Bitrix/LMS."""

    async def publish(
        self,
        session: AsyncSession,
        *,
        aggregate_type: str,
        aggregate_id: uuid.UUID,
        event_type: str,
        payload: dict[str, Any] | None = None,
        target: str | None = None,
    ) -> None:
        session.add(
            OutboxEvent(
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                event_type=event_type,
                payload=payload or {},
                target=target,
            )
        )
        await session.flush()


_service: OutboxService = LoggingOutboxService()


def register_outbox_service(service: OutboxService) -> None:
    """Вызывается модулем integration при инициализации приложения."""
    global _service
    _service = service


def get_outbox_service() -> OutboxService:
    return _service


# --- Источники интеграций ---------------------------------------------------


class IntegrationSourceService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    async def list_all(self) -> list[IntegrationSource]:
        rows = (
            (
                await self._session.execute(
                    select(IntegrationSource).order_by(IntegrationSource.code)
                )
            )
            .scalars()
            .all()
        )
        return list(rows)

    async def get_by_code(self, code: str) -> IntegrationSource:
        source = (
            await self._session.execute(
                select(IntegrationSource).where(IntegrationSource.code == code)
            )
        ).scalar_one_or_none()
        if source is None:
            raise NotFoundError("Источник интеграции", code)
        return source

    async def update(self, code: str, payload: Any) -> IntegrationSource:
        source = await self.get_by_code(code)
        updates = payload.model_dump(exclude_unset=True)
        changes: dict[str, Any] = {}
        for field, new_value in updates.items():
            old_value = getattr(source, field)
            if old_value != new_value:
                changes[field] = {"old": old_value, "new": new_value}
                setattr(source, field, new_value)
        if changes:
            await self._session.flush()
            await self._audit.record(
                AuditAction.INTEGRATION_SOURCE_UPDATED,
                entity_type="integration_source",
                entity_id=source.id,
                changes=changes,
            )
        return source


# --- Синтетический Principal для потоков, инициированных внешними
# системами (вебхуки CMS/LMS/Bitrix, раздел 3.2: роль INTEGRATION) ----------

_integration_user_id: uuid.UUID | None = None


async def get_integration_principal(session: AsyncSession) -> Principal:
    """Находит служебную учётку `role=INTEGRATION` (заводится
    `integration.seed`) и оборачивает её в `Principal` — тот же тип, что
    обычный JWT-путь строит в `core.security`, только без реального
    Keycloak-токена: входящий вебхук аутентифицирован HMAC-подписью (раздел
    4.14), а не сессией браузера. `DealService.create()` уже ветвится на
    `principal.role == Role.INTEGRATION.value` (см. `crm.service`: `source`/
    `external_ids` обязательны для этой роли) — этот Principal туда и
    подаётся.

    ID кэшируется на процесс (тот же приём, что `jwks_cache` — учётка не
    меняется во время работы приложения, а вебхук может прилетать часто).
    """
    global _integration_user_id
    user: User | None = None
    if _integration_user_id is not None:
        user = await session.get(User, _integration_user_id)
        if user is None:  # учётку удалили между вызовами — перечитать
            _integration_user_id = None

    if user is None:
        user = (
            (await session.execute(select(User).where(User.role == Role.INTEGRATION.value)))
            .scalars()
            .first()
        )
        if user is None:
            raise AppError(
                ErrorCode.DEPENDENCY_UNAVAILABLE,
                "Служебная учётка role=INTEGRATION не найдена — запустите "
                "`python -m app.modules.integration.seed`",
                status=503,
            )
        _integration_user_id = user.id

    set_actor(ActorContext(user_id=user.id, role=user.role))
    return Principal(
        user_id=user.id,
        keycloak_id=user.keycloak_id or "system:integration",
        role=user.role,
        status=user.status,
        email=user.email,
        full_name=user.full_name,
        team_id=user.team_id,
        manager_id=user.manager_id,
        perm_epoch=user.perm_epoch,
        session_id=None,
        consent_version=None,
        must_change_password=False,
        claims=TokenClaims(subject=user.keycloak_id or "system:integration", raw={}),
    )


async def pick_least_loaded_owner(session: AsyncSession) -> uuid.UUID | None:
    """Назначение ответственного за сделку, созданную вебхуком (раздел 4.9:
    «round-robin внутри команды» — упрощённая, но честная реализация:
    активный КАМ с наименьшим числом открытых сделок, а не циклический
    счётчик, для которого негде хранить состояние без отдельной таблицы).
    `None`, если активных КАМов нет — тогда `DealService.create()` сама
    падает на `principal.user_id` (см. её тело), сделка не остаётся вовсе
    без владельца."""
    open_deals = (
        select(Deal.owner_id, func.count(Deal.id).label("open_count"))
        .where(Deal.closed_at.is_(None), Deal.deleted_at.is_(None))
        .group_by(Deal.owner_id)
        .subquery()
    )
    stmt = (
        select(User.id)
        .outerjoin(open_deals, open_deals.c.owner_id == User.id)
        .where(User.role == Role.KAM.value, User.status == "active", User.deleted_at.is_(None))
        .order_by(func.coalesce(open_deals.c.open_count, 0).asc(), User.created_at.asc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()
