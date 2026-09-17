"""Системные настройки, флаги и проверка целостности аудита (раздел 6.12).

Каждое изменение флага или настройки пишется в аудит в той же транзакции.
Секретные значения наружу в открытом виде не отдаются.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query
from sqlalchemy import select

from app.core.deps import AuditDep, DbSession, Pagination, require_permission
from app.core.errors import NotFoundError
from app.core.pagination import Page
from app.core.permissions import Permission
from app.core.security import Principal
from app.modules.admin.models import FeatureFlag, SystemSetting
from app.modules.admin.schemas import (
    AuditChainReport,
    FeatureFlagListResponse,
    FeatureFlagOut,
    FeatureFlagPatch,
    SystemSettingListResponse,
    SystemSettingOut,
    SystemSettingPut,
)
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService, diff_changes

router = APIRouter(prefix="/admin", tags=["admin"])

SECRET_PLACEHOLDER = "********"


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
        stmt = stmt.where(
            (FeatureFlag.created_at, FeatureFlag.id) < (cursor.as_datetime(), cursor.id)
        )

    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())
    built: Page = Page.build(rows, limit=page.limit, serializer=FeatureFlagOut.model_validate)
    return FeatureFlagListResponse(items=built.items, next_cursor=built.next_cursor)


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
        "значение секретной настройки в аудит не попадает. Роль: ADMIN."
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
    before = (
        {}
        if is_new
        else {"value": setting.value, "is_secret": setting.is_secret}  # type: ignore[union-attr]
    )

    if setting is None:
        setting = SystemSetting(key=key, value=payload.value)
        session.add(setting)
    else:
        setting.value = payload.value

    if payload.description is not None:
        setting.description = payload.description
    if payload.is_secret is not None:
        setting.is_secret = payload.is_secret
    setting.updated_by = principal.user_id
    await session.flush()

    after = {"value": setting.value, "is_secret": setting.is_secret}
    if setting.is_secret:
        # Само значение секрета в журнал не пишем — только факт изменения.
        before = {**before, "value": "***"} if before else {}
        after = {**after, "value": "***"}

    await audit.record(
        AuditAction.SYSTEM_SETTING_CHANGED,
        entity_type="system_setting",
        entity_id=None,
        changes={
            "key": {"old": None if is_new else key, "new": key},
            **diff_changes(before, after),
        },
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
