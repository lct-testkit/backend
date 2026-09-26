"""Сервис файлов и вложений (раздел 9, 3.7).

Полный цикл без прогона байтов через API: `upload_intent` выдаёт presigned
PUT, клиент грузит объект напрямую в SeaweedFS, `commit` скачивает объект
обратно (`app.core.storage.inspect_object`), сверяет реальный размер с
лимитом (`upload_intent` верит `size_bytes` из тела запроса, а не факту),
считает `sha256`, сверяет magic bytes с заявленным расширением и прогоняет
антивирусную проверку — только после этого файл становится `ready` и
пригоден для вложений.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import NoReturn, Protocol, runtime_checkable

from sqlalchemy import Select, func, or_, select, text, update
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

_UTF8_BOM = b"\xef\xbb\xbf"
# Пробельные символы, допустимые между BOM и началом JSON (RFC 8259, §2).
_JSON_WHITESPACE = b" \t\r\n"
_JSON_ROOT_OPENERS = frozenset(b"[{")


def _extension(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


# Символы, которых не должно быть в имени: кавычка ломает `Content-Disposition: filename="..."`,
# остальные — запрещённые в именах файлов Windows (файл скачивают и на ней).
_FILENAME_FORBIDDEN = frozenset(['"', "\\", "<", ">", ":", "|", "?", "*"])
# Категории Unicode, которых нет в честном имени: управляющие (в том числе NUL, CR и LF —
# внедрение заголовков), «форматирующие» (RLO и прочие двунаправленные метки: имя с такой
# меткой показывается задом наперёд, и `fdp.exe` выглядит как `exe.pdf`), суррогаты, частное
# использование и неназначенные.
_FILENAME_BAD_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})
_FILENAME_MAX = 255


def sanitize_filename(name: str) -> str:
    """Безопасное имя файла для БД, заголовков и скачивания.

    Клиентское имя шло в `original_filename` и в `Content-Disposition` как есть: путь (`../`),
    управляющие символы и кавычки, двунаправленные метки. Берётся только последний компонент
    пути, выбрасываются опасные символы, схлопываются пробелы, обрезаются точки и пробелы по
    краям, длина — не больше колонки (расширение сохраняется). Пустой результат — `file`."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    # Переводы строки и табуляция — пробелы, а не «ничего»: два слова не должны склеиваться.
    base = base.translate({ord(ch): " " for ch in "\t\n\r\v\f"})
    cleaned = "".join(
        ch
        for ch in base
        if ch not in _FILENAME_FORBIDDEN
        and unicodedata.category(ch) not in _FILENAME_BAD_CATEGORIES
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        return "file"
    if len(cleaned) > _FILENAME_MAX:
        stem, dot, ext = cleaned.rpartition(".")
        if dot and 0 < len(ext) <= 16:
            cleaned = stem[: _FILENAME_MAX - len(ext) - 1].rstrip(" .") + "." + ext
        else:
            cleaned = cleaned[:_FILENAME_MAX].rstrip(" .")
    return cleaned or "file"


# Форматы, у которых `commit` проверил содержимое по сигнатуре: MIME берётся отсюда, а не из
# заявления клиента. Иначе `.pdf` с `mime_type: text/html` (или наоборот, чужой файл с
# `application/pdf`) проходил бы в места, которые доверяют полю (подпись требует PDF по MIME).
_VERIFIED_MIME: dict[str, str] = {
    "pdf": "application/pdf",
    "png": "image/png",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "zip": "application/zip",
    "gz": "application/gzip",
    "gzip": "application/gzip",
    "doc": "application/msword",
    "xls": "application/vnd.ms-excel",
}


async def lock_storage_key(session: AsyncSession, bucket: str, key: str) -> None:
    """Транзакционный замок на объект хранилища.

    Несколько записей `files` могут ссылаться на один объект (дедупликация по `sha256`).
    Привязка новой записи к чужому объекту и его физическое удаление (истёк срок отчёта, очистка)
    идут под одним замком: иначе объект удалялся бы между «нашли, на что сослаться» и
    «сослались», и живая запись оставалась бы без файла."""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"{bucket}/{key}"},
    )


async def delete_object_if_unreferenced(
    session: AsyncSession, *, bucket: str, key: str, excluding_file_id: uuid.UUID | None = None
) -> bool:
    """Физически удаляет объект, только если на него не ссылается ни одна другая живая запись
    `files`. `True` — объект удалён.

    Дедупликация направляла новые записи на уже лежащий объект, а очистка отчётов удаляла его
    безусловно: файл пользователя оставался `ready` без содержимого. Вызывающий сам переводит
    свою запись в `deleted` (в этой же транзакции), здесь она исключается из подсчёта."""
    await lock_storage_key(session, bucket, key)
    stmt = (
        select(func.count())
        .select_from(File)
        .where(
            File.bucket == bucket,
            File.storage_key == key,
            File.deleted_at.is_(None),
            File.status != FileStatus.DELETED.value,
        )
    )
    if excluding_file_id is not None:
        stmt = stmt.where(File.id != excluding_file_id)
    if await session.scalar(stmt):
        return False
    await delete_object(bucket=bucket, key=key)
    return True


def _looks_like_json(magic: bytes) -> bool:
    """У JSON нет сигнатуры, но корень файла с данными — массив или объект: первый значащий байт
    (после необязательного BOM и пробелов) — `[` или `{`. Так `.json`, за которым лежит PNG, HTML
    или исполняемый файл, не проходит `commit`.

    Проверяется только префикс, который снимает `storage.inspect_object` (16 байт). Если в нём
    одни BOM и пробелы, значащий символ дальше — судить не по чему, файл принимается; битый JSON
    отклонит уже импорт с внятным сообщением. Пустой объект (нет ни одного байта) — не JSON."""
    if not magic:
        return False
    body = magic.removeprefix(_UTF8_BOM).lstrip(_JSON_WHITESPACE)
    return not body or body[0] in _JSON_ROOT_OPENERS


# Форматы без бинарной сигнатуры, у которых начало содержимого всё же проверяется.
_CONTENT_CHECKS: dict[str, Callable[[bytes], bool]] = {"json": _looks_like_json}


def _check_magic_bytes(extension: str, magic: bytes) -> bool:
    content_check = _CONTENT_CHECKS.get(extension)
    if content_check is not None:
        return content_check(magic)
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
    (`erasure_request` — исполнение запросов на удаление ещё не реализовано)
    доступ разрешён только администратору — делегировать скоуп пока некому.
    `signature_document` (модуль `signing`, спринт 6) и `report_job` (модуль
    `reporting`, спринт 8) — уже настоящая делегация, не заглушка.
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
    if entity_type == "report_job":
        # Раздел 4.13 (спринт 8): отчёт доступен только тому, кто его
        # запросил, либо администратору. `ReportJobService.ensure_read_access`
        # уже это реализует — делегируем, тем же приёмом, что и
        # `signature_document` ниже.
        from app.modules.reporting.service import ReportJobService

        report_service = ReportJobService(session)
        job = await report_service.get_or_404(entity_id)
        report_service.ensure_read_access(job, principal)
        return
    if entity_type == "edm_agreement":
        # Скан соглашения об ЭДО (`edm_agreements.agreement_file_id`, dop.md §13):
        # читает администратор и тот, у кого `edm:read` (AUDITOR).
        if not (principal.is_admin or has_permission(principal.role, Permission.EDM_READ)):
            raise ForbiddenError("Файл соглашения об ЭДО недоступен")
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
        # Имя санитизируется до любых проверок: расширение берётся из очищенного имени, а в БД и
        # в заголовок скачивания попадает только оно.
        filename = sanitize_filename(payload.filename)
        extension = _extension(filename)
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
        # Ключ не содержит имени: `quote()` кириллицы даёт 6 символов на букву, и длинное имя
        # (от ~80 букв) не помещалось в `storage_key` (500), а `..` в имени мог попасть в путь.
        # Настоящее имя живёт только в БД (`original_filename`), расширение уже проверено.
        storage_key = f"{file_id}/upload.{extension}"
        file = File(
            id=file_id,
            storage_key=storage_key,
            bucket=bucket,
            original_filename=filename,
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
            changes={"original_filename": {"old": None, "new": filename}},
        )
        return file, upload_url, expires_at

    async def _reject_commit(self, error: AppError) -> NoReturn:
        """Фиксирует статус и аудит отказа и только потом бросает исключение.

        `get_db_session` откатывает транзакцию на любом исключении: `infected`/`file_too_large` и
        запись аудита, сделанные перед `raise`, исчезали вместе с ней. Файл оставался `pending`
        (а при слишком большом размере ещё и указывал на удалённый объект), инцидент не
        попадал в журнал. К этому моменту в транзакции только статус файла и аудит: проверка
        идёт до любых других записей, поэтому немедленный коммит безопасен (так же устроен
        `audit.service.record_denied_and_commit`)."""
        await self._session.commit()
        raise error

    async def commit(self, file: File, *, expected_sha256: str | None = None) -> File:
        # Строка файла под замком: два одновременных `commit` одного файла не должны оба
        # дойти до дедупликации и удаления объекта.
        await self._session.refresh(file, with_for_update=True)
        if file.status == FileStatus.INFECTED.value:
            # Отказ теперь сохраняется, и повтор не должен «молча вернуть 200» с чужим статусом.
            raise AppError(
                ErrorCode.FILE_INFECTED,
                "Файл отклонён проверкой при загрузке: загрузите его заново",
            )
        if file.status != FileStatus.PENDING.value:
            return file

        inspection = await inspect_object(bucket=file.bucket, key=file.storage_key)
        if not inspection.exists:
            raise AppError(
                ErrorCode.VALIDATION, "Объект не найден в хранилище: загрузка не завершена"
            )

        file.sha256 = inspection.sha256
        file.size_bytes = inspection.size_bytes

        # Раздел 3.7: `upload_intent` сверяет лимит только с `size_bytes` из
        # тела запроса — значением, которое клиент указывает ДО получения
        # presigned PUT и может занизить как угодно, а затем закачать в
        # SeaweedFS сколько угодно байт напрямую, в обход API. Здесь — первый
        # момент, когда размер известен из факта, а не из заявления.
        # `deal_scope` (см. `upload_intent`) нигде не сохраняется в записи
        # `files`, поэтому какой из двух лимитов применялся при выдаче
        # ссылки, отсюда не видно; берём больший как потолок — меньше него
        # объект не может оказаться ни при одном сценарии `upload_intent`.
        settings = get_settings()
        max_size = max(settings.files_max_size_bytes, settings.deal_files_max_size_bytes)
        if inspection.size_bytes > max_size:
            file.status = FileStatus.INFECTED.value
            file.scan_result = "file_too_large"
            file.scanned_at = dt.datetime.now(dt.UTC)
            await delete_object(bucket=file.bucket, key=file.storage_key)
            await self._session.flush()
            await self._audit.record(
                AuditAction.FILE_TOO_LARGE,
                entity_type="file",
                entity_id=file.id,
                changes={
                    "reason": {"old": None, "new": "file_too_large"},
                    "limit_bytes": {"old": None, "new": max_size},
                },
            )
            await self._reject_commit(
                AppError(ErrorCode.FILE_TOO_LARGE, "Превышен допустимый размер файла")
            )

        extension = _extension(file.original_filename)
        magic_ok = _check_magic_bytes(extension, inspection.magic_bytes)
        hash_ok = expected_sha256 is None or expected_sha256 == inspection.sha256

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
            await self._reject_commit(
                AppError(
                    ErrorCode.FILE_INFECTED,
                    "Содержимое файла не соответствует заявленному формату",
                )
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
            await self._reject_commit(
                AppError(ErrorCode.FILE_INFECTED, "Файл не прошёл антивирусную проверку")
            )

        await self._dedup_storage(file)

        # MIME — по проверенному содержимому, а не по заявлению клиента (см. `_VERIFIED_MIME`).
        verified_mime = _VERIFIED_MIME.get(extension)
        if verified_mime is not None:
            file.mime_type = verified_mime
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
        `storage_key`. Физически объект хранится один раз.

        Только внутри одного бакета и только на живую запись: объект другого бакета живёт по
        своим правилам (отчёты истекают через 7 дней, и очистка удаляла бы объект, на который
        ссылается файл пользователя). Привязка идёт под замком ключа: очистка и удаление
        (`delete_object_if_unreferenced`) берут тот же замок, поэтому объект не исчезнет между
        выбором кандидата и ссылкой на него."""
        candidate = (
            select(File)
            .where(
                File.sha256 == file.sha256,
                File.bucket == file.bucket,
                File.status == FileStatus.READY.value,
                File.deleted_at.is_(None),
                File.id != file.id,
            )
            .order_by(File.created_at, File.id)
            .limit(1)
        )
        existing = await self._session.scalar(candidate)
        if existing is None:
            return
        await lock_storage_key(self._session, existing.bucket, existing.storage_key)
        # Пока ждали замок, кандидата могли удалить: проверяем заново, уже под замком.
        existing = await self._session.scalar(candidate.execution_options(populate_existing=True))
        if existing is None:
            return
        await delete_object(bucket=file.bucket, key=file.storage_key)
        file.storage_key = existing.storage_key

    async def download_url(self, file: File) -> tuple[str, dt.datetime]:
        if file.status != FileStatus.READY.value:
            raise AppError(ErrorCode.FILE_ACCESS_DENIED, "Файл недоступен для скачивания")
        url = await generate_presigned_get(
            bucket=file.bucket,
            key=file.storage_key,
            expires_seconds=_DOWNLOAD_URL_TTL_SECONDS,
            # Имя санитизируется и здесь: старые записи (до очистки при загрузке) могли
            # сохранить кавычку или перевод строки, а хранилище вставляет имя в заголовок ответа.
            filename=sanitize_filename(file.original_filename),
        )
        expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=_DOWNLOAD_URL_TTL_SECONDS)
        await self._audit.record(
            AuditAction.FILE_DOWNLOADED,
            entity_type="file",
            entity_id=file.id,
            changes={"contains_pd": {"old": None, "new": file.contains_pd}},
        )
        return url, expires_at

    def ensure_can_delete(self, principal: Principal, file: File) -> None:
        """Удалить файл может тот, у кого есть `file:delete`, и его автор; файл
        с вложениями не удаляется никем (`soft_delete`)."""
        if has_permission(principal.role, Permission.FILE_DELETE) or (
            file.uploaded_by == principal.user_id
        ):
            return
        raise ForbiddenError("Удалить файл может его автор или руководитель")

    async def _referenced_by(self, file: File) -> str | None:
        """Что удерживает файл: вложение, подпись, соглашение об ЭДО, отчёт, акт, импорт.

        Счётчик `refcount` вели только вложения через API: файлы подписи, протоколы, вложения
        импорта и подписанные копии создавались мимо него, и `DELETE /files/{id}` удалял их у
        автора или руководителя. Поэтому смотрим на сами ссылки, а не на счётчик. Импорты
        моделей — внутри метода: слой `files` не должен зависеть от них на уровне модуля."""
        from app.modules.identity.models import DataErasureRequest
        from app.modules.imports.models import ImportJob
        from app.modules.registry.models import RegistryVersion
        from app.modules.reporting.models import ReportJob
        from app.modules.signing.models import EdmAgreement, SignatureDocument

        session = self._session
        if file.refcount > 0 or await session.scalar(
            select(Attachment.id)
            .where(Attachment.file_id == file.id, Attachment.deleted_at.is_(None))
            .limit(1)
        ):
            return "вложениях"
        checks = (
            (
                "документе на подпись (оригинал, подписанная копия или протокол)",
                select(SignatureDocument.id).where(
                    or_(
                        SignatureDocument.file_id == file.id,
                        SignatureDocument.signed_file_id == file.id,
                        SignatureDocument.protocol_file_id == file.id,
                    )
                ),
            ),
            (
                "соглашении об ЭДО",
                select(EdmAgreement.id).where(EdmAgreement.agreement_file_id == file.id),
            ),
            ("отчёте", select(ReportJob.id).where(ReportJob.file_id == file.id)),
            (
                "акте уничтожения данных",
                select(DataErasureRequest.id).where(DataErasureRequest.act_file_id == file.id),
            ),
            (
                "задании импорта",
                select(ImportJob.id).where(
                    or_(ImportJob.file_id == file.id, ImportJob.result_file_id == file.id)
                ),
            ),
            (
                "версии реестра ЕГРЮЛ",
                select(RegistryVersion.id).where(RegistryVersion.file_id == file.id),
            ),
        )
        for label, stmt in checks:
            if await session.scalar(stmt.limit(1)) is not None:
                return label
        return None

    async def soft_delete(self, file: File) -> None:
        # Раздел 3.7/9: подписанные документы и файлы, закрывающие пройденный
        # переход, не удаляются без административного действия — здесь это
        # выражено проще: пока есть хоть одна активная ссылка, удалить нельзя.
        reason = await self._referenced_by(file)
        if reason is not None:
            raise AppError(
                ErrorCode.VALIDATION,
                f"Файл используется в {reason}: сначала уберите ссылку, удалить его нельзя"
                if reason != "вложениях"
                else "Файл привязан к вложениям, сначала отвяжите их",
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
        if entity_type == "edm_agreement":
            # Скан лежит в самом соглашении (`agreement_file_id`), а не во вложениях.
            from app.modules.signing.models import EdmAgreement

            return bool(
                await self._session.scalar(
                    select(EdmAgreement.id).where(
                        EdmAgreement.id == entity_id, EdmAgreement.agreement_file_id == file_id
                    )
                )
            )
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
        self._session.add(attachment)
        # Счётчик растёт в самой БД: `refcount += 1` в Python при параллельных привязках терял
        # инкременты, и файл с двумя вложениями считался привязанным к одному.
        await self._session.execute(
            update(File)
            .where(File.id == file.id)
            .values(refcount=File.refcount + 1)
            .execution_options(synchronize_session=False)
        )
        await self._session.flush()
        await self._session.refresh(file, attribute_names=["refcount"])
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

    def ensure_can_delete(self, principal: Principal, attachment: Attachment) -> None:
        """Отвязать вложение может тот, у кого есть `file:delete` (HEAD, ADMIN),
        и его автор — KAM убирает своё ошибочное вложение. Доступ к родительской
        сущности проверяет роутер отдельно."""
        if (
            has_permission(principal.role, Permission.FILE_DELETE)
            or attachment.uploaded_by == principal.user_id
        ):
            return
        raise ForbiddenError("Отвязать вложение может его автор или руководитель")

    async def delete(self, attachment: Attachment) -> None:
        file = await self._session.get(File, attachment.file_id)
        attachment.deleted_at = dt.datetime.now(dt.UTC)
        if file is not None:
            await self._session.execute(
                update(File)
                .where(File.id == file.id)
                .values(refcount=func.greatest(File.refcount - 1, 0))
                .execution_options(synchronize_session=False)
            )
        await self._session.flush()
        if file is not None:
            await self._session.refresh(file, attribute_names=["refcount"])
        await self._audit.record(
            AuditAction.ATTACHMENT_DELETED,
            entity_type=attachment.entity_type,
            entity_id=attachment.entity_id,
            changes={"file_id": {"old": str(attachment.file_id), "new": None}},
        )
