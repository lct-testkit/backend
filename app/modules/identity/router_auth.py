"""Эндпоинты аутентификации (BFF-слой).

Браузер получает только httpOnly + Secure + SameSite=Lax cookie с
идентификатором сессии. Токены остаются на сервере в Redis и никогда не
попадают в JavaScript-контекст.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import secrets
import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Path, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select

from app.core.cache import invalidate_principal, invalidate_principal_after_commit
from app.core.config import get_settings
from app.core.context import ActorContext, get_client, set_actor
from app.core.csrf import clear_csrf_cookie, new_csrf_token, set_csrf_cookie
from app.core.deps import DbSession
from app.core.errors import AppError, ErrorCode, UnauthenticatedError
from app.core.masking import mask_email
from app.core.rate_limit import enforce as rate_limit
from app.core.redis_client import require_redis
from app.core.security import decode_access_token, decode_id_token, decode_logout_token
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.identity.keycloak import keycloak_client
from app.modules.identity.models import User, UserInvite
from app.modules.identity.redirects import safe_next_path
from app.modules.identity.router_me import build_me
from app.modules.identity.schemas import (
    AuthCallbackRequest,
    AuthCallbackResponse,
    BackchannelLogoutRequest,
    InviteCheckResponse,
    LogoutRequest,
    OperationResult,
)
from app.modules.identity.service import IdentityService, hash_token
from app.modules.identity.session_store import session_store

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# Состояние OIDC-потока живёт коротко: только между redirect и callback.
_OIDC_STATE_TTL = 600


def _state_key(state: str) -> str:
    return f"oidc:state:{state}"


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    )
    return verifier, challenge


def _default_redirect_uri() -> str:
    return f"{get_settings().base_url.rstrip('/')}/api/auth/callback"


def _set_session_cookie(response: Response, sid: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=settings.session_cookie_name,
        value=sid,
        max_age=settings.session_ttl,
        httponly=True,
        # Secure по реальной схеме install.sh-адреса (base_url), не по профилю — cookies_secure.
        secure=settings.cookies_secure,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(key=settings.session_cookie_name, path="/")


@router.get(
    "/login",
    summary="Начать OIDC-поток",
    description=(
        "Редирект на Keycloak. State, nonce и PKCE-верификатор сохраняются в Redis "
        "и проверяются в callback. Роль: доступно без аутентификации."
    ),
    status_code=status.HTTP_307_TEMPORARY_REDIRECT,
)
async def login(
    next_url: Annotated[str | None, Query(alias="next")] = None,
) -> RedirectResponse:
    redis = await require_redis()
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier, challenge = _pkce_pair()
    redirect_uri = _default_redirect_uri()

    await redis.setex(
        _state_key(state),
        _OIDC_STATE_TTL,
        json.dumps(
            {
                "nonce": nonce,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
                "next": safe_next_path(next_url),
            }
        ),
    )

    url = keycloak_client.authorization_url(
        redirect_uri=redirect_uri,
        state=state,
        nonce=nonce,
        code_challenge=challenge,
    )
    return RedirectResponse(url, status_code=status.HTTP_307_TEMPORARY_REDIRECT)


async def _sync_password_requirement(session: DbSession, user: User) -> None:
    """Снимает локальный флаг обязательной смены пароля, если пароль уже сменили в самом Keycloak.

    Флаг ставится при создании учётки и сбросе пароля, а снимался только при смене через CRM
    (`POST /me/password`). Пользователь, выполнивший `UPDATE_PASSWORD` на странице Keycloak (там
    его и отправляют письмом), входил снова и упирался в CRM-1106 навсегда. Источник истины —
    список обязательных действий в Keycloak; недоступность Admin API вход не ломает."""
    try:
        kc_user = await keycloak_client.get_user(user.keycloak_id)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 — вход важнее сверки
        logger.warning("password_requirement_sync_failed", user_id=str(user.id))
        return
    if kc_user is None or "UPDATE_PASSWORD" in (kc_user.get("requiredActions") or []):
        return
    user.must_change_password = False
    user.password_changed_at = dt.datetime.now(dt.UTC)
    await session.flush()
    invalidate_principal_after_commit(session, user.id, keycloak_id=user.keycloak_id)


async def _complete_login(
    *,
    session: DbSession,
    code: str,
    state: str,
    code_verifier: str | None,
    redirect_uri: str | None,
) -> tuple[AuthCallbackResponse, str, str | None]:
    """Общая часть POST- и GET-варианта callback. Возвращает ответ, sid и next-url."""
    redis = await require_redis()
    raw_state = await redis.getdel(_state_key(state))
    stored_state = json.loads(raw_state) if raw_state else None

    if stored_state is None:
        # `state` обязателен всегда. Раньше его отсутствие прощалось, если
        # клиент сам прислал `code_verifier`, — и это был готовый login-CSRF:
        # жертву можно было посадить в чужую сессию, подсунув свой код.
        raise AppError(ErrorCode.UNAUTHENTICATED, "Некорректный или истёкший state")

    # Верификатор из Redis — источник истины; параметр принимается только
    # если BFF хранит его у себя и в Redis его нет.
    verifier = stored_state.get("code_verifier") or code_verifier
    resolved_redirect = stored_state.get("redirect_uri") or redirect_uri or _default_redirect_uri()

    tokens = await keycloak_client.exchange_code(
        code=code, redirect_uri=resolved_redirect, code_verifier=verifier
    )

    # id_token проверяется полностью: подпись, iss, aud, exp и nonce. Без
    # сверки nonce ответ авторизации можно переиграть (new_spec §3.1 п.4).
    if not tokens.id_token:
        raise AppError(ErrorCode.UNAUTHENTICATED, "Keycloak не вернул id_token")
    id_claims = await decode_id_token(tokens.id_token, nonce=stored_state.get("nonce"))

    claims = await decode_access_token(tokens.access_token)
    if id_claims.get("sub") != claims.subject:
        # Токены обязаны описывать одного субъекта.
        raise AppError(ErrorCode.UNAUTHENTICATED, "id_token и access_token выданы разным субъектам")

    identity = IdentityService(session)
    audit = AuditService(session)

    user = await identity.provision_from_claims(claims)
    await identity.mark_login(user)
    if user.must_change_password and user.keycloak_id:
        await _sync_password_requirement(session, user)

    # Актор нужен до записи аудита, иначе событие входа будет анонимным.
    set_actor(ActorContext(user_id=user.id, role=user.role))

    client = get_client()
    stored_session = await session_store.create(
        user_id=user.id,
        keycloak_id=claims.subject,
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
        id_token=tokens.id_token,
        kc_session_state=claims.session_state or tokens.session_state,
        ip=client.ip if client else None,
        user_agent=client.user_agent if client else None,
        access_expires_at=tokens.access_expires_at,
    )

    await audit.record(
        AuditAction.LOGIN_SUCCEEDED,
        entity_type="user",
        entity_id=user.id,
        changes={"method": {"old": None, "new": "oidc"}},
    )

    me = await build_me(session, user)
    payload = AuthCallbackResponse(
        user=me,
        consent_required=me.consent_required,
        session_expires_in=get_settings().session_ttl,
        # Вторая половина double-submit: фронтенд обязан вернуть это значение
        # заголовком на каждом мутирующем запросе.
        csrf_token=new_csrf_token(),
    )
    return payload, stored_session.sid, stored_state.get("next")


@router.post(
    "/callback",
    summary="Завершить OIDC-поток",
    description=(
        "Обменивает код на токены, валидирует id_token, создаёт серверную сессию "
        "и ставит httpOnly-cookie. Если пользователь появился впервые, выполняется "
        "just-in-time provisioning. Роль: доступно без аутентификации."
    ),
    response_model=AuthCallbackResponse,
)
async def callback(
    payload: AuthCallbackRequest, response: Response, session: DbSession
) -> AuthCallbackResponse:
    result, sid, _ = await _complete_login(
        session=session,
        code=payload.code,
        state=payload.state,
        code_verifier=payload.code_verifier,
        redirect_uri=payload.redirect_uri,
    )
    _set_session_cookie(response, sid)
    set_csrf_cookie(response, result.csrf_token)
    return result


@router.get(
    "/callback",
    summary="Завершить OIDC-поток (браузерный редирект)",
    description=(
        "Вариант для прямого редиректа из Keycloak, когда SvelteKit BFF ещё не "
        "подключён. Ставит cookie и возвращает пользователя на базовый URL."
    ),
    include_in_schema=False,
)
async def callback_redirect(
    session: DbSession,
    code: Annotated[str, Query()],
    state: Annotated[str, Query()],
) -> RedirectResponse:
    result, sid, next_url = await _complete_login(
        session=session, code=code, state=state, code_verifier=None, redirect_uri=None
    )
    target = next_url or get_settings().base_url
    redirect = RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
    _set_session_cookie(redirect, sid)
    set_csrf_cookie(redirect, result.csrf_token)
    return redirect


@router.post(
    "/logout",
    summary="Выйти из системы",
    description=(
        "Удаляет серверную сессию, очищает cookie и по умолчанию выполняет "
        "single logout в Keycloak. Роль: любой аутентифицированный пользователь."
    ),
    response_model=OperationResult,
)
async def logout(
    payload: LogoutRequest,
    request: Request,
    response: Response,
    session: DbSession,
) -> OperationResult:
    settings = get_settings()
    sid = request.cookies.get(settings.session_cookie_name)
    if not sid:
        # Идемпотентно: выход без сессии не является ошибкой.
        _clear_session_cookie(response)
        clear_csrf_cookie(response)
        return OperationResult(ok=True, detail="Активная сессия не найдена")

    stored = await session_store.delete(sid)
    _clear_session_cookie(response)
    clear_csrf_cookie(response)

    if stored:
        set_actor(ActorContext(user_id=uuid.UUID(stored.user_id), role=None, session_id=sid))
        if not payload.local_only and stored.refresh_token:
            await keycloak_client.logout(stored.refresh_token)
        # Раздел 16: кэш прав сбрасывается в том числе при выходе.
        await invalidate_principal(stored.user_id, keycloak_id=stored.keycloak_id)
        audit = AuditService(session)
        await audit.record(
            AuditAction.LOGOUT,
            entity_type="user",
            entity_id=uuid.UUID(stored.user_id),
            changes={"local_only": {"old": None, "new": payload.local_only}},
        )
    return OperationResult(ok=True, detail="Сессия завершена")


@router.get(
    "/invite/{token}",
    summary="Проверить приглашение",
    description=(
        "Проверяет одноразовую ссылку приглашения: в базе хранится только её "
        "sha256. Возвращает адрес входа и маскированный email. Ограничение "
        "частоты — защита от перебора токенов. Роль: доступно без аутентификации."
    ),
    response_model=InviteCheckResponse,
)
async def check_invite(
    session: DbSession,
    request: Request,
    token: Annotated[str, Path(min_length=16, max_length=128)],
) -> InviteCheckResponse:
    client = get_client()
    await rate_limit(
        client.ip or "unknown",
        "invite:check",
        limit=20,
        window_seconds=3600,
        detail="Слишком много попыток проверки приглашения",
        # Лимит — единственная защита от перебора токенов приглашения: без Redis не пускаем.
        fail_closed=True,
    )

    invite = (
        await session.execute(select(UserInvite).where(UserInvite.token_hash == hash_token(token)))
    ).scalar_one_or_none()
    if invite is None or not invite.is_active:
        # Единый ответ: ручка не подсказывает, существовал ли токен вообще.
        raise AppError(ErrorCode.NOT_FOUND, "Приглашение не найдено или срок его действия истёк")

    user = (
        await session.execute(select(User).where(User.id == invite.user_id))
    ).scalar_one_or_none()
    if user is None or user.deleted_at is not None:
        raise AppError(ErrorCode.NOT_FOUND, "Приглашение недействительно")

    return InviteCheckResponse(
        valid=True,
        email_masked=mask_email(user.email),
        full_name=user.full_name,
        expires_at=invite.expires_at,
        login_url=f"{get_settings().base_url.rstrip('/')}/api/auth/login",
    )


@router.post(
    "/backchannel-logout",
    summary="Backchannel logout от Keycloak",
    description=(
        "Принимает Logout Token, проверяет подпись и назначение, завершает "
        "связанные серверные сессии. Нужен, чтобы блокировка пользователя в "
        "Keycloak немедленно обрывала сессии в CRM."
    ),
    response_model=OperationResult,
)
async def backchannel_logout(
    payload: BackchannelLogoutRequest, session: DbSession
) -> OperationResult:
    claims = await decode_logout_token(payload.logout_token)
    subject = claims.get("sub")
    kc_session_state = claims.get("sid")

    identity = IdentityService(session)
    removed = 0

    if subject:
        user = await identity.get_by_keycloak_id(subject)
        if user:
            if kc_session_state:
                removed = await session_store.delete_by_kc_session_state(
                    kc_session_state, str(user.id)
                )
            if removed == 0:
                # Без sid Keycloak просит завершить все сессии субъекта.
                removed = await session_store.delete_all_for_user(user.id)
            set_actor(ActorContext(user_id=user.id, role=user.role))
            # Отдельного типа security_event для logout спецификация не вводит,
            # поэтому фиксируем факт только в аудите.
            await AuditService(session).record(
                AuditAction.SESSIONS_TERMINATED,
                entity_type="user",
                entity_id=user.id,
                changes={"sessions_removed": {"old": None, "new": removed}},
            )

    if not subject and not kc_session_state:
        raise UnauthenticatedError("Logout-токен не содержит субъекта или sid")

    logger.info("backchannel_logout", sessions_removed=removed)
    return OperationResult(ok=True, detail=f"Завершено сессий: {removed}")
