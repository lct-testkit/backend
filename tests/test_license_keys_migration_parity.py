"""Копия нормализации внутри миграции 0026 не должна расходиться с
`app.modules.catalog.license_keys` (см. докстринг миграции и «Риски и
откат» задачи B3-1: копия внутри файла миграции — намеренная развязка от
будущих правок `app/`, но за паритетом на сегодняшний день следит этот
тест).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from app.modules.catalog import license_keys as app_keys

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "migrations"
    / "versions"
    / "0041_license_key_catalog_link.py"
)


def _load_migration_module():
    spec = importlib.util.spec_from_file_location(
        "migration_0041_license_key_catalog_link", _MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # `alembic.op` не инициализирован вне миграционного контекста — модуль
    # импортируется только за функциями нормализации, их вызов не трогает `op`.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def migration():
    return _load_migration_module()


_SAMPLES = [
    "ООО «Базис»",
    'ооо "Базис"',
    "ПАО Ростелеком",
    "RT.DataLake",
    "№ Д-12 / 2024",
    "д-12/2024",
    "N12-2024",
    "  МГТУ им. Баумана  ",
    "",
    None,
    "Яндекс — Облако",
    "Компания «А» / «Б»",
]


class TestCompanyKeyParity:
    @pytest.mark.parametrize("value", _SAMPLES)
    def test_vendor_key_matches_migration_copy(self, migration, value: object) -> None:
        assert migration._company_key(value) == app_keys.vendor_key(value)

    @pytest.mark.parametrize("value", _SAMPLES)
    def test_product_key_matches_migration_copy(self, migration, value: object) -> None:
        assert migration._company_key(value) == app_keys.product_key(value)


class TestContractKeyParity:
    @pytest.mark.parametrize("value", _SAMPLES)
    def test_contract_key_matches_migration_copy(self, migration, value: object) -> None:
        assert migration._contract_key(value) == app_keys.contract_key(value)
