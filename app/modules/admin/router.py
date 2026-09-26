"""Системные настройки, флаги и проверка целостности аудита (раздел 6.12).

Каждое изменение флага или настройки пишется в аудит в той же транзакции.
Секретные значения наружу в открытом виде не отдаются.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Response, status
from sqlalchemy import select

from app.core.deps import AuditDep, DbSession, Pagination, require_permission
from app.core.errors import AppError, ErrorCode, NotFoundError
from app.core.pagination import Page, keyset_before
from app.core.permissions import Permission
from app.core.security import Principal
from app.modules.admin.models import FeatureFlag, SystemSetting
from app.modules.admin.schemas import (
    SECRET_PLACEHOLDER,
    AuditChainReport,
    AuditEntryOut,
    AuditListResponse,
    FeatureFlagCreate,
    FeatureFlagListResponse,
    FeatureFlagOut,
    FeatureFlagPatch,
    SystemSettingListResponse,
    SystemSettingOut,
    SystemSettingPut,
)
from app.modules.admin.setting_crypto import decrypt_value, encrypt_value
from app.modules.audit.actions import AuditAction
from app.modules.audit.models import AuditLog
from app.modules.audit.service import AuditFilters, AuditService, diff_changes
from app.modules.identity.models import Role, SecurityEventType, Severity
from app.modules.identity.service import IdentityService

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get(
    "/feature-flags",
    summary="Список флагов",
    description="Возвращает флаги функциональности с курсорной пагинацией. Роль: ADMIN.",
    response_model=FeatureFlagListResponse,
)
async def list_feature_flags(
    session: DbSession,
    page: Pagination,
    _: Annotated[Principal, Depends(require_permission(Permission.SETTINGS_WRITE))],
) -> FeatureFlagListResponse:
    stmt = select(FeatureFlag).order_by(FeatureFlag.created_at.desc(), FeatureFlag.id.desc())

    cursor = page.decoded_cursor
    if cursor:
        # Курсор по паре (created_at, id): OFFSET не используется.
        stmt = stmt.where(keyset_before(FeatureFlag.created_at, FeatureFlag.id, cursor))

    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())
    built: Page = Page.build(rows, limit=page.limit, serializer=FeatureFlagOut.model_validate)
    return FeatureFlagListResponse(items=built.items, next_cursor=built.next_cursor)


@router.post(
    "/feature-flags",
    summary="Создать флаг",
    description=(
        "Заводит флаг функциональности. Код уникален (повтор — 409). Изменение пишется "
        "в аудит. Роль: ADMIN."
    ),
    response_model=FeatureFlagOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_feature_flag(
    payload: FeatureFlagCreate,
    session: DbSession,
    audit: AuditDep,
    principal: Annotated[Principal, Depends(require_permission(Permission.SETTINGS_WRITE))],
) -> FeatureFlagOut:
    exists = await session.scalar(select(FeatureFlag.id).where(FeatureFlag.code == payload.code))
    if exists is not None:
        raise AppError(ErrorCode.DUPLICATE, f"Флаг {payload.code!r} уже существует")
    flag = FeatureFlag(
        code=payload.code,
        is_enabled=payload.is_enabled,
        description=payload.description,
        rollout=payload.rollout,
        updated_by=principal.user_id,
    )
    session.add(flag)
    await session.flush()
    await audit.record(
        AuditAction.FEATURE_FLAG_CHANGED,
        entity_type="feature_flag",
        entity_id=flag.id,
        changes=diff_changes(
            {},
            {
                "code": flag.code,
                "is_enabled": flag.is_enabled,
                "description": flag.description,
                "rollout": flag.rollout,
            },
        ),
    )
    return FeatureFlagOut.model_validate(flag)


@router.patch(
    "/feature-flags/{code}",
    summary="Изменить флаг",
    description=(
        "Включает, выключает или меняет процент раскатки флага. Изменение пишется "
        "в аудит. Роль: ADMIN."
    ),
    response_model=FeatureFlagOut,
)
async def patch_feature_flag(
    payload: FeatureFlagPatch,
    session: DbSession,
    audit: AuditDep,
    principal: Annotated[Principal, Depends(require_permission(Permission.SETTINGS_WRITE))],
    code: Annotated[str, Path(description="Код флага")],
) -> FeatureFlagOut:
    flag = (
        await session.execute(select(FeatureFlag).where(FeatureFlag.code == code))
    ).scalar_one_or_none()
    if flag is None:
        raise NotFoundError("Флаг", code)

    before = {
        "is_enabled": flag.is_enabled,
        "description": flag.description,
        "rollout": flag.rollout,
    }
    updates = payload.model_dump(exclude_unset=True)
    for field_name, value in updates.items():
        setattr(flag, field_name, value)
    flag.updated_by = principal.user_id
    await session.flush()

    after = {
        "is_enabled": flag.is_enabled,
        "description": flag.description,
        "rollout": flag.rollout,
    }
    await audit.record(
        AuditAction.FEATURE_FLAG_CHANGED,
        entity_type="feature_flag",
        entity_id=flag.id,
        changes=diff_changes(before, after),
    )
    return FeatureFlagOut.model_validate(flag)


def _readable(stored: Any) -> Any:
    """Прежнее значение настройки для сравнения; нечитаемое (нет ключа) — как «неизвестное»."""
    try:
        return decrypt_value(stored)
    except AppError:
        return None


@router.get(
    "/system-settings",
    summary="Список настроек",
    description=(
        "Возвращает системные настройки. Значения секретных настроек "
        "заменяются маркером. Роль: ADMIN."
    ),
    response_model=SystemSettingListResponse,
)
async def list_system_settings(
    session: DbSession,
    _: Annotated[Principal, Depends(require_permission(Permission.SETTINGS_WRITE))],
    prefix: Annotated[str | None, Query(description="Фильтр по префиксу ключа")] = None,
) -> SystemSettingListResponse:
    stmt = select(SystemSetting).order_by(SystemSetting.key)
    if prefix:
        stmt = stmt.where(SystemSetting.key.startswith(prefix))
    rows = (await session.execute(stmt)).scalars().all()

    return SystemSettingListResponse(
        items=[
            SystemSettingOut(
                key=row.key,
                value=SECRET_PLACEHOLDER if row.is_secret else row.value,
                description=row.description,
                is_secret=row.is_secret,
                updated_by=row.updated_by,
                updated_at=row.updated_at,
            )
            for row in rows
        ]
    )


@router.put(
    "/system-settings/{key}",
    summary="Изменить настройку",
    description=(
        "Создаёт или обновляет системную настройку. Изменение пишется в аудит; "
        "значение секретной настройки в аудит не попадает. Маркер `********`, которым "
        "секрет скрыт в списке, как значение не принимается (422). Роль: ADMIN."
    ),
    response_model=SystemSettingOut,
)
async def put_system_setting(
    payload: SystemSettingPut,
    session: DbSession,
    audit: AuditDep,
    principal: Annotated[Principal, Depends(require_permission(Permission.SETTINGS_WRITE))],
    key: Annotated[str, Path(description="Ключ настройки")],
) -> SystemSettingOut:
    setting = (
        await session.execute(select(SystemSetting).where(SystemSetting.key == key))
    ).scalar_one_or_none()

    is_new = setting is None
    # Для сравнения «было → стало» значения берутся расшифрованными: шифротекст каждый раз новый,
    # и запись того же значения выглядела бы изменением. В журнал секреты всё равно не попадают.
    before = (
        {}
        if is_new
        else {
            "value": _readable(setting.value),  # type: ignore[union-attr]
            "is_secret": setting.is_secret,  # type: ignore[union-attr]
        }
    )

    will_be_secret = (
        payload.is_secret if payload.is_secret is not None else bool(setting and setting.is_secret)
    )
    # Секретное значение ложится в БД зашифрованным (если задан SETTINGS_ENCRYPTION_KEY).
    stored_value = encrypt_value(payload.value) if will_be_secret else payload.value
    if setting is None:
        setting = SystemSetting(key=key, value=stored_value)
        session.add(setting)
    else:
        setting.value = stored_value

    if payload.description is not None:
        setting.description = payload.description
    if payload.is_secret is not None:
        setting.is_secret = payload.is_secret
    setting.updated_by = principal.user_id
    await session.flush()

    after = {"value": payload.value, "is_secret": setting.is_secret}
    changes: dict[str, Any] = {
        "key": {"old": None if is_new else key, "new": key},
        **diff_changes(before, after),
    }
    # Значение секрета в неизменяемый журнал не пишем ни в каком виде — ни новое, ни прежнее.
    # Смотрим и на прежний признак: при снятии «секретности» старое значение раньше уходило в
    # журнал открытым текстом. Сама смена значения остаётся видна маркером.
    was_secret = bool(before.get("is_secret"))
    if (was_secret or setting.is_secret) and "value" in changes:
        changes["value"] = {"old": "***" if not is_new else None, "new": "***"}
    await audit.record(
        AuditAction.SYSTEM_SETTING_CHANGED,
        entity_type="system_setting",
        entity_id=None,
        changes=changes,
    )

    return SystemSettingOut(
        key=setting.key,
        value=SECRET_PLACEHOLDER if setting.is_secret else setting.value,
        description=setting.description,
        is_secret=setting.is_secret,
        updated_by=setting.updated_by,
        updated_at=setting.updated_at,
    )


@router.get(
    "/audit",
    summary="Журнал аудита",
    description=(
        "Фильтры: актор, действие, сущность, результат, период, request_id. "
        "Курсорная пагинация. ADMIN и AUDITOR видят весь журнал, HEAD — только "
        "действия своей команды. Роль: ADMIN, AUDITOR, HEAD."
    ),
    response_model=AuditListResponse,
)
async def list_audit(
    session: DbSession,
    page: Pagination,
    principal: Annotated[Principal, Depends(require_permission(Permission.AUDIT_READ))],
    actor_id: Annotated[uuid.UUID | None, Query()] = None,
    action: Annotated[str | None, Query()] = None,
    entity_type: Annotated[str | None, Query()] = None,
    entity_id: Annotated[uuid.UUID | None, Query()] = None,
    result: Annotated[str | None, Query(description="success | denied | error")] = None,
    request_id: Annotated[str | None, Query()] = None,
    date_from: Annotated[dt.datetime | None, Query(alias="from")] = None,
    date_to: Annotated[dt.datetime | None, Query(alias="to")] = None,
) -> AuditListResponse:
    filters = AuditFilters(
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        result=result,
        request_id=request_id,
        date_from=date_from,
        date_to=date_to,
        actor_ids=await _audit_scope(session, principal),
    )
    stmt = (
        AuditService(session)
        .query(filters)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
    )
    cursor = page.decoded_cursor
    if cursor:
        stmt = stmt.where(keyset_before(AuditLog.created_at, AuditLog.id, cursor))

    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())
    built: Page = Page.build(rows, limit=page.limit, serializer=AuditEntryOut.model_validate)
    return AuditListResponse(items=built.items, next_cursor=built.next_cursor)


@router.get(
    "/audit/export",
    summary="Экспорт журнала аудита",
    description=(
        "Отдаёт журнал построчным JSON (NDJSON) по тем же фильтрам. Экспорт "
        "считается массовой выгрузкой: пишется событие безопасности MASS_EXPORT "
        "и запись аудита. Роль: ADMIN, AUDITOR."
    ),
)
async def export_audit(
    session: DbSession,
    audit: AuditDep,
    principal: Annotated[Principal, Depends(require_permission(Permission.AUDIT_EXPORT))],
    action: Annotated[str | None, Query()] = None,
    entity_type: Annotated[str | None, Query()] = None,
    result: Annotated[str | None, Query()] = None,
    date_from: Annotated[dt.datetime | None, Query(alias="from")] = None,
    date_to: Annotated[dt.datetime | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=100_000)] = 10_000,
) -> Response:
    filters = AuditFilters(
        action=action,
        entity_type=entity_type,
        result=result,
        date_from=date_from,
        date_to=date_to,
        actor_ids=await _audit_scope(session, principal),
    )
    stmt = (
        AuditService(session)
        .query(filters)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(limit)
    )
    rows = list((await session.execute(stmt)).scalars().all())

    # Выгрузка журнала — событие безопасности само по себе (раздел 18).
    await IdentityService(session).record_security_event(
        SecurityEventType.MASS_EXPORT,
        user_id=principal.user_id,
        severity=Severity.WARNING,
        details={"kind": "audit_export", "rows": len(rows)},
    )
    await audit.record(
        AuditAction.AUDIT_EXPORTED,
        entity_type="audit_log",
        entity_id=None,
        changes={
            "rows": {"old": None, "new": len(rows)},
            "filters": {
                "old": None,
                "new": {
                    "action": action,
                    "entity_type": entity_type,
                    "result": result,
                    "from": date_from.isoformat() if date_from else None,
                    "to": date_to.isoformat() if date_to else None,
                },
            },
        },
    )

    body = "\n".join(AuditEntryOut.model_validate(row).model_dump_json() for row in rows)
    return Response(
        content=body,
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="audit-export.ndjson"'},
    )


async def _audit_scope(session: DbSession, principal: Principal) -> list[uuid.UUID] | None:
    """Скоуп журнала по роли (матрица прав, часть 5 new_spec).

    ADMIN и AUDITOR видят всё. HEAD — только действия своей команды, включая
    собственные: иначе руководитель читал бы журнал всей организации.
    """
    if principal.role in (Role.ADMIN.value, Role.AUDITOR.value):
        return None
    if principal.role == Role.HEAD.value:
        if principal.team_id is None:
            return [principal.user_id]
        members = await IdentityService(session).team_member_ids(principal.team_id)
        return sorted({*members, principal.user_id})
    return [principal.user_id]


@router.get(
    "/audit/verify-chain",
    summary="Проверить цепочку аудита",
    description=(
        "Пересчитывает хэши хвоста журнала и проверяет связность цепочки. "
        "Позволяет обнаружить вырезанную или изменённую запись. Роль: ADMIN, AUDITOR."
    ),
    response_model=AuditChainReport,
)
async def verify_audit_chain(
    session: DbSession,
    _: Annotated[Principal, Depends(require_permission(Permission.AUDIT_READ))],
    limit: Annotated[int, Query(ge=1, le=10000)] = 1000,
) -> AuditChainReport:
    report = await AuditService(session).verify_chain(limit=limit)
    return AuditChainReport(**report)
