"""Схемы импорта каталогов (раздел 4.12, 5.10)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ImportEntityLiteral = Literal["organization", "product"]
ImportModeLiteral = Literal["insert", "upsert", "update"]
SourceFormatLiteral = Literal["xlsx", "xls", "csv"]


class ImportJobCreateRequest(BaseModel):
    file_id: uuid.UUID
    entity_type: ImportEntityLiteral
    mode: ImportModeLiteral = "upsert"
    source_format: SourceFormatLiteral


class ImportJobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    file_id: uuid.UUID
    entity_type: str
    mode: str
    mapping: dict[str, str]
    status: str
    total_rows: int
    ok_rows: int
    warn_rows: int
    error_rows: int
    result_file_id: uuid.UUID | None = None
    initiated_by: uuid.UUID | None = None
    source_format: str
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    rollback_available: bool
    rolled_back_at: dt.datetime | None = None
    created_at: dt.datetime


class ImportJobListResponse(BaseModel):
    items: list[ImportJobOut]
    next_cursor: str | None = None


class ImportProfileResponse(BaseModel):
    headers: list[str]
    sample_rows: list[list[str]]
    suggested_mapping: dict[str, str]
    total_rows: int


class ImportMappingRequest(BaseModel):
    mapping: dict[str, str] = Field(description="`{колонка_файла: код_целевого_поля}`")
    save_as_preset: str | None = Field(default=None, max_length=255)


class ImportPresetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    entity_type: str
    mapping: dict[str, str]
    created_by: uuid.UUID | None = None
    created_at: dt.datetime


class ImportPresetListResponse(BaseModel):
    items: list[ImportPresetOut]
    next_cursor: str | None = None
