"""Ручки импорта каталогов (раздел 4.12): загрузка, профиль, маппинг,
dry-run, применение, откат."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status
from sqlalchemy import select

from app.core.deps import DbSession, Pagination, require_permission
from app.core.pagination import Page, keyset_before
from app.core.permissions import Permission
from app.core.security import Principal
from app.modules.imports.models import ImportJob, ImportPreset
from app.modules.imports.schemas import (
    ImportJobCreateRequest,
    ImportJobListResponse,
    ImportJobOut,
    ImportMappingRequest,
    ImportPresetListResponse,
    ImportPresetOut,
    ImportProfileResponse,
)
from app.modules.imports.service import ImportService

import_jobs_router = APIRouter(prefix="/imports", tags=["imports"])
import_presets_router = APIRouter(prefix="/import-presets", tags=["imports"])

ImportRunPerm = Annotated[Principal, Depends(require_permission(Permission.IMPORT_RUN))]
ImportRollbackPerm = Annotated[Principal, Depends(require_permission(Permission.IMPORT_ROLLBACK))]


@import_jobs_router.post(
    "",
    summary="Создать задание импорта",
    description=(
        "Тело содержит `file_id` уже загруженного и подтверждённого файла "
        "(см. `/api/files/upload-intent` + `/commit`), тип сущности, стратегию "
        "обработки дублей и формат файла. Роль: HEAD, ADMIN."
    ),
    response_model=ImportJobOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_import_job(
    payload: ImportJobCreateRequest, session: DbSession, principal: ImportRunPerm
) -> ImportJobOut:
    job = await ImportService(session).create_job(
        principal,
        file_id=payload.file_id,
        entity_type=payload.entity_type,
        mode=payload.mode,
        source_format=payload.source_format,
    )
    return ImportJobOut.model_validate(job)


@import_jobs_router.get("", summary="Список заданий импорта", response_model=ImportJobListResponse)
async def list_import_jobs(
    session: DbSession, page: Pagination, _: ImportRunPerm
) -> ImportJobListResponse:
    stmt = (
        ImportService(session)
        .list_query()
        .order_by(ImportJob.created_at.desc(), ImportJob.id.desc())
    )
    cursor = page.decoded_cursor
    if cursor:
        stmt = stmt.where(keyset_before(ImportJob.created_at, ImportJob.id, cursor))
    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())
    built: Page = Page.build(rows, limit=page.limit, serializer=ImportJobOut.model_validate)
    return ImportJobListResponse(items=built.items, next_cursor=built.next_cursor)


@import_jobs_router.get(
    "/{job_id}", summary="Карточка задания импорта", response_model=ImportJobOut
)
async def get_import_job(
    session: DbSession, _: ImportRunPerm, job_id: Annotated[uuid.UUID, Path()]
) -> ImportJobOut:
    job = await ImportService(session).get_or_404(job_id)
    return ImportJobOut.model_validate(job)


@import_jobs_router.get(
    "/{job_id}/profile",
    summary="Профиль файла: первые строки и подсказка маппинга",
    description="Раздел 4.12, фаза 2-3: превью первых 100 строк и автоподбор колонок.",
    response_model=ImportProfileResponse,
)
async def profile_import_job(
    session: DbSession, _: ImportRunPerm, job_id: Annotated[uuid.UUID, Path()]
) -> ImportProfileResponse:
    job = await ImportService(session).get_or_404(job_id)
    result = await ImportService(session).profile(job)
    return ImportProfileResponse(
        headers=result.headers,
        sample_rows=result.sample_rows,
        suggested_mapping=result.suggested_mapping,
        total_rows=result.total_rows,
    )


@import_jobs_router.put(
    "/{job_id}/mapping",
    summary="Сохранить маппинг колонок",
    description="Опционально сохраняет маппинг как пресет для повторного использования.",
    response_model=ImportJobOut,
)
async def save_import_mapping(
    payload: ImportMappingRequest,
    session: DbSession,
    principal: ImportRunPerm,
    job_id: Annotated[uuid.UUID, Path()],
) -> ImportJobOut:
    service = ImportService(session)
    job = await service.get_or_404(job_id)
    job = await service.save_mapping(
        job, mapping=payload.mapping, principal=principal, save_as_preset=payload.save_as_preset
    )
    return ImportJobOut.model_validate(job)


@import_jobs_router.post(
    "/{job_id}/dry-run",
    summary="Проверка без записи",
    description=(
        "Валидирует все строки: типы, форматы, дубли внутри файла, коллизии с БД, "
        "обогащение по ИНН из локального ЕГРЮЛ. Отчёт — в счётчиках задания и "
        "скачиваемом файле ошибок (`result_file_id`)."
    ),
    response_model=ImportJobOut,
)
async def dry_run_import_job(
    session: DbSession, _: ImportRunPerm, job_id: Annotated[uuid.UUID, Path()]
) -> ImportJobOut:
    service = ImportService(session)
    job = await service.get_or_404(job_id)
    job = await service.dry_run(job)
    return ImportJobOut.model_validate(job)


@import_jobs_router.post(
    "/{job_id}/apply",
    summary="Применить импорт",
    description=(
        "Переводит задание в статус `applying`; построчная обработка батчами по "
        "500 с чекпоинтом продолжается фоновой задачей. Прогресс — в счётчиках "
        "и статусе задания при повторном GET."
    ),
    response_model=ImportJobOut,
)
async def apply_import_job(
    session: DbSession, _: ImportRunPerm, job_id: Annotated[uuid.UUID, Path()]
) -> ImportJobOut:
    service = ImportService(session)
    job = await service.get_or_404(job_id)
    job = await service.start_apply(job)
    return ImportJobOut.model_validate(job)


@import_jobs_router.post(
    "/{job_id}/rollback",
    summary="Откатить импорт",
    description=(
        "Созданные импортом записи помечаются удалёнными (если не заблокированы "
        "связанными сделками), изменённые — восстанавливаются из `before_snapshot`. "
        "Роль: HEAD, ADMIN."
    ),
    response_model=ImportJobOut,
)
async def rollback_import_job(
    session: DbSession, _: ImportRollbackPerm, job_id: Annotated[uuid.UUID, Path()]
) -> ImportJobOut:
    service = ImportService(session)
    job = await service.get_or_404(job_id)
    job = await service.start_rollback(job)
    return ImportJobOut.model_validate(job)


@import_presets_router.get(
    "", summary="Список пресетов маппинга", response_model=ImportPresetListResponse
)
async def list_import_presets(
    session: DbSession,
    page: Pagination,
    _: ImportRunPerm,
    entity_type: Annotated[str | None, Query()] = None,
) -> ImportPresetListResponse:
    stmt = select(ImportPreset).order_by(ImportPreset.created_at.desc(), ImportPreset.id.desc())
    if entity_type:
        stmt = stmt.where(ImportPreset.entity_type == entity_type)
    cursor = page.decoded_cursor
    if cursor:
        stmt = stmt.where(keyset_before(ImportPreset.created_at, ImportPreset.id, cursor))
    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())
    built: Page = Page.build(rows, limit=page.limit, serializer=ImportPresetOut.model_validate)
    return ImportPresetListResponse(items=built.items, next_cursor=built.next_cursor)
