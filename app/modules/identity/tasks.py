"""Сборщик исполнения удаления/обезличивания (new_spec §4.8.4 шаг 5).

Раньше `ERASURE_EXECUTED` существовал только как значение `AuditAction` и ни
разу не вызывался: `grace_until` вычислялся роутером для ответа API и нигде
не сохранялся, сборщика не было вовсе. `identity.erasure_service.
ErasureExecutionService.execute` закрывает половину «что» — этот файл
закрывает «когда».

Каждая заявка исполняется в своей собственной транзакции (`session_scope()`
на заявку, а не одна на весь тик), а не в одной общей на весь список, как
`crm.tasks.sweep_sla_breaches` — там мутация одного поля не может провалиться
частично, здесь исполнение делает вызов в Keycloak, загрузку акта в S3 и
несколько мутаций подряд: если оно упадёт на середине одной заявки, откат не
должен задевать уже исполненные соседние. Тот же принцип «батчами, каждый в
своей транзакции с чекпоинтом», что new_spec §4.11 описывает для миграции
статусов при архивировании.

`sweep_user_lifecycle` — вторая половина этого же файла: автоматика учётных
записей по времени (снятие блокировки по `auto_unblock_at`, `INVITE_EXPIRED`).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import structlog
from sqlalchemy import func, select

from app.core.db import session_scope
from app.core.metrics import track_task
from app.modules.identity.admin_service import AdminUserService
from app.modules.identity.erasure_service import ErasureExecutionService
from app.modules.identity.models import DataErasureRequest, ErasureStatus, User, UserStatus

logger = structlog.get_logger(__name__)

# new_spec §4.1: «не вошёл за 30 дней — автоматическое событие `INVITE_EXPIRED`».
INVITE_EXPIRE_DAYS = 30
AUTO_UNBLOCK_REASON = "Срок блокировки истёк"


@track_task
async def sweep_erasure_requests(ctx: dict[str, Any]) -> dict[str, int]:
    now = dt.datetime.now(dt.UTC)
    async with session_scope() as session:
        due_ids = list(
            (
                await session.execute(
                    select(DataErasureRequest.id).where(
                        DataErasureRequest.status.in_(
                            [ErasureStatus.PENDING.value, ErasureStatus.APPROVED.value]
                        ),
                        DataErasureRequest.grace_until.is_not(None),
                        DataErasureRequest.grace_until <= now,
                    )
                )
            )
            .scalars()
            .all()
        )

    executed = blocked = failed = 0
    for request_id in due_ids:
        try:
            async with session_scope() as session:
                request = await session.get(DataErasureRequest, request_id)
                if request is None:
                    continue
                result = await ErasureExecutionService(session).execute(request)
            if result["executed"]:
                executed += 1
            else:
                blocked += 1
        except Exception:  # noqa: BLE001 — одна проблемная заявка не должна ронять сборщик
            failed += 1
            logger.exception("erasure_execution_failed", request_id=str(request_id))

    if executed or blocked or failed:
        logger.info("erasure_sweep_completed", executed=executed, blocked=blocked, failed=failed)
    return {"executed": executed, "blocked": blocked, "failed": failed}


@track_task
async def sweep_user_lifecycle(ctx: dict[str, Any]) -> dict[str, int]:
    """Автоматика учётных записей по времени.

    * Блокировка со сроком (`users.auto_unblock_at`, new_spec §4.5): по его
      наступлении учётка разблокируется так же, как ручным `/unblock` —
      Keycloak, сделки, уведомление, аудит.
    * `INVITE_EXPIRED` (new_spec §4.1): по приглашению не вошёл за
      `INVITE_EXPIRE_DAYS` дней (считая от последнего приглашения, а без него —
      от создания учётки) — учётка отключается, администраторам уходит
      уведомление.

    Каждая учётка — в своей транзакции, как в `sweep_erasure_requests`: сбой
    Keycloak на одной не задевает остальные, а сама она повторится на
    следующем тике.
    """
    now = dt.datetime.now(dt.UTC)
    invite_cutoff = now - dt.timedelta(days=INVITE_EXPIRE_DAYS)
    async with session_scope() as session:
        due_unblock = list(
            (
                await session.execute(
                    select(User.id).where(
                        User.status == UserStatus.BLOCKED.value,
                        User.auto_unblock_at.is_not(None),
                        User.auto_unblock_at <= now,
                        User.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        due_expire = list(
            (
                await session.execute(
                    select(User.id).where(
                        User.status == UserStatus.INVITED.value,
                        User.last_login_at.is_(None),
                        func.coalesce(User.invited_at, User.created_at) < invite_cutoff,
                        User.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )

    unblocked = invites_expired = failed = 0
    for user_id in due_unblock:
        try:
            async with session_scope() as session:
                user = await session.get(User, user_id)
                if user is None or user.status != UserStatus.BLOCKED.value:
                    continue  # успели разблокировать вручную
                await AdminUserService(session).unblock(user=user, reason=AUTO_UNBLOCK_REASON)
            unblocked += 1
        except Exception:  # noqa: BLE001 — одна учётка не должна ронять сборщик
            failed += 1
            logger.exception("user_auto_unblock_failed", user_id=str(user_id))

    for user_id in due_expire:
        try:
            async with session_scope() as session:
                user = await session.get(User, user_id)
                if user is None or not await AdminUserService(session).expire_invite(user):
                    continue
            invites_expired += 1
        except Exception:  # noqa: BLE001
            failed += 1
            logger.exception("user_invite_expiry_failed", user_id=str(user_id))

    if unblocked or invites_expired or failed:
        logger.info(
            "user_lifecycle_swept",
            unblocked=unblocked,
            invites_expired=invites_expired,
            failed=failed,
        )
    return {"unblocked": unblocked, "invites_expired": invites_expired, "failed": failed}
