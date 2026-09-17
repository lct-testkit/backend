"""Схемы файлов и вложений (раздел 6, 9)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

AttachmentCategoryLiteral = Literal[
    "contract", "presentation", "act", "license", "report", "signature_container", "other"
]


class UploadIntentRequest(BaseModel):
    filename: NonEmptyStr = Field(max_length=255)
    size_bytes: int = Field(gt=0)
    mime_type: NonEmptyStr = Field(max_length=128)
    purpose: str | None = Field(default=None, max_length=64)
    category: AttachmentCategoryLiteral | None = None


class UploadIntentResponse(BaseModel):
    file_id: uuid.UUID
    upload_url: str
    upload_method: Literal["PUT"] = "PUT"
    upload_headers: dict[str, str] = Field(default_factory=dict)
    expires_at: dt.datetime


class FileCommitRequest(BaseModel):
    """Клиент может сообщить контрольную сумму заранее — сервер всё равно
    пересчитывает её из объекта в S3 (раздел 9), это поле только для
    ранней диагностики несовпадения на клиенте."""

    sha256: str | None = Field(default=None, min_length=64, max_length=64)


class FileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_filename: str
    mime_type: str
    size_bytes: int
    sha256: str | None = None
    status: str
    contains_pd: bool
    uploaded_by: uuid.UUID | None = None
    created_at: dt.datetime


class DownloadUrlRequest(BaseModel):
    """Права проверяются по родительской сущности вложения (раздел 9)."""

    entity_type: str = Field(max_length=32)
    entity_id: uuid.UUID


class DownloadUrlResponse(BaseModel):
    download_url: str
    expires_at: dt.datetime


class AttachmentCreateRequest(BaseModel):
    file_id: uuid.UUID
    entity_type: NonEmptyStr = Field(max_length=32)
    entity_id: uuid.UUID
    category: AttachmentCategoryLiteral
    description: str | None = None


class AttachmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    file_id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    category: str
    description: str | None = None
    uploaded_by: uuid.UUID | None = None
    created_at: dt.datetime


class AttachmentListResponse(BaseModel):
    items: list[AttachmentOut]
    next_cursor: str | None = None
