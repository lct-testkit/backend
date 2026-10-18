"""B5-9: универсальные webhook-подписки на исходящие события.

Критерий жюри — «интеграция в любые Enterprise-системы». До этой задачи исходящие события
знали только два получателя, зашитых в код (`integration.tasks._KNOWN_TARGETS`: `lms`,
`bitrix24`). Администратор теперь подписывает произвольный URL на события из `WEBHOOK_EVENT_TYPES`
без релиза — `POST /api/admin/integrations/webhooks`.

Раздача (`fan_out`) происходит в ТОЙ ЖЕ транзакции, что публикация `OutboxEvent`
(`RealOutboxService.publish`, раздел 3.6) — переход сделки (бюджет 300 мс) не должен ждать ни
одного дополнительного похода в БД: список активных подписок кэшируется в Redis
(`cache:webhook_subs`, TTL 30 с) и с прогретым кэшем `fan_out` не делает ни одного SELECT, только
вставку строк доставки для подошедших подписок (см. `tests/test_query_budget.py`).

Доставка — отдельно, в фоне (`sweep_webhook_deliveries`, `integration.tasks`, см. её докстринг):
паблишер никогда не делает исходящих HTTP-вызовов сам, тот же принцип, что уже применяет
`RealOutboxService`/`sweep_outbox_events` к `lms`/`bitrix24`.
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
import uuid
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import run_after_commit
from app.core.errors import AppError, ErrorCode, FieldError, NotFoundError, ValidationError
from app.core.redis_client import TTL_WEBHOOK_SUBS, get_redis, key_webhook_subs
from app.core.security import Principal
from app.modules.admin.setting_crypto import decrypt_value, encrypt_value, encryption_enabled
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.integration.http import TIMEOUT as _HTTP_TIMEOUT
from app.modules.integration.models import (
    WEBHOOK_EVENT_TYPES,
    WebhookDelivery,
    WebhookDeliveryStatus,
    WebhookSubscription,
)
from app.modules.integration.security import check_outbound_url_resolved, compute_signature

_SECRET_PREFIX = "whsec_"
_SECRET_BYTES = 32
_MAX_FILTER_KEYS = 10


def _generate_secret() -> str:
    return f"{_SECRET_PREFIX}{secrets.token_urlsafe(_SECRET_BYTES)}"


# --- Кэш активных подписок (раздел «раздача») ----------------------------------------------


async def _load_cached_subscriptions(session: AsyncSession) -> list[dict[str, Any]]:
    """`[{"id", "event_types", "filters"}, ...]` активных подписок. Без секрета — он не нужен,
    чтобы решить, какие подписки получат доставку, только чтобы её отправить (там он читается
    из БД напрямую, см. `integration.tasks._send_webhook`)."""
    try:
        redis = get_redis()
        raw = await redis.get(key_webhook_subs())
    except Exception:  # noqa: BLE001 — кэш не источник истины
        raw = None
    if raw is not None:
        try:
            return json.loads(raw)
        except ValueError:
            pass

    rows = (
        (
            await session.execute(
                select(WebhookSubscription).where(WebhookSubscription.is_active.is_(True))
            )
        )
        .scalars()
        .all()
    )
    serialized = [
        {
            "id": str(row.id),
            "event_types": list(row.event_types or []),
            "filters": row.filters or {},
        }
        for row in rows
    ]
    try:
        await get_redis().setex(key_webhook_subs(), TTL_WEBHOOK_SUBS, json.dumps(serialized))
    except Exception:  # noqa: BLE001
        pass
    return serialized


async def _invalidate_cache() -> None:
    try:
        await get_redis().delete(key_webhook_subs())
    except Exception:  # noqa: BLE001
        pass


def invalidate_cache_after_commit(session: AsyncSession) -> None:
    """Любое изменение подписки (создание/правка/удаление/ротация секрета/включение) обязано
    сбросить кэш ПОСЛЕ коммита — сброс до коммита рискует кэшем, который следующий читатель
    заполнит снова из ещё не закоммиченных (для него) данных."""
    run_after_commit(session, _invalidate_cache)


def _filters_match(filters: dict[str, Any], payload: dict[str, Any] | None) -> bool:
    """Пустые `filters` — подписка получает любое событие из своих `event_types`. Непустые —
    строгое совпадение по каждому ключу; событие без этого поля в нагрузке не подходит."""
    if not filters:
        return True
    payload = payload or {}
    return all(payload.get(key) == value for key, value in filters.items())


async def fan_out(
    session: AsyncSession,
    *,
    aggregate_type: str,
    aggregate_id: uuid.UUID,
    event_type: str,
    payload: dict[str, Any] | None,
) -> None:
    """Раздаёт событие подпискам, подходящим по `event_types`/`filters` — вызывается из
    `RealOutboxService.publish` в ТОЙ ЖЕ транзакции. См. докстринг модуля про бюджет запросов."""
    if event_type not in WEBHOOK_EVENT_TYPES:
        return
    subscriptions = await _load_cached_subscriptions(session)
    if not subscriptions:
        return
    matched_ids = [
        uuid.UUID(sub["id"])
        for sub in subscriptions
        if event_type in sub.get("event_types", ())
        and _filters_match(sub.get("filters") or {}, payload)
    ]
    if not matched_ids:
        return
    session.add_all(
        WebhookDelivery(
            subscription_id=subscription_id,
            event_type=event_type,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            payload=payload or {},
        )
        for subscription_id in matched_ids
    )
    await session.flush()


# --- CRUD подписок (ADMIN, `integration:admin`) --------------------------------------------


class WebhookSubscriptionService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    async def list_all(self) -> list[WebhookSubscription]:
        rows = (
            (
                await self._session.execute(
                    select(WebhookSubscription).order_by(WebhookSubscription.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        return list(rows)

    async def get_or_404(self, subscription_id: uuid.UUID) -> WebhookSubscription:
        subscription = await self._session.get(WebhookSubscription, subscription_id)
        if subscription is None:
            raise NotFoundError("Подписка webhook", subscription_id)
        return subscription

    def _validate_event_types(self, event_types: list[str]) -> None:
        if not event_types:
            raise ValidationError(
                "Нужен хотя бы один тип события",
                [FieldError(field="event_types", reason="пустой список")],
            )
        unknown = sorted(set(event_types) - WEBHOOK_EVENT_TYPES)
        if unknown:
            raise ValidationError(
                "Неизвестный тип события",
                [
                    FieldError(
                        field="event_types", reason=f"неизвестные значения: {', '.join(unknown)}"
                    )
                ],
            )

    def _validate_filters(self, filters: dict[str, Any]) -> None:
        if len(filters) > _MAX_FILTER_KEYS:
            raise ValidationError(
                "Слишком много условий фильтра",
                [FieldError(field="filters", reason=f"не более {_MAX_FILTER_KEYS} ключей")],
            )

    async def _validated_url(self, url: str) -> str:
        settings = get_settings()
        try:
            return await check_outbound_url_resolved(url, strict=settings.is_prod)
        except ValueError as exc:
            raise ValidationError(
                "Адрес подписки не принят", [FieldError(field="url", reason=str(exc))]
            ) from exc

    def _require_encryption_in_prod(self) -> None:
        if get_settings().is_prod and not encryption_enabled():
            raise AppError(
                ErrorCode.WEBHOOK_ENCRYPTION_REQUIRED,
                "В prod секрет подписки хранится только зашифрованным — задайте "
                "SETTINGS_ENCRYPTION_KEY перед созданием подписки",
            )

    async def create(self, principal: Principal, payload: Any) -> tuple[WebhookSubscription, str]:
        """Возвращает подписку и секрет в открытом виде — он показывается РОВНО ОДИН РАЗ, в
        ответе на этот вызов (и на `rotate_secret`); `GET` его не отдаёт."""
        self._require_encryption_in_prod()
        self._validate_event_types(payload.event_types)
        filters = payload.filters or {}
        self._validate_filters(filters)
        url = await self._validated_url(payload.url)
        secret = _generate_secret()
        subscription = WebhookSubscription(
            name=payload.name,
            url=url,
            event_types=list(payload.event_types),
            filters=filters,
            secret_encrypted=encrypt_value(secret),
            created_by=principal.user_id,
        )
        self._session.add(subscription)
        await self._session.flush()
        await self._audit.record(
            AuditAction.WEBHOOK_SUBSCRIPTION_CREATED,
            entity_type="webhook_subscription",
            entity_id=subscription.id,
            changes={
                "name": {"old": None, "new": subscription.name},
                "url": {"old": None, "new": subscription.url},
                "event_types": {"old": None, "new": subscription.event_types},
                "filters": {"old": None, "new": subscription.filters},
            },
        )
        invalidate_cache_after_commit(self._session)
        return subscription, secret

    async def update(self, subscription: WebhookSubscription, payload: Any) -> WebhookSubscription:
        updates = payload.model_dump(exclude_unset=True)
        if "event_types" in updates:
            self._validate_event_types(updates["event_types"])
        if "filters" in updates and updates["filters"] is not None:
            self._validate_filters(updates["filters"])
        if updates.get("url") is not None:
            updates["url"] = await self._validated_url(updates["url"])
        reactivated = updates.get("is_active") is True and not subscription.is_active

        changes: dict[str, Any] = {}
        for field, new_value in updates.items():
            old_value = getattr(subscription, field)
            if old_value != new_value:
                changes[field] = {"old": old_value, "new": new_value}
                setattr(subscription, field, new_value)
        if reactivated:
            # Администратор сознательно включил подписку обратно — хвост дохлых попыток не
            # должен сразу же выключить её снова.
            subscription.consecutive_failures = 0
            subscription.disabled_reason = None
        if not changes:
            return subscription
        await self._session.flush()
        await self._audit.record(
            AuditAction.WEBHOOK_SUBSCRIPTION_UPDATED,
            entity_type="webhook_subscription",
            entity_id=subscription.id,
            changes=changes,
        )
        invalidate_cache_after_commit(self._session)
        return subscription

    async def delete(self, subscription: WebhookSubscription) -> None:
        await self._audit.record(
            AuditAction.WEBHOOK_SUBSCRIPTION_DELETED,
            entity_type="webhook_subscription",
            entity_id=subscription.id,
            changes={"name": {"old": subscription.name, "new": None}},
        )
        await self._session.delete(subscription)
        await self._session.flush()
        invalidate_cache_after_commit(self._session)

    async def rotate_secret(self, subscription: WebhookSubscription) -> str:
        self._require_encryption_in_prod()
        secret = _generate_secret()
        subscription.secret_encrypted = encrypt_value(secret)
        await self._session.flush()
        await self._audit.record(
            AuditAction.WEBHOOK_SUBSCRIPTION_SECRET_ROTATED,
            entity_type="webhook_subscription",
            entity_id=subscription.id,
            changes={},
        )
        invalidate_cache_after_commit(self._session)
        return secret

    async def test_ping(self, subscription: WebhookSubscription) -> dict[str, Any]:
        """`POST .../webhooks/{id}/test`: синхронная проверка вне очереди доставки —
        администратор видит код ответа и время немедленно, не дожидаясь тика `sweep_
        webhook_deliveries`. Не пишет строку в `webhook_deliveries` и не считается попыткой
        подписки (`consecutive_failures` не меняется)."""
        settings = get_settings()
        secret = decrypt_value(subscription.secret_encrypted)
        delivery_id = uuid.uuid4()
        body = json.dumps(
            {
                "id": str(delivery_id),
                "event": "PING",
                "occurred_at": dt.datetime.now(dt.UTC).isoformat(),
                "data": {},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        timestamp = str(int(dt.datetime.now(dt.UTC).timestamp()))
        signature = compute_signature(secret, timestamp.encode("ascii") + b"." + body)
        headers = {
            "Content-Type": "application/json",
            "X-Signature": f"sha256={signature}",
            "X-Timestamp": timestamp,
            "X-CRM-Event": "PING",
            "X-CRM-Delivery": str(delivery_id),
        }
        started = dt.datetime.now(dt.UTC)
        try:
            url = await check_outbound_url_resolved(subscription.url, strict=settings.is_prod)
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                response = await client.post(
                    url, content=body, headers=headers, follow_redirects=False
                )
        except (ValueError, httpx.HTTPError) as exc:
            elapsed_ms = (dt.datetime.now(dt.UTC) - started).total_seconds() * 1000
            return {
                "status_code": None,
                "elapsed_ms": round(elapsed_ms, 1),
                "error": type(exc).__name__,
            }
        elapsed_ms = (dt.datetime.now(dt.UTC) - started).total_seconds() * 1000
        return {
            "status_code": response.status_code,
            "elapsed_ms": round(elapsed_ms, 1),
            "error": None,
        }

    async def retry_delivery(self, delivery_id: uuid.UUID) -> WebhookDelivery:
        delivery = (
            await self._session.execute(
                select(WebhookDelivery).where(WebhookDelivery.id == delivery_id).with_for_update()
            )
        ).scalar_one_or_none()
        if delivery is None:
            raise NotFoundError("Доставка webhook", delivery_id)
        retryable = (WebhookDeliveryStatus.FAILED.value, WebhookDeliveryStatus.DEAD.value)
        if delivery.status not in retryable:
            raise AppError(
                ErrorCode.INTEGRATION_EVENT_NOT_RETRYABLE, extra={"status": delivery.status}
            )
        before = {"status": delivery.status, "attempts": delivery.attempts}
        delivery.status = WebhookDeliveryStatus.PENDING.value
        delivery.attempts = 0
        delivery.last_error = None
        delivery.next_retry_at = dt.datetime.now(dt.UTC)
        await self._session.flush()
        await self._audit.record(
            AuditAction.WEBHOOK_DELIVERY_RETRIED,
            entity_type="webhook_delivery",
            entity_id=delivery.id,
            changes={
                "status": {"old": before["status"], "new": delivery.status},
                "attempts": {"old": before["attempts"], "new": 0},
            },
        )
        return delivery
