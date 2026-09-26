"""Схемы импорта каталогов (раздел 4.12, 5.10)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ImportEntityLiteral = Literal[
    "organization", "product", "license", "vendor_contact", "payment", "learner"
]
ImportModeLiteral = Literal["insert", "upsert", "update"]
SourceFormatLiteral = Literal["xlsx", "xls", "csv", "json"]


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
    #: Сколько строк уже обработано при применении (прогресс фоновой задачи).
    processed_rows: int = 0
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
    #: Имя пресета, чей маппинг наложен поверх автоподбора; `null` — пресет не применялся.
    applied_preset: str | None = None


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


class ImportFieldOut(BaseModel):
    """Целевое поле типа сущности: то, что можно указать в маппинге."""

    target: str
    label: str
    kind: str
    required: bool


class ImportEntityTypeOut(BaseModel):
    """Тип сущности импорта с полями, форматами файла и условиями достаточности маппинга — по нему
    клиент строит окно сопоставления колонок, не зашивая списки полей у себя."""

    code: str
    label: str
    source_formats: list[str]
    fields: list[ImportFieldOut]
    #: Что должно быть в маппинге, чтобы строки можно было применить (человекочитаемо).
    requirements: list[str]


class ImportEntityTypeListResponse(BaseModel):
    items: list[ImportEntityTypeOut]


class ImportRowOut(BaseModel):
    """Строка результата проверки/применения; значения ПДн в `row_data` замаскированы."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    row_number: int
    status: str
    entity_id: uuid.UUID | None = None
    errors: list[str] = Field(default_factory=list)
    row_data: dict[str, Any] = Field(default_factory=dict)


class ImportRowListResponse(BaseModel):
    items: list[ImportRowOut]
    next_cursor: str | None = None
