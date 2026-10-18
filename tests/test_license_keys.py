"""Составной ключ лицензии (B3-1): `vendor_key`, `product_key`, `contract_key`.

Без БД — чистые функции нормализации. См. `app.modules.catalog.license_keys`
и докстринг `OrganizationLicense` про то, зачем составной ключ вместо
единственного `contract_number` (BUG-9).
"""

from __future__ import annotations

import uuid

import pytest

from app.modules.catalog.license_keys import (
    LicenseKey,
    contract_key,
    product_key,
    vendor_key,
)


class TestContractKey:
    def test_strips_typographic_prefix_and_spaces(self) -> None:
        assert contract_key("№ Д-12 / 2024") == contract_key("д-12/2024")

    @pytest.mark.parametrize("value", [None, "", "   ", " "])
    def test_empty_or_blank_gives_empty_string_not_none(self, value: object) -> None:
        # Частичный уникальный индекс в БД не ловит дубли по NULL — пустой
        # номер договора должен быть пустой строкой, а не None.
        assert contract_key(value) == ""
        assert isinstance(contract_key(value), str)

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("No 12/2024", "n12/2024"),
            ("N. 12-2024", "12-2024"),
            ("Д-12 / 2024", "д-12/2024"),
        ],
    )
    def test_prefix_variants_match(self, left: str, right: str) -> None:
        assert contract_key(left) == contract_key(right)

    def test_different_numbers_differ(self) -> None:
        assert contract_key("Д-12/2024") != contract_key("Д-13/2024")


class TestVendorAndProductKey:
    def test_quotes_and_case_do_not_differ(self) -> None:
        assert vendor_key("ООО «Базис»") == vendor_key('ооо "Базис"')

    def test_legal_form_differs(self) -> None:
        # Организационно-правовая форма остаётся в ключе: ПАО и ООО с одним
        # названием — разные юрлица (см. docstring company_key).
        assert vendor_key("ПАО «Ростелеком»") != vendor_key("ООО «Ростелеком»")

    def test_product_key_same_rules_as_vendor(self) -> None:
        assert product_key("RT.DataLake") == product_key("rt.datalake")


class TestLicenseKeyBuild:
    def test_build_combines_all_four_parts(self) -> None:
        org_id = uuid.uuid4()
        key = LicenseKey.build(
            org_id,
            vendor="ООО «Базис»",
            product_name="RT.DataLake",
            contract_number="№ Д-12/2024",
        )
        assert key.organization_id == org_id
        assert key.vendor_key == vendor_key("ООО Базис")
        assert key.product_key == product_key("RT.DataLake")
        assert key.contract_key == contract_key("Д-12/2024")
        assert key.as_tuple() == (org_id, key.vendor_key, key.product_key, key.contract_key)

    def test_empty_contract_number_is_empty_string_in_key(self) -> None:
        key = LicenseKey.build(
            uuid.uuid4(), vendor="Вендор", product_name="ПО", contract_number=None
        )
        assert key.contract_key == ""
