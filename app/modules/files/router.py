"""Ручки файлов и вложений (раздел 6, 9)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, status

from app.core.deps import DbSession, Pagination, require_permission
from app.core.errors import AppError, ErrorCode, ForbiddenError
from app.core.pagination import Page, keyset_before
from app.core.permissions import Permission
from app.core.security import Principal
from app.modules.files.models import Attachment
from app.modules.files.schemas import (
    AttachmentCreateRequest,
    AttachmentListResponse,
    AttachmentOut,
    DownloadUrlResponse,
    FileCommitRequest,
    FileOut,
    UploadIntentRequest,
    UploadIntentResponse,
)
from app.modules.files.service import AttachmentService, FileService, check_entity_access
from app.modules.identity.schemas import OperationResult

files_router = APIRouter(prefix="/files", tags=["files"])
attachments_router = APIRouter(prefix="/attachments", tags=["attachments"])

FileUploadPerm = Annotated[Principal, Depends(require_permission(Permission.FILE_UPLOAD))]
FileDownloadPerm = Annotated[Principal, Depends(require_permission(Permission.FILE_DOWNLOAD))]
FileDeletePerm = Annotated[Principal, Depends(require_permission(Permission.FILE_DELETE))]


@files_router.post(
    "/upload-intent",
    summary="Запросить загрузку файла",
    description=(
        "Проверяет расширение, MIME и лимит размера, создаёт запись `files` в "
        "статусе pending и возвращает presigned PUT URL к SeaweedFS (раздел 9). "
        "Файл грузится клиентом напрямую, не через это API."
    ),
    response_model=UploadIntentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_intent(
    payload: UploadIntentRequest, session: DbSession, principal: FileUploadPerm
) -> UploadIntentResponse:
    file, upload_url, expires_at = await FileService(session).upload_intent(principal, payload)
    return UploadIntentResponse(file_id=file.id, upload_url=upload_url, expires_at=expires_at)


@files_router.post(
    "/{file_id}/commit",
    summary="Подтвердить загрузку",
    description=(
        "Проверяет реальный объект в хранилище: размер, sha256, magic bytes, "
        "антивирус. До успешного commit файл нельзя привязать вложением."
    ),
    response_model=FileOut,
)
async def commit_file(
    payload: FileCommitRequest,
    session: DbSession,
    principal: FileUploadPerm,
    file_id: Annotated[uuid.UUID, Path()],
) -> FileOut:
    service = FileService(session)
    file = await service.get_or_404(file_id)
    is_owner_or_admin = (
        file.uploaded_by is None or file.uploaded_by == principal.user_id or principal.is_admin
    )
    if not is_owner_or_admin:
        raise ForbiddenError("Подтвердить загрузку может только автор или администратор")
    file = await service.commit(file, expected_sha256=payload.sha256)
    return FileOut.model_validate(file)


@files_router.get(
    "/{file_id}/download-url",
    summary="Получить ссылку на скачивание",
    description=(
        "Право проверяется по родительской сущности вложения (раздел 9), а не "
        "только по общему праву на файлы: `entity_type`/`entity_id` должны "
        "указывать на сущность, к которой файл реально привязан, иначе 404 — "
        "иначе можно было бы подобрать чужой file_id к своей же сущности. "
        "Ссылка короткоживущая (TTL 5 минут)."
    ),
    response_model=DownloadUrlResponse,
)
async def get_download_url(
    session: DbSession,
    principal: FileDownloadPerm,
    file_id: Annotated[uuid.UUID, Path()],
    entity_type: Annotated[str, Query(max_length=32)],
    entity_id: Annotated[uuid.UUID, Query()],
) -> DownloadUrlResponse:
    service = FileService(session)
    file = await service.get_or_404(file_id)
    if file.uploaded_by != principal.user_id and not principal.is_admin:
        await check_entity_access(session, principal, entity_type=entity_type, entity_id=entity_id)
        attached = await AttachmentService(session).exists_for(
            file_id=file_id, entity_type=entity_type, entity_id=entity_id
        )
        if not attached:
            raise AppError(ErrorCode.FILE_ACCESS_DENIED, "Файл не привязан к указанной сущности")
    url, expires_at = await service.download_url(file)
    return DownloadUrlResponse(download_url=url, expires_at=expires_at)


@files_router.delete(
    "/{file_id}",
    summary="Удалить файл",
    description="Мягкое удаление. Файл с активными вложениями (refcount > 0) удалить нельзя.",
    response_model=OperationResult,
)
async def delete_file(
    session: DbSession, principal: FileDeletePerm, file_id: Annotated[uuid.UUID, Path()]
) -> OperationResult:
    service = FileService(session)
    file = await service.get_or_404(file_id)
    await service.soft_delete(file)
    return OperationResult(ok=True, detail="Файл удалён")


# =============================================================================
# Вложения (раздел 6, полиморфная связь файл ↔ сущность)
# =============================================================================


@attachments_router.get("", summary="Вложения сущности", response_model=AttachmentListResponse)
async def list_attachments(
    session: DbSession,
    page: Pagination,
    principal: FileDownloadPerm,
    entity_type: Annotated[str, Query(max_length=32)],
    entity_id: Annotated[uuid.UUID, Query()],
) -> AttachmentListResponse:
    await check_entity_access(session, principal, entity_type=entity_type, entity_id=entity_id)
    stmt = (
        AttachmentService(session)
        .list_query(entity_type=entity_type, entity_id=entity_id)
        .order_by(Attachment.created_at.desc(), Attachment.id.desc())
    )
    cursor = page.decoded_cursor
    if cursor:
        stmt = stmt.where(keyset_before(Attachment.created_at, Attachment.id, cursor))
    rows = list((await session.execute(stmt.limit(page.fetch_limit))).scalars().all())
    built = Page.build(rows, limit=page.limit, serializer=AttachmentOut.model_validate)
    return AttachmentListResponse(items=built.items, next_cursor=built.next_cursor)


@attachments_router.post(
    "",
    summary="Привязать файл к сущности",
    response_model=AttachmentOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_attachment(
    payload: AttachmentCreateRequest, session: DbSession, principal: FileUploadPerm
) -> AttachmentOut:
    await check_entity_access(
        session, principal, entity_type=payload.entity_type, entity_id=payload.entity_id
    )
    file = await FileService(session).get_or_404(payload.file_id)
    attachment = await AttachmentService(session).create(
        principal,
        file=file,
        entity_type=payload.entity_type,
        entity_id=payload.entity_id,
        category=payload.category,
        description=payload.description,
    )
    return AttachmentOut.model_validate(attachment)


@attachments_router.delete(
    "/{attachment_id}", summary="Отвязать файл", response_model=OperationResult
)
async def delete_attachment(
    session: DbSession,
    principal: FileDeletePerm,
    attachment_id: Annotated[uuid.UUID, Path()],
) -> OperationResult:
    service = AttachmentService(session)
    attachment = await service.get_or_404(attachment_id)
    await check_entity_access(
        session,
        principal,
        entity_type=attachment.entity_type,
        entity_id=attachment.entity_id,
    )
    await service.delete(attachment)
    return OperationResult(ok=True, detail="Вложение отвязано")
