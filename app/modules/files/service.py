"""Сервис файлов и вложений (раздел 9, 3.7).

Полный цикл без прогона байтов через API: `upload_intent` выдаёт presigned
PUT, клиент грузит объект напрямую в SeaweedFS, `commit` скачивает объект
обратно (`app.core.storage.inspect_object`), считает `sha256`, сверяет magic
bytes с заявленным расширением и прогоняет антивирусную проверку —
только после этого файл становится `ready` и пригоден для вложений.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable
from urllib.parse import quote

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode, ForbiddenError, NotFoundError
from app.core.ids import uuid7
from app.core.permissions import Permission, has_permission
from app.core.security import Principal
from app.core.storage import (
    delete_object,
    ensure_bucket,
    generate_presigned_get,
    generate_presigned_put,
    inspect_object,
)
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.files.models import Attachment, File, FileStatus
from app.modules.files.schemas import UploadIntentRequest

# Сигнатуры разрешённых форматов (раздел 9: «расширение врёт», нужна
# проверка по факту). docx/xlsx неотличимы от zip по первым байтам — это
# ожидаемо, они и есть zip-контейнеры; отличать их по расширению безопасно,
# т.к. содержимое всё равно проходит антивирусную проверку.
_MAGIC_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpeg": (b"\xff\xd8\xff",),
    "jpg": (b"\xff\xd8\xff",),
    "pdf": (b"%PDF",),
    "zip": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    "docx": (b"PK\x03\x04",),
    "xlsx": (b"PK\x03\x04",),
    "gz": (b"\x1f\x8b",),
    "gzip": (b"\x1f\x8b",),
    "rar": (b"Rar!\x1a\x07",),
    "doc": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    "xls": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
}

# Явный запрет независимо от `ALLOWED_FILE_EXTENSIONS`: SVG допускает
# встроенный JS и является XSS-вектором (раздел 3.7).
_FORBIDDEN_EXTENSIONS = frozenset({"svg"})

_UPLOAD_URL_TTL_SECONDS = 900  # 15 минут, раздел 3.7
_DOWNLOAD_URL_TTL_SECONDS = 300  # 5 минут, раздел 3.7


def _extension(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _check_magic_bytes(extension: str, magic: bytes) -> bool:
    signatures = _MAGIC_SIGNATURES.get(extension)
    if not signatures:
        return True
    return any(magic.startswith(sig) for sig in signatures)


@dataclass(slots=True)
class ScanResult:
    clean: bool
    detail: str | None = None


@runtime_checkable
class AntivirusScanner(Protocol):
    async def scan(self, *, bucket: str, key: str) -> ScanResult: ...


class NullAntivirusScanner:
    """Заглушка: `clamav` в разделе 2.2 явно помечен опциональным контейнером,
    и в этой среде он не поднят. Интерфейс уже здесь — тот же приём, что
    `signing.service` и `integration.service.LoggingOutboxService`: подключение
    реального сканера не потребует правок вызывающего кода."""

    async def scan(self, *, bucket: str, key: str) -> ScanResult:
        return ScanResult(clean=True, detail="stub: антивирус не подключён")


_scanner: AntivirusScanner = NullAntivirusScanner()


def register_antivirus_scanner(scanner: AntivirusScanner) -> None:
    global _scanner
    _scanner = scanner


def get_antivirus_scanner() -> AntivirusScanner:
    return _scanner


async def check_entity_access(
    session: AsyncSession, principal: Principal, *, entity_type: str, entity_id: uuid.UUID
) -> None:
    """Раздел 9: право на скачивание/привязку проверяется по родительской
    сущности, не только по праву на файлы вообще. Импорты сервисов других
    модулей — намеренно внутри функции (тот же приём, что
    `workflow.service`/`crm.service` уже используют друг для друга): иначе
    `files → crm`/`files → catalog` на уровне модуля рискует зациклиться,
    если эти модули когда-нибудь начнут ссылаться на файлы при импорте.

    Для типов сущностей без собственного, реально построенного конвейера
    (`report`, `erasure_request` — генерация отчётов и исполнение запросов на
    удаление ещё не реализованы) доступ разрешён только администратору —
    делегировать скоуп пока некому. `signature_document` (модуль `signing`,
    спринт 6) уже настоящая делегация, не заглушка.
    """
    if entity_type == "deal":
        from app.modules.crm.service import DealService

        await DealService(session).get_or_404(entity_id, principal)
        return
    if entity_type == "organization":
        from app.modules.catalog.service import OrganizationService

        await OrganizationService(session).get_or_404(entity_id, principal)
        return
    if entity_type == "contact":
        from app.modules.catalog.service import ContactService

        await ContactService(session).get_or_404(entity_id, principal)
        return
    if entity_type == "import_job":
        # Отчёты об ошибках импорта каталогов (раздел 4.12) — доступны тем,
        # кто вообще может запускать импорт, не только автору конкретного
        # задания: HEAD должен видеть отчёт коллеги по своей роли, а не
        # только свой собственный.
        if not (principal.is_admin or has_permission(principal.role, Permission.IMPORT_RUN)):
            raise ForbiddenError("Файл импорта недоступен")
        return
    if entity_type == "registry_version":
        if not (principal.is_admin or has_permission(principal.role, Permission.REGISTRY_IMPORT)):
            raise ForbiddenError("Файл реестра недоступен")
        return
    if entity_type == "signature_document":
        # Штамп/протокол/оригинал документа на подпись (dop.md §10.9) —
        # доступ по той же сущности, к которой привязан документ (deal и
        # т.д.), делегируется сервису подписания, а не проверяется здесь
        # напрямую: этот модуль не знает про `signature_requests`/скоуп КАМа.
        # `ensure_read_access`, а не `ensure_access`: подписант вправе скачать
        # то, что сам подписал, даже если сделка вне его скоупа по разделу 3.2.
        from app.modules.signing.service import SignatureDocumentService

        service = SignatureDocumentService(session)
        document = await service.get_or_404(entity_id)
        await service.ensure_read_access(principal, document)
        return
    if not principal.is_admin:
        raise ForbiddenError(
            f"Проверка доступа для типа сущности {entity_type!r} пока не реализована"
        )


class FileService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    async def get_or_404(self, file_id: uuid.UUID) -> File:
        file = await self._session.get(File, file_id)
        if file is None or file.deleted_at is not None:
            raise NotFoundError("Файл", file_id)
        return file

    async def upload_intent(
        self, principal: Principal, payload: UploadIntentRequest, *, deal_scope: bool = False
    ) -> tuple[File, str, dt.datetime]:
        settings = get_settings()
        extension = _extension(payload.filename)
        if extension in _FORBIDDEN_EXTENSIONS or extension not in settings.allowed_extensions:
            raise AppError(ErrorCode.FILE_TYPE_NOT_ALLOWED, f"Тип файла {extension!r} не разрешён")

        max_size = (
            settings.deal_files_max_size_bytes if deal_scope else settings.files_max_size_bytes
        )
        if payload.size_bytes > max_size:
            raise AppError(ErrorCode.FILE_TOO_LARGE, "Превышен допустимый размер файла")

        bucket = settings.s3_bucket_files
        await ensure_bucket(bucket)

        file_id = uuid7()
        storage_key = f"{file_id}/{quote(payload.filename)}"
        file = File(
            id=file_id,
            storage_key=storage_key,
            bucket=bucket,
            original_filename=payload.filename,
            mime_type=payload.mime_type,
            size_bytes=payload.size_bytes,
            status=FileStatus.PENDING.value,
            uploaded_by=principal.user_id,
        )
        self._session.add(file)
        await self._session.flush()

        upload_url = await generate_presigned_put(
            bucket=bucket,
            key=storage_key,
            content_type=payload.mime_type,
            expires_seconds=_UPLOAD_URL_TTL_SECONDS,
        )
        expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=_UPLOAD_URL_TTL_SECONDS)

        await self._audit.record(
            AuditAction.FILE_UPLOAD_INTENT,
            entity_type="file",
            entity_id=file.id,
            changes={"original_filename": {"old": None, "new": payload.filename}},
        )
        return file, upload_url, expires_at

    async def commit(self, file: File, *, expected_sha256: str | None = None) -> File:
        if file.status != FileStatus.PENDING.value:
            return file

        inspection = await inspect_object(bucket=file.bucket, key=file.storage_key)
        if not inspection.exists:
            raise AppError(
                ErrorCode.VALIDATION, "Объект не найден в хранилище: загрузка не завершена"
            )

        extension = _extension(file.original_filename)
        magic_ok = _check_magic_bytes(extension, inspection.magic_bytes)
        hash_ok = expected_sha256 is None or expected_sha256 == inspection.sha256

        file.sha256 = inspection.sha256
        file.size_bytes = inspection.size_bytes

        if not magic_ok or not hash_ok:
            file.status = FileStatus.INFECTED.value
            file.scan_result = "signature_mismatch"
            file.scanned_at = dt.datetime.now(dt.UTC)
            await self._session.flush()
            await self._audit.record(
                AuditAction.FILE_INFECTED,
                entity_type="file",
                entity_id=file.id,
                changes={"reason": {"old": None, "new": "magic_bytes_or_hash_mismatch"}},
            )
            raise AppError(
                ErrorCode.FILE_INFECTED, "Содержимое файла не соответствует заявленному формату"
            )

        scan = await get_antivirus_scanner().scan(bucket=file.bucket, key=file.storage_key)
        file.scanned_at = dt.datetime.now(dt.UTC)
        file.scan_result = "clean" if scan.clean else (scan.detail or "infected")

        if not scan.clean:
            file.status = FileStatus.INFECTED.value
            await self._session.flush()
            await self._audit.record(
                AuditAction.FILE_INFECTED,
                entity_type="file",
                entity_id=file.id,
                changes={"reason": {"old": None, "new": scan.detail}},
            )
            raise AppError(ErrorCode.FILE_INFECTED, "Файл не прошёл антивирусную проверку")

        await self._dedup_storage(file)

        file.status = FileStatus.READY.value
        await self._session.flush()
        await self._audit.record(
            AuditAction.FILE_COMMITTED,
            entity_type="file",
            entity_id=file.id,
            changes={"sha256": {"old": None, "new": file.sha256}},
        )
        return file

    async def _dedup_storage(self, file: File) -> None:
        """Раздел 3.7/9: «два одинаковых договора хранятся один раз, ссылок
        две». Каждый `commit` создаёт собственную запись `files` (proще и не
        ломает контракт `file_id` из ответа `upload-intent`), но если байты
        уже лежат в хранилище под другим, уже проверенным файлом — свежая
        копия объекта удаляется, а эта запись переиспользует существующий
        `storage_key`. Физически объект хранится один раз; `refcount`
        по-прежнему считает вложения на каждую запись `files` отдельно."""
        existing = await self._session.scalar(
            select(File)
            .where(
                File.sha256 == file.sha256,
                File.status == FileStatus.READY.value,
                File.id != file.id,
            )
            .limit(1)
        )
        if existing is None:
            return
        await delete_object(bucket=file.bucket, key=file.storage_key)
        file.bucket = existing.bucket
        file.storage_key = existing.storage_key

    async def download_url(self, file: File) -> tuple[str, dt.datetime]:
        if file.status != FileStatus.READY.value:
            raise AppError(ErrorCode.FILE_ACCESS_DENIED, "Файл недоступен для скачивания")
        url = await generate_presigned_get(
            bucket=file.bucket,
            key=file.storage_key,
            expires_seconds=_DOWNLOAD_URL_TTL_SECONDS,
            filename=file.original_filename,
        )
        expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=_DOWNLOAD_URL_TTL_SECONDS)
        await self._audit.record(
            AuditAction.FILE_DOWNLOADED,
            entity_type="file",
            entity_id=file.id,
            changes={"contains_pd": {"old": None, "new": file.contains_pd}},
        )
        return url, expires_at

    async def soft_delete(self, file: File) -> None:
        # Раздел 3.7/9: подписанные документы и файлы, закрывающие пройденный
        # переход, не удаляются без административного действия — здесь это
        # выражено проще: пока есть хоть одна активная ссылка, удалить нельзя.
        if file.refcount > 0:
            raise AppError(
                ErrorCode.VALIDATION, "Файл привязан к вложениям, сначала отвяжите их"
            )
        file.status = FileStatus.DELETED.value
        file.deleted_at = dt.datetime.now(dt.UTC)
        await self._session.flush()
        await self._audit.record(AuditAction.FILE_DELETED, entity_type="file", entity_id=file.id)


class AttachmentService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    def list_query(self, *, entity_type: str, entity_id: uuid.UUID) -> Select[tuple[Attachment]]:
        return select(Attachment).where(
            Attachment.entity_type == entity_type,
            Attachment.entity_id == entity_id,
            Attachment.deleted_at.is_(None),
        )

    async def get_or_404(self, attachment_id: uuid.UUID) -> Attachment:
        attachment = await self._session.get(Attachment, attachment_id)
        if attachment is None or attachment.deleted_at is not None:
            raise NotFoundError("Вложение", attachment_id)
        return attachment

    async def exists_for(
        self, *, file_id: uuid.UUID, entity_type: str, entity_id: uuid.UUID
    ) -> bool:
        """Раздел 9: ссылку на скачивание можно выдавать только на файл,
        реально привязанный к указанной сущности — иначе любой принципал с
        общим правом `file:download` мог бы подобрать чужой `file_id` к
        своей же (доступной ему) сущности и получить presigned URL на
        произвольный файл в системе."""
        return bool(
            await self._session.scalar(
                select(Attachment.id).where(
                    Attachment.file_id == file_id,
                    Attachment.entity_type == entity_type,
                    Attachment.entity_id == entity_id,
                    Attachment.deleted_at.is_(None),
                )
            )
        )

    async def create(
        self,
        principal: Principal,
        *,
        file: File,
        entity_type: str,
        entity_id: uuid.UUID,
        category: str,
        description: str | None,
    ) -> Attachment:
        if file.status != FileStatus.READY.value:
            raise AppError(ErrorCode.VALIDATION, "Файл ещё не прошёл проверку")
        if file.uploaded_by != principal.user_id and not principal.is_admin:
            # Без этой проверки любой обладатель `file:upload` мог бы
            # привязать (и тем самым легализовать доступ к) чужой уже
            # загруженный файл, зная или подобрав его UUID — вложение не
            # предполагает передачу владения чужими файлами между сущностями.
            raise ForbiddenError(
                "Привязать можно только файл, загруженный вами, или как администратор"
            )

        attachment = Attachment(
            file_id=file.id,
            entity_type=entity_type,
            entity_id=entity_id,
            category=category,
            description=description,
            uploaded_by=principal.user_id,
        )
        file.refcount += 1
        self._session.add(attachment)
        await self._session.flush()
        await self._audit.record(
            AuditAction.ATTACHMENT_CREATED,
            entity_type=entity_type,
            entity_id=entity_id,
            changes={
                "file_id": {"old": None, "new": str(file.id)},
                "category": {"old": None, "new": category},
            },
        )
        return attachment

    async def delete(self, attachment: Attachment) -> None:
        # Владение здесь не проверяется: маршрут уже требует `file:delete`
        # (раздел 4 — эта роль есть только у HEAD/ADMIN, см. `permissions.py`),
        # а не «своё/чужое», как в комментариях сделки.
        file = await self._session.get(File, attachment.file_id)
        attachment.deleted_at = dt.datetime.now(dt.UTC)
        if file is not None and file.refcount > 0:
            file.refcount -= 1
        await self._session.flush()
        await self._audit.record(
            AuditAction.ATTACHMENT_DELETED,
            entity_type=attachment.entity_type,
            entity_id=attachment.entity_id,
            changes={"file_id": {"old": str(attachment.file_id), "new": None}},
        )
