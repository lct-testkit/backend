"""Схемы импорта каталогов (раздел 4.12, 5.10)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ImportEntityLiteral = Literal[
    "organization",
    "product",
    "license",
    "vendor_contact",
    "payment",
    "learner",
    "direction",
    "responsible",
]
ImportModeLiteral = Literal["insert", "upsert", "update"]
SourceFormatLiteral = Literal["xlsx", "xls", "csv", "json"]


class ImportOptions(BaseModel):
    """B3-2: выбор листа и строки заголовка книги, сохраняется на задании (`ImportJob.options`),
    переживает повторный разбор файла на dry-run/apply. Поля необязательны — без них разбор ведёт
    себя как раньше (первый лист, первая строка)."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "sheet": None,
                "sheets": ["*"],
                "header_row": 3,
                "fill_down": ["Название ВУЗа", "Вендор"],
            }
        }
    )

    #: Один лист по имени; взаимоисключим с `sheets`.
    sheet: str | None = None
    #: `["*"]` — склеить все видимые листы с одинаковой шапкой; явный список имён — склеить именно
    #: их (даже скрытые, если пользователь выбрал их сам).
    sheets: list[str] | None = None
    #: Номер строки заголовка книги (с 1, считая пустые строки). `null` — первая строка.
    header_row: int | None = Field(default=None, ge=1)
    #: Имена колонок файла, где пустая ячейка берёт значение из строки выше (объединённые ячейки
    #: вуза/вендора на блок строк).
    fill_down: list[str] = Field(default_factory=list)
    #: B3-4: автосоздание вуза/вендора/ПО, если не нашлись в каталоге (только тип `license`).
    create_missing_organizations: bool = False
    create_missing_vendors: bool = True
    create_missing_software: bool = True

    @model_validator(mode="after")
    def _sheet_xor_sheets(self) -> ImportOptions:
        # `parse_table` при непустом `sheets` склеивает листы и молча игнорирует `sheet`.
        if self.sheet is not None and self.sheets:
            raise ValueError("`sheet` и `sheets` взаимоисключающие: укажите что-то одно")
        return self


class ImportJobCreateRequest(BaseModel):
    # B9-2: пример — реальное тело из `tests/test_imports.py::TestLicenseImportEndToEnd`
    # (импорт реестра лицензий вуз-вендор-ПО, B3).
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "file_id": "018f0a3e-4444-7c3e-8c3e-3e7c3e7c3e7c",
                "entity_type": "license",
                "mode": "upsert",
                "source_format": "xlsx",
            }
        }
    )

    file_id: uuid.UUID
    entity_type: ImportEntityLiteral
    mode: ImportModeLiteral = "upsert"
    source_format: SourceFormatLiteral


class RowIssueOut(BaseModel):
    """B3-5: один код проблемы строки — машиночитаемый, в отличие от `ImportRowOut.errors`
    (список готовых строк, оставлен для обратной совместимости)."""

    code: str
    field: str | None = None
    message: str
    hint: str | None = None
    severity: Literal["error", "warning"] = "error"


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
    # B9-8 (OPEN-ISSUES B-14): миграция 0025_import_job_last_error и `imports.tasks` (запись
    # причины ухода в `failed`, фоновая партия вне построчного цикла) существуют с прошлого
    # спринта, но поле не отдавалось в ответе — пользователь видел только статус `failed` без
    # причины, пока не открывал БД напрямую.
    last_error: str | None = None
    rollback_available: bool
    rolled_back_at: dt.datetime | None = None
    #: B3-5: сводка dry-run (`by_issue`/`would`/`applied`); `null` до первого dry-run или у
    #: задания, провалидированного до этой задачи.
    summary: dict[str, Any] | None = None
    created_at: dt.datetime


class ImportJobListResponse(BaseModel):
    items: list[ImportJobOut]
    next_cursor: str | None = None


class SheetOut(BaseModel):
    """B3-2: один лист книги для экрана выбора при импорте."""

    name: str
    rows: int
    visible: bool
    suggested_header_row: int


class ImportProfileResponse(BaseModel):
    headers: list[str]
    sample_rows: list[list[str]]
    suggested_mapping: dict[str, str]
    total_rows: int
    #: Имя пресета, чей маппинг наложен поверх автоподбора; `null` — пресет не применялся.
    applied_preset: str | None = None
    #: B3-2: листы книги (пусто для CSV/JSON), выбранный лист/строка заголовка и подсказка
    #: автопоиска — ничего не сохраняет, профиль можно запрашивать повторно с другими `sheet`/
    #: `header_row` в query, пока маппинг не сохранён `PUT .../mapping`.
    sheets: list[SheetOut] = Field(default_factory=list)
    sheet: str | None = None
    header_row: int = 1
    suggested_header_row: int = 1


class ImportMappingRequest(BaseModel):
    mapping: dict[str, str] = Field(description="`{колонка_файла: код_целевого_поля}`")
    save_as_preset: str | None = Field(default=None, max_length=255)
    #: B3-2: опции листа/шапки/`fill_down`, сохраняются на задании вместе с маппингом.
    options: ImportOptions | None = None


class ImportPresetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID | None = None
    name: str
    entity_type: str
    mapping: dict[str, str]
    options: dict[str, Any] | None = None
    created_by: uuid.UUID | None = None
    created_at: dt.datetime | None = None
    #: B3-9: встроенный пресет («Лицензии: 10 колонок ТЗ») — без `id`, применить может любой
    #: пользователь; пользовательский пресет применяет только автор (`created_by`).
    builtin: bool = False


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
    #: B3-8: `direction`/`responsible` — создание задания этого типа только для ADMIN.
    admin_only: bool = False


class ImportEntityTypeListResponse(BaseModel):
    items: list[ImportEntityTypeOut]


class ImportRowOut(BaseModel):
    """Строка результата проверки/применения; значения ПДн в `row_data` замаскированы.

    `errors` — список готовых строк (оставлен для обратной совместимости: старые строки, записанные
    до B3-5, хранят в БД просто текст). `issues` — то же, но структурировано (код, поле,
    сообщение, подсказка, серьёзность); у строк, записанных до B3-5, пуст."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    row_number: int
    #: B3-2/B3-5: номер строки Excel (считая пустые строки) и имя листа; `null` — CSV/JSON или
    #: задание, созданное до B3-2.
    source_row: int | None = None
    sheet: str | None = None
    status: str
    entity_id: uuid.UUID | None = None
    errors: list[str] = Field(default_factory=list)
    issues: list[RowIssueOut] = Field(default_factory=list)
    row_data: dict[str, Any] = Field(default_factory=dict)


class ImportRowListResponse(BaseModel):
    items: list[ImportRowOut]
    next_cursor: str | None = None


class IssueSummaryOut(BaseModel):
    """B3-5: один код проблемы в сводке dry-run — сколько строк и несколько примеров номеров."""

    code: str
    message: str
    rows: int
    examples: list[int] = Field(default_factory=list)


class ImportSummaryOut(BaseModel):
    """B3-5: `GET /api/imports/{id}/summary` — то же, что лежит в `ImportJob.summary`, плюс
    текущие счётчики задания (на случай, если сводка ещё не посчитана)."""

    status: str
    total_rows: int
    ok_rows: int
    warn_rows: int
    error_rows: int
    by_issue: list[IssueSummaryOut] = Field(default_factory=list)
    would: dict[str, dict[str, int]] = Field(default_factory=dict)
    applied: dict[str, dict[str, int]] = Field(default_factory=dict)
    #: B3-M: заголовки, повторённые в файле — словарь колонок строится по имени, вторая колонка с
    #: тем же заголовком иначе терялась бы молча.
    duplicate_headers: list[str] = Field(default_factory=list)
    result_file_id: uuid.UUID | None = None
