"""Тесты локального реестра ЕГРЮЛ и автоподстановки (спринт 5, раздел 5.11).

Как и в остальных тестах этого репозитория, здесь нет поднятых
PostgreSQL/Redis: покрываются чистые функции — разбор XML-выгрузки
(`registry.egrul_xml`), провайдер без БД (`MockProvider`), маскирование
запроса автоподстановки и права. Реальный доступ к `egrul_entries`/
`registry_versions` (провайдеры с сессией, фоновая задача импорта) не
покрыт юнит-тестами по той же причине, что и остальной DB-слой репозитория —
не потому что не важен, а потому что здесь для него нет инфраструктуры.
Исключение — `TestDeleteRegistryVersion` (П4): настоящая Postgres
обязательна, тот же приём, что `tests/test_imports.py::
TestLicenseImportEndToEnd` — см. `tests/conftest.py`.
"""

from __future__ import annotations

import datetime as dt
import io
import uuid

import pytest

from app.core.permissions import Permission, has_permission
from app.modules.registry.egrul_xml import iter_entries
from app.modules.registry.models import EgrulStatus, is_educational_okved
from app.modules.registry.providers import MockProvider
from app.modules.registry.service import _mask_query
from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run


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


class TestDeleteRegistryVersion:
    """П4: `DELETE /api/admin/registry/versions/{id}` — нельзя оставить
    систему без реестра. Настоящая Postgres обязательна — см. докстринг
    модуля."""

    pytestmark = pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
    )

    async def _seed_version(self, status: str) -> uuid.UUID:
        from app.core.db import session_scope
        from app.modules.registry.models import RegistryVersion

        async with session_scope() as session:
            version = RegistryVersion(
                source="fns_egrul",
                file_id=uuid.uuid4(),
                status=status,
            )
            session.add(version)
            await session.flush()
            return version.id

    def _admin(self, client) -> None:
        admin = run(client, _make_user, "ADMIN")
        csrf = authenticate(client, admin)
        client.headers["X-CSRF-Token"] = csrf

    def test_deletes_a_completed_version_when_another_one_remains(self, client) -> None:
        self._admin(client)
        run(client, self._seed_version, "completed")
        extra_id = run(client, self._seed_version, "completed")

        delete = client.delete(f"/api/admin/registry/versions/{extra_id}")
        assert delete.status_code == 204, delete.text

    async def _clear_other_completed_versions(self) -> None:
        """Другие тесты этого же класса тоже заводят `completed`-версии в
        той же настоящей Postgres (`client` не пересоздаёт БД между тестами
        — только приложение) — без явной чистки «это последняя завершённая»
        зависело бы от порядка запуска. Безопасно только потому, что это
        собственная тестовая БД агента, не `rtk-crm-postgres-1`."""
        from sqlalchemy import delete as sa_delete

        from app.core.db import session_scope
        from app.modules.registry.models import RegistryVersion

        async with session_scope() as session:
            await session.execute(
                sa_delete(RegistryVersion).where(RegistryVersion.status == "completed")
            )

    def test_cannot_delete_the_last_completed_version(self, client) -> None:
        self._admin(client)
        run(client, self._clear_other_completed_versions)
        only_id = run(client, self._seed_version, "completed")

        delete = client.delete(f"/api/admin/registry/versions/{only_id}")
        assert delete.status_code == 409, delete.text
        assert delete.json()["code"] == "CRM-1303"

    def test_cannot_delete_a_version_that_is_currently_running(self, client) -> None:
        self._admin(client)
        # Есть и другая completed-версия — блокирующая причина здесь именно
        # "running", не "последняя завершённая".
        run(client, self._seed_version, "completed")
        running_id = run(client, self._seed_version, "running")

        delete = client.delete(f"/api/admin/registry/versions/{running_id}")
        assert delete.status_code == 409, delete.text
        assert delete.json()["code"] == "CRM-1303"

    def test_deletes_a_failed_version_freely(self, client) -> None:
        self._admin(client)
        run(client, self._seed_version, "completed")
        failed_id = run(client, self._seed_version, "failed")

        delete = client.delete(f"/api/admin/registry/versions/{failed_id}")
        assert delete.status_code == 204, delete.text
