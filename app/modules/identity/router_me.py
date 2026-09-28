"""Профиль, сессии, согласие и смена пароля (раздел 6.1).

Смена пароля — не «поменять строку», а цепочка последствий из new_spec §4.4:
завершение всех остальных сессий, переиздание текущей, сброс кэша прав,
событие безопасности, уведомление и аннулирование незавершённых запросов
подписи. Сам пароль не логируется, не кэшируется и не попадает в аудит.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Annotated

import structlog
from fastapi import APIRouter, Path, Request, Response, status
from sqlalchemy import select

from app.core.config import get_settings
from app.core.csrf import new_csrf_token, set_csrf_cookie
from app.core.deps import CurrentUser, DbSession
from app.core.errors import AppError, ErrorCode, NotFoundError
from app.core.permissions import scopes_for
from app.core.rate_limit import enforce as rate_limit
from app.core.rate_limit import reset as reset_rate_limit
from app.core.redis_client import RECENT_MAX_ITEMS, get_redis, key_recent
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.identity.keycloak import keycloak_client
from app.modules.identity.models import SecurityEventType, Severity, User
from app.modules.identity.schemas import (
    ConsentRequest,
    ConsentResponse,
    MePatchRequest,
    MeResponse,
    OperationResult,
    PasswordChangeRequest,
    PasswordChangeResponse,
    PolicyResponse,
    RecentItemOut,
    RecentListResponse,
    SessionInfo,
    SessionListResponse,
    SessionsTerminatedResponse,
)
from app.modules.identity.service import IdentityService
from app.modules.identity.session_store import session_store
from app.modules.notification.service import (
    TPL_PASSWORD_CHANGED,
    NotificationPriority,
    get_notification_service,
)
from app.modules.signing.service import (
    VOID_REASON_CREDENTIALS_CHANGED,
    get_signing_service,
)

logger = structlog.get_logger(__name__)
router = APIRouter(prefix="/me", tags=["me"])


async def build_me(session, user: User) -> MeResponse:
    identity = IdentityService(session)
    policy = await identity.current_policy()
    return MeResponse(
        id=user.id,
        full_name=user.full_name,
        display_name=user.display_name,
        email=user.email,
        phone=user.phone,
        role=user.role,
        team_id=user.team_id,
        manager_id=user.manager_id,
        status=user.status,
        locale=user.locale,
        timezone=user.timezone,
        consent_version=user.consent_version,
        consent_required=user.consent_version != policy.version,
        policy_version=policy.version,
        password_change_required=user.must_change_password,
        scopes=scopes_for(user.role),
        teams=[user.team_id] if user.team_id else [],
        perm_epoch=user.perm_epoch,
        last_login_at=user.last_login_at,
        version=user.version,
    )


@router.get(
    "",
    summary="Профиль текущего пользователя",
    description=(
        "Возвращает локальную проекцию пользователя и набор прав. Если требуется "
        "принять новую версию политики обработки ПДн или сменить пароль, это "
        "указывается отдельными признаками. Роль: любой аутентифицированный."
    ),
    response_model=MeResponse,
)
async def get_me(principal: CurrentUser, session: DbSession) -> MeResponse:
    user = (await session.execute(select(User).where(User.id == principal.user_id))).scalar_one()
    return await build_me(session, user)


@router.patch(
    "",
    summary="Изменить свой профиль",
    description=(
        "Отображаемое имя, часовой пояс и телефон — только себе; чужой профиль "
        "меняется через администрирование. Телефон нужен для кода подтверждения "
        "подписи (SMS), `null` очищает его. Роль, email и статус здесь не "
        "принимаются (422). Сбрасывает кэш прав, пишет `USER_UPDATED` в аудит "
        "(телефон маскируется). Роль: любой аутентифицированный пользователь."
    ),
    response_model=MeResponse,
)
async def patch_me(
    payload: MePatchRequest, principal: CurrentUser, session: DbSession
) -> MeResponse:
    user = (await session.execute(select(User).where(User.id == principal.user_id))).scalar_one()
    await IdentityService(session).update_profile(user, payload.model_dump(exclude_unset=True))
    return await build_me(session, user)


@router.get(
    "/policy",
    summary="Действующая политика обработки ПДн",
    description=(
        "Версия и хэш текста действующей редакции. Окно согласия подставляет "
        "их в `POST /api/me/consent`. Роль: любой аутентифицированный."
    ),
    response_model=PolicyResponse,
)
async def get_policy(_: CurrentUser, session: DbSession) -> PolicyResponse:
    policy = await IdentityService(session).current_policy()
    return PolicyResponse(version=policy.version, text_hash=policy.text_hash)


@router.get(
    "/recent",
    summary="Последние открытые объекты",
    description=(
        "Возвращает до 20 последних открытых пользователем объектов из Redis ZSET "
        "`recent:{user_id}`, новые первыми: вид (`type`), `id`, название и время "
        "открытия. Сейчас в историю попадают сделки. "
        "Роль: любой аутентифицированный пользователь."
    ),
    response_model=RecentListResponse,
)
async def get_recent(principal: CurrentUser) -> RecentListResponse:
    try:
        raw_items = await get_redis().zrevrange(
            key_recent(principal.user_id), 0, RECENT_MAX_ITEMS - 1, withscores=True
        )
    except Exception:
        # Redis — не источник истины: пустой список лучше, чем ошибка (раздел 16).
        return RecentListResponse(items=[])

    items: list[RecentItemOut] = []
    for member, score in raw_items:
        try:
            entry = json.loads(member)
            items.append(
                RecentItemOut(
                    type=entry["type"],
                    id=entry["id"],
                    title=entry["title"],
                    opened_at=dt.datetime.fromtimestamp(score, dt.UTC),
                )
            )
        except (ValueError, KeyError, TypeError):
            continue  # битая запись истории не должна ломать список
    return RecentListResponse(items=items)


# Префикс публичного идентификатора сессии, которая живёт только в Keycloak (вход по Bearer).
KC_SESSION_PREFIX = "kc-"


def keycloak_session_info(raw: dict, *, current_state: str | None) -> SessionInfo:
    """Сессия Keycloak в том же виде, что и серверная: без токенов, времена в ISO."""

    def _iso(millis: object) -> str:
        return dt.datetime.fromtimestamp(int(millis or 0) / 1000, dt.UTC).isoformat()  # type: ignore[call-overload]

    return SessionInfo(
        sid=f"{KC_SESSION_PREFIX}{raw['id']}",
        # Keycloak не хранит User-Agent, поэтому подпись честная: откуда вход, а не браузер.
        device="Вход в систему",
        ip=raw.get("ipAddress"),
        user_agent=None,
        created_at=_iso(raw.get("start")),
        last_seen_at=_iso(raw.get("lastAccess")),
        is_current=bool(current_state) and raw["id"] == current_state,
    )


async def _keycloak_only_sessions(principal, known_states: set[str]) -> list[dict]:
    """Сессии Keycloak пользователя, которых нет среди серверных.

    В режиме Bearer (демо) SPA берёт токены у Keycloak сам и cookie-сессии в Redis не
    создаётся — раньше список был пуст, хотя вход выполнен на нескольких устройствах.
    Серверные сессии уже порождают свою сессию Keycloak (`kc_session_state`), её не дублируем.
    Keycloak недоступен или admin-клиент не настроен — просто показываем то, что есть."""
    try:
        raw = await keycloak_client.list_user_sessions(principal.keycloak_id)
    except Exception:
        logger.warning("keycloak_sessions_unavailable")
        return []
    return [item for item in raw if item.get("id") and item["id"] not in known_states]


@router.get(
    "/sessions",
    summary="Активные сессии",
    description=(
        "Список входов пользователя: серверные сессии из Redis (устройство, IP, User-Agent, "
        "время создания и последняя активность) и, для входа по Bearer-токену без серверной "
        "сессии, сессии Keycloak (устройство и User-Agent там неизвестны). Токены не "
        "возвращаются, а `sid` — не значение session-cookie, а необратимый публичный "
        "идентификатор сессии: по нему работает `DELETE /me/sessions/{sid}`. "
        "Роль: любой аутентифицированный пользователь."
    ),
    response_model=SessionListResponse,
)
async def list_sessions(principal: CurrentUser) -> SessionListResponse:
    sessions = await session_store.list_for_user(principal.user_id)
    items = [
        SessionInfo(
            **stored.public_view(),
            is_current=stored.sid == principal.session_id,
        )
        for stored in sessions
    ]
    known = {stored.kc_session_state for stored in sessions if stored.kc_session_state}
    current_state = principal.claims.session_state if principal.session_id is None else None
    items += [
        keycloak_session_info(raw, current_state=current_state)
        for raw in await _keycloak_only_sessions(principal, known)
    ]
    return SessionListResponse(items=items)


@router.post(
    "/sessions/terminate-others",
    summary="Завершить все остальные сессии",
    description=(
        "Гасит все сессии пользователя, кроме текущей: серверные (и их сессии в Keycloak) и, "
        "при входе по Bearer, остальные сессии Keycloak. Текущая сессия остаётся рабочей. "
        "Идемпотентно: если других сессий нет, возвращает `terminated: 0`. Факт пишется в "
        "аудит (одна запись с числом завершённых сессий). Роль: любой аутентифицированный."
    ),
    response_model=SessionsTerminatedResponse,
)
async def terminate_other_sessions(
    principal: CurrentUser, session: DbSession
) -> SessionsTerminatedResponse:
    own = await session_store.list_for_user(principal.user_id)
    others = [item for item in own if item.sid != principal.session_id]
    terminated = 0
    for stored in others:
        await session_store.delete(stored.sid)
        terminated += 1
        if stored.refresh_token:
            try:
                await keycloak_client.logout(stored.refresh_token)
            except Exception:
                # Локально сессия уже погашена; сбой Keycloak не должен оставить остальные.
                logger.warning("keycloak_logout_failed", sid=stored.public_id)

    # Сессии, живущие только в Keycloak (Bearer). Свою текущую (по sid из токена) и сессии,
    # порождённые серверными, исключаем: первую нельзя рвать, вторые уже погашены выше.
    keep = {stored.kc_session_state for stored in own if stored.kc_session_state}
    if principal.session_id is None and principal.claims.session_state:
        keep.add(principal.claims.session_state)
    for raw in await _keycloak_only_sessions(principal, keep):
        try:
            await keycloak_client.delete_session(raw["id"])
            terminated += 1
        except Exception:
            logger.warning("keycloak_session_delete_failed", kc_session=raw["id"])

    if terminated:
        await AuditService(session).record(
            AuditAction.SESSION_TERMINATED,
            entity_type="user",
            entity_id=principal.user_id,
            changes={
                "self_service": {"old": None, "new": True},
                "terminated_others": {"old": None, "new": terminated},
            },
        )
    return SessionsTerminatedResponse(terminated=terminated)


@router.delete(
    "/sessions/{sid}",
    summary="Завершить сессию",
    description=(
        "Завершает конкретную сессию пользователя по публичному идентификатору из списка "
        "сессий (для совместимости принимается и сам sid). Завершить можно только свою "
        "сессию: чужие завершаются через административный механизм; сессия гасится и в "
        "Keycloak. Факт завершения пишется в аудит. Роль: любой аутентифицированный."
    ),
    response_model=OperationResult,
)
async def delete_session(
    principal: CurrentUser,
    session: DbSession,
    sid: Annotated[str, Path(description="Идентификатор сессии")],
) -> OperationResult:
    if sid.startswith(KC_SESSION_PREFIX):
        # Сессия, которая есть только в Keycloak: гасим её там, но лишь если она принадлежит
        # самому пользователю (ищем среди его собственных сессий, а не по голому id).
        kc_id = sid[len(KC_SESSION_PREFIX) :]
        own_kc = await _keycloak_only_sessions(principal, set())
        if not any(item["id"] == kc_id for item in own_kc):
            raise NotFoundError("Сессия", sid)
        await keycloak_client.delete_session(kc_id)
        await AuditService(session).record(
            AuditAction.SESSION_TERMINATED,
            entity_type="user",
            entity_id=principal.user_id,
            changes={"self_service": {"old": None, "new": True}},
        )
        return OperationResult(ok=True, detail="Сессия завершена")

    # Идентификатор из списка сессий — свёртка (`public_id`), не значение cookie; для совместимости
    # принимается и сам `sid`. Ищем среди сессий самого пользователя: чужая сессия по публичному
    # идентификатору неотличима от несуществующей.
    own = await session_store.list_for_user(principal.user_id)
    stored = next((item for item in own if sid in (item.public_id, item.sid)), None)
    if stored is None:
        foreign = await session_store.get(sid)
        if foreign is not None and foreign.user_id != str(principal.user_id):
            # Не раскрываем существование чужой сессии деталями ошибки.
            raise AppError(ErrorCode.FORBIDDEN, "Можно завершать только свои сессии")
        raise NotFoundError("Сессия", sid)

    await session_store.delete(stored.sid)
    if stored.refresh_token:
        # Локальной сессии мало: пока жива сессия в Keycloak, вход по ней возможен (тот же
        # refresh-токен), и «завершённая» сессия оставалась рабочей.
        await keycloak_client.logout(stored.refresh_token)
    await AuditService(session).record(
        AuditAction.SESSION_TERMINATED,
        entity_type="user",
        entity_id=principal.user_id,
        changes={"self_service": {"old": None, "new": True}},
    )
    return OperationResult(ok=True, detail="Сессия завершена")


@router.post(
    "/consent",
    summary="Принять политику обработки ПДн",
    description=(
        "Создаёт запись в `consents` с версией политики, хэшем текста, IP и "
        "User-Agent. Хэш сверяется с опубликованной редакцией — иначе согласие "
        "нечем доказать. Без согласия доступ к данным закрыт (CRM-1105). "
        "Роль: любой аутентифицированный пользователь."
    ),
    response_model=ConsentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def accept_consent(
    payload: ConsentRequest, principal: CurrentUser, session: DbSession
) -> ConsentResponse:
    user = (await session.execute(select(User).where(User.id == principal.user_id))).scalar_one()
    consent = await IdentityService(session).accept_consent(
        user,
        policy_version=payload.policy_version,
        policy_text_hash=payload.policy_text_hash,
    )
    return ConsentResponse(
        accepted=True,
        policy_version=consent.policy_version,
        accepted_at=consent.accepted_at,  # type: ignore[arg-type]
    )


@router.post(
    "/password",
    summary="Сменить пароль",
    description=(
        "Проверяет текущий пароль через Keycloak и устанавливает новый. "
        "Последствия: завершаются все сессии кроме текущей, текущая переиздаётся "
        "с новым идентификатором, сбрасывается кэш прав, создаются событие "
        "безопасности и запись аудита без значений паролей, аннулируются "
        "незавершённые запросы подписи (причина `credentials_changed`). "
        "Пять неудачных попыток блокируют форму на 15 минут. "
        "Роль: любой аутентифицированный пользователь."
    ),
    response_model=PasswordChangeResponse,
)
async def change_password(
    payload: PasswordChangeRequest,
    principal: CurrentUser,
    session: DbSession,
    request: Request,
    response: Response,
) -> PasswordChangeResponse:
    settings = get_settings()
    identity = IdentityService(session)
    user = (await session.execute(select(User).where(User.id == principal.user_id))).scalar_one()

    if not user.keycloak_id or not user.email:
        raise AppError(
            ErrorCode.VALIDATION,
            "Смена пароля доступна только пользователям с учётной записью Keycloak",
        )

    # Перебор текущего пароля должен упираться в лимит, а не в терпение.
    await rate_limit(
        str(user.id),
        "password:change",
        limit=settings.password_change_max_attempts,
        window_seconds=settings.password_change_lock_seconds,
        detail="Слишком много попыток смены пароля, попробуйте позже",
        # Лимит — единственная защита от перебора текущего пароля: без Redis не пускаем.
        fail_closed=True,
    )

    try:
        await keycloak_client.password_grant(username=user.email, password=payload.current_password)
    except AppError:
        await identity.record_security_event(
            SecurityEventType.LOGIN_FAILED,
            user_id=user.id,
            severity=Severity.WARNING,
            details={"context": "password_change", "reason": "current_password_invalid"},
        )
        # `core.db.get_db_session` откатывает ВСЮ транзакцию на любом
        # исключении — без явного commit здесь `SecurityEvent` откатывался
        # бы вместе с ответом: неверный текущий пароль не оставлял бы следа.
        # Тот же приём, что уже закрыл этот пробел в
        # `identity.admin_service.ApprovalService.require`/`signing.service.
        # _fail_otp`/`identity.service`'s «нет роли CRM» (см. эти файлы) —
        # перебор самого поля здесь отдельно ограничен `rate_limit(...)`
        # выше, поэтому это только потеря телеметрии, не обход защиты.
        await session.commit()
        # Значение пароля никуда не попадает — только факт неудачи.
        raise AppError(ErrorCode.VALIDATION, "Текущий пароль неверен") from None

    await keycloak_client.set_password(
        user.keycloak_id, password=payload.new_password, temporary=False
    )
    # Обязательное действие выполнено — снимаем его и в Keycloak, и локально.
    await keycloak_client.update_required_actions(user.keycloak_id, remove=["UPDATE_PASSWORD"])
    user.must_change_password = False
    user.password_changed_at = dt.datetime.now(dt.UTC)
    await session.flush()
    await reset_rate_limit(str(user.id), "password:change")

    # Keycloak сам refresh-токены не инвалидирует — делаем это явно.
    await keycloak_client.logout_all_sessions(user.keycloak_id)
    terminated = await session_store.delete_all_for_user(user.id, except_sid=principal.session_id)

    voided = await get_signing_service().void_pending_for_user(
        session, user.id, reason=VOID_REASON_CREDENTIALS_CHANGED
    )

    if principal.session_id:
        current = await session_store.get(principal.session_id)
        if current is not None:
            # Переиздание sid — защита от session fixation (new_spec §4.4).
            rotated = await session_store.rotate_sid(current)
            response.set_cookie(
                key=settings.session_cookie_name,
                value=rotated.sid,
                max_age=settings.session_ttl,
                httponly=True,
                secure=settings.cookies_secure,
                samesite="lax",
                path="/",
            )
            csrf_token = new_csrf_token()
            set_csrf_cookie(response, csrf_token)

    await identity.record_security_event(
        SecurityEventType.PASSWORD_CHANGED,
        user_id=user.id,
        severity=Severity.WARNING,
        details={"sessions_terminated": terminated, "signature_requests_voided": voided},
    )
    await get_notification_service().notify_user(
        session,
        recipient_id=user.id,
        template_code=TPL_PASSWORD_CHANGED,
        priority=NotificationPriority.HIGH,
        payload={
            "changed_at": user.password_changed_at.isoformat(),
            "ip": request.headers.get("X-Real-Ip"),
        },
    )
    await AuditService(session).record(
        AuditAction.PASSWORD_CHANGED,
        entity_type="user",
        entity_id=user.id,
        changes={
            # Значения паролей в аудит не попадают — только последствия.
            "sessions_terminated": {"old": None, "new": terminated},
            "signature_requests_voided": {"old": None, "new": voided},
        },
    )

    return PasswordChangeResponse(
        sessions_terminated=terminated,
        signature_requests_voided=voided,
        password_changed_at=user.password_changed_at,
    )
