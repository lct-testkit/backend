"""Тесты каталога: организации, контакты, справочники (спринт 4, раздел 6).

Как и `tests/test_deals.py`, здесь нет реального Postgres/Redis: покрываются
чистые функции (контрольные суммы ИНН/КПП/ОГРН, маскирование, права),
которые ломаются молча и не требуют сессии БД. Проверка ИНН — самая
рискованная часть: реальные компании (Сбербанк, Яндекс, Ростелеком) взяты
как фикстуры, потому что придуманный вручную номер легко случайно окажется
валидным по контрольной сумме и не поймает регрессию в весах.
"""

from __future__ import annotations

import uuid

from app.core.permissions import Permission, has_permission
from app.modules.catalog.schemas import ContactOut
from app.modules.catalog.validators import (
    validate_inn,
    validate_kpp,
    validate_ogrn,
    validate_ogrnip,
    validate_requisite,
)


class _Contact:
    """Минимальный дублёр `Contact` ORM-модели — только поля, которые
    читает `ContactOut.from_model`."""

    def __init__(self, **overrides: object) -> None:
        self.id = uuid.uuid4()
        self.organization_id = None
        self.first_name = "Иван"
        self.last_name = "Петров"
        self.middle_name = None
        self.position = "Проректор"
        self.email = "ivanov@university.ru"
        self.phone = "+79991234567"
        self.is_decision_maker = True
        self.is_anonymized = False
        self.source = None
        self.external_ids: dict[str, object] = {}
        self.version = 1
        import datetime as dt

        now = dt.datetime.now(dt.UTC)
        self.created_at = now
        self.updated_at = now
        for key, value in overrides.items():
            setattr(self, key, value)


class TestInnChecksum:
    """Реальные ИНН юрлиц (общедоступные регистрационные данные)."""

    def test_sberbank_inn_10_digits_valid(self) -> None:
        assert validate_inn("7707083893").ok

    def test_yandex_inn_10_digits_valid(self) -> None:
        assert validate_inn("7736207543").ok

    def test_rostelecom_inn_10_digits_valid(self) -> None:
        assert validate_inn("7707049388").ok

    def test_known_valid_12_digit_inn(self) -> None:
        assert validate_inn("500100732259").ok

    def test_wrong_checksum_rejected(self) -> None:
        # Последняя цифра настоящего ИНН Ростелекома испорчена намеренно.
        result = validate_inn("7707049380")
        assert not result.ok
        assert result.reason

    def test_non_digit_rejected(self) -> None:
        assert not validate_inn("770704938X").ok

    def test_wrong_length_rejected(self) -> None:
        assert not validate_inn("77070493").ok

    def test_all_same_digit_rejected(self) -> None:
        assert not validate_inn("1111111111").ok

    def test_invalid_region_code_rejected(self) -> None:
        # "00" не является кодом субъекта РФ.
        assert not validate_inn("0007049388").ok

    def test_none_rejected(self) -> None:
        assert not validate_inn(None).ok


class TestKppOgrnChecksum:
    def test_valid_kpp(self) -> None:
        assert validate_kpp("770701001").ok

    def test_kpp_wrong_length(self) -> None:
        assert not validate_kpp("7707010").ok

    def test_valid_ogrn(self) -> None:
        # ОГРН Сбербанка.
        assert validate_ogrn("1027700132195").ok

    def test_ogrn_wrong_checksum(self) -> None:
        assert not validate_ogrn("1027700132196").ok

    def test_ogrnip_wrong_length(self) -> None:
        assert not validate_ogrnip("12345").ok


class TestValidateRequisiteDispatch:
    def test_dispatches_by_kind(self) -> None:
        assert validate_requisite("inn", "7707049388").ok
        assert not validate_requisite("inn", "bad").ok

    def test_unknown_kind_rejected(self) -> None:
        result = validate_requisite("passport", "1234")
        assert not result.ok
        assert result.reason


class TestContactMasking:
    def test_from_model_masks_phone_and_email(self) -> None:
        contact = _Contact()
        out = ContactOut.from_model(contact)
        assert out.phone == "+7 (9**) ***-**-67"
        assert out.email == "i***@university.ru"
        # Незамаскированные поля переносятся как есть.
        assert out.first_name == "Иван"
        assert out.is_decision_maker is True

    def test_from_model_handles_missing_contacts(self) -> None:
        contact = _Contact(phone=None, email=None)
        out = ContactOut.from_model(contact)
        assert out.phone is None
        assert out.email is None


class TestCatalogPermissions:
    def test_kam_can_read_and_write_organizations_and_contacts(self) -> None:
        for perm in (
            Permission.ORG_READ,
            Permission.ORG_WRITE,
            Permission.CONTACT_READ,
            Permission.CONTACT_WRITE,
            Permission.CONTACT_REVEAL,
            Permission.CATALOG_READ,
        ):
            assert has_permission("KAM", perm)

    def test_kam_cannot_write_catalog_reference_data(self) -> None:
        # Продукты/направления/причины отказа/календарь — только ADMIN.
        assert not has_permission("KAM", Permission.CATALOG_WRITE)
        assert has_permission("ADMIN", Permission.CATALOG_WRITE)

    def test_auditor_has_no_organization_or_contact_access(self) -> None:
        for perm in (
            Permission.ORG_READ,
            Permission.CONTACT_READ,
            Permission.CONTACT_REVEAL,
            Permission.CATALOG_READ,
        ):
            assert not has_permission("AUDITOR", perm)

    def test_integration_can_read_organizations_but_not_contacts(self) -> None:
        # dop.md §11.8: организации — не ПДн, контакты — ПДн.
        assert has_permission("INTEGRATION", Permission.ORG_READ)
        assert has_permission("INTEGRATION", Permission.CONTACT_WRITE)
        assert not has_permission("INTEGRATION", Permission.CONTACT_READ)

    def test_only_head_and_admin_delete_files(self) -> None:
        assert not has_permission("KAM", Permission.FILE_DELETE)
        assert has_permission("HEAD", Permission.FILE_DELETE)
        assert has_permission("ADMIN", Permission.FILE_DELETE)
