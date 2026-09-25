"""FastAPI-зависимости: сессия БД, аутентификация, права, If-Match.

Аутентификация принимает два источника токена:
  * сессионную cookie (основной путь для браузера через BFF);
  * заголовок `Authorization: Bearer` (сервисные вызовы и INTEGRATION).

Второй путь ограничен настройкой `allow_bearer_auth`: в prod через него
ходит только роль INTEGRATION, иначе обходился бы весь BFF-контур из
new_spec §3.1 — серверная сессия, idle-таймаут, CSRF и завершение сессий.

В обоих случаях JWT проверяется локально по кэшированному JWKS.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import (
    CachedPrincipal,
    get_principal_cache,
    invalidate_principal,
    set_principal_cache,
)
from app.core.config import Settings, get_settings
from app.core.context import ActorContext, set_actor
from app.core.csrf import verify_csrf
from app.core.db import get_db_session, run_after_commit
from app.core.errors import (
    AppError,
    ErrorCode,
    FieldError,
    UnauthenticatedError,
    ValidationError,
)
from app.core.pagination import PageParams
from app.core.permissions import Permission, has_permission, scopes_for
from app.core.security import Principal, TokenClaims, decode_access_token
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService, record_denied_and_commit
from app.modules.identity.models import Role, UserStatus
from app.modules.identity.service import IdentityService
from app.modules.identity.session_store import (
    SessionData,
    ensure_fresh_access_token,
    session_store,
)

DbSession = Annotated[AsyncSession, Depends(get_db_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

# Статусы, при которых доступ закрыт немедленно, даже если токен ещё живой.
_DENIED_STATUSES = frozenset(
    {UserStatus.BLOCKED.value, UserStatus.TERMINATED.value, UserStatus.ANONYMIZED.value}
)


async def get_audit_service(session: DbSession) -> AuditService:
    return AuditService(session)


async def get_identity_service(session: DbSession) -> IdentityService:
    return IdentityService(session)


AuditDep = Annotated[AuditService, Depends(get_audit_service)]
IdentityDep = Annotated[IdentityService, Depends(get_identity_service)]


def _bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


async def _resolve_from_cache(claims: TokenClaims) -> CachedPrincipal | None:
    """Профиль из `cache:perm:{user_id}` — чтобы не читать БД на каждый запрос.

    Кэш живёт 5 минут и сбрасывается любой операцией, меняющей роль, статус
    или эпоху прав (раздел 16), поэтому блокировка срабатывает сразу.
    """
    cached = await get_principal_cache(claims.subject)
    if cached is None:
        return None
    # Токен мог быть выпущен раньше повышения эпохи прав: такую запись
    # проверяем по БД, а не по кэшу.
    token_epoch = claims.raw.get("perm_epoch")
    if token_epoch is not None and int(token_epoch) < cached.perm_epoch:
        return None
    return cached


async def get_principal(
    request: Request,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """Основная зависимость аутентификации.

    Порядок: достаём токен (обновляя его по refresh, если истёк), валидируем
    локально, выполняем JIT provisioning, проверяем статус и эпоху прав.
    Актор кладётся в контекст, чтобы аудит и логи подхватили его без передачи
    через аргументы.
    """
    settings = get_settings()
    sid: str | None = None
    stored: SessionData | None = None

    token = _bearer_token(authorization)
    if token is not None and settings.bearer_auth_mode == "off":
        raise UnauthenticatedError(
            "Прямая аутентификация по Bearer отключена: используйте вход через BFF"
        )

    if token is None:
        sid = request.cookies.get(settings.session_cookie_name)
        if not sid:
            raise UnauthenticatedError("Сессия не найдена: выполните вход")
        stored = await session_store.get(sid)
        if stored is None:
            raise UnauthenticatedError("Сессия истекла или была завершена")

        # Сессионная аутентификация = cookie, значит нужен double-submit токен.
        verify_csrf(
            method=request.method,
            cookie_value=request.cookies.get(settings.csrf_cookie_name),
            header_value=request.headers.get(settings.csrf_header_name),
        )
        token = await ensure_fresh_access_token(stored)

    claims = await decode_access_token(token)

    cached = await _resolve_from_cache(claims)
    if cached is not None:
        principal = _principal_from_cache(cached, claims, sid)
    else:
        identity = IdentityService(session)
        user = await identity.provision_from_claims(claims)
        entry = CachedPrincipal(
            user_id=str(user.id),
            keycloak_id=user.keycloak_id or claims.subject,
            role=user.role,
            status=user.status,
            email=user.email,
            full_name=user.effective_name,
            team_id=str(user.team_id) if user.team_id else None,
            manager_id=str(user.manager_id) if user.manager_id else None,
            perm_epoch=user.perm_epoch,
            consent_version=user.consent_version,
            must_change_password=user.must_change_password,
        )
        principal = _principal_from_cache(entry, claims, sid)
        if principal.status not in _DENIED_STATUSES:
            # В кэш — только после коммита запроса. JIT-пользователь создан в этой же
            # транзакции: отклонят запрос (403 CRM-1105 без согласия) — строка `users`
            # откатится, а запись в Redis осталась бы указывать на неё.
            run_after_commit(session, lambda: set_principal_cache(entry))

    # Блокировка и увольнение должны отсекать пользователя немедленно,
    # даже если его токен ещё формально живой.
    if principal.status in _DENIED_STATUSES:
        await invalidate_principal(principal.user_id, keycloak_id=principal.keycloak_id)
        if sid:
            await session_store.delete_all_for_user(principal.user_id)
        raise AppError(
            ErrorCode.USER_BLOCKED,
            "Учётная запись заблокирована или деактивирована",
            extra={"status": principal.status},
        )

    # Эпоха прав: понижение роли обесценивает ранее выданный токен.
    token_epoch = claims.raw.get("perm_epoch")
    if token_epoch is not None and int(token_epoch) < principal.perm_epoch:
        raise AppError(
            ErrorCode.TOKEN_STALE,
            "Права пользователя изменились, обновите токен и повторите запрос",
            extra={"token_epoch": int(token_epoch), "current_epoch": principal.perm_epoch},
        )

    if (
        settings.bearer_auth_mode == "integration_only"
        and sid is None
        and principal.role != Role.INTEGRATION.value
    ):
        raise UnauthenticatedError(
            "В этом профиле Bearer-доступ разрешён только сервисной роли INTEGRATION"
        )

    set_actor(ActorContext(user_id=principal.user_id, role=principal.role, session_id=sid))

    if stored is not None:
        # Продлевает сессию и двигает idle-таймаут (new_spec §3.1).
        await session_store.touch(stored)

    return principal


def _principal_from_cache(
    entry: CachedPrincipal, claims: TokenClaims, sid: str | None
) -> Principal:
    return Principal(
        user_id=uuid.UUID(entry.user_id),
        keycloak_id=entry.keycloak_id,
        role=entry.role,
        status=entry.status,
        email=entry.email,
        full_name=entry.full_name,
        team_id=uuid.UUID(entry.team_id) if entry.team_id else None,
        manager_id=uuid.UUID(entry.manager_id) if entry.manager_id else None,
        perm_epoch=entry.perm_epoch,
        session_id=sid,
        consent_version=entry.consent_version,
        must_change_password=entry.must_change_password,
        claims=claims,
    )


CurrentUser = Annotated[Principal, Depends(get_principal)]


async def get_consented_principal(principal: CurrentUser, session: DbSession) -> Principal:
    """Для бизнес-ручек: без согласия на ПДн и с просроченным паролем доступа нет.

    Требования: раздел 6.1 (CRM-1105) и new_spec §4.4 «Ситуация B» —
    невыполненное обязательное действие смены пароля должно отвергать
    запросы, а не только показывать баннер во фронтенде.
    """
    identity = IdentityService(session)
    policy = await identity.current_policy()
    if principal.consent_version != policy.version:
        raise AppError(
            ErrorCode.CONSENT_REQUIRED,
            "Требуется принять действующую политику обработки персональных данных",
            extra={"policy_version": policy.version},
        )
    if principal.must_change_password:
        raise AppError(
            ErrorCode.PASSWORD_CHANGE_REQUIRED,
            "Требуется сменить пароль перед продолжением работы",
        )
    return principal


ConsentedUser = Annotated[Principal, Depends(get_consented_principal)]


def require_permission(*permissions: Permission):
    """Первый уровень проверки прав: маршрут и базовое право (раздел 4).

    Объектный уровень и SQL-скоуп реализуются в сервисах и репозиториях —
    полагаться только на эту проверку нельзя.
    """

    async def dependency(principal: ConsentedUser, session: DbSession) -> Principal:
        missing = [p for p in permissions if not has_permission(principal.role, p)]
        if missing:
            # Отказ обязан попасть в журнал (раздел 18), но исключение ниже
            # откатит транзакцию запроса вместе с записью. Поэтому запись
            # фиксируется сразу: бизнес-изменений к этому моменту ещё нет.
            await record_denied_and_commit(
                session,
                AuditAction.ACCESS_DENIED,
                entity_type="user",
                entity_id=principal.user_id,
                changes={
                    "required": [p.value for p in missing],
                    "role": principal.role,
                },
            )
            raise AppError(
                ErrorCode.FORBIDDEN,
                "Недостаточно прав для выполнения операции",
                extra={
                    "required": [p.value for p in permissions],
                    "granted": scopes_for(principal.role),
                },
            )
        return principal

    return dependency


async def get_if_match(
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> int:
    """Оптимистичная блокировка: `If-Match` обязателен для обновлений."""
    if if_match is None:
        raise ValidationError(
            "Требуется заголовок If-Match с текущей версией объекта",
            [FieldError(field="If-Match", reason="заголовок обязателен")],
        )
    value = if_match.strip().strip('"').removeprefix("W/").strip('"')
    if not value.isdigit():
        raise ValidationError(
            "If-Match должен содержать числовую версию объекта",
            [FieldError(field="If-Match", reason="ожидается целое число")],
        )
    return int(value)


IfMatch = Annotated[int, Depends(get_if_match)]


async def get_idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str | None:
    if idempotency_key is not None and not (8 <= len(idempotency_key) <= 255):
        raise ValidationError(
            "Idempotency-Key должен быть длиной от 8 до 255 символов",
            [FieldError(field="Idempotency-Key", reason="недопустимая длина")],
        )
    return idempotency_key


IdempotencyKeyHeader = Annotated[str | None, Depends(get_idempotency_key)]


async def get_page_params(
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> PageParams:
    return PageParams(limit=limit, cursor=cursor)


Pagination = Annotated[PageParams, Depends(get_page_params)]


async def db_session_iterator() -> AsyncIterator[AsyncSession]:
    async for session in get_db_session():
        yield session
