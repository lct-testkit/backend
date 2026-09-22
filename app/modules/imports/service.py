"""Сервис импорта каталогов — 5 фаз dop.md §4.12: загрузка (уже сделана
`files`-модулем), профилирование, маппинг, dry-run, применение (+ откат).

Батчи по 500 с чекпоинтом обрабатываются не здесь, а в `imports.tasks`
(периодический скан, тот же приём, что `workflow.tasks`/`registry.tasks`):
`apply()`/`rollback()` только переводят задание в состояние
`applying`/`rolling_back` в рамках HTTP-запроса, фактическую построчную
работу продолжает фоновая задача, читая уже провалидированные `row_data` из
`import_row_results` — так апдейт с батчами переживает падение воркера
без повторного разбора файла.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import openpyxl
from sqlalchemy import Select, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode, NotFoundError
from app.core.ids import uuid7
from app.core.security import Principal
from app.core.storage import (
    download_object_bytes,
    ensure_bucket,
    inspect_object,
    upload_object_bytes,
)
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.catalog.models import Direction, Organization, OrganizationLicense, Product, Region
from app.modules.files.models import Attachment, File, FileStatus
from app.modules.imports.fields import FieldSpec, fields_for, natural_key_for, validate_field
from app.modules.imports.mapping import suggest_mapping
from app.modules.imports.models import (
    ImportEntityType,
    ImportJob,
    ImportJobStatus,
    ImportMode,
    ImportPreset,
    ImportRowResult,
    ImportRowStatus,
)
from app.modules.imports.parsing import ParsedTable, parse_table, sanitize_formula
from app.modules.registry.models import EgrulEntry

# kind -> (модель, целевое поле в row_data, колонка поиска по значению из
# файла). Третий элемент раньше был неявно `model.code` — П3 добавляет
# `organization_name`, у которого колонка поиска не `code`, а `name`, так
# что параметризовали явно (region_code/direction_code продолжают resolve'ить
# по `.code`, поведение не изменилось).
_FK_TARGETS: dict[str, tuple[type, str, Any]] = {
    "region_code": (Region, "region_id", Region.code),
    "direction_code": (Direction, "direction_id", Direction.code),
    "organization_name": (Organization, "organization_id", Organization.name),
}
_MODEL_BY_ENTITY: dict[str, type] = {
    ImportEntityType.ORGANIZATION.value: Organization,
    ImportEntityType.PRODUCT.value: Product,
    ImportEntityType.LICENSE.value: OrganizationLicense,
}
_PENDING_STATUSES = (ImportRowStatus.OK.value, ImportRowStatus.WARN.value)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dt.date | dt.datetime):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


@dataclass(slots=True)
class ProfileResult:
    headers: list[str]
    sample_rows: list[list[str]]
    suggested_mapping: dict[str, str]
    total_rows: int


class ImportService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    # -- служебное ------------------------------------------------------

    def list_query(self) -> Select[tuple[ImportJob]]:
        return select(ImportJob)

    async def get_or_404(self, job_id: uuid.UUID) -> ImportJob:
        job = await self._session.get(ImportJob, job_id)
        if job is None:
            raise NotFoundError("Задание импорта", job_id)
        return job

    async def _get_ready_file(self, file_id: uuid.UUID) -> File:
        file = await self._session.get(File, file_id)
        if file is None or file.deleted_at is not None:
            raise NotFoundError("Файл", file_id)
        if file.status != FileStatus.READY.value:
            raise AppError(ErrorCode.VALIDATION, "Файл ещё не прошёл проверку")
        return file

    # -- фаза 1: загрузка (создание задания на уже готовый file_id) ----

    async def create_job(
        self,
        principal: Principal,
        *,
        file_id: uuid.UUID,
        entity_type: str,
        mode: str,
        source_format: str,
    ) -> ImportJob:
        if entity_type not in _MODEL_BY_ENTITY:
            raise AppError(
                ErrorCode.VALIDATION, f"Импорт типа {entity_type!r} не поддерживается"
            )
        await self._get_ready_file(file_id)

        job = ImportJob(
            file_id=file_id,
            entity_type=entity_type,
            mode=mode,
            source_format=source_format,
            status=ImportJobStatus.UPLOADED.value,
            initiated_by=principal.user_id,
        )
        self._session.add(job)
        await self._session.flush()
        return job

    # -- фаза 2: профилирование и подсказка маппинга --------------------

    async def _load_table(self, job: ImportJob) -> ParsedTable:
        file = await self._get_ready_file(job.file_id)

        # Раздел 4.12/3.7: лимит размера файла импорта нигде не проверялся
        # до разбора — `download_object_bytes` тянет объект целиком в память
        # одним куском, а лимит строк (`dry_run`) в принципе не может
        # сработать раньше, чем файл уже полностью скачан и распарсен.
        # `inspect_object` — тот же приём, что уже использует
        # `files.service.commit`: читает объект потоково, не материализуя
        # его целиком, только чтобы узнать реальный размер и сходу отсеять
        # то, что заведомо превышает лимит.
        settings = get_settings()
        inspection = await inspect_object(bucket=file.bucket, key=file.storage_key)
        if inspection.size_bytes > settings.import_max_file_size_bytes:
            raise AppError(
                ErrorCode.IMPORT_BAD_FORMAT,
                "Файл превышает допустимый размер импорта: "
                f"{inspection.size_bytes} > {settings.import_max_file_size_bytes}",
            )

        content = await download_object_bytes(bucket=file.bucket, key=file.storage_key)
        return parse_table(content, source_format=job.source_format)

    async def profile(self, job: ImportJob) -> ProfileResult:
        table = await self._load_table(job)
        fields = fields_for(job.entity_type)
        mapping = suggest_mapping(table.headers, list(fields))

        # dop.md §4.12, фаза 3: «второй импорт того же реестра не требует
        # ручной работы» — точное совпадение заголовка с уже сохранённым
        # пресетом важнее нечёткого угадывания по словарю синонимов.
        preset = await self._latest_preset(job.entity_type)
        if preset is not None:
            for header, target in preset.mapping.items():
                if header in table.headers:
                    mapping[header] = target

        return ProfileResult(
            headers=table.headers,
            sample_rows=table.rows[:100],
            suggested_mapping=mapping,
            total_rows=len(table.rows),
        )

    async def _latest_preset(self, entity_type: str) -> ImportPreset | None:
        return await self._session.scalar(
            select(ImportPreset)
            .where(ImportPreset.entity_type == entity_type)
            .order_by(ImportPreset.created_at.desc())
            .limit(1)
        )

    # -- фаза 3: маппинг --------------------------------------------------

    async def save_mapping(
        self,
        job: ImportJob,
        *,
        mapping: dict[str, str],
        principal: Principal,
        save_as_preset: str | None,
    ) -> ImportJob:
        fields_by_target = {f.target: f for f in fields_for(job.entity_type)}
        unknown = [target for target in mapping.values() if target not in fields_by_target]
        if unknown:
            raise AppError(
                ErrorCode.IMPORT_MAPPING_INCOMPLETE,
                f"Неизвестные целевые поля в маппинге: {', '.join(unknown)}",
            )
        natural_key = natural_key_for(job.entity_type)
        if natural_key not in mapping.values():
            raise AppError(
                ErrorCode.IMPORT_MAPPING_INCOMPLETE,
                f"Маппинг должен включать ключевое поле «{fields_by_target[natural_key].label}»",
            )

        job.mapping = mapping
        job.status = ImportJobStatus.MAPPED.value
        await self._session.flush()

        if save_as_preset:
            self._session.add(
                ImportPreset(
                    name=save_as_preset,
                    entity_type=job.entity_type,
                    mapping=mapping,
                    created_by=principal.user_id,
                )
            )
            await self._session.flush()
            await self._audit.record(
                AuditAction.IMPORT_PRESET_CREATED,
                entity_type="import_preset",
                entity_id=None,
                changes={"name": {"old": None, "new": save_as_preset}},
            )
        return job

    # -- фаза 4: dry-run ---------------------------------------------------

    async def dry_run(self, job: ImportJob) -> ImportJob:
        if job.status not in (ImportJobStatus.MAPPED.value, ImportJobStatus.VALIDATED.value):
            raise AppError(
                ErrorCode.IMPORT_MAPPING_INCOMPLETE, "Сначала сохраните маппинг колонок"
            )

        fields_by_target = {f.target: f for f in fields_for(job.entity_type)}
        natural_key = natural_key_for(job.entity_type)
        key_spec = fields_by_target[natural_key]

        table = await self._load_table(job)
        settings = get_settings()
        if len(table.rows) > settings.import_max_rows:
            raise AppError(
                ErrorCode.IMPORT_BAD_FORMAT,
                f"Слишком много строк: {len(table.rows)} > {settings.import_max_rows}",
            )

        header_index = {h: i for i, h in enumerate(table.headers)}
        column_map: list[tuple[int, FieldSpec]] = [
            (header_index[header], fields_by_target[target])
            for header, target in job.mapping.items()
            if header in header_index and target in fields_by_target
        ]
        key_col_idx = next(
            (idx for idx, spec in column_map if spec.target == natural_key), None
        )
        if key_col_idx is None:
            raise AppError(
                ErrorCode.IMPORT_MAPPING_INCOMPLETE,
                f"Колонка для ключевого поля «{key_spec.label}» не найдена в файле",
            )

        fk_lookup = await self._resolve_fk_codes(column_map, table.rows)

        raw_keys = {
            row[key_col_idx].strip()
            for row in table.rows
            if key_col_idx < len(row) and row[key_col_idx].strip()
        }
        model = _MODEL_BY_ENTITY[job.entity_type]
        is_organization = job.entity_type == ImportEntityType.ORGANIZATION.value
        # Раньше — `model.inn if is_organization else model.code` (годилось
        # ровно для двух типов). `natural_key` уже посчитан выше через
        # `natural_key_for(job.entity_type)` и всегда совпадает с реальным
        # именем колонки на модели (`inn`/`code`/`contract_number`), так что
        # `getattr` — чистое обобщение без изменения поведения для
        # organization/product.
        key_column = getattr(model, natural_key)
        existing_map: dict[str, uuid.UUID] = {}
        if raw_keys:
            key_stmt = select(key_column, model.id).where(key_column.in_(raw_keys))
            rows = (await self._session.execute(key_stmt)).all()
            existing_map = dict(rows)

        egrul_map: dict[str, EgrulEntry] = {}
        if job.entity_type == ImportEntityType.ORGANIZATION.value and raw_keys:
            entries = (
                await self._session.execute(select(EgrulEntry).where(EgrulEntry.inn.in_(raw_keys)))
            ).scalars().all()
            egrul_map = {e.inn: e for e in entries}

        await self._session.execute(
            delete(ImportRowResult).where(ImportRowResult.import_job_id == job.id)
        )

        seen_keys: set[str] = set()
        ok = warn = error = 0
        report_rows: list[tuple[list[str], str]] = []

        for row_number, row in enumerate(table.rows, start=1):
            row_data, field_errors = self._extract_row(row, column_map, fk_lookup)
            notes: list[str] = list(field_errors)
            key_value = row_data.get(natural_key)

            if not key_value:
                notes.append(f"Не удалось определить ключевое поле «{key_spec.label}»")
                status = ImportRowStatus.ERROR.value
            else:
                status = ImportRowStatus.OK.value
                if key_value in seen_keys:
                    notes.append("Дубликат ключа внутри файла")
                    status = ImportRowStatus.WARN.value
                seen_keys.add(key_value)

                existing_id = existing_map.get(key_value)
                if existing_id is not None and job.mode == ImportMode.INSERT.value:
                    notes.append("Уже существует — будет пропущена (режим «только создание»)")
                    status = ImportRowStatus.WARN.value
                elif existing_id is None and job.mode == ImportMode.UPDATE.value:
                    notes.append("Запись для обновления не найдена")
                    status = ImportRowStatus.ERROR.value

                if field_errors:
                    status = ImportRowStatus.ERROR.value

                is_organization = job.entity_type == ImportEntityType.ORGANIZATION.value
                if is_organization and key_value in egrul_map:
                    self._enrich_organization_row(row_data, egrul_map[key_value])

            if status == ImportRowStatus.OK.value:
                ok += 1
            elif status == ImportRowStatus.WARN.value:
                warn += 1
            else:
                error += 1

            self._session.add(
                ImportRowResult(
                    import_job_id=job.id,
                    row_number=row_number,
                    status=status,
                    row_data=row_data,
                    errors=notes,
                )
            )
            if status != ImportRowStatus.OK.value:
                report_rows.append((row, "; ".join(notes)))

        await self._session.flush()

        job.total_rows = len(table.rows)
        job.ok_rows, job.warn_rows, job.error_rows = ok, warn, error
        job.status = ImportJobStatus.VALIDATED.value
        job.result_file_id = await self._write_error_report(job, table.headers, report_rows)
        await self._session.flush()

        await self._audit.record(
            AuditAction.IMPORT_VALIDATED,
            entity_type="import_job",
            entity_id=job.id,
            changes={
                "total_rows": {"old": None, "new": job.total_rows},
                "ok_rows": {"old": None, "new": ok},
                "warn_rows": {"old": None, "new": warn},
                "error_rows": {"old": None, "new": error},
            },
        )
        return job

    async def _resolve_fk_codes(
        self, column_map: list[tuple[int, FieldSpec]], rows: list[list[str]]
    ) -> dict[str, dict[str, uuid.UUID]]:
        codes_by_kind: dict[str, set[str]] = {kind: set() for kind in _FK_TARGETS}
        for col_idx, spec in column_map:
            if spec.kind not in _FK_TARGETS:
                continue
            for row in rows:
                if col_idx < len(row) and row[col_idx].strip():
                    codes_by_kind[spec.kind].add(row[col_idx].strip())

        resolved: dict[str, dict[str, uuid.UUID]] = {}
        for kind, (model, _field, lookup_column) in _FK_TARGETS.items():
            codes = codes_by_kind[kind]
            if not codes:
                resolved[kind] = {}
                continue
            code_stmt = select(lookup_column, model.id).where(lookup_column.in_(codes))
            rows_found = (await self._session.execute(code_stmt)).all()
            # `dict(rows_found)`: при неуникальном значении колонки поиска
            # (например, два вуза с совпадающим `name` — в отличие от
            # `region_code`/`direction_code`, `Organization.name` не
            # уникален) побеждает последняя строка из выборки. Это тот же
            # компромисс, что и у остального импортёра: dry-run показывает
            # результат резолва в `row_data` до применения, и явную
            # неоднозначность видно на этапе проверки, а не после apply.
            resolved[kind] = dict(rows_found)
        return resolved

    def _extract_row(
        self,
        row: list[str],
        column_map: list[tuple[int, FieldSpec]],
        fk_lookup: dict[str, dict[str, uuid.UUID]],
    ) -> tuple[dict[str, Any], list[str]]:
        row_data: dict[str, Any] = {}
        errors: list[str] = []
        for col_idx, spec in column_map:
            raw = row[col_idx] if col_idx < len(row) else ""
            if spec.kind in _FK_TARGETS:
                stripped = raw.strip()
                if stripped:
                    _model, target_field, _lookup_column = _FK_TARGETS[spec.kind]
                    resolved_id = fk_lookup[spec.kind].get(stripped)
                    if resolved_id is None:
                        errors.append(f"«{spec.label}»: значение {stripped!r} не найдено")
                    else:
                        row_data[target_field] = str(resolved_id)
                elif spec.required:
                    # Раньше ни один FK-вид (region_code/direction_code) не
                    # был обязательным, поэтому пустая ячейка молча
                    # пропускалась. П3 заводит первый обязательный FK
                    # (`organization_name`) — без этой ветки пустое
                    # «Название ВУЗа» проходило бы строку в `ok` без
                    # `organization_id` и падало бы уже на `apply()`.
                    errors.append(f"«{spec.label}» — обязательное поле")
                continue
            value, err = validate_field(spec, raw)
            if err:
                errors.append(err)
                continue
            if value is not None:
                row_data[spec.target] = _json_safe(value)
        return row_data, errors

    @staticmethod
    def _enrich_organization_row(row_data: dict[str, Any], entry: EgrulEntry) -> None:
        """dop.md §11.5: «строки обогащаются из реестра» — только пустые поля,
        то, что уже есть в файле, считается более точным для этой конкретной
        строки (импортёр не должен затирать явно указанные пользователем
        данные значениями из реестра)."""
        candidates = {
            "name": entry.full_name,
            "short_name": entry.short_name,
            "kpp": entry.kpp,
            "ogrn": entry.ogrn,
            "legal_address": entry.legal_address,
        }
        for field, value in candidates.items():
            if not row_data.get(field) and value:
                row_data[field] = value
        row_data["_verified_source"] = "fns_registry"
        row_data["_registry_version_id"] = (
            str(entry.registry_version_id) if entry.registry_version_id else None
        )
        row_data["_registry_status"] = entry.status

    async def _write_error_report(
        self, job: ImportJob, headers: list[str], report_rows: list[tuple[list[str], str]]
    ) -> uuid.UUID | None:
        if not report_rows:
            return None

        buffer = io.BytesIO()
        workbook = openpyxl.Workbook(write_only=True)
        sheet = workbook.create_sheet("Ошибки импорта")
        sheet.append([*headers, "Причина ошибки"])
        for row, reason in report_rows:
            sheet.append([sanitize_formula(cell) for cell in row] + [reason])
        workbook.save(buffer)
        content = buffer.getvalue()

        settings = get_settings()
        await ensure_bucket(settings.s3_bucket_files)
        file_id = uuid7()
        storage_key = f"{file_id}/import-errors.xlsx"
        await upload_object_bytes(
            bucket=settings.s3_bucket_files,
            key=storage_key,
            body=content,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        report_file = File(
            id=file_id,
            storage_key=storage_key,
            bucket=settings.s3_bucket_files,
            original_filename="import-errors.xlsx",
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            status=FileStatus.READY.value,
            uploaded_by=job.initiated_by,
        )
        self._session.add(report_file)
        # Отдельный flush перед вложением: `File`/`Attachment` не связаны
        # ORM `relationship()` (полиморфная связь по `entity_type`/
        # `entity_id`, см. docstring `files.models`), поэтому unit-of-work
        # не всегда выводит порядок вставки из одних только `ForeignKey` —
        # без явного flush INSERT `attachments` иногда уходит раньше
        # ещё не вставленного `files` и падает по FK.
        await self._session.flush()
        self._session.add(
            Attachment(
                file_id=file_id,
                entity_type="import_job",
                entity_id=job.id,
                category="report",
                uploaded_by=job.initiated_by,
            )
        )
        await self._session.flush()
        return file_id

    # -- фаза 5: применение -------------------------------------------------

    async def start_apply(self, job: ImportJob) -> ImportJob:
        if job.status != ImportJobStatus.VALIDATED.value:
            raise AppError(
                ErrorCode.IMPORT_NOT_APPLICABLE,
                "Сначала выполните проверку (dry-run) без ошибок в маппинге",
            )
        if job.ok_rows + job.warn_rows == 0:
            raise AppError(ErrorCode.IMPORT_NOT_APPLICABLE, "Нет строк, пригодных к применению")

        job.status = ImportJobStatus.APPLYING.value
        job.started_at = dt.datetime.now(dt.UTC)
        await self._session.flush()
        await self._audit.record(
            AuditAction.IMPORT_STARTED,
            entity_type="import_job",
            entity_id=job.id,
            changes={"mode": {"old": None, "new": job.mode}},
        )
        return job

    async def apply_batch(self, job: ImportJob, *, batch_size: int) -> int:
        """Обрабатывает одну партию — вызывается `imports.tasks.sweep_import_jobs`.
        Возвращает число обработанных строк (0 — партия исчерпана)."""
        pending = (
            (
                await self._session.execute(
                    select(ImportRowResult)
                    .where(
                        ImportRowResult.import_job_id == job.id,
                        ImportRowResult.status.in_(_PENDING_STATUSES),
                        ImportRowResult.entity_id.is_(None),
                    )
                    .order_by(ImportRowResult.row_number)
                    .limit(batch_size)
                )
            )
            .scalars()
            .all()
        )
        if not pending:
            return 0

        for row in pending:
            if job.entity_type == ImportEntityType.ORGANIZATION.value:
                await self._apply_organization_row(job, row)
            elif job.entity_type == ImportEntityType.PRODUCT.value:
                await self._apply_product_row(job, row)
            else:
                await self._apply_license_row(job, row)
        await self._session.flush()
        return len(pending)

    async def _apply_organization_row(self, job: ImportJob, row: ImportRowResult) -> None:
        data = dict(row.row_data)
        internal = {k: data.pop(k) for k in list(data) if k.startswith("_")}
        inn = data.get("inn")
        existing = (
            await self._session.scalar(select(Organization).where(Organization.inn == inn))
            if inn
            else None
        )

        if existing is None:
            if job.mode == ImportMode.UPDATE.value:
                # Не должно случиться: dry-run уже помечает такие строки
                # `error`, значит они не попадают в выборку `apply_batch`.
                # Терминальный статус на всякий случай — иначе пустая
                # строка с `entity_id IS NULL` обрабатывалась бы вечно.
                row.status = ImportRowStatus.SKIPPED.value
                row.errors = [*row.errors, "Запись для обновления не найдена"]
                return
            org = Organization(
                name=data.get("name") or f"Организация (ИНН {inn})",
                short_name=data.get("short_name"),
                org_type=data.get("org_type", "university"),
                inn=inn,
                kpp=data.get("kpp"),
                ogrn=data.get("ogrn"),
                legal_address=data.get("legal_address"),
                actual_address=data.get("actual_address"),
                region_id=uuid.UUID(data["region_id"]) if data.get("region_id") else None,
                website=data.get("website"),
                main_phone=data.get("main_phone"),
                main_email=data.get("main_email"),
                students_count=data.get("students_count"),
                source="import",
                import_job_id=job.id,
                created_by=job.initiated_by,
                owner_id=job.initiated_by,
            )
            if internal.get("_verified_source"):
                org.verified_source = internal["_verified_source"]
                org.verified_at = dt.datetime.now(dt.UTC)
                org.registry_version_id = (
                    uuid.UUID(internal["_registry_version_id"])
                    if internal.get("_registry_version_id")
                    else None
                )
                org.registry_status = internal.get("_registry_status")
            self._session.add(org)
            await self._session.flush()
            row.entity_id = org.id
            row.before_snapshot = None
            return

        if job.mode == ImportMode.INSERT.value:
            row.status = ImportRowStatus.SKIPPED.value
            row.errors = [*row.errors, "Пропущена при применении: уже существует"]
            return

        before: dict[str, Any] = {}
        for field in ("name", "short_name", "kpp", "ogrn", "legal_address", "actual_address",
                      "website", "main_phone", "main_email", "students_count", "region_id"):
            if field not in data:
                continue
            new_value = uuid.UUID(data[field]) if field == "region_id" else data[field]
            old_value = getattr(existing, field)
            if _json_safe(old_value) != _json_safe(new_value):
                before[field] = _json_safe(old_value)
                setattr(existing, field, new_value)
        existing.version += 1
        await self._session.flush()
        row.entity_id = existing.id
        row.before_snapshot = before or {}

    async def _apply_product_row(self, job: ImportJob, row: ImportRowResult) -> None:
        data = dict(row.row_data)
        code = data.get("code")
        existing = (
            await self._session.scalar(select(Product).where(Product.code == code))
            if code
            else None
        )

        if existing is None:
            if job.mode == ImportMode.UPDATE.value:
                row.status = ImportRowStatus.SKIPPED.value
                row.errors = [*row.errors, "Запись для обновления не найдена"]
                return
            raw_price = data.get("base_price")
            product = Product(
                code=code,
                name=data.get("name") or code,
                description=data.get("description"),
                direction_id=uuid.UUID(data["direction_id"]) if data.get("direction_id") else None,
                duration_hours=data.get("duration_hours"),
                format=data.get("format"),
                base_price=Decimal(str(raw_price)) if raw_price is not None else None,
                currency=data.get("currency") or "RUB",
                import_job_id=job.id,
            )
            self._session.add(product)
            await self._session.flush()
            row.entity_id = product.id
            row.before_snapshot = None
            return

        if job.mode == ImportMode.INSERT.value:
            row.status = ImportRowStatus.SKIPPED.value
            row.errors = [*row.errors, "Пропущена при применении: уже существует"]
            return

        before: dict[str, Any] = {}
        for field in ("name", "description", "direction_id", "duration_hours", "format",
                      "base_price", "currency"):
            if field not in data:
                continue
            if field == "direction_id":
                new_value: Any = uuid.UUID(data[field])
            elif field == "base_price":
                new_value = Decimal(str(data[field]))
            else:
                new_value = data[field]
            old_value = getattr(existing, field)
            if _json_safe(old_value) != _json_safe(new_value):
                before[field] = _json_safe(old_value)
                setattr(existing, field, new_value)
        existing.version += 1
        await self._session.flush()
        row.entity_id = existing.id
        row.before_snapshot = before or {}

    async def _apply_license_row(self, job: ImportJob, row: ImportRowResult) -> None:
        """П3 — natural key `contract_number` (см. `catalog.models.
        OrganizationLicense`, докстринг). Структура — буквальная копия
        `_apply_product_row`, тот же generic-приём для третьего типа."""
        data = dict(row.row_data)
        contract_number = data.get("contract_number")
        existing = (
            await self._session.scalar(
                select(OrganizationLicense).where(
                    OrganizationLicense.contract_number == contract_number
                )
            )
            if contract_number
            else None
        )

        if existing is None:
            if job.mode == ImportMode.UPDATE.value:
                row.status = ImportRowStatus.SKIPPED.value
                row.errors = [*row.errors, "Запись для обновления не найдена"]
                return
            license_ = OrganizationLicense(
                organization_id=uuid.UUID(data["organization_id"]),
                vendor=data.get("vendor"),
                product_name=data.get("product_name"),
                contract_number=contract_number,
                license_signed_at=(
                    dt.date.fromisoformat(data["license_signed_at"])
                    if data.get("license_signed_at")
                    else None
                ),
                license_valid_year=data.get("license_valid_year"),
                transfer_status=data.get("transfer_status"),
                manager_full_name=data.get("manager_full_name"),
                responsible_contacts=data.get("responsible_contacts"),
                comment=data.get("comment"),
                import_job_id=job.id,
            )
            self._session.add(license_)
            await self._session.flush()
            row.entity_id = license_.id
            row.before_snapshot = None
            return

        if job.mode == ImportMode.INSERT.value:
            row.status = ImportRowStatus.SKIPPED.value
            row.errors = [*row.errors, "Пропущена при применении: уже существует"]
            return

        before: dict[str, Any] = {}
        for field in (
            "organization_id", "vendor", "product_name", "license_signed_at",
            "license_valid_year", "transfer_status", "manager_full_name",
            "responsible_contacts", "comment",
        ):
            if field not in data:
                continue
            if field == "organization_id":
                new_value: Any = uuid.UUID(data[field])
            elif field == "license_signed_at":
                new_value = dt.date.fromisoformat(data[field]) if data[field] else None
            else:
                new_value = data[field]
            old_value = getattr(existing, field)
            if _json_safe(old_value) != _json_safe(new_value):
                before[field] = _json_safe(old_value)
                setattr(existing, field, new_value)
        existing.version += 1
        await self._session.flush()
        row.entity_id = existing.id
        row.before_snapshot = before or {}

    async def finalize_apply_if_done(self, job: ImportJob) -> bool:
        """Возвращает `True`, если задание больше не в статусе `applying`."""
        remaining = await self._session.scalar(
            select(func.count(ImportRowResult.id)).where(
                ImportRowResult.import_job_id == job.id,
                ImportRowResult.status.in_(_PENDING_STATUSES),
                ImportRowResult.entity_id.is_(None),
            )
        )
        if remaining:
            return False

        job.status = (
            ImportJobStatus.COMPLETED_WITH_ERRORS.value
            if job.error_rows
            else ImportJobStatus.COMPLETED.value
        )
        job.finished_at = dt.datetime.now(dt.UTC)
        job.rollback_available = True
        await self._session.flush()
        await self._audit.record(
            AuditAction.IMPORT_APPLIED,
            entity_type="import_job",
            entity_id=job.id,
            changes={"status": {"old": None, "new": job.status}},
        )
        return True

    # -- откат ---------------------------------------------------------

    async def start_rollback(self, job: ImportJob) -> ImportJob:
        if not job.rollback_available or job.status not in (
            ImportJobStatus.COMPLETED.value,
            ImportJobStatus.COMPLETED_WITH_ERRORS.value,
        ):
            raise AppError(ErrorCode.IMPORT_NOT_ROLLBACKABLE, "Импорт нельзя откатить")
        job.status = ImportJobStatus.ROLLING_BACK.value
        job.rollback_available = False
        await self._session.flush()
        return job

    async def rollback_batch(self, job: ImportJob, *, batch_size: int) -> int:
        pending = (
            (
                await self._session.execute(
                    select(ImportRowResult)
                    .where(
                        ImportRowResult.import_job_id == job.id,
                        ImportRowResult.status.in_(_PENDING_STATUSES),
                        ImportRowResult.entity_id.is_not(None),
                    )
                    .order_by(ImportRowResult.row_number)
                    .limit(batch_size)
                )
            )
            .scalars()
            .all()
        )
        if not pending:
            return 0

        model = _MODEL_BY_ENTITY[job.entity_type]
        for row in pending:
            entity = await self._session.get(model, row.entity_id)
            if entity is None:
                row.status = ImportRowStatus.ROLLED_BACK.value
                continue
            if row.before_snapshot:
                for field, old_value in row.before_snapshot.items():
                    if (
                        field in ("region_id", "direction_id", "organization_id")
                        and old_value is not None
                    ):
                        old_value = uuid.UUID(old_value)
                    if field == "base_price" and old_value is not None:
                        old_value = Decimal(str(old_value))
                    if field == "license_signed_at" and old_value is not None:
                        old_value = dt.date.fromisoformat(old_value)
                    setattr(entity, field, old_value)
                entity.version += 1
                row.status = ImportRowStatus.ROLLED_BACK.value
            else:
                blocked = await self._has_critical_dependents(job.entity_type, entity.id)
                if blocked:
                    row.status = ImportRowStatus.ROLLBACK_BLOCKED.value
                    row.errors = [*row.errors, "Откат заблокирован: есть связанные сделки"]
                else:
                    entity.deleted_at = dt.datetime.now(dt.UTC)
                    row.status = ImportRowStatus.ROLLED_BACK.value
        await self._session.flush()
        return len(pending)

    async def _has_critical_dependents(self, entity_type: str, entity_id: uuid.UUID) -> bool:
        if entity_type == ImportEntityType.ORGANIZATION.value:
            from app.modules.crm.models import Deal

            return bool(
                await self._session.scalar(
                    select(Deal.id).where(
                        Deal.organization_id == entity_id, Deal.deleted_at.is_(None)
                    ).limit(1)
                )
            )
        if entity_type == ImportEntityType.PRODUCT.value:
            from app.modules.crm.models import DealProduct

            return bool(
                await self._session.scalar(
                    select(DealProduct.id).where(DealProduct.product_id == entity_id).limit(1)
                )
            )
        return False

    async def finalize_rollback_if_done(self, job: ImportJob) -> bool:
        remaining = await self._session.scalar(
            select(func.count(ImportRowResult.id)).where(
                ImportRowResult.import_job_id == job.id,
                ImportRowResult.status.in_(_PENDING_STATUSES),
                ImportRowResult.entity_id.is_not(None),
            )
        )
        if remaining:
            return False

        job.status = ImportJobStatus.ROLLED_BACK.value
        job.rolled_back_at = dt.datetime.now(dt.UTC)
        await self._session.flush()
        await self._audit.record(
            AuditAction.IMPORT_ROLLBACK,
            entity_type="import_job",
            entity_id=job.id,
            changes={"rolled_back_at": {"old": None, "new": job.rolled_back_at.isoformat()}},
        )
        return True
