"""Сопоставление вуза/вендора/ПО с каталогом для лицензий (B3-4, BUG-11).

Настоящая Postgres обязательна (см. `tests/conftest.py`): матчер читает
`organizations`/`products` напрямую. Без `TEST_DATABASE_URL` модуль
пропускается целиком.
"""

from __future__ import annotations

import uuid

import pytest

from app.modules.catalog.license_matching import LicenseMatcher
from tests.conftest import TEST_DATABASE_URL, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


async def _seed_university(
    session, name: str, *, short_name: str | None = None, inn: str | None = None
):
    from app.modules.catalog.models import Organization

    org = Organization(name=name, short_name=short_name, org_type="university", inn=inn)
    session.add(org)
    await session.flush()
    return org


async def _seed_company(session, name: str, *, short_name: str | None = None):
    from app.modules.catalog.models import Organization

    org = Organization(name=name, short_name=short_name, org_type="company")
    session.add(org)
    await session.flush()
    return org


async def _seed_product(session, name: str, *, kind: str = "software", vendor_id=None):
    from app.modules.catalog.models import Product

    product = Product(code=f"p-{uuid.uuid4().hex[:10]}", name=name, kind=kind, vendor_id=vendor_id)
    session.add(product)
    await session.flush()
    return product


class TestMatchUniversity:
    def test_two_universities_same_name_is_ambiguous(self, client) -> None:
        from app.core.db import session_scope

        name = f"Дубль {uuid.uuid4().hex[:8]}"

        async def scenario():
            async with session_scope() as session:
                await _seed_university(session, name)
                await _seed_university(session, name)
                return await LicenseMatcher(session).match_university(name)

        result = run(client, scenario)
        assert result.how == "ambiguous"
        assert result.entity is None
        assert len(result.candidates) == 2

    def test_inn_beats_name_match_of_a_different_university(self, client) -> None:
        from app.core.db import session_scope

        shared_name = f"Университет {uuid.uuid4().hex[:8]}"
        inn = "".join(str((i * 7) % 10) for i in range(10))

        async def scenario():
            async with session_scope() as session:
                decoy = await _seed_university(session, shared_name)
                target = await _seed_university(session, f"{shared_name} (настоящий)", inn=inn)
                result = await LicenseMatcher(session).match_university(shared_name, inn=inn)
                return result, target.id, decoy.id

        result, target_id, decoy_id = run(client, scenario)
        assert result.how == "inn"
        assert result.entity is not None
        assert result.entity.id == target_id
        assert result.entity.id != decoy_id

    def test_found_by_short_name(self, client) -> None:
        from app.core.db import session_scope

        full_name = f"ФГБОУ ВО Технический университет {uuid.uuid4().hex[:8]}"
        short = f"МГТУ-{uuid.uuid4().hex[:6]}"

        async def scenario():
            async with session_scope() as session:
                await _seed_university(session, full_name, short_name=short)
                return await LicenseMatcher(session).match_university(short)

        result = run(client, scenario)
        assert result.how == "short_name"
        assert result.entity is not None
        assert result.entity.short_name == short

    def test_found_after_normalization(self, client) -> None:
        from app.core.db import session_scope

        name = f"ООО «Базис {uuid.uuid4().hex[:6]}»"

        async def scenario():
            async with session_scope() as session:
                # org_type=university, но название с кавычками — проверяем именно нормализацию
                await _seed_university(session, name)
                return await LicenseMatcher(session).match_university(
                    name.replace("«", '"').replace("»", '"').upper()
                )

        result = run(client, scenario)
        assert result.how == "normalized"
        assert result.entity is not None

    def test_found_after_stripping_organizational_noise(self, client) -> None:
        from app.core.db import session_scope

        core = f"Бауманский университет {uuid.uuid4().hex[:6]}"
        full_name = f"ФГБОУ ВО {core}"

        async def scenario():
            async with session_scope() as session:
                await _seed_university(session, full_name)
                return await LicenseMatcher(session).match_university(core)

        result = run(client, scenario)
        assert result.how == "stripped"
        assert result.entity is not None

    def test_not_found_gives_suggestions(self, client) -> None:
        from app.core.db import session_scope

        name = f"Университет Правильный {uuid.uuid4().hex[:8]}"
        typo = name.replace("Правильный", "Правилный")

        async def scenario():
            async with session_scope() as session:
                await _seed_university(session, name)
                return await LicenseMatcher(session).match_university(typo)

        result = run(client, scenario)
        assert result.how == "not_found"
        assert result.entity is None
        assert any(name in suggestion for suggestion in result.suggestions)

    def test_company_type_organization_is_not_a_candidate(self, client) -> None:
        from app.core.db import session_scope

        name = f"Общая Компания {uuid.uuid4().hex[:8]}"

        async def scenario():
            async with session_scope() as session:
                await _seed_company(session, name)
                return await LicenseMatcher(session).match_university(name)

        result = run(client, scenario)
        assert result.how == "not_found"


class TestMatchVendor:
    def test_quotes_and_case_do_not_matter(self, client) -> None:
        from app.core.db import session_scope

        name = f"ООО «Базис {uuid.uuid4().hex[:6]}»"

        async def scenario():
            async with session_scope() as session:
                await _seed_company(session, name)
                return await LicenseMatcher(session).match_vendor(
                    name.replace("«", '"').replace("»", '"').lower()
                )

        result = run(client, scenario)
        assert result.entity is not None
        assert result.how == "normalized"

    def test_not_found_without_candidates(self, client) -> None:
        from app.core.db import session_scope

        async def scenario():
            async with session_scope() as session:
                return await LicenseMatcher(session).match_vendor(
                    f"Несуществующий вендор {uuid.uuid4().hex[:8]}"
                )

        result = run(client, scenario)
        assert result.entity is None
        assert result.how == "not_found"


class TestMatchSoftware:
    def test_found_by_normalized_name(self, client) -> None:
        from app.core.db import session_scope

        name = f"RT.DataLake-{uuid.uuid4().hex[:6]}"

        async def scenario():
            async with session_scope() as session:
                await _seed_product(session, name, kind="software")
                return await LicenseMatcher(session).match_software(name.lower())

        result = run(client, scenario)
        assert result.entity is not None
        assert result.how == "normalized"

    def test_course_with_the_same_name_is_not_matched(self, client) -> None:
        """B3-7: `kind` разводит каталоги — курс с тем же названием не перехватывает поиск ПО."""
        from app.core.db import session_scope

        name = f"Общее название {uuid.uuid4().hex[:8]}"

        async def scenario():
            async with session_scope() as session:
                await _seed_product(session, name, kind="course")
                return await LicenseMatcher(session).match_software(name)

        result = run(client, scenario)
        assert result.entity is None
        assert result.how == "not_found"

    def test_ambiguous_resolved_by_vendor_hint(self, client) -> None:
        from app.core.db import session_scope

        name = f"Общее ПО {uuid.uuid4().hex[:8]}"

        async def scenario():
            async with session_scope() as session:
                vendor_a = await _seed_company(session, f"Вендор А {uuid.uuid4().hex[:6]}")
                vendor_b = await _seed_company(session, f"Вендор Б {uuid.uuid4().hex[:6]}")
                await _seed_product(session, name, kind="software", vendor_id=vendor_a.id)
                await _seed_product(session, name, kind="software", vendor_id=vendor_b.id)
                ambiguous = await LicenseMatcher(session).match_software(name)
                resolved = await LicenseMatcher(session).match_software(name, vendor_id=vendor_a.id)
                return ambiguous, resolved, vendor_a.id

        ambiguous, resolved, vendor_a_id = run(client, scenario)
        assert ambiguous.how == "ambiguous"
        assert resolved.entity is not None
        assert resolved.entity.vendor_id == vendor_a_id
