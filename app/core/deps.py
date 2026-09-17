"""FastAPI-зависимости: сессия БД, аутентификация, права, If-Match.

Аутентификация принимает два источника токена:
  * сессионную cookie (основной путь для браузера через BFF);
  * заголовок `Authorization: Bearer` (сервисные вызовы и INTEGRATION).

В обоих случаях JWT проверяется локально по кэшированному JWKS.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.context import ActorContext, set_actor
from app.core.db import get_db_session
from app.core.errors import (
    AppError,
    ErrorCode,
    FieldError,
    UnauthenticatedError,
    ValidationError,
)
from app.core.pagination import PageParams
from app.core.permissions import Permission, has_permission, scopes_for
from app.core.security import Principal, decode_access_token
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService, record_out_of_band
from app.modules.identity.models import UserStatus
from app.modules.identity.service import IdentityService
from app.modules.identity.session_store import session_store

DbSession = Annotated[AsyncSession, Depends(get_db_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


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


async def get_principal(
    request: Request,
    session: DbSession,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """Основная зависимость аутентификации.

    Порядок: достаём токен, валидируем локально, выполняем JIT provisioning,
    проверяем статус и эпоху прав. Актор кладётся в контекст, чтобы аудит и
    логи подхватили его без передачи через аргументы.
    """
    settings = get_settings()
    sid: str | None = None

    token = _bearer_token(authorization)
    if token is None:
        sid = request.cookies.get(settings.session_cookie_name)
        if not sid:
            raise UnauthenticatedError("Сессия не найдена: выполните вход")
        stored = await session_store.get(sid)
        if stored is None:
            raise UnauthenticatedError("Сессия истекла или была завершена")
        token = stored.access_token

    claims = await decode_access_token(token)

    identity = IdentityService(session)
    user = await identity.provision_from_claims(claims)

    # Блокировка и увольнение должны отсекать пользователя немедленно,
    # даже если его токен ещё формально живой.
    if user.status in (UserStatus.BLOCKED, UserStatus.TERMINATED, UserStatus.ANONYMIZED):
        raise AppError(
            ErrorCode.USER_BLOCKED,
            "Учётная запись заблокирована или деактивирована",
            extra={"status": user.status},
        )

    # Эпоха прав: понижение роли обесценивает ранее выданный токен.
    token_epoch = claims.raw.get("perm_epoch")
    if token_epoch is not None and int(token_epoch) < user.perm_epoch:
        raise AppError(
            ErrorCode.TOKEN_STALE,
            "Права пользователя изменились, обновите токен и повторите запрос",
            extra={"token_epoch": int(token_epoch), "current_epoch": user.perm_epoch},
        )

    principal = Principal(
        user_id=user.id,
        keycloak_id=user.keycloak_id or claims.subject,
        role=user.role,
        status=user.status,
        email=user.email,
        full_name=user.effective_name,
        team_id=user.team_id,
        perm_epoch=user.perm_epoch,
        session_id=sid,
        consent_required=identity.consent_required(user),
        claims=claims,
    )
    set_actor(
        ActorContext(user_id=user.id, role=user.role, session_id=sid)
    )
    return principal


CurrentUser = Annotated[Principal, Depends(get_principal)]


async def get_consented_principal(principal: CurrentUser) -> Principal:
    """Для бизнес-ручек: без согласия на обработку ПДн доступа к данным нет."""
    if principal.consent_required:
        raise AppError(
            ErrorCode.CONSENT_REQUIRED,
            "Требуется принять действующую политику обработки персональных данных",
            extra={"policy_version": get_settings().consent_policy_version},
        )
    return principal


ConsentedUser = Annotated[Principal, Depends(get_consented_principal)]


def require_permission(*permissions: Permission):
    """Первый уровень проверки прав: маршрут и базовое право (раздел 4).

    Объектный уровень и SQL-скоуп реализуются в сервисах и репозиториях —
    полагаться только на эту проверку нельзя.
    """

    async def dependency(principal: ConsentedUser) -> Principal:
        missing = [p for p in permissions if not has_permission(principal.role, p)]
        if missing:
            # Отказ в доступе тоже пишется в аудит, но в отдельной транзакции:
            # основная откатится вместе с исключением ниже.
            await record_out_of_band(
                AuditAction.ACCESS_DENIED,
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
