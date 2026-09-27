"""Тесты импорта каталогов (спринт 5, раздел 4.12; П3 — лицензии, см. отчёт
backend-агента про rtk_requiriments.md разд. 4, Треб.1).

Чистые функции без БД: разбор файлов (`imports.parsing`), автоподбор
маппинга (`imports.mapping`), валидация и нормализация полей
(`imports.fields`). Построчное применение/откат (`imports.service`)
требует сессии Postgres — раньше не было покрыто вообще (та же граница, что
у остального DB-слоя репозитория), `TestLicenseImportEndToEnd` теперь
покрывает генерик-пайплайн (dry-run → apply → повторный импорт = upsert по
`contract_number`) тем же приёмом, что `tests/test_api_smoke.py`/`tests/
conftest.py`: настоящая Postgres за `TEST_DATABASE_URL`, S3 — заглушка в
памяти (`imports.service.inspect_object`/`download_object_bytes`/
`ensure_bucket`/`upload_object_bytes` подменены `monkeypatch`, реальный
SeaweedFS не нужен). Без `TEST_DATABASE_URL` класс пропускается целиком —
остальной файл остаётся полностью офлайновым.
"""

from __future__ import annotations

import hashlib
import io
import uuid
from decimal import Decimal

import openpyxl
import pytest

from app.core.permissions import Permission, has_permission
from app.modules.imports.fields import (
    LICENSE_FIELDS,
    ORGANIZATION_FIELDS,
    PRODUCT_FIELDS,
    FieldSpec,
    natural_key_for,
    normalize_phone_e164,
    validate_field,
)
from app.modules.imports.mapping import _levenshtein, suggest_mapping
from app.modules.imports.parsing import parse_csv, parse_xlsx, sanitize_formula
from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run


class TestPhoneNormalization:
    def test_local_format_with_leading_8(self) -> None:
        assert normalize_phone_e164("89991234567") == "+79991234567"

    def test_leading_plus7(self) -> None:
        assert normalize_phone_e164("+7 (999) 123-45-67") == "+79991234567"

    def test_ten_digits_no_country_code(self) -> None:
        assert normalize_phone_e164("9991234567") == "+79991234567"

    def test_invalid_length_rejected(self) -> None:
        assert normalize_phone_e164("12345") is None


class TestValidateField:
    def test_required_missing_fails(self) -> None:
        spec = FieldSpec("name", "Наименование", "text", required=True)
        value, error = validate_field(spec, "  ")
        assert value is None
        assert error is not None

    def test_optional_missing_is_none_without_error(self) -> None:
        spec = FieldSpec("short_name", "Краткое наименование", "text")
        value, error = validate_field(spec, "")
        assert value is None
        assert error is None

    def test_int_parses_from_float_like_string(self) -> None:
        spec = FieldSpec("students_count", "Студенты", "int")
        value, error = validate_field(spec, "1234")
        assert value == 1234
        assert error is None

    def test_int_rejects_garbage(self) -> None:
        spec = FieldSpec("students_count", "Студенты", "int")
        value, error = validate_field(spec, "много")
        assert value is None
        assert error is not None

    def test_decimal_accepts_comma_separator(self) -> None:
        spec = FieldSpec("base_price", "Цена", "decimal")
        value, error = validate_field(spec, "1500,50")
        assert value == Decimal("1500.50")
        assert error is None

    def test_email_valid(self) -> None:
        spec = FieldSpec("main_email", "Email", "email")
        value, error = validate_field(spec, "test@university.ru")
        assert value == "test@university.ru"
        assert error is None

    def test_email_invalid(self) -> None:
        spec = FieldSpec("main_email", "Email", "email")
        value, error = validate_field(spec, "not-an-email")
        assert value is None
        assert error is not None

    def test_phone_kind_normalizes(self) -> None:
        spec = FieldSpec("main_phone", "Телефон", "phone")
        value, error = validate_field(spec, "89991234567")
        assert value == "+79991234567"
        assert error is None

    def test_inn_kind_reuses_checksum_validator(self) -> None:
        spec = FieldSpec("inn", "ИНН", "inn")
        value, error = validate_field(spec, "7707049388")
        assert value == "7707049388"
        assert error is None

    def test_inn_kind_rejects_bad_checksum(self) -> None:
        spec = FieldSpec("inn", "ИНН", "inn")
        value, error = validate_field(spec, "7707049380")
        assert value is None
        assert error is not None

    def test_org_type_valid_value(self) -> None:
        spec = FieldSpec("org_type", "Тип", "org_type")
        value, error = validate_field(spec, "university")
        assert value == "university"
        assert error is None

    def test_org_type_invalid_value(self) -> None:
        spec = FieldSpec("org_type", "Тип", "org_type")
        value, error = validate_field(spec, "vendor")
        assert value is None
        assert error is not None

    def test_format_kind(self) -> None:
        spec = FieldSpec("format", "Формат", "format")
        assert validate_field(spec, "online") == ("online", None)
        assert validate_field(spec, "hybrid")[1] is not None


class TestSuggestMapping:
    def test_exact_synonym_match(self) -> None:
        mapping = suggest_mapping(["ИНН", "Наименование"], list(ORGANIZATION_FIELDS))
        assert mapping["ИНН"] == "inn"
        assert mapping["Наименование"] == "name"

    def test_fuzzy_match_with_typo(self) -> None:
        mapping = suggest_mapping(["Наимнование"], list(ORGANIZATION_FIELDS))
        assert mapping.get("Наимнование") == "name"

    def test_organization_name_headers_from_real_files(self) -> None:
        mapping = suggest_mapping(
            ["Название организации", "Краткое название"], list(ORGANIZATION_FIELDS)
        )
        assert mapping["Название организации"] == "name"
        assert mapping["Краткое название"] == "short_name"
        assert suggest_mapping(["Наименование вуза"], list(ORGANIZATION_FIELDS)) == {
            "Наименование вуза": "name"
        }

    def test_unmatched_header_absent_from_mapping(self) -> None:
        mapping = suggest_mapping(["Совершенно постороннее поле xyz"], list(ORGANIZATION_FIELDS))
        assert "Совершенно постороннее поле xyz" not in mapping

    def test_does_not_map_two_headers_to_same_target(self) -> None:
        mapping = suggest_mapping(["Наименование", "название"], list(ORGANIZATION_FIELDS))
        assert len(set(mapping.values())) == len(mapping.values())

    def test_product_fields_code_synonym(self) -> None:
        mapping = suggest_mapping(["Код", "Наименование"], list(PRODUCT_FIELDS))
        assert mapping["Код"] == "code"
        assert mapping["Наименование"] == "name"


class TestLevenshtein:
    def test_identical_strings(self) -> None:
        assert _levenshtein("test", "test") == 0

    def test_single_substitution(self) -> None:
        assert _levenshtein("test", "tent") == 1

    def test_empty_string(self) -> None:
        assert _levenshtein("", "abc") == 3


class TestSanitizeFormula:
    def test_neutralizes_leading_equals(self) -> None:
        assert sanitize_formula("=cmd|'/c calc'!A1").startswith("'=")

    def test_neutralizes_leading_at(self) -> None:
        assert sanitize_formula("@SUM(1,2)").startswith("'@")

    def test_leaves_normal_text_untouched(self) -> None:
        assert sanitize_formula("Обычный текст") == "Обычный текст"

    def test_leaves_negative_looking_number_prefixed(self) -> None:
        # Осознанный компромисс: "-79991234567" тоже попадёт под защиту,
        # это безопаснее, чем угадывать намерение по содержимому ячейки.
        assert sanitize_formula("-79991234567").startswith("'-")


class TestParseCsv:
    def test_semicolon_delimiter_detected(self) -> None:
        content = "Наименование;ИНН\nМГУ;7707049388\n".encode()
        table = parse_csv(content)
        assert table.headers == ["Наименование", "ИНН"]
        assert table.rows == [["МГУ", "7707049388"]]

    def test_cp1251_encoding_detected(self) -> None:
        content = "Наименование;ИНН\nМГУ;7707049388\n".encode("cp1251")
        table = parse_csv(content)
        assert table.headers == ["Наименование", "ИНН"]

    def test_utf8_bom_stripped(self) -> None:
        content = "﻿Наименование,ИНН\nМГУ,7707049388\n".encode()
        table = parse_csv(content)
        assert table.headers[0] == "Наименование"

    def test_blank_rows_skipped(self) -> None:
        content = "Наименование;ИНН\nМГУ;7707049388\n;\n".encode()
        table = parse_csv(content)
        assert len(table.rows) == 1


class TestParseXlsx:
    def test_reads_header_and_rows(self, tmp_path) -> None:
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["Наименование", "ИНН"])
        sheet.append(["МГУ", "7707049388"])
        path = tmp_path / "test.xlsx"
        workbook.save(path)

        table = parse_xlsx(path.read_bytes())
        assert table.headers == ["Наименование", "ИНН"]
        assert table.rows == [["МГУ", "7707049388"]]

    def test_integer_cell_rendered_without_decimal(self, tmp_path) -> None:
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["Студенты"])
        sheet.append([1200])
        path = tmp_path / "test.xlsx"
        workbook.save(path)

        table = parse_xlsx(path.read_bytes())
        assert table.rows == [["1200"]]


class TestImportPermissions:
    def test_only_head_and_admin_run_imports(self) -> None:
        assert not has_permission("KAM", Permission.IMPORT_RUN)
        assert has_permission("HEAD", Permission.IMPORT_RUN)
        assert has_permission("ADMIN", Permission.IMPORT_RUN)


class TestLicenseFields:
    """П3: каталог лицензий/договоров вуз↔вендор↔ПО — 10 полей буквально из
    rtk_requiriments.md разд. 4, Треб.1."""

    def test_ten_fields_in_case_order(self) -> None:
        assert [f.target for f in LICENSE_FIELDS] == [
            "organization_name",
            "vendor",
            "product_name",
            "contract_number",
            "license_signed_at",
            "license_valid_year",
            "transfer_status",
            "manager_full_name",
            "responsible_contacts",
            "comment",
        ]

    def test_natural_key_is_contract_number(self) -> None:
        assert natural_key_for("license") == "contract_number"

    def test_required_fields_are_the_identifying_ones(self) -> None:
        required = {f.target for f in LICENSE_FIELDS if f.required}
        assert required == {"organization_name", "vendor", "product_name", "contract_number"}

    def test_transfer_status_accepts_known_codes(self) -> None:
        spec = next(f for f in LICENSE_FIELDS if f.target == "transfer_status")
        for code in ("not_started", "in_progress", "transferred", "declined"):
            value, err = validate_field(spec, code)
            assert err is None
            assert value == code

    def test_transfer_status_rejects_unknown_value(self) -> None:
        spec = next(f for f in LICENSE_FIELDS if f.target == "transfer_status")
        value, err = validate_field(spec, "как-то так")
        assert value is None
        assert err is not None

    def test_transfer_status_is_optional(self) -> None:
        spec = next(f for f in LICENSE_FIELDS if f.target == "transfer_status")
        value, err = validate_field(spec, "")
        assert value is None
        assert err is None

    def test_organization_name_column_maps_by_exact_label(self) -> None:
        mapping = suggest_mapping(["Название ВУЗа", "Вендор"], list(LICENSE_FIELDS))
        assert mapping["Название ВУЗа"] == "organization_name"
        assert mapping["Вендор"] == "vendor"


class TestLicenseImportEndToEnd:
    """Настоящая Postgres обязательна — см. докстринг модуля и `tests/
    conftest.py`."""

    pytestmark = pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
    )

    _HEADERS = [
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
    _MAPPING = {
        "Название ВУЗа": "organization_name",
        "Вендор": "vendor",
        "ПО": "product_name",
        "Номер договора": "contract_number",
        "Подписание лицензии": "license_signed_at",
        "Срок действия лицензии (год)": "license_valid_year",
        "Статус по передаче": "transfer_status",
        "ФИО Менеджера": "manager_full_name",
        "Ответственные от ВУЗа": "responsible_contacts",
        "Комментарий": "comment",
    }

    def _xlsx_bytes(self, rows: list[list[str]]) -> bytes:
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(self._HEADERS)
        for row in rows:
            sheet.append(row)
        buffer = io.BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()

    def _stub_storage(self, monkeypatch, content: bytes) -> None:
        """Заглушка S3 в памяти — см. докстринг модуля наверху файла."""
        from app.core.storage import ObjectInspection
        from app.modules.imports import service as imports_service

        async def fake_inspect(*, bucket: str, key: str) -> ObjectInspection:
            return ObjectInspection(
                exists=True,
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                magic_bytes=content[:8],
            )

        async def fake_download(*, bucket: str, key: str) -> bytes:
            return content

        async def fake_ensure_bucket(bucket: str) -> None:
            return None

        async def fake_upload(*, bucket: str, key: str, body: bytes, content_type: str) -> None:
            return None

        monkeypatch.setattr(imports_service, "inspect_object", fake_inspect)
        monkeypatch.setattr(imports_service, "download_object_bytes", fake_download)
        monkeypatch.setattr(imports_service, "ensure_bucket", fake_ensure_bucket)
        monkeypatch.setattr(imports_service, "upload_object_bytes", fake_upload)

    async def _seed_organization_and_file(
        self,
        org_name: str,
        content: bytes,
        uploaded_by: uuid.UUID | None = None,
        create_org: bool = True,
    ):
        from sqlalchemy import select

        from app.core.db import session_scope
        from app.core.ids import uuid7
        from app.modules.catalog.models import Organization
        from app.modules.files.models import File, FileStatus

        async with session_scope() as session:
            if create_org:
                org = Organization(name=org_name, org_type="university", source="test")
                session.add(org)
                await session.flush()
            else:
                # Повторный импорт того же вуза: организация уже есть, вторую с тем же названием не
                # заводим — иначе разрешение «название -> вуз» становится неоднозначным.
                org = (
                    await session.execute(select(Organization).where(Organization.name == org_name))
                ).scalar_one()
            # Файл принадлежит тому, кто его загрузил: руководитель импортирует только свои файлы.
            file = File(
                id=uuid7(),
                storage_key="test/license-import.xlsx",
                bucket="imports",
                original_filename="license-import.xlsx",
                mime_type=("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                status=FileStatus.READY.value,
                uploaded_by=uploaded_by,
            )
            session.add(file)
            await session.flush()
            org_id, file_id = org.id, file.id
        return org_id, file_id

    def test_dry_run_resolves_organization_and_flags_missing_required_field(
        self, client, monkeypatch
    ) -> None:
        org_name = f"Тестовый университет П3 {uuid.uuid4().hex[:8]}"
        rows = [
            [
                org_name,
                "ВендорА",
                "ПродуктА",
                "TU-001",
                "2026-01-10",
                "3",
                "transferred",
                "Иванов И.И.",
                "Петров П.П.",
                "первая строка",
            ],
            [
                "",
                "ВендорБ",
                "ПродуктБ",
                "TU-002",
                "2026-02-01",
                "1",
                "not_started",
                "",
                "",
                "без названия вуза — должна дать ошибку",
            ],
        ]
        content = self._xlsx_bytes(rows)
        self._stub_storage(monkeypatch, content)
        head = run(client, _make_user, "HEAD")
        _org_id, file_id = run(client, self._seed_organization_and_file, org_name, content, head.id)
        csrf = authenticate(client, head)
        client.headers["X-CSRF-Token"] = csrf

        create_resp = client.post(
            "/api/imports",
            json={
                "file_id": str(file_id),
                "entity_type": "license",
                "mode": "upsert",
                "source_format": "xlsx",
            },
        )
        assert create_resp.status_code == 201, create_resp.text
        job_id = create_resp.json()["id"]

        map_resp = client.put(f"/api/imports/{job_id}/mapping", json={"mapping": self._MAPPING})
        assert map_resp.status_code == 200, map_resp.text

        dry_resp = client.post(f"/api/imports/{job_id}/dry-run")
        assert dry_resp.status_code == 200, dry_resp.text
        body = dry_resp.json()
        assert body["ok_rows"] == 1
        assert body["error_rows"] == 1
        assert body["total_rows"] == 2

    async def _apply_synchronously(self, job_id: uuid.UUID) -> None:
        """Роутер только переводит задание в `applying` — построчная
        обработка обычно идёт фоновой задачей (`imports.tasks.
        sweep_import_jobs`), которая в тестах не запущена. Вызываем
        `apply_batch` напрямую тем же приёмом, что и сама периодическая
        задача — единственный способ проверить upsert без поднятого воркера."""
        from app.core.db import session_scope
        from app.modules.imports.service import ImportService

        async with session_scope() as session:
            service = ImportService(session)
            job = await service.get_or_404(job_id)
            await service.start_apply(job)
            while await service.apply_batch(job, batch_size=500):
                pass
            await service.finalize_apply_if_done(job)

    def test_apply_creates_rows_then_second_import_upserts_by_contract_number(
        self, client, monkeypatch
    ) -> None:
        from sqlalchemy import select

        from app.core.db import session_scope
        from app.modules.catalog.models import OrganizationLicense

        # Уникальные на каждый прогон — тест не должен зависеть от того,
        # накопилась ли в БД история прошлых запусков (та же тестовая
        # Postgres переживает между вызовами pytest, ничего не чистит).
        org_name = f"Тестовый университет П3 {uuid.uuid4().hex[:8]}"
        contract_number = f"TU-{uuid.uuid4().hex[:10]}"

        rows = [
            [
                org_name,
                "ВендорА",
                "ПродуктА",
                contract_number,
                "2026-01-10",
                "3",
                "not_started",
                "Иванов И.И.",
                "Петров П.П.",
                "исходная запись",
            ],
        ]
        content = self._xlsx_bytes(rows)
        self._stub_storage(monkeypatch, content)
        head = run(client, _make_user, "HEAD")
        _org_id, file_id = run(client, self._seed_organization_and_file, org_name, content, head.id)
        csrf = authenticate(client, head)
        client.headers["X-CSRF-Token"] = csrf

        job_id = client.post(
            "/api/imports",
            json={
                "file_id": str(file_id),
                "entity_type": "license",
                "mode": "upsert",
                "source_format": "xlsx",
            },
        ).json()["id"]
        client.put(f"/api/imports/{job_id}/mapping", json={"mapping": self._MAPPING})
        dry = client.post(f"/api/imports/{job_id}/dry-run").json()
        assert dry["ok_rows"] == 1, dry
        run(client, self._apply_synchronously, uuid.UUID(job_id))

        async def _count_and_fetch():
            async with session_scope() as session:
                rows = (
                    (
                        await session.execute(
                            select(OrganizationLicense).where(
                                OrganizationLicense.contract_number == contract_number
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                return [(r.id, r.vendor, r.comment, r.version) for r in rows]

        first_pass = run(client, _count_and_fetch)
        assert len(first_pass) == 1
        first_id, vendor, comment, version = first_pass[0]
        assert vendor == "ВендорА"
        assert comment == "исходная запись"
        assert version == 1

        # Второй импорт того же номера договора с другим вендором/комментарием
        # — должен ОБНОВИТЬ существующую запись (не создать вторую).
        rows_v2 = [
            [
                org_name,
                "ВендорБ-обновлённый",
                "ПродуктА",
                contract_number,
                "2026-01-10",
                "3",
                "transferred",
                "Иванов И.И.",
                "Петров П.П.",
                "обновлённая запись",
            ],
        ]
        content_v2 = self._xlsx_bytes(rows_v2)
        self._stub_storage(monkeypatch, content_v2)
        _org_id2, file_id_v2 = run(
            client,
            self._seed_organization_and_file,
            org_name,
            content_v2,
            head.id,
            False,
        )

        job_id_v2 = client.post(
            "/api/imports",
            json={
                "file_id": str(file_id_v2),
                "entity_type": "license",
                "mode": "upsert",
                "source_format": "xlsx",
            },
        ).json()["id"]
        client.put(f"/api/imports/{job_id_v2}/mapping", json={"mapping": self._MAPPING})
        dry_v2 = client.post(f"/api/imports/{job_id_v2}/dry-run").json()
        assert dry_v2["ok_rows"] == 1, dry_v2
        run(client, self._apply_synchronously, uuid.UUID(job_id_v2))

        second_pass = run(client, _count_and_fetch)
        assert len(second_pass) == 1, "upsert должен обновить строку, а не размножить её"
        second_id, vendor2, comment2, version2 = second_pass[0]
        assert second_id == first_id
        assert vendor2 == "ВендорБ-обновлённый"
        assert comment2 == "обновлённая запись"
        assert version2 == 2

    def test_only_head_and_admin_rollback(self) -> None:
        assert not has_permission("KAM", Permission.IMPORT_ROLLBACK)
        assert has_permission("HEAD", Permission.IMPORT_ROLLBACK)
        assert has_permission("ADMIN", Permission.IMPORT_ROLLBACK)
