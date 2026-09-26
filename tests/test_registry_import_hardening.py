"""Импорт выгрузки ЕГРЮЛ и сверка дрейфа: лимит параметров, повторный запуск, пустой и битый файл.

Найдено внешним тестированием: партия 5000×21 столбец превышала лимит asyncpg (32 767 параметров)
и любая реальная выгрузка падала на первой же партии; версия шла одной транзакцией и коммитила
`running` только в конце (следующий тик запускал импорт повторно); пустой или обрезанный файл
завершался как `completed`; выгрузка читалась в память целиком; сверка держала глобальный лок
аудита на весь проход и раз в интервал заново поднимала версию организации и слала уведомление.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import io
import pathlib
import uuid

import pytest
from lxml import etree

from app.modules.registry import tasks as registry_tasks
from app.modules.registry.egrul_xml import iter_entries
from tests.conftest import TEST_DATABASE_URL, run

pytestmark_db = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _xml(*bodies: str) -> bytes:
    joined = "\n".join(bodies)
    return f"<?xml version='1.0' encoding='UTF-8'?><EGRUL>{joined}</EGRUL>".encode()


def _entry(inn: str, okved: str = "85.22", name: str = "ВУЗ") -> str:
    return (
        f'<СвЮЛ ИНН="{inn}" ОГРН="1027700132195">'
        f'<СвНаимЮЛ НаимЮЛПолн="{name}" НаимЮЛСокр="{name}"/>'
        f'<СведОКВЭД><СвОКВЭДОсн КодОКВЭД="{okved}"/></СведОКВЭД>'
        '<СвСтатус НаимСтатус="Действующая организация"/></СвЮЛ>'
    )


def _inn() -> str:
    return str(7_000_000_000 + int(uuid.uuid4().hex[:8], 16) % 999_999_999).zfill(10)[:10]


class TestParsing:
    def test_batch_fits_the_asyncpg_parameter_limit(self) -> None:
        assert registry_tasks._ENTRY_BATCH_SIZE * 21 < 32_767

    def test_truncated_file_is_an_error_by_default(self) -> None:
        whole = _xml(_entry("7707049388"), _entry("7736207543"))
        truncated = whole[: len(whole) - 60]

        with pytest.raises(etree.XMLSyntaxError):
            list(iter_entries(io.BytesIO(truncated)))

    def test_lenient_mode_is_still_available_for_callers_that_want_it(self) -> None:
        whole = _xml(_entry("7707049388"))
        assert list(iter_entries(io.BytesIO(whole[:-10]), strict=False))


class _Storage:
    """Подмена скачивания: пишет заданные байты в файл и возвращает sha256, как настоящая."""

    def __init__(self, content: bytes) -> None:
        self.content = content

    async def __call__(self, *, bucket: str, key: str, path: str) -> str:
        pathlib.Path(path).write_bytes(self.content)
        return hashlib.sha256(self.content).hexdigest()


async def _pending_version(content_marker: str = "x") -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.files.models import File
    from app.modules.registry.models import RegistryVersion

    async with session_scope() as session:
        file = File(
            storage_key=f"registry/{uuid.uuid4()}.xml",
            bucket="files",
            original_filename="egrul.xml",
            mime_type="application/xml",
            size_bytes=10,
            status="ready",
            refcount=1,
        )
        session.add(file)
        await session.flush()
        version = RegistryVersion(source="fns_egrul", file_id=file.id, status="pending")
        session.add(version)
        await session.flush()
        return version.id


async def _status(version_id: uuid.UUID) -> tuple[str, int, str | None, str | None]:
    from app.core.db import session_scope
    from app.modules.registry.models import RegistryVersion

    async with session_scope() as session:
        version = await session.get(RegistryVersion, version_id)
        return version.status, version.entries_count, version.error, version.checksum


async def _drain_pending_except(version_id: uuid.UUID) -> None:
    """Общая БД тестов: чужие `pending`-версии не должны попасть в наш проход."""
    from sqlalchemy import update

    from app.core.db import session_scope
    from app.modules.registry.models import RegistryVersion

    async with session_scope() as session:
        await session.execute(
            update(RegistryVersion)
            .where(RegistryVersion.status == "pending", RegistryVersion.id != version_id)
            .values(status="failed", error="снято тестом")
        )


async def _import(monkeypatch_storage, content: bytes) -> tuple[str, int, str | None, str | None]:
    version_id = await _pending_version()
    await _drain_pending_except(version_id)
    registry_tasks.download_object_to_file = _Storage(content)  # type: ignore[assignment]
    await registry_tasks.sweep_registry_imports({})
    return await _status(version_id)


@pytest.fixture
def storage(monkeypatch):
    original = registry_tasks.download_object_to_file
    yield None
    registry_tasks.download_object_to_file = original


@pytestmark_db
class TestImportOutcome:
    def test_valid_file_is_completed_with_the_streamed_checksum(self, client, storage) -> None:
        content = _xml(
            _entry(_inn()), _entry(_inn()), _entry(_inn()), _entry(_inn(), okved="46.90")
        )

        status, count, error, checksum = run(client, _import, None, content)

        assert status == "completed", error
        assert count == 3  # четвёртая запись — не образовательная
        assert checksum == hashlib.sha256(content).hexdigest()

    def test_duplicate_inn_in_one_batch_does_not_fail_the_import(self, client, storage) -> None:
        inn = _inn()
        content = _xml(_entry(inn, name="ПЕРВЫЙ"), _entry(inn, name="ВТОРОЙ"))

        status, count, error, _checksum = run(client, _import, None, content)

        assert status == "completed", error
        assert count == 1

    def test_file_without_a_single_legal_entity_is_a_failure(self, client, storage) -> None:
        status, count, error, _ = run(client, _import, None, _xml())

        assert status == "failed"
        assert count == 0 and "нет ни одной записи" in (error or "")

    def test_file_without_educational_organizations_is_a_failure(self, client, storage) -> None:
        status, count, error, _ = run(
            client,
            _import,
            None,
            _xml(_entry(_inn(), okved="46.90"), _entry(_inn(), okved="47.11")),
        )

        assert status == "failed"
        assert "образовательн" in (error or "")

    def test_truncated_file_is_a_failure_not_a_smaller_success(self, client, storage) -> None:
        whole = _xml(_entry(_inn()), _entry(_inn()))

        status, _count, error, _ = run(client, _import, None, whole[: len(whole) - 40])

        assert status == "failed"
        assert "Ошибка разбора" in (error or "")

    def test_garbage_is_a_failure(self, client, storage) -> None:
        status, _count, error, _ = run(client, _import, None, "это вообще не xml".encode())

        assert status == "failed", error

    def test_entries_committed_before_a_failure_stay(self, client, storage) -> None:
        # Партия за партией в собственных транзакциях: сбой на второй партии не откатывает первую.
        first, second, third = _inn(), _inn(), _inn()
        content = _xml(_entry(first), _entry(second), _entry(third))
        original_batch = registry_tasks._ENTRY_BATCH_SIZE
        original_upsert = registry_tasks._upsert_batch
        calls = {"n": 0}

        async def _flaky(session, batch, version_id) -> int:
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("сбой второй партии")
            return await original_upsert(session, batch, version_id)

        registry_tasks._ENTRY_BATCH_SIZE = 2
        registry_tasks._upsert_batch = _flaky  # type: ignore[assignment]
        try:
            status, _count, error, _ = run(client, _import, None, content)
        finally:
            registry_tasks._ENTRY_BATCH_SIZE = original_batch
            registry_tasks._upsert_batch = original_upsert  # type: ignore[assignment]

        assert status == "failed" and "сбой второй партии" in (error or "")

        async def _exists() -> tuple[bool, bool, bool]:
            from app.core.db import session_scope
            from app.modules.registry.models import EgrulEntry

            async with session_scope() as session:
                found = [
                    (await session.get(EgrulEntry, inn)) is not None
                    for inn in (first, second, third)
                ]
            return found[0], found[1], found[2]

        assert run(client, _exists) == (True, True, False)


@pytestmark_db
class TestOnlyOneWorkerTakesAVersion:
    def test_two_simultaneous_claims_never_return_the_same_version(self, client) -> None:
        async def _race() -> list:
            version_id = await _pending_version()
            await _drain_pending_except(version_id)
            claims = await asyncio.gather(
                registry_tasks._claim_pending_version(), registry_tasks._claim_pending_version()
            )
            return [c[0] for c in claims if c is not None] + [version_id]

        ids = run(client, _race)
        claimed = ids[:-1]
        assert len(claimed) == len(set(claimed)) <= 1

    def test_a_running_version_left_by_a_dead_worker_is_failed_after_a_while(self, client) -> None:
        from sqlalchemy import update

        from app.core.db import session_scope
        from app.modules.registry.models import RegistryVersion

        async def _scenario() -> tuple[str, str | None]:
            version_id = await _pending_version()
            async with session_scope() as session:
                await session.execute(
                    update(RegistryVersion)
                    .where(RegistryVersion.id == version_id)
                    .values(
                        status="running",
                        updated_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=13),
                    )
                )
            await registry_tasks._fail_stale_running_versions()
            status, _count, error, _checksum = await _status(version_id)
            return status, error

        status, error = run(client, _scenario)
        assert status == "failed" and "прерван" in (error or "")


@pytestmark_db
class TestDriftSweep:
    def test_unchanged_drift_is_not_reported_again(self, client) -> None:
        from app.core.db import session_scope
        from app.modules.catalog.models import Organization
        from app.modules.registry.models import EgrulEntry

        inn = _inn()

        async def _prepare() -> uuid.UUID:
            async with session_scope() as session:
                session.add(
                    EgrulEntry(
                        inn=inn,
                        full_name="ИМЯ ИЗ РЕЕСТРА",
                        status="active",
                        is_educational=True,
                    )
                )
                org = Organization(name="Имя в CRM", org_type="university", inn=inn)
                session.add(org)
                await session.flush()
                return org.id

        org_id = run(client, _prepare)

        async def _reset_and_sweep() -> tuple[int, int, int]:
            from sqlalchemy import update

            async with session_scope() as session:
                await session.execute(
                    update(Organization)
                    .where(Organization.id == org_id)
                    .values(registry_checked_at=None)
                )
            first = await registry_tasks.sweep_registry_drift({})
            async with session_scope() as session:
                version_after_first = (await session.get(Organization, org_id)).version
                await session.execute(
                    update(Organization)
                    .where(Organization.id == org_id)
                    .values(registry_checked_at=None)
                )
            second = await registry_tasks.sweep_registry_drift({})
            async with session_scope() as session:
                version_after_second = (await session.get(Organization, org_id)).version
            return first["drifted"], second["drifted"], version_after_second - version_after_first

        first_drifted, second_drifted, version_delta = run(client, _reset_and_sweep)

        assert first_drifted >= 1
        assert version_delta == 0, "то же расхождение не должно заново поднимать версию"
        assert second_drifted == 0 or second_drifted < first_drifted
