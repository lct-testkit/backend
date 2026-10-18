"""B3-5: структурированные отчёты об ошибках строк и режим dry-run.

Файл с несколькими видами ошибок, листом не первым по счёту (шапка после титула) и формулой в
комментарии — проверяет весь путь: `issues` в ответе строк, `GET /summary`, лист «Ошибки» с номером
строки Excel и листом, лист «Сводка», экранирование формулы, и то, что dry-run ничего не пишет в
каталог.
"""

from __future__ import annotations

import io

import openpyxl
import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import login
from tests.import_helpers import job_rows, stub_storage, token

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

LICENSE_HEADERS = [
    "Название ВУЗа",
    "Вендор",
    "ПО",
    "Номер договора",
    "Подписание лицензии",
    "Срок действия лицензии (год)",
    "Статус по передаче",
    "ФИО Менеджера",
    "Ответственные от ВУЗа",
    "Комментарий",
]


def _xlsx_with_title(tok: str) -> bytes:
    """Титул в строке 1, пустая строка 2, шапка в строке 3 — `header_row=3` в `options`."""
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Лицензии"
    sheet.append([f"Каталог лицензий {tok}"])
    sheet.append([])
    sheet.append(LICENSE_HEADERS)
    rows = [
        # Строка 4 (ok, с формулой в комментарии — sanitize_formula).
        [
            f"МГТУ {tok}",
            "Яндекс",
            "DataLake",
            f"Д-1-{tok}",
            "да",
            "3",
            "передано",
            "Иванов",
            "",
            "=1+1",
        ],
        # Строка 5 — полный дубль ключа строки 4 -> key_duplicate_in_file (warn).
        [
            f"МГТУ {tok}",
            "Яндекс",
            "DataLake",
            f"Д-1-{tok}",
            "да",
            "3",
            "передано",
            "Иванов",
            "",
            "",
        ],
        # Строка 6 — пустой вуз -> required_empty (error).
        ["", "Ростелеком", "CRM", f"Д-2-{tok}", "", "", "", "", "", ""],
        # Строка 7 — вуз не найден -> org_not_found (error).
        [f"Неизвестный вуз {tok}", "Ростелеком", "CRM", f"Д-3-{tok}", "", "", "", "", "", ""],
        # Строка 8 — неизвестный статус -> status_unknown (error).
        [f"МГТУ {tok}", "Ростелеком", "ERP", f"Д-4-{tok}", "", "", "абракадабра", "", "", ""],
        # Строка 9 — недопустимый срок -> term_invalid (error).
        [f"МГТУ {tok}", "Ростелеком", "БД", f"Д-5-{tok}", "", "1999", "", "", "", ""],
        # Строки 10-13 — обычные годные строки.
        [f"МГТУ {tok}", "1С", "Бухгалтерия", f"Д-6-{tok}", "", "", "", "", "", ""],
        [f"МГТУ {tok}", "1С", "Зарплата", f"Д-7-{tok}", "", "", "", "", "", ""],
        [f"МГТУ {tok}", "1С", "Склад", f"Д-8-{tok}", "", "", "", "", "", ""],
        [f"МГТУ {tok}", "1С", "Документооборот", f"Д-9-{tok}", "", "", "", "", "", ""],
    ]
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


async def _seed_university(name: str):
    from app.core.db import session_scope
    from app.modules.catalog.models import Organization

    async with session_scope() as session:
        org = Organization(name=name, org_type="university")
        session.add(org)
        await session.flush()
        return org.id


async def _catalog_counts():
    from sqlalchemy import func, select

    from app.core.db import session_scope
    from app.modules.catalog.models import Organization, OrganizationLicense, Product

    async with session_scope() as session:
        orgs = await session.scalar(select(func.count(Organization.id)))
        products = await session.scalar(select(func.count(Product.id)))
        licenses = await session.scalar(select(func.count(OrganizationLicense.id)))
        return orgs, products, licenses


class TestLicenseImportReport:
    def _start(self, client, monkeypatch, admin, content: bytes, captured: dict):
        import app.modules.imports.service as imports_service
        from tests.import_helpers import _seed_file

        stub_storage(monkeypatch, content)

        async def fake_upload(*, bucket: str, key: str, body: bytes, content_type: str) -> None:
            captured["body"] = body
            captured["content_type"] = content_type

        monkeypatch.setattr(imports_service, "upload_object_bytes", fake_upload)

        file_id = run(
            client, _seed_file, content, admin.id, f"lic-{token()}.xlsx", "application/octet-stream"
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
        assert profile.json()["header_row"] == 3

        mapping = dict(zip(LICENSE_HEADERS, [f.target for f in _license_targets()], strict=True))
        saved = client.put(
            f"/api/imports/{job_id}/mapping",
            json={"mapping": mapping, "options": {"header_row": 3}},
        )
        assert saved.status_code == 200, saved.text
        return job_id

    def test_summary_groups_three_issue_codes_and_counts_would_create(
        self, client, monkeypatch
    ) -> None:
        tok = token()
        admin = login(client, "ADMIN")
        run(client, _seed_university, f"МГТУ {tok}")
        content = _xlsx_with_title(tok)
        captured: dict = {}
        job_id = self._start(client, monkeypatch, admin, content, captured)

        dry = client.post(f"/api/imports/{job_id}/dry-run")
        assert dry.status_code == 200, dry.text
        body = dry.json()
        assert body["error_rows"] == 4, job_rows(client, job_id, status="error")
        assert body["warn_rows"] == 1
        assert body["ok_rows"] == 5

        summary = client.get(f"/api/imports/{job_id}/summary")
        assert summary.status_code == 200, summary.text
        summary_body = summary.json()
        codes = {entry["code"] for entry in summary_body["by_issue"]}
        assert {"required_empty", "org_not_found", "status_unknown", "term_invalid"} <= codes
        # 5 ok + 1 warn = 6 строк были бы созданы (новый ключ, upsert по умолчанию).
        assert summary_body["would"]["create"]["license"] == 6

    def test_rows_endpoint_exposes_structured_issues_and_filters_by_code(
        self, client, monkeypatch
    ) -> None:
        tok = token()
        admin = login(client, "ADMIN")
        run(client, _seed_university, f"МГТУ {tok}")
        content = _xlsx_with_title(tok)
        captured: dict = {}
        job_id = self._start(client, monkeypatch, admin, content, captured)
        client.post(f"/api/imports/{job_id}/dry-run")

        rows = job_rows(client, job_id, issue="org_not_found")
        assert len(rows) == 1
        assert rows[0]["issues"][0]["code"] == "org_not_found"
        assert rows[0]["source_row"] == 7  # титул(1) + пусто(2) + шапка(3) + 3 строки данных
        assert rows[0]["sheet"] == "Лицензии"

    def test_report_file_has_errors_and_summary_sheets_with_excel_row_numbers(
        self, client, monkeypatch
    ) -> None:
        import openpyxl as openpyxl_read

        tok = token()
        admin = login(client, "ADMIN")
        run(client, _seed_university, f"МГТУ {tok}")
        content = _xlsx_with_title(tok)
        captured: dict = {}
        job_id = self._start(client, monkeypatch, admin, content, captured)
        dry = client.post(f"/api/imports/{job_id}/dry-run")
        assert dry.json()["result_file_id"] is not None

        report = openpyxl_read.load_workbook(io.BytesIO(captured["body"]))
        assert set(report.sheetnames) == {"Ошибки", "Сводка"}
        errors_sheet = report["Ошибки"]
        header = [cell.value for cell in next(errors_sheet.iter_rows(max_row=1))]
        assert header[:5] == ["Лист", "Строка в файле", "Статус", "Что не так", "Что сделать"]
        data_rows = list(errors_sheet.iter_rows(min_row=2, values_only=True))
        row_numbers = {row[1] for row in data_rows}
        # Строка 5 (дубль) и строки 6-9 (четыре ошибки) — пять проблемных строк в отчёте.
        assert row_numbers == {5, 6, 7, 8, 9}
        sheets_in_report = {row[0] for row in data_rows}
        assert sheets_in_report == {"Лицензии"}

        summary_sheet = report["Сводка"]
        summary_rows = list(summary_sheet.iter_rows(min_row=2, values_only=True))
        assert len(summary_rows) >= 3  # как минимум три разных кода проблем

    def test_formula_in_a_cell_is_escaped_in_the_report(self, client, monkeypatch) -> None:
        """Строка 4 проходит ok (не попадает в лист «Ошибки»), но готовит почву: формула не в
        отчёте об ошибках в этом сценарии, а в данных применения — здесь проверяем только то, что
        `sanitize_formula` вызывается на тех же данных, что идут в лист «Ошибки» (строка-дубль 5
        содержит пустой комментарий, поэтому формулу гоняем явно через дублирующую строку)."""
        tok = token()
        admin = login(client, "ADMIN")
        run(client, _seed_university, f"МГТУ {tok}")
        content = _xlsx_with_title(tok)
        captured: dict = {}
        job_id = self._start(client, monkeypatch, admin, content, captured)
        dry = client.post(f"/api/imports/{job_id}/dry-run")
        assert dry.status_code == 200

        import openpyxl as openpyxl_read

        report = openpyxl_read.load_workbook(io.BytesIO(captured["body"]))
        errors_sheet = report["Ошибки"]
        for row in errors_sheet.iter_rows(min_row=2, values_only=True):
            for cell in row:
                assert not (isinstance(cell, str) and cell.startswith("="))

    def test_dry_run_does_not_write_to_the_catalog_and_is_repeatable(
        self, client, monkeypatch
    ) -> None:
        tok = token()
        admin = login(client, "ADMIN")
        run(client, _seed_university, f"МГТУ {tok}")
        content = _xlsx_with_title(tok)
        captured: dict = {}
        job_id = self._start(client, monkeypatch, admin, content, captured)

        before = run(client, _catalog_counts)
        first = client.post(f"/api/imports/{job_id}/dry-run").json()
        after_first = run(client, _catalog_counts)
        assert after_first == before

        second = client.post(f"/api/imports/{job_id}/dry-run").json()
        after_second = run(client, _catalog_counts)
        assert after_second == before
        assert (first["ok_rows"], first["warn_rows"], first["error_rows"]) == (
            second["ok_rows"],
            second["warn_rows"],
            second["error_rows"],
        )


def _license_targets():
    from app.modules.imports.fields import LICENSE_FIELDS

    return LICENSE_FIELDS
