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
from app.modules.identity.keycloak import keycloak_client
from app.modules.identity.keycloak_events import KeycloakEventSync
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


@track_task
async def sweep_keycloak_events(ctx: dict[str, Any]) -> dict[str, Any]:
    """B6-3: опрос Admin API Keycloak (`LOGIN_ERROR` -> `LOGIN_FAILED`/`BRUTE_FORCE_LOCK`).

    Нет-опом, пока `KEYCLOAK_EVENTS_POLL_ENABLED=false` (дефолт — до включения событий в realm,
    спринт D1): `KeycloakEventSync.run` сам проверяет флаг и не делает запросов."""
    async with session_scope() as session:
        result = await KeycloakEventSync(session, keycloak_client).run()
    if result.skipped:
        return {"skipped": True}
    if result.errored:
        logger.warning("keycloak_events_sweep_error")
        return {"errored": True}
    if result.fetched:
        logger.info(
            "keycloak_events_swept",
            fetched=result.fetched,
            batches=result.batches,
            locked=result.locked,
        )
    return {"fetched": result.fetched, "batches": result.batches, "locked": result.locked}


# B6-12: пороги напоминаний о сроке РКН (часов до дедлайна); `0` — уже просрочен.
_INCIDENT_ALERT_THRESHOLDS_HOURS = (6, 1, 0)


@track_task
async def sweep_incident_deadlines(ctx: dict[str, Any]) -> dict[str, int]:
    """B6-12: напоминания о приближающемся/пропущенном сроке уведомления РКН (24/72 ч).

    Каждый порог (`_INCIDENT_ALERT_THRESHOLDS_HOURS`) отправляется не больше одного раза на
    инцидент и стадию — отметка в `deadline_alerts_sent` (JSONB-список строк вида
    `"24h:6h"`/`"24h:overdue"`). Просрочка дополнительно пишет аудит один раз — по тому же
    отметке-флагу, не по отдельному запросу "писали уже или нет"."""
    from app.modules.audit.actions import AuditAction
    from app.modules.audit.service import AuditService
    from app.modules.identity.incident_service import TPL_SECURITY_INCIDENT_DEADLINE
    from app.modules.identity.models import IncidentStatus, Role, SecurityIncident
    from app.modules.notification.service import NotificationPriority, get_notification_service

    now = dt.datetime.now(dt.UTC)
    async with session_scope() as session:
        due_ids = list(
            (
                await session.execute(
                    select(SecurityIncident.id).where(
                        SecurityIncident.status.in_(
                            [IncidentStatus.OPEN.value, IncidentStatus.RKN_24_SENT.value]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )

    notified = overdue_count = failed = 0
    for incident_id in due_ids:
        try:
            async with session_scope() as session:
                incident = await session.get(SecurityIncident, incident_id)
                if incident is None:
                    continue
                stage, due_at = (
                    ("24h", incident.rkn_24_due_at)
                    if incident.status == IncidentStatus.OPEN.value
                    else ("72h", incident.rkn_72_due_at)
                )
                if due_at is None:
                    continue
                hours_left = (due_at - now).total_seconds() / 3600
                alerts_sent = set(incident.deadline_alerts_sent or [])
                for threshold in _INCIDENT_ALERT_THRESHOLDS_HOURS:
                    marker = f"{stage}:{'overdue' if threshold == 0 else f'{threshold}h'}"
                    if marker in alerts_sent:
                        continue
                    crossed = hours_left <= 0 if threshold == 0 else 0 < hours_left <= threshold
                    if not crossed:
                        continue
                    overdue = threshold == 0
                    recipients = set(
                        (
                            await session.execute(
                                select(User.id).where(
                                    User.role == Role.ADMIN.value,
                                    User.status == UserStatus.ACTIVE.value,
                                    User.deleted_at.is_(None),
                                )
                            )
                        ).scalars()
                    )
                    if incident.responsible_user_id:
                        recipients.add(incident.responsible_user_id)
                    for recipient_id in recipients:
                        await get_notification_service().notify_user(
                            session,
                            recipient_id=recipient_id,
                            template_code=TPL_SECURITY_INCIDENT_DEADLINE,
                            payload={
                                "number": incident.number,
                                "stage": stage,
                                "overdue": overdue,
                                "due_at": due_at.isoformat(),
                            },
                            priority=NotificationPriority.HIGH,
                            entity_type="security_incident",
                            entity_id=incident.id,
                        )
                    alerts_sent.add(marker)
                    incident.deadline_alerts_sent = sorted(alerts_sent)
                    notified += 1
                    if overdue:
                        overdue_count += 1
                        await AuditService(session).record(
                            AuditAction.INCIDENT_DEADLINE_OVERDUE,
                            entity_type="security_incident",
                            entity_id=incident.id,
                            changes={"stage": {"old": None, "new": stage}},
                        )
                    # Только один порог за тик на инцидент/стадию — следующий тик (15 мин)
                    # заметит следующий порог, если он тоже уже прошёл.
                    break
        except Exception:  # noqa: BLE001 — один проблемный инцидент не должен ронять сборщик
            failed += 1
            logger.exception("incident_deadline_sweep_failed", incident_id=str(incident_id))

    if notified or failed:
        logger.info(
            "incident_deadlines_swept", notified=notified, overdue=overdue_count, failed=failed
        )
    return {"notified": notified, "overdue": overdue_count, "failed": failed}
