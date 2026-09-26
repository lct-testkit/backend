"""Ручки импорта каталогов (раздел 4.12): загрузка, профиль, маппинг,
dry-run, применение, откат."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status
from sqlalchemy import select

from app.core.deps import DbSession, Pagination, require_permission
from app.core.masking import mask_mapping
from app.core.pagination import Page, keyset_after, keyset_before
from app.core.permissions import Permission
from app.core.security import Principal
from app.modules.imports.fields import (
    ENTITY_TYPE_LABELS,
    fields_for,
    requirements_for,
)
from app.modules.imports.models import ImportJob, ImportPreset, ImportRowResult
from app.modules.imports.schemas import (
    ImportEntityTypeListResponse,
    ImportEntityTypeOut,
    ImportFieldOut,
    ImportJobCreateRequest,
    ImportJobListResponse,
    ImportJobOut,
    ImportMappingRequest,
    ImportPresetListResponse,
    ImportPresetOut,
    ImportProfileResponse,
    ImportRowListResponse,
    ImportRowOut,
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


@import_jobs_router.get(
    "/entity-types",
    summary="Типы импорта и их поля",
    description=(
        "Что можно импортировать: для каждого типа — поля для сопоставления колонок, допустимые "
        "форматы файла и то, что обязательно должно быть в маппинге. Окно сопоставления строится "
        "по этому ответу, списки полей на клиенте не хранятся."
    ),
    response_model=ImportEntityTypeListResponse,
)
async def list_import_entity_types(_: ImportRunPerm) -> ImportEntityTypeListResponse:
    return ImportEntityTypeListResponse(
        items=[
            ImportEntityTypeOut(
                code=code,
                label=label,
                source_formats=["xlsx", "xls", "csv", "json"],
                fields=[
                    ImportFieldOut(
                        target=field.target,
                        label=field.label,
                        kind=field.kind,
                        required=field.required,
                    )
                    for field in fields_for(code)
                ],
                requirements=requirements_for(code),
            )
            for code, label in ENTITY_TYPE_LABELS.items()
        ]
    )


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
        applied_preset=result.applied_preset,
    )


@import_jobs_router.get(
    "/{job_id}/rows",
    summary="Результаты по строкам",
    description=(
        "Построчный итог проверки и применения: статус, причины, разобранные значения. Значения "
        "ПДн (телефон, email, СНИЛС, паспорт) замаскированы. Фильтр `status` — `ok`, `warn`, "
        "`error`, `skipped`, `rolled_back`, `rollback_blocked`."
    ),
    response_model=ImportRowListResponse,
)
async def list_import_rows(
    session: DbSession,
    page: Pagination,
    _: ImportRunPerm,
    job_id: Annotated[uuid.UUID, Path()],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> ImportRowListResponse:
    await ImportService(session).get_or_404(job_id)
    stmt = (
        select(ImportRowResult)
        .where(ImportRowResult.import_job_id == job_id)
        .order_by(ImportRowResult.row_number, ImportRowResult.id)
    )
    if status_filter:
        stmt = stmt.where(ImportRowResult.status == status_filter)
    cursor = page.decoded_cursor
    if cursor:
        stmt = stmt.where(keyset_after(ImportRowResult.row_number, ImportRowResult.id, cursor))
    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())

    def _serialize(row: ImportRowResult) -> ImportRowOut:
        return ImportRowOut(
            id=row.id,
            row_number=row.row_number,
            status=row.status,
            entity_id=row.entity_id,
            errors=[str(e) for e in row.errors or []],
            row_data=mask_mapping(row.row_data or {}),  # type: ignore[arg-type]
        )

    built: Page = Page.build(
        rows, limit=page.limit, cursor_value=lambda r: r.row_number, serializer=_serialize
    )
    return ImportRowListResponse(items=built.items, next_cursor=built.next_cursor)


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
