"""B3-11: образцы файлов для демо собираются тем же генератором, что кладёт их в
`docs/samples/imports/` (`tools/make_import_samples.py`) — файлы из каталога недоступны из
backend-тестов напрямую (путь общий с демо-стендом, не фиксирован для CI), поэтому тест строит их
заново и прогоняет через настоящий dry-run."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import login
from tests.import_helpers import job_rows, start_import, token

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tools.make_import_samples as samples  # noqa: E402 — путь добавлен выше


async def _seed_universities() -> None:
    """Идемпотентно: тестовая БД не очищается между прогонами — повторный вызов не должен
    множить вузы с одним названием (иначе `LicenseMatcher` на следующем прогоне нашёл бы их
    несколько и отдал бы `ambiguous` там, где образец ожидает чистый dry-run)."""
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.catalog.models import Organization

    async with session_scope() as session:
        for name in samples.UNIVERSITIES:
            existing = await session.scalar(select(Organization).where(Organization.name == name))
            if existing is None:
                session.add(Organization(name=name, org_type="university"))
        await session.flush()


class TestLicensesTenColumnsSample:
    def test_xlsx_sample_has_three_errors_and_27_good_rows(self, client, monkeypatch) -> None:
        run(client, _seed_universities)
        head = login(client, "HEAD")
        content = samples.make_licenses_10_columns_xlsx()
        started = start_import(
            client, monkeypatch, head, entity_type="license", content=content, source_format="xlsx"
        )
        dry = started["dry"]
        assert dry["error_rows"] == 3, job_rows(client, started["id"], status="error")
        assert dry["ok_rows"] + dry["warn_rows"] == 27

    def test_xls_sample_has_three_errors_and_27_good_rows(self, client, monkeypatch) -> None:
        run(client, _seed_universities)
        head = login(client, "HEAD")
        content = samples.make_licenses_10_columns_xls()
        started = start_import(
            client, monkeypatch, head, entity_type="license", content=content, source_format="xls"
        )
        dry = started["dry"]
        assert dry["error_rows"] == 3, job_rows(client, started["id"], status="error")
        assert dry["ok_rows"] + dry["warn_rows"] == 27


class TestLicensesMultisheetSample:
    def test_merges_three_years_with_fill_down_for_vendor(self, client, monkeypatch) -> None:
        run(client, _seed_universities)
        head = login(client, "HEAD")
        content = samples.make_licenses_multisheet()
        from tests.import_helpers import _seed_file, stub_storage

        stub_storage(monkeypatch, content)
        file_id = run(
            client, _seed_file, content, head.id, f"ms-{token()}.xlsx", "application/octet-stream"
        )
        created = client.post(
            "/api/imports",
            json={
                "file_id": str(file_id),
                "entity_type": "license",
                "mode": "upsert",
                "source_format": "xlsx",
            },
        )
        assert created.status_code == 201, created.text
        job_id = created.json()["id"]

        profile = client.get(f"/api/imports/{job_id}/profile", params={"header_row": 3})
        assert profile.status_code == 200, profile.text
        body = profile.json()
        assert len(body["sheets"]) == 3

        saved = client.put(
            f"/api/imports/{job_id}/mapping",
            json={
                "mapping": body["suggested_mapping"],
                "options": {"sheets": ["*"], "header_row": 3, "fill_down": ["Вендор"]},
            },
        )
        assert saved.status_code == 200, saved.text
        dry = client.post(f"/api/imports/{job_id}/dry-run")
        assert dry.status_code == 200, dry.text
        assert dry.json()["total_rows"] == 9  # 3 листа x 3 строки


class TestDirectionsAndResponsibleSamples:
    def test_directions_sample_applies_cleanly(self, client, monkeypatch) -> None:
        admin = login(client, "ADMIN")
        content = samples.make_directions()
        started = start_import(
            client,
            monkeypatch,
            admin,
            entity_type="direction",
            content=content,
            source_format="xlsx",
        )
        assert started["dry"]["error_rows"] == 0, job_rows(client, started["id"], status="error")

    def test_responsible_sample_applies_cleanly(self, client, monkeypatch) -> None:
        run(client, _seed_universities)
        admin = login(client, "ADMIN")

        async def seed_managers() -> None:
            # Идемпотентно (как и реальный демо-сид, B3-11): тестовая БД не очищается между
            # прогонами, повторная загрузка образца не должна упираться в уникальность email.
            import uuid

            from sqlalchemy import select

            from app.core.db import session_scope
            from app.modules.identity.models import User

            managers = [
                ("ivanov@example.com", "Иванов Иван Иванович", "KAM"),
                ("petrova@example.com", "Петрова Анна Сергеевна", "KAM"),
                ("sidorov@example.com", "Сидоров Пётр Николаевич", "HEAD"),
            ]
            async with session_scope() as session:
                for email, full_name, role in managers:
                    existing = await session.scalar(select(User).where(User.email == email))
                    if existing is not None:
                        continue
                    session.add(
                        User(
                            keycloak_id=str(uuid.uuid4()),
                            email=email,
                            full_name=full_name,
                            role=role,
                            status="active",
                            consent_version="1.0",
                        )
                    )
                await session.flush()

        run(client, seed_managers)
        content = samples.make_responsible()
        started = start_import(
            client,
            monkeypatch,
            admin,
            entity_type="responsible",
            content=content,
            source_format="xlsx",
        )
        assert started["dry"]["error_rows"] == 0, job_rows(client, started["id"], status="error")
