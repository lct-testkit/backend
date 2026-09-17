"""Тесты локального реестра ЕГРЮЛ и автоподстановки (спринт 5, раздел 5.11).

Как и в остальных тестах этого репозитория, здесь нет поднятых
PostgreSQL/Redis: покрываются чистые функции — разбор XML-выгрузки
(`registry.egrul_xml`), провайдер без БД (`MockProvider`), маскирование
запроса автоподстановки и права. Реальный доступ к `egrul_entries`/
`registry_versions` (провайдеры с сессией, фоновая задача импорта) не
покрыт юнит-тестами по той же причине, что и остальной DB-слой репозитория —
не потому что не важен, а потому что здесь для него нет инфраструктуры.
"""

from __future__ import annotations

import datetime as dt
import io

from app.core.permissions import Permission, has_permission
from app.modules.registry.egrul_xml import iter_entries
from app.modules.registry.models import EgrulStatus, is_educational_okved
from app.modules.registry.providers import MockProvider
from app.modules.registry.service import _mask_query


def _xml(*bodies: str) -> bytes:
    joined = "\n".join(bodies)
    return f"<?xml version='1.0' encoding='UTF-8'?><EGRUL>{joined}</EGRUL>".encode()


class TestEgrulXmlParsing:
    def test_parses_full_entry_with_educational_okved(self) -> None:
        xml = _xml(
            """
            <СвЮЛ ОГРН="1027700132195" ИНН="7707049388" ДатаОГРН="1998-08-19">
              <СвНаимЮЛ НаимЮЛПолн="ФГБОУ ВО ТЕСТ" НаимЮЛСокр="ТЕСТ ВУЗ"/>
              <СвАдресЮЛ>
                <АдресРФ КодРегион="77" Регион="Г.МОСКВА">
                  <Улица НаимУлица="ЛЕНИНСКИЕ ГОРЫ"/>
                  <Дом Дом="1"/>
                </АдресРФ>
              </СвАдресЮЛ>
              <СведОКВЭД>
                <СвОКВЭДОсн КодОКВЭД="85.22"/>
                <СвОКВЭДДоп КодОКВЭД="85.23"/>
              </СведОКВЭД>
              <СвСтатус НаимСтатус="Действующая организация"/>
              <СвУчетНО КПП="770701001"/>
              <СвРуководитель>
                <ФИОРуководителя Фамилия="Иванов" Имя="Иван" Отчество="Иванович"/>
                <СвДолжн НаимДолжн="Ректор"/>
              </СвРуководитель>
            </СвЮЛ>
            """
        )
        entries = list(iter_entries(io.BytesIO(xml)))
        assert len(entries) == 1
        entry = entries[0]

        assert entry.inn == "7707049388"
        assert entry.ogrn == "1027700132195"
        assert entry.kpp == "770701001"
        assert entry.full_name == "ФГБОУ ВО ТЕСТ"
        assert entry.short_name == "ТЕСТ ВУЗ"
        assert entry.status == EgrulStatus.ACTIVE.value
        assert entry.region_code == "77"
        assert "ЛЕНИНСКИЕ ГОРЫ" in (entry.legal_address or "")
        assert entry.okved_main == "85.22"
        assert entry.okved_extra == ["85.23"]
        assert entry.is_educational is True
        assert entry.director_name == "Иванов Иван Иванович"
        assert entry.director_position == "Ректор"
        assert entry.registration_date == dt.date(1998, 8, 19)

    def test_liquidating_status_detected_from_status_name(self) -> None:
        xml = _xml(
            '<СвЮЛ ИНН="7736207543" ОГРН="1027700229193">'
            '<СвНаимЮЛ НаимЮЛПолн="ООО ТЕСТ"/>'
            '<СвСтатус НаимСтатус="Находится в процессе ликвидации"/>'
            "</СвЮЛ>"
        )
        entries = list(iter_entries(io.BytesIO(xml)))
        assert entries[0].status == EgrulStatus.LIQUIDATING.value

    def test_termination_date_without_status_name_implies_liquidated(self) -> None:
        xml = _xml(
            '<СвЮЛ ИНН="7736207543">'
            '<СвНаимЮЛ НаимЮЛПолн="ООО ТЕСТ"/>'
            '<СвПрекрЮЛ ДатаПрекр="2020-01-01"/>'
            "</СвЮЛ>"
        )
        entries = list(iter_entries(io.BytesIO(xml)))
        assert entries[0].status == EgrulStatus.LIQUIDATED.value
        assert entries[0].termination_date == dt.date(2020, 1, 1)

    def test_missing_inn_is_skipped_not_raised(self) -> None:
        xml = _xml(
            '<СвЮЛ ОГРН="1027700132195"><СвНаимЮЛ НаимЮЛПолн="БЕЗ ИНН"/></СвЮЛ>',
            '<СвЮЛ ИНН="7707049388"><СвНаимЮЛ НаимЮЛПолн="С ИНН"/></СвЮЛ>',
        )
        entries = list(iter_entries(io.BytesIO(xml)))
        assert len(entries) == 1
        assert entries[0].inn == "7707049388"

    def test_non_educational_okved_flagged_false(self) -> None:
        xml = _xml(
            '<СвЮЛ ИНН="7707049388">'
            '<СвНаимЮЛ НаимЮЛПолн="ООО РОГА И КОПЫТА"/>'
            '<СведОКВЭД><СвОКВЭДОсн КодОКВЭД="46.90"/></СведОКВЭД>'
            "</СвЮЛ>"
        )
        entries = list(iter_entries(io.BytesIO(xml)))
        assert entries[0].is_educational is False


class TestIsEducationalOkved:
    def test_main_code_prefix_85(self) -> None:
        assert is_educational_okved("85.22", None) is True

    def test_extra_code_prefix_85(self) -> None:
        assert is_educational_okved("46.90", ["85.21"]) is True

    def test_no_matching_prefix(self) -> None:
        assert is_educational_okved("46.90", ["47.11"]) is False

    def test_none_values(self) -> None:
        assert is_educational_okved(None, None) is False


class TestMockProvider:
    async def test_suggest_matches_by_partial_inn(self) -> None:
        provider = MockProvider()
        results = await provider.suggest("7707", 5)
        assert results
        assert results[0].inn == "7707049388"

    async def test_suggest_no_match_returns_empty(self) -> None:
        provider = MockProvider()
        assert await provider.suggest("0000000000", 5) == []

    async def test_get_by_inn_known(self) -> None:
        provider = MockProvider()
        details = await provider.get_by_inn("7707049388")
        assert details is not None
        assert details.provider == "mock"

    async def test_get_by_inn_unknown_returns_none(self) -> None:
        provider = MockProvider()
        assert await provider.get_by_inn("0000000000") is None

    async def test_health_always_true(self) -> None:
        assert await MockProvider().health() is True


class TestMaskQuery:
    def test_numeric_query_masked_as_inn(self) -> None:
        assert _mask_query("7707049388") == "77******88"

    def test_short_text_fully_redacted(self) -> None:
        assert _mask_query("МГУ") == "***"

    def test_long_text_keeps_edges(self) -> None:
        masked = _mask_query("Московский государственный университет")
        assert masked.startswith("Мо")
        assert masked.endswith("ет")
        assert "***" in masked


class TestRegistryPermissions:
    def test_kam_can_use_org_lookup(self) -> None:
        assert has_permission("KAM", Permission.ORG_LOOKUP_USE)
        assert has_permission("HEAD", Permission.ORG_LOOKUP_USE)
        assert has_permission("ADMIN", Permission.ORG_LOOKUP_USE)

    def test_only_admin_imports_registry(self) -> None:
        assert not has_permission("KAM", Permission.REGISTRY_IMPORT)
        assert not has_permission("HEAD", Permission.REGISTRY_IMPORT)
        assert has_permission("ADMIN", Permission.REGISTRY_IMPORT)

    def test_auditor_and_integration_have_no_org_lookup(self) -> None:
        assert not has_permission("AUDITOR", Permission.ORG_LOOKUP_USE)
        assert not has_permission("INTEGRATION", Permission.ORG_LOOKUP_USE)
