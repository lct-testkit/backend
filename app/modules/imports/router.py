"""Ручки импорта каталогов (раздел 4.12): загрузка, профиль, маппинг,
dry-run, применение, откат."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Response, status
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.deps import DbSession, Pagination, require_permission
from app.core.errors import AppError, ErrorCode
from app.core.masking import mask_mapping
from app.core.pagination import Page, keyset_after, keyset_before
from app.core.permissions import Permission
from app.core.queue import enqueue_after_commit
from app.core.security import Principal
from app.modules.imports.fields import (
    ADMIN_ONLY_ENTITY_TYPES,
    ENTITY_TYPE_LABELS,
    fields_for,
    requirements_for,
)
from app.modules.imports.models import ImportJob, ImportJobStatus, ImportPreset, ImportRowResult
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
    ImportSummaryOut,
    IssueSummaryOut,
    RowIssueOut,
    SheetOut,
)
from app.modules.imports.service import ImportService, entry_to_issue_dict, issue_text
from app.modules.imports.templates import all_builtin_presets, build_template

_TEMPLATE_CONTENT_TYPES = {
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
}

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
                admin_only=code in ADMIN_ONLY_ENTITY_TYPES,
            )
            for code, label in ENTITY_TYPE_LABELS.items()
        ]
    )


@import_jobs_router.get(
    "/templates/{entity_type}",
    summary="Шаблон файла для скачивания",
    description=(
        "B3-9: заголовки, две учебные строки и лист «Инструкция» (обязательность, допустимые "
        "значения). `format=xlsx` — с выпадающими списками и текстовым форматом номера договора; "
        "`format=xls` — то же без списков (формат BIFF их не поддерживает). Роль: HEAD, ADMIN."
    ),
)
async def download_import_template(
    _: ImportRunPerm,
    entity_type: Annotated[str, Path()],
    format: Annotated[str, Query()] = "xlsx",  # noqa: A002 — имя параметра из контракта задачи
) -> Response:
    if entity_type not in ENTITY_TYPE_LABELS:
        raise AppError(ErrorCode.VALIDATION, f"Неизвестный тип сущности: {entity_type!r}")
    if format not in _TEMPLATE_CONTENT_TYPES:
        raise AppError(ErrorCode.VALIDATION, f"Неизвестный формат шаблона: {format!r}")
    content = build_template(entity_type, format)
    filename = f"{entity_type}-template.{format}"
    return Response(
        content=content,
        media_type=_TEMPLATE_CONTENT_TYPES[format],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@import_jobs_router.get(
    "",
    summary="Список заданий импорта",
    description=(
        "Задания импорта всех типов (организации, продукты, лицензии и т.п.) со статусом "
        "и счётчиками строк, от новых к старым. Роль: HEAD, ADMIN."
    ),
    response_model=ImportJobListResponse,
)
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
    "/{job_id}",
    summary="Карточка задания импорта",
    description=(
        "Статус, тип сущности, режим, маппинг колонок и счётчики строк "
        "(`ok`/`warn`/`error`); не существует — 404 CRM-9004. Роль: HEAD, ADMIN."
    ),
    response_model=ImportJobOut,
)
async def get_import_job(
    session: DbSession, _: ImportRunPerm, job_id: Annotated[uuid.UUID, Path()]
) -> ImportJobOut:
    job = await ImportService(session).get_or_404(job_id)
    return ImportJobOut.model_validate(job)


@import_jobs_router.get(
    "/{job_id}/profile",
    summary="Профиль файла: первые строки и подсказка маппинга",
    description=(
        "Раздел 4.12, фаза 2-3: превью первых 100 строк и автоподбор колонок. B3-2: `sheet` и "
        "`header_row` — предпросмотр книги с другим листом/строкой заголовка (ничего не "
        "сохраняет; сохраняет только `PUT .../mapping` параметром `options`). Без параметров — "
        "уже сохранённые на задании опции, а для нового задания первый видимый лист, строка 1."
    ),
    response_model=ImportProfileResponse,
)
async def profile_import_job(
    session: DbSession,
    _: ImportRunPerm,
    job_id: Annotated[uuid.UUID, Path()],
    sheet: Annotated[str | None, Query()] = None,
    header_row: Annotated[int | None, Query(ge=1)] = None,
) -> ImportProfileResponse:
    job = await ImportService(session).get_or_404(job_id)
    result = await ImportService(session).profile(job, sheet=sheet, header_row=header_row)
    return ImportProfileResponse(
        headers=result.headers,
        sample_rows=result.sample_rows,
        suggested_mapping=result.suggested_mapping,
        total_rows=result.total_rows,
        applied_preset=result.applied_preset,
        sheets=[
            SheetOut(
                name=s.name,
                rows=s.rows,
                visible=s.visible,
                suggested_header_row=s.suggested_header_row,
            )
            for s in result.sheets
        ],
        sheet=result.sheet,
        header_row=result.header_row,
        suggested_header_row=result.suggested_header_row,
    )


@import_jobs_router.get(
    "/{job_id}/rows",
    summary="Результаты по строкам",
    description=(
        "Построчный итог проверки и применения: статус, причины, разобранные значения. Значения "
        "ПДн (телефон, email, СНИЛС, паспорт) замаскированы. Фильтр `status` — `ok`, `warn`, "
        "`error`, `skipped`, `rolled_back`, `rollback_blocked`. Фильтр `issue` (B3-5) — код "
        "проблемы строки (см. `issues[].code`), например `org_not_found`."
    ),
    response_model=ImportRowListResponse,
)
async def list_import_rows(
    session: DbSession,
    page: Pagination,
    _: ImportRunPerm,
    job_id: Annotated[uuid.UUID, Path()],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    issue: Annotated[str | None, Query()] = None,
) -> ImportRowListResponse:
    await ImportService(session).get_or_404(job_id)
    stmt = (
        select(ImportRowResult)
        .where(ImportRowResult.import_job_id == job_id)
        .order_by(ImportRowResult.row_number, ImportRowResult.id)
    )
    if status_filter:
        stmt = stmt.where(ImportRowResult.status == status_filter)
    if issue:
        # B3-5: `errors` — JSONB-массив объектов `{code, field, message, hint, severity}` (старые
        # строки-задания хранят просто текст — `->>'code'` на них даёт NULL, не ошибку, строка
        # просто не попадёт в фильтр).
        stmt = stmt.where(
            text(
                "EXISTS (SELECT 1 FROM jsonb_array_elements(import_row_results.errors) e "
                "WHERE e->>'code' = :issue_code)"
            ).bindparams(issue_code=issue)
        )
    cursor = page.decoded_cursor
    if cursor:
        stmt = stmt.where(keyset_after(ImportRowResult.row_number, ImportRowResult.id, cursor))
    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())

    def _serialize(row: ImportRowResult) -> ImportRowOut:
        entries = [entry_to_issue_dict(e) for e in row.errors or []]
        return ImportRowOut(
            id=row.id,
            row_number=row.row_number,
            source_row=row.source_row,
            sheet=row.sheet,
            status=row.status,
            entity_id=row.entity_id,
            errors=[issue_text(e) for e in row.errors or []],
            issues=[RowIssueOut(**e) for e in entries],
            row_data=mask_mapping(row.row_data or {}),  # type: ignore[arg-type]
        )

    built: Page = Page.build(
        rows, limit=page.limit, cursor_value=lambda r: r.row_number, serializer=_serialize
    )
    return ImportRowListResponse(items=built.items, next_cursor=built.next_cursor)


@import_jobs_router.get(
    "/{job_id}/summary",
    summary="Сводка проверки: коды проблем и что будет создано/обновлено",
    description=(
        "B3-5: сколько строк с каждым кодом проблемы (до 5 примеров номеров строки файла на код) "
        "и что будет создано/обновлено/пропущено при применении. Пусто до первого dry-run."
    ),
    response_model=ImportSummaryOut,
)
async def get_import_summary(
    session: DbSession, _: ImportRunPerm, job_id: Annotated[uuid.UUID, Path()]
) -> ImportSummaryOut:
    job = await ImportService(session).get_or_404(job_id)
    summary = job.summary or {}
    return ImportSummaryOut(
        status=job.status,
        total_rows=job.total_rows,
        ok_rows=job.ok_rows,
        warn_rows=job.warn_rows,
        error_rows=job.error_rows,
        by_issue=[IssueSummaryOut(**entry) for entry in summary.get("by_issue", [])],
        would=summary.get("would", {}),
        applied=summary.get("applied", {}),
        duplicate_headers=summary.get("duplicate_headers", []),
        result_file_id=job.result_file_id,
    )


@import_jobs_router.put(
    "/{job_id}/mapping",
    summary="Сохранить маппинг колонок",
    description=(
        "Опционально сохраняет маппинг как пресет для повторного использования. B3-2: "
        "`options.sheet`/`sheets`/`header_row`/`fill_down` — выбор листа книги и строки "
        "заголовка, сохраняется вместе с маппингом и переживает повторный разбор файла на "
        "dry-run/apply."
    ),
    response_model=ImportJobOut,
)
async def save_import_mapping(
    payload: ImportMappingRequest,
    session: DbSession,
    principal: ImportRunPerm,
    job_id: Annotated[uuid.UUID, Path()],
) -> ImportJobOut:
    service = ImportService(session)
    job = await service.get_for_write_or_404(job_id, principal)
    job = await service.save_mapping(
        job,
        mapping=payload.mapping,
        principal=principal,
        save_as_preset=payload.save_as_preset,
        options=payload.options,
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
    session: DbSession, principal: ImportRunPerm, job_id: Annotated[uuid.UUID, Path()]
) -> ImportJobOut:
    service = ImportService(session)
    job = await service.get_for_write_or_404(job_id, principal)
    job = await service.dry_run(job)
    return ImportJobOut.model_validate(job)


@import_jobs_router.post(
    "/{job_id}/apply",
    summary="Применить импорт",
    description=(
        "B3-12: задание не больше `IMPORT_INLINE_MAX_ROWS` строк (по умолчанию 500) применяется "
        "сразу — ответ приходит уже со статусом `completed`/`completed_with_errors`. Большее "
        "задание переходит в `applying`; построчная обработка батчами по 500 с чекпоинтом "
        "продолжается фоновой задачей, прогресс — в счётчиках и статусе при повторном GET."
    ),
    response_model=ImportJobOut,
)
async def apply_import_job(
    session: DbSession, principal: ImportRunPerm, job_id: Annotated[uuid.UUID, Path()]
) -> ImportJobOut:
    service = ImportService(session)
    job = await service.get_for_write_or_404(job_id, principal)
    job = await service.start_apply(job)
    # B8-11: не ждать минутного тика cron — поставить задание в arq сразу после коммита.
    # B3-12: небольшое задание `start_apply` уже применил инлайн (статус терминальный) — ставить
    # задачу в очередь незачем, `run_import_job` нашёл бы там только обработанные строки.
    if get_settings().reports_enqueue and job.status == ImportJobStatus.APPLYING.value:
        enqueue_after_commit(
            session, "run_import_job", str(job.id), "apply", job_id=f"import:{job.id}:apply"
        )
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
    session: DbSession, principal: ImportRollbackPerm, job_id: Annotated[uuid.UUID, Path()]
) -> ImportJobOut:
    service = ImportService(session)
    job = await service.get_for_write_or_404(job_id, principal)
    job = await service.start_rollback(job)
    if get_settings().reports_enqueue:
        enqueue_after_commit(
            session, "run_import_job", str(job.id), "rollback", job_id=f"import:{job.id}:rollback"
        )
    return ImportJobOut.model_validate(job)


@import_presets_router.get(
    "",
    summary="Список пресетов маппинга",
    description=(
        "Сохранённые маппинги колонок для повторного использования (создаются из "
        "`PUT /{job_id}/mapping` с `save_as_preset`); фильтр по типу сущности "
        "`entity_type`. Роль: HEAD, ADMIN."
    ),
    response_model=ImportPresetListResponse,
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
    items = built.items
    if cursor is None:
        # B3-9: встроенные пресеты («Лицензии: 10 колонок ТЗ» и т.п.) не хранятся в БД (нет `id`/
        # `created_at`), поэтому не участвуют в keyset-пагинации — показываем их только на первой
        # странице, перед пользовательскими.
        builtin = [
            ImportPresetOut(
                name=p.name,
                entity_type=p.entity_type,
                mapping=p.mapping,
                options=p.options,
                builtin=True,
            )
            for p in all_builtin_presets(entity_type)
        ]
        items = [*builtin, *items]
    return ImportPresetListResponse(items=items, next_cursor=built.next_cursor)


@import_presets_router.delete(
    "/{preset_id}",
    summary="Удалить пресет маппинга",
    description=(
        "Жёсткое удаление — пресет только сохранённый маппинг колонок, не бизнес-сущность с "
        "историей; на уже созданные задания импорта не влияет (маппинг скопирован в них при "
        "применении пресета). Не существует — 404 CRM-9004. Роль: HEAD, ADMIN."
    ),
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_import_preset(
    session: DbSession, _: ImportRunPerm, preset_id: Annotated[uuid.UUID, Path()]
) -> None:
    service = ImportService(session)
    preset = await service.get_preset_or_404(preset_id)
    await service.delete_preset(preset)
