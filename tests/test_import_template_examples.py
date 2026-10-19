"""M-3 (QA 20.10): в шаблоне каждого типа импорта есть две учебные строки, и они проходят
проверку полей (иначе «скачал, не менял, загрузил» давало бы ошибки). Чистые функции, без БД."""

from __future__ import annotations

import io

import openpyxl
import pytest

from app.modules.imports.fields import ENTITY_TYPE_LABELS, fields_for, validate_field
from app.modules.imports.templates import _example_rows, build_template


@pytest.mark.parametrize("entity_type", list(ENTITY_TYPE_LABELS))
def test_two_example_rows_and_every_cell_is_valid(entity_type: str) -> None:
    fields = fields_for(entity_type)
    rows = _example_rows(entity_type, fields)
    assert len(rows) == 2
    for row in rows:
        assert len(row) == len(fields)
        for spec, cell in zip(fields, row, strict=True):
            if spec.required:
                assert cell.strip(), (entity_type, spec.label)
            if not cell.strip():
                continue
            if spec.kind in ("organization_name", "region_code", "direction_code"):
                continue  # справочники проверяются при применении (нужна БД)
            _, error = validate_field(spec, cell)
            assert error is None, (entity_type, spec.label, cell, error)


@pytest.mark.parametrize("entity_type", list(ENTITY_TYPE_LABELS))
def test_xlsx_template_contains_the_examples(entity_type: str) -> None:
    workbook = openpyxl.load_workbook(io.BytesIO(build_template(entity_type, "xlsx")))
    assert workbook["Данные"].max_row == 3  # шапка и две учебные строки
