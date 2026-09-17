"""Профиль и сессии текущего пользователя (раздел 6.1).

`POST /api/me/password` реализуется в спринте identity вместе с аннулированием
незавершённых запросов подписи — без модуля signing эта ручка была бы
наполовину рабочей, поэтому здесь её нет.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Annotated, Any

from fastapi import APIRouter, Path, status
from sqlalchemy import select

from app.core.config import get_settings
from app.core.deps import CurrentUser, DbSession
from app.core.errors import AppError, ErrorCode, NotFoundError
from app.core.permissions import scopes_for
from app.core.redis_client import RECENT_MAX_ITEMS, get_redis, key_recent
from app.modules.identity.models import User
from app.modules.identity.schemas import (
    ConsentRequest,
    ConsentResponse,
    MeResponse,
    OperationResult,
    SessionInfo,
    SessionListResponse,
)
from app.modules.identity.service import IdentityService
from app.modules.identity.session_store import session_store

router = APIRouter(prefix="/me", tags=["me"])


@router.get(
    "",
    summary="Профиль текущего пользователя",
    description=(
        "Возвращает локальную проекцию пользователя и набор прав. Если требуется "
        "принять новую версию политики обработки ПДн, поднимается consent_required. "
        "Роль: любой аутентифицированный пользователь."
    ),
    response_model=MeResponse,
)
async def get_me(principal: CurrentUser, session: DbSession) -> MeResponse:
    user = (
        await session.execute(select(User).where(User.id == principal.user_id))
    ).scalar_one()
    identity = IdentityService(session)

    # Незавершённые required actions в Keycloak означают ограниченный доступ.
    required_actions = principal.claims.raw.get("required_actions") or []
    password_change_required = "UPDATE_PASSWORD" in required_actions

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
        consent_required=identity.consent_required(user),
        password_change_required=password_change_required,
        scopes=scopes_for(user.role),
        teams=[user.team_id] if user.team_id else [],
        perm_epoch=user.perm_epoch,
        last_login_at=user.last_login_at,
    )


@router.get(
    "/recent",
    summary="Последние открытые объекты",
    description=(
        "Возвращает до 20 последних открытых пользователем сделок, организаций, "
        "контактов и отчётов из Redis ZSET `recent:{user_id}`. "
        "Роль: любой аутентифицированный пользователь."
    ),
)
async def get_recent(principal: CurrentUser) -> dict[str, list[dict[str, Any]]]:
    try:
        raw_items = await get_redis().zrevrange(
            key_recent(principal.user_id), 0, RECENT_MAX_ITEMS - 1, withscores=True
        )
    except Exception:
        # Redis — не источник истины: пустой список лучше, чем ошибка (раздел 16).
        return {"items": []}

    items: list[dict[str, Any]] = []
    for member, score in raw_items:
        try:
            entry = json.loads(member)
        except ValueError:
            continue
        entry["opened_at"] = dt.datetime.fromtimestamp(score, dt.UTC).isoformat()
        items.append(entry)
    return {"items": items}


@router.get(
    "/sessions",
    summary="Активные сессии",
    description=(
        "Список серверных сессий пользователя из Redis: устройство, IP, User-Agent, "
        "время создания и последняя активность. Токены не возвращаются. "
        "Роль: любой аутентифицированный пользователь."
    ),
    response_model=SessionListResponse,
)
async def list_sessions(principal: CurrentUser) -> SessionListResponse:
    sessions = await session_store.list_for_user(principal.user_id)
    return SessionListResponse(
        items=[
            SessionInfo(
                **stored.public_view(),
                is_current=stored.sid == principal.session_id,
            )
            for stored in sessions
        ]
    )


@router.delete(
    "/sessions/{sid}",
    summary="Завершить сессию",
    description=(
        "Завершает конкретную сессию пользователя. Завершить можно только свою "
        "сессию: чужие завершаются через административный механизм. "
        "Роль: любой аутентифицированный пользователь."
    ),
    response_model=OperationResult,
)
async def delete_session(
    principal: CurrentUser,
    sid: Annotated[str, Path(description="Идентификатор сессии")],
) -> OperationResult:
    stored = await session_store.get(sid)
    if stored is None:
        raise NotFoundError("Сессия", sid)
    if stored.user_id != str(principal.user_id):
        # Не раскрываем существование чужой сессии деталями ошибки.
        raise AppError(ErrorCode.FORBIDDEN, "Можно завершать только свои сессии")

    await session_store.delete(sid)
    return OperationResult(ok=True, detail="Сессия завершена")


@router.post(
    "/consent",
    summary="Принять политику обработки ПДн",
    description=(
        "Создаёт запись в `consents` с версией политики, хэшем текста, IP и "
        "User-Agent. Без согласия доступ к данным закрыт (CRM-1105). "
        "Роль: любой аутентифицированный пользователь."
    ),
    response_model=ConsentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def accept_consent(
    payload: ConsentRequest, principal: CurrentUser, session: DbSession
) -> ConsentResponse:
    settings = get_settings()
    if payload.policy_version != settings.consent_policy_version:
        raise AppError(
            ErrorCode.VALIDATION,
            "Версия политики не совпадает с действующей",
            extra={"expected": settings.consent_policy_version},
        )

    user = (
        await session.execute(select(User).where(User.id == principal.user_id))
    ).scalar_one()
    identity = IdentityService(session)
    consent = await identity.accept_consent(
        user,
        policy_version=payload.policy_version,
        policy_text_hash=payload.policy_text_hash,
    )
    return ConsentResponse(
        accepted=True,
        policy_version=consent.policy_version,
        accepted_at=consent.accepted_at,  # type: ignore[arg-type]
    )
