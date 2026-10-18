"""B3-9: шаблоны для скачивания (xlsx/xls) и встроенные пресеты маппинга.

Чтобы загрузить каталог, пользователю нужен образец файла с правильными заголовками — без него
колонки приходится угадывать по ТЗ. `build_template` собирает один набор данных (заголовки из
`fields_for()`, две учебные строки, лист «Инструкция» с обязательностью и допустимыми значениями)
и пишет его либо в xlsx (openpyxl: жирная шапка, закрепление строки, выпадающие списки, текстовый
формат номера договора), либо в xls (`xlwt`: те же данные без выпадающих списков — формат BIFF их
не поддерживает).

`BUILTIN_PRESETS` — пресеты маппинга, которые не нужно сохранять вручную: заголовки шаблона уже
совпадают с метками полей (`{label: target}` — тождественный маппинг), только для типов, где
порядок и названия колонок зафиксированы требованием заказчика (сейчас — только `license`, 10
колонок ТЗ).
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

import openpyxl
import xlwt
from openpyxl.styles import Font
from openpyxl.worksheet.datavalidation import DataValidation

from app.core.errors import AppError, ErrorCode
from app.modules.catalog.license_values import TRANSFER_STATUS_LABELS
from app.modules.imports.fields import FieldSpec, fields_for

_BOLD = Font(bold=True)

# Учебные строки — выдуманные, две на тип (B3-9: «скачать -> дополнить -> dry-run» должен дать
# `error_rows=0`). Порядок колонок = порядок `fields_for(entity_type)`.
_EXAMPLE_ROWS: dict[str, list[list[str]]] = {
    "license": [
        [
            "МГТУ им. Н.Э. Баумана",
            "ООО «Базис»",
            "RT.DataLake",
            "Д-12/2024",
            "15.03.2024",
            "3",
            "передано",
            "Иванов Иван Иванович",
            "Петров П.П.",
            "",
        ],
        [
            "СПбГУ",
            "ПАО «Ростелеком»",
            "RT.CRM",
            "",
            "да",
            "бессрочно",
            "в работе",
            "",
            "",
            "Пример без номера договора",
        ],
    ],
    "direction": [
        ["qa", "Тестирование (QA)", ""],
        ["qa-auto", "Автоматизация тестирования", "qa"],
    ],
    "responsible": [
        ["МГТУ им. Н.Э. Баумана", "", "ivanov@example.com", ""],
        ["СПбГУ", "", "", "Петров Пётр Петрович"],
    ],
}

# Колонки (по индексу, 0 с начала) с выпадающим списком в xlsx — удобство, не строгая проверка
# (поле «Подписание лицензии» принимает и дату, которую список не перечислит).
_LICENSE_SIGNED_VALUES = ("да", "нет")


@dataclass(slots=True)
class BuiltinPreset:
    name: str
    entity_type: str
    mapping: dict[str, str]
    options: dict[str, Any] | None = None


def _label_order(entity_type: str) -> dict[str, str]:
    return {f.label: f.target for f in fields_for(entity_type)}


BUILTIN_PRESETS: dict[str, list[BuiltinPreset]] = {
    "license": [
        BuiltinPreset(
            name="Лицензии: 10 колонок ТЗ",
            entity_type="license",
            mapping=_label_order("license"),
        )
    ],
    "direction": [
        BuiltinPreset(
            name="Направления: код/название/родитель",
            entity_type="direction",
            mapping=_label_order("direction"),
        )
    ],
    "responsible": [
        BuiltinPreset(
            name="Ответственные: вуз/ИНН/почта/ФИО",
            entity_type="responsible",
            mapping=_label_order("responsible"),
        )
    ],
}


def all_builtin_presets(entity_type: str | None = None) -> list[BuiltinPreset]:
    presets = [p for group in BUILTIN_PRESETS.values() for p in group]
    if entity_type is not None:
        presets = [p for p in presets if p.entity_type == entity_type]
    return presets


def _allowed_values_hint(spec: FieldSpec) -> str:
    if spec.kind == "transfer_status":
        return "Допустимо: " + ", ".join(TRANSFER_STATUS_LABELS.values())
    if spec.kind == "license_signed":
        return "да / нет / дата подписания (15.03.2024)"
    if spec.kind == "license_term":
        return "Срок в годах (1–50), год окончания (2000–2100) или «бессрочно»"
    if spec.kind == "product_kind":
        return "курс, программа или ПО / ИТ-продукт"
    if spec.kind == "org_type":
        return "university, college, company, individual_entrepreneur"
    if spec.kind == "email":
        return "адрес электронной почты"
    if spec.kind == "phone":
        return "телефон (российский номер)"
    if spec.max_length:
        return f"текст, не длиннее {spec.max_length} символов"
    return ""


def _build_xlsx(entity_type: str, fields: tuple[FieldSpec, ...]) -> bytes:
    headers = [f.label for f in fields]
    examples = _EXAMPLE_ROWS.get(entity_type, [[""] * len(fields)])

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Данные"
    sheet.append(headers)
    for col in range(1, len(headers) + 1):
        sheet.cell(row=1, column=col).font = _BOLD
    sheet.freeze_panes = "A2"
    for row in examples:
        sheet.append(row)

    for idx, spec in enumerate(fields, start=1):
        width = max(12, min(40, len(spec.label) + 4))
        sheet.column_dimensions[sheet.cell(row=1, column=idx).column_letter].width = width
        if spec.target == "contract_number":
            # Раздел 4.12: текстовый формат — иначе Excel молча превращает «01/2024» в дату.
            for cell in sheet.iter_rows(min_row=2, min_col=idx, max_col=idx):
                cell[0].number_format = "@"
        if spec.kind == "transfer_status":
            validation = DataValidation(
                type="list",
                formula1='"' + ",".join(TRANSFER_STATUS_LABELS.values()) + '"',
                allow_blank=True,
            )
            sheet.add_data_validation(validation)
            validation.add(
                f"{sheet.cell(row=2, column=idx).coordinate}:"
                f"{sheet.cell(row=1000, column=idx).coordinate}"
            )
        if spec.kind == "license_signed":
            validation = DataValidation(
                type="list", formula1='"' + ",".join(_LICENSE_SIGNED_VALUES) + '"', allow_blank=True
            )
            sheet.add_data_validation(validation)
            validation.add(
                f"{sheet.cell(row=2, column=idx).coordinate}:"
                f"{sheet.cell(row=1000, column=idx).coordinate}"
            )

    instructions = workbook.create_sheet("Инструкция")
    instructions.append(["Колонка", "Обязательна", "Допустимые значения"])
    for col in range(1, 4):
        instructions.cell(row=1, column=col).font = _BOLD
    for spec in fields:
        instructions.append(
            [spec.label, "да" if spec.required else "нет", _allowed_values_hint(spec)]
        )
    instructions.column_dimensions["A"].width = 32
    instructions.column_dimensions["B"].width = 14
    instructions.column_dimensions["C"].width = 60

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _build_xls(entity_type: str, fields: tuple[FieldSpec, ...]) -> bytes:
    """`xlwt`: тот же набор данных, без выпадающих списков (BIFF их не поддерживает) и без
    текстового формата (xlrd читает значение как строку независимо от формата ячейки)."""
    headers = [f.label for f in fields]
    examples = _EXAMPLE_ROWS.get(entity_type, [[""] * len(fields)])

    workbook = xlwt.Workbook(encoding="utf-8")
    bold_style = xlwt.easyxf("font: bold on")
    data_sheet = workbook.add_sheet("Данные")
    for col, header in enumerate(headers):
        data_sheet.write(0, col, header, bold_style)
    for row_idx, row in enumerate(examples, start=1):
        for col, value in enumerate(row):
            data_sheet.write(row_idx, col, value)

    instructions = workbook.add_sheet("Инструкция")
    for col, header in enumerate(["Колонка", "Обязательна", "Допустимые значения"]):
        instructions.write(0, col, header, bold_style)
    for row_idx, spec in enumerate(fields, start=1):
        instructions.write(row_idx, 0, spec.label)
        instructions.write(row_idx, 1, "да" if spec.required else "нет")
        instructions.write(row_idx, 2, _allowed_values_hint(spec))

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def build_template(entity_type: str, fmt: str) -> bytes:
    fields = fields_for(entity_type)
    if fmt == "xlsx":
        return _build_xlsx(entity_type, fields)
    if fmt == "xls":
        return _build_xls(entity_type, fields)
    raise AppError(ErrorCode.VALIDATION, f"Формат шаблона {fmt!r} не поддерживается")
