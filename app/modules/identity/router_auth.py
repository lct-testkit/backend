"""Эндпоинты аутентификации (BFF-слой).

Браузер получает только httpOnly + Secure + SameSite=Lax cookie с
идентификатором сессии. Токены остаются на сервере в Redis и никогда не
попадают в JavaScript-контекст.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from typing import Annotated

import structlog
from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import RedirectResponse

from app.core.config import get_settings
from app.core.context import ActorContext, get_client, set_actor
from app.core.deps import DbSession
from app.core.errors import AppError, ErrorCode, UnauthenticatedError
from app.core.permissions import scopes_for
from app.core.redis_client import require_redis
from app.core.security import decode_access_token, decode_logout_token
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.identity.keycloak import keycloak_client
from app.modules.identity.schemas import (
    AuthCallbackRequest,
    AuthCallbackResponse,
    BackchannelLogoutRequest,
    LogoutRequest,
    MeResponse,
    OperationResult,
)
from app.modules.identity.service import IdentityService
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
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
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
        # В dev по http Secure-cookie браузер не примет, поэтому только в prod/demo.
        secure=settings.app_profile != "dev",
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    settings = get_settings()
    response.delete_cookie(key=settings.session_cookie_name, path="/")


async def _build_me(identity: IdentityService, user, *, consent_required: bool) -> MeResponse:
    return MeResponse(
        id=user.id,
        full_name=user.full_name,
        display_name=user.display_name,
        email=user.email,
        role=user.role,
        team_id=user.team_id,
        manager_id=user.manager_id,
        status=user.status,
        locale=user.locale,
        timezone=user.timezone,
        consent_version=user.consent_version,
        consent_required=consent_required,
        scopes=scopes_for(user.role),
        teams=[user.team_id] if user.team_id else [],
        perm_epoch=user.perm_epoch,
        last_login_at=user.last_login_at,
    )


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
                "next": next_url,
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

    if stored_state is None and code_verifier is None:
        # State не найден и BFF не передал верификатор — поток не подтверждается.
        raise AppError(ErrorCode.UNAUTHENTICATED, "Некорректный или истёкший state")

    verifier = code_verifier or (stored_state or {}).get("code_verifier")
    resolved_redirect = (
        redirect_uri or (stored_state or {}).get("redirect_uri") or _default_redirect_uri()
    )

    tokens = await keycloak_client.exchange_code(
        code=code, redirect_uri=resolved_redirect, code_verifier=verifier
    )
    claims = await decode_access_token(tokens.access_token)

    identity = IdentityService(session)
    audit = AuditService(session)

    user = await identity.provision_from_claims(claims)
    await identity.mark_login(user)

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

    consent_required = identity.consent_required(user)
    payload = AuthCallbackResponse(
        user=await _build_me(identity, user, consent_required=consent_required),
        consent_required=consent_required,
        session_expires_in=get_settings().session_ttl,
    )
    return payload, stored_session.sid, (stored_state or {}).get("next")


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
    _, sid, next_url = await _complete_login(
        session=session, code=code, state=state, code_verifier=None, redirect_uri=None
    )
    target = next_url or get_settings().base_url
    redirect = RedirectResponse(target, status_code=status.HTTP_303_SEE_OTHER)
    _set_session_cookie(redirect, sid)
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
        return OperationResult(ok=True, detail="Активная сессия не найдена")

    stored = await session_store.delete(sid)
    _clear_session_cookie(response)

    if stored:
        set_actor(ActorContext(user_id=None, role=None, session_id=sid))
        if not payload.local_only and stored.refresh_token:
            await keycloak_client.logout(stored.refresh_token)
        audit = AuditService(session)
        await audit.record(
            AuditAction.LOGOUT,
            entity_type="user",
            entity_id=None,
            changes={"local_only": {"old": None, "new": payload.local_only}},
        )
    return OperationResult(ok=True, detail="Сессия завершена")


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
