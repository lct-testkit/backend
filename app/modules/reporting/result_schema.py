"""Контракт результирующего json-файла отчёта (B9-9; rtk_requiriments.md: «формирование
результирующего json-файла»). `reporting.rendering.render_json` (B1-5) строит конверт
`rtk-crm-report/1` — здесь он описан Pydantic-моделью, а не только словарём внутри рендера, и
проверяемой JSON Schema (`tools/export_report_schema.py` → `docs/schemas/
rtk-crm-report-1.schema.json`), чтобы форма файла не разошлась незаметно с документацией.

Правило версий (см. README): добавление необязательных полей не требует новой версии схемы —
старый клиент продолжает читать файл, неизвестные поля он просто не видит (`model_config
extra="allow"` ниже на каждом уровне — тот же эффект и для будущих полей, которых эта версия
модели ещё не знает). Удаление поля, переименование или смена типа — `rtk-crm-report/2`;
прежняя версия отдаётся по запросу минимум два релиза после введения следующей.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

#: Версия конверта — тот же литерал, что `reporting.rendering._JSON_SCHEMA`. Не импортируется
#: оттуда напрямую (`rendering.py` импортирует эту модель, не наоборот — иначе цикл).
REPORT_RESULT_SCHEMA_VERSION = "rtk-crm-report/1"


class ReportResultColumn(BaseModel):
    """Один элемент `columns` — заголовок колонки, её ключ каталога (B1-2, `None` у видов без
    каталога колонок) и тип значения для форматирования на стороне клиента."""

    model_config = ConfigDict(extra="allow")

    key: str | None = None
    title: str
    kind: str


class ReportResultReport(BaseModel):
    """`report` — метаданные самого файла: какой вид отчёта, для какого задания, когда и в
    каком поясе построен."""

    model_config = ConfigDict(extra="allow")

    code: str
    title: str
    job_id: str | None = None
    generated_at: str
    timezone: str


class ReportResultTotals(BaseModel):
    """`totals` — то же усечение, что несёт файл (B1-6): `row_count` — сколько строк реально в
    `rows`, `total_rows` — сколько их всего по выборке (может быть больше `row_count`, если
    отчёт усечён по `limit`), `truncated` — признак усечения."""

    model_config = ConfigDict(extra="allow")

    row_count: int
    total_rows: int
    truncated: bool


class ReportResultV1(BaseModel):
    """Форма файла `format=json` (`POST /api/reports`, B1-5) — тот же датасет, что отдаёт
    `GET /reports/{id}/data` (`reporting.schemas.ReportDataOut`), версионированным конвертом.
    Поле `schema` — зарезервированное имя в части прежних версий Pydantic/JSON Schema
    инструментов, поэтому в Python-коде оно `schema_` с `alias="schema"` — на проводе и в файле
    ключ остаётся `schema`, как документирует контракт (`populate_by_name=True` позволяет
    собирать модель и по имени атрибута, и по алиасу)."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    schema_: Literal["rtk-crm-report/1"] = Field(alias="schema")
    report: ReportResultReport
    params: dict[str, Any]
    columns: list[ReportResultColumn]
    rows: list[list[Any]]
    totals: ReportResultTotals
    note: str | None = None


__all__ = [
    "REPORT_RESULT_SCHEMA_VERSION",
    "ReportResultColumn",
    "ReportResultReport",
    "ReportResultTotals",
    "ReportResultV1",
]
