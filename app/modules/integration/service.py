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

import datetime as dt
import uuid
from typing import Any, Protocol, runtime_checkable

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.context import ActorContext, set_actor
from app.core.errors import AppError, ErrorCode, FieldError, NotFoundError, ValidationError
from app.core.security import Principal, TokenClaims
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService, diff_changes
from app.modules.crm.models import Deal
from app.modules.identity.models import Role, User
from app.modules.integration.field_mapping import FieldMapping, validate_mapping
from app.modules.integration.models import (
    InboundMessage,
    IntegrationSource,
    OutboxEvent,
    OutboxStatus,
    SyncCursor,
)
from app.modules.integration.routing import Routing, validate_routing
from app.modules.integration.security import check_outbound_url_resolved

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
        if updates.get("base_url") is not None:
            # Адрес, к которому воркер пойдёт сам (и, у LMS, с токеном): SSRF и утечка токена.
            try:
                updates["base_url"] = await check_outbound_url_resolved(
                    updates["base_url"], strict=get_settings().is_prod
                )
            except ValueError as exc:
                raise ValidationError(
                    "Адрес внешней системы не принят",
                    [FieldError(field="base_url", reason=str(exc))],
                ) from exc
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

    # --- B5-3: настраиваемый маппинг «утверждённых полей» ------------------------------

    async def get_mapping(self, code: str) -> FieldMapping | None:
        source = await self.get_by_code(code)
        if not source.field_mapping:
            return None
        return FieldMapping.model_validate(source.field_mapping)

    async def set_mapping(
        self, code: str, mapping: FieldMapping, *, approved_by: uuid.UUID
    ) -> FieldMapping:
        """Проверка (`validate_mapping`) бросает 422 с `errors[]` при любой проблеме — цель из
        реестра, не более 50 правил, `value_map`/`template` в границах, обязательно хоть одна
        цель `email`/`phone`, `custom_fields.<код>` — активное определение для сделки."""
        source = await self.get_by_code(code)
        errors = await validate_mapping(self._session, mapping, code)
        if errors:
            raise ValidationError(
                "Маппинг не принят",
                [FieldError(field=error.field, reason=error.reason) for error in errors],
            )
        stored = mapping.model_copy(
            update={
                "version": (source.field_mapping or {}).get("version", 0) + 1,
                "approved_by": approved_by,
                "approved_at": dt.datetime.now(dt.UTC),
            }
        )
        before = source.field_mapping
        source.field_mapping = stored.model_dump(mode="json")
        await self._session.flush()
        await self._audit.record(
            AuditAction.INTEGRATION_MAPPING_UPDATED,
            entity_type="integration_source",
            entity_id=source.id,
            changes=diff_changes(before or {}, source.field_mapping),
        )
        return stored

    async def reset_mapping(self, code: str) -> None:
        source = await self.get_by_code(code)
        if source.field_mapping is None:
            return
        before = source.field_mapping
        source.field_mapping = None
        await self._session.flush()
        await self._audit.record(
            AuditAction.INTEGRATION_MAPPING_RESET,
            entity_type="integration_source",
            entity_id=source.id,
            changes=diff_changes(before, {}),
        )

    # --- B5-4: маршрутизация (воронка/тип сделки на источник) --------------------------

    async def get_routing(self, code: str) -> Routing | None:
        source = await self.get_by_code(code)
        if not source.routing:
            return None
        return Routing.model_validate(source.routing)

    async def set_routing(self, code: str, routing: Routing) -> Routing:
        """Проверка (`validate_routing`) бросает 422 с `errors[]`: воронка не найдена/не
        опубликована/не того `deal_type`, статус не найден в графе, `fixed_owner_id`
        неактивен или не найден, больше `_MAX_RULES` правил."""
        source = await self.get_by_code(code)
        errors = await validate_routing(self._session, routing)
        if errors:
            raise ValidationError(
                "Маршрутизация не принята",
                [FieldError(field=error.field, reason=error.reason) for error in errors],
            )
        before = source.routing
        source.routing = routing.model_dump(mode="json")
        await self._session.flush()
        await self._audit.record(
            AuditAction.INTEGRATION_ROUTING_UPDATED,
            entity_type="integration_source",
            entity_id=source.id,
            changes=diff_changes(before or {}, source.routing),
        )
        return routing

    async def reset_routing(self, code: str) -> None:
        source = await self.get_by_code(code)
        if source.routing is None:
            return
        before = source.routing
        source.routing = None
        await self._session.flush()
        await self._audit.record(
            AuditAction.INTEGRATION_ROUTING_RESET,
            entity_type="integration_source",
            entity_id=source.id,
            changes=diff_changes(before, {}),
        )

    # --- B5-5: ручной запуск «Забрать сейчас» и статус синхронизации --------------------

    _DEFAULT_BASE_URL_SETTING: dict[str, str] = {"cms": "cms_base_url", "lms": "lms_base_url"}

    async def request_pull(
        self, code: str, principal: Principal, *, updated_since: str | None
    ) -> dict[str, Any]:
        """Сама ничего не забирает: пишет запрос в `pull_request`, его первым делом читает
        `sweep_cms_leads_pull`/`sweep_lms_progress_pull` на следующем тике (раз в минуту,
        `worker/main.py`) и запускает забор сразу, независимо от `pull_enabled` и интервала —
        в течение минуты, а не по расписанию."""
        source = await self.get_by_code(code)
        settings = get_settings()
        default_attr = self._DEFAULT_BASE_URL_SETTING.get(code)
        default_base_url = getattr(settings, default_attr, None) if default_attr else None
        if not source.is_active or not (source.base_url or default_base_url):
            raise AppError(
                ErrorCode.INTEGRATION_SOURCE_INACTIVE, "Источник выключен или адрес не настроен"
            )
        if source.pull_request:
            raise AppError(
                ErrorCode.INTEGRATION_PULL_BUSY, "Запрос на забор уже стоит в очереди"
            )
        pull_request = {
            "requested_at": dt.datetime.now(dt.UTC).isoformat(),
            "requested_by": str(principal.user_id),
            "updated_since": updated_since,
        }
        source.pull_request = pull_request
        await self._session.flush()
        await self._audit.record(
            AuditAction.INTEGRATION_PULL_REQUESTED,
            entity_type="integration_source",
            entity_id=source.id,
            changes={"pull_request": {"old": None, "new": pull_request}},
        )
        return pull_request

    async def get_status(self, code: str) -> dict[str, Any]:
        """Сводка для экрана источника: настройки забора, курсоры (по всем `resource`,
        названия не захардкожены здесь — у CMS и LMS они разные), итог последнего прогона
        (`last_run` — снимок момента завершения) и живые счётчики за последние 24 часа по
        журналу входящих и очереди исходящих (у `cms` исходящих нет — источник только
        принимает, `outbox_last_24h` будет пустым, и это ожидаемо)."""
        source = await self.get_by_code(code)
        cursor_rows = (
            await self._session.execute(
                select(SyncCursor.resource, SyncCursor.cursor_value).where(
                    SyncCursor.source_code == code
                )
            )
        ).all()
        cursors: dict[str, str | None] = {row.resource: row.cursor_value for row in cursor_rows}

        since = dt.datetime.now(dt.UTC) - dt.timedelta(hours=24)
        inbound_rows = (
            await self._session.execute(
                select(InboundMessage.status, func.count())
                .where(InboundMessage.source_code == code, InboundMessage.received_at >= since)
                .group_by(InboundMessage.status)
            )
        ).all()
        inbound_counts: dict[str, int] = {row[0]: row[1] for row in inbound_rows}

        outbox_rows = (
            await self._session.execute(
                select(OutboxEvent.status, func.count())
                .where(OutboxEvent.target == code, OutboxEvent.created_at >= since)
                .group_by(OutboxEvent.status)
            )
        ).all()
        outbox_counts: dict[str, int] = {row[0]: row[1] for row in outbox_rows}
        return {
            "code": source.code,
            "is_active": source.is_active,
            "pull_enabled": source.pull_enabled,
            "pull_interval_seconds": source.pull_interval_seconds,
            "cursors": cursors,
            "last_sync_at": source.last_sync_at,
            "last_error": source.last_error,
            "last_run": source.last_run,
            "pull_request": source.pull_request,
            "inbound_last_24h": inbound_counts,
            "outbox_last_24h": outbox_counts,
        }


class OutboxEventService:
    """Ручное управление очередью доставки — то, что цикл
    `integration.tasks.sweep_outbox_events` сам не делает."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    async def retry(self, event_id: uuid.UUID) -> OutboxEvent:
        """Возвращает `failed`/`dead` в очередь: следующий тик доставки берёт
        событие как новое (счётчик попыток и ошибка сброшены; прежние значения
        остаются в аудите)."""
        # Блокировка строки: два одновременных «Повторить» не должны оба пройти.
        event = (
            await self._session.execute(
                select(OutboxEvent).where(OutboxEvent.id == event_id).with_for_update()
            )
        ).scalar_one_or_none()
        if event is None:
            raise NotFoundError("Событие outbox", event_id)
        retryable = (OutboxStatus.FAILED.value, OutboxStatus.DEAD.value)
        if event.status not in retryable:
            raise AppError(
                ErrorCode.INTEGRATION_EVENT_NOT_RETRYABLE,
                extra={"status": event.status},
            )

        before = {
            "status": event.status,
            "attempts": event.attempts,
            "last_error": event.last_error,
        }
        event.status = OutboxStatus.PENDING.value
        event.attempts = 0
        event.last_error = None
        event.next_retry_at = dt.datetime.now(dt.UTC)
        await self._session.flush()
        await self._audit.record(
            AuditAction.INTEGRATION_OUTBOX_RETRIED,
            entity_type="outbox_event",
            entity_id=event.id,
            changes=diff_changes(
                before,
                {"status": event.status, "attempts": event.attempts, "last_error": None},
            ),
        )
        return event


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
