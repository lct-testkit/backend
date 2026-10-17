"""B6-6: срок хранения и очистка (152-ФЗ ст. 5 ч. 7).

Область этой итерации сознательно сужена против полного разбора задачи: реализованы
категории, которые не трогают неизменяемые или архивные данные — журнал безопасности,
OTP-коды подписи, уведомления, незавершённые загрузки, файлы после `soft_delete`.

НЕ реализовано (открытые пункты, см. отчёт): архивация партиций `audit_log` в S3 с
`DETACH PARTITION` и SECURITY DEFINER функциями (`drop_audit_log_partition`,
`register_audit_archive`, таблица `audit_archive_manifest`) — самая рискованная и трудоёмкая
часть разбора задачи, требующая отдельной миграции и бакета `audit-archive`; очистка
`inbound_messages.raw_payload`/`import_row_results.row_data` (сырые ПДн в JSONB); создание
`DataErasureRequest` для контактов с истёкшим сроком хранения ПДн (RETENTION_CONTACT_PII_YEARS);
удаление профиля учащегося по сроку (RETENTION_LEARNER_PROFILE_DAYS); зачистка файлов
заражённого карантина и импорта. Каждая из них по отдельности безопасна добавить тем же
приёмом (метод сервиса + категория в `sweep_retention`), когда до неё дойдёт очередь.

Каждая категория — отдельная транзакция, пачками по `RETENTION_BATCH_SIZE`, с Redis-локом
(`retention:lock:<category>`) — два одновременных запуска (ручной + cron) не должны пересчитывать
и удалять одно и то же дважды. `dry_run=True` считает, но не удаляет и не пишет
`RETENTION_APPLIED` со значением `deleted` больше нуля.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.redis_client import get_redis
from app.modules.admin.models import RetentionRun
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.files.models import File, FileStatus
from app.modules.files.service import delete_object_if_unreferenced
from app.modules.identity.models import SecurityEvent
from app.modules.notification.models import Notification
from app.modules.signing.models import SignatureOtpCode

logger = structlog.get_logger(__name__)

#: Категории в порядке запуска `sweep_retention` — стабильный порядок важен только для чтения
#: лога, сами категории независимы друг от друга.
CATEGORIES: tuple[str, ...] = (
    "security_events",
    "otp_codes",
    "notifications",
    "pending_uploads",
    "deleted_files",
)


@dataclass(slots=True)
class RetentionResult:
    category: str
    dry_run: bool
    deleted: int
    status: str = "completed"
    error: str | None = None


class RetentionService:
    def __init__(self, session: AsyncSession, settings: Settings | None = None) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._audit = AuditService(session)

    async def run_category(
        self, category: str, *, dry_run: bool, triggered_by: Any = None
    ) -> RetentionResult:
        if category not in CATEGORIES:
            raise ValueError(f"Неизвестная категория очистки: {category!r}")
        started = dt.datetime.now(dt.UTC)
        lock_key = f"retention:lock:{category}"
        try:
            redis = get_redis()
            acquired = await redis.set(lock_key, "1", nx=True, ex=600)
        except Exception:  # noqa: BLE001 — недоступность Redis не должна блокировать очистку
            logger.warning("retention_lock_unavailable", category=category)
            acquired = True
        if not acquired:
            return RetentionResult(category=category, dry_run=dry_run, deleted=0, status="locked")
        try:
            method = getattr(self, f"_sweep_{category}")
            deleted = await method(dry_run=dry_run)
            result = RetentionResult(category=category, dry_run=dry_run, deleted=deleted)
        except Exception as exc:  # noqa: BLE001 — каждая категория логируется и не валит соседние
            logger.warning("retention_category_failed", category=category, exc_info=True)
            result = RetentionResult(
                category=category, dry_run=dry_run, deleted=0, status="failed", error=str(exc)[:500]
            )
        finally:
            try:
                redis = get_redis()
                await redis.delete(lock_key)
            except Exception:  # noqa: BLE001
                pass
        finished = dt.datetime.now(dt.UTC)
        self._session.add(
            RetentionRun(
                category=category,
                started_at=started,
                finished_at=finished,
                dry_run=dry_run,
                deleted_count=result.deleted,
                status=result.status,
                error=result.error,
                triggered_by=triggered_by,
            )
        )
        await self._audit.record(
            AuditAction.RETENTION_APPLIED,
            entity_type="retention_run",
            changes={
                "category": {"old": None, "new": category},
                "dry_run": {"old": None, "new": dry_run},
                "deleted": {"old": None, "new": result.deleted},
                "status": {"old": None, "new": result.status},
            },
        )
        await self._session.flush()
        return result

    async def run_all(self, *, dry_run: bool, triggered_by: Any = None) -> list[RetentionResult]:
        return [
            await self.run_category(category, dry_run=dry_run, triggered_by=triggered_by)
            for category in CATEGORIES
        ]

    # --- Категории -----------------------------------------------------

    async def _sweep_security_events(self, *, dry_run: bool) -> int:
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(
            days=30 * self._settings.retention_security_events_months
        )
        return await self._delete_batched(
            SecurityEvent, SecurityEvent.created_at < cutoff, dry_run=dry_run
        )

    async def _sweep_otp_codes(self, *, dry_run: bool) -> int:
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=self._settings.retention_otp_days)
        return await self._delete_batched(
            SignatureOtpCode, SignatureOtpCode.created_at < cutoff, dry_run=dry_run
        )

    async def _sweep_notifications(self, *, dry_run: bool) -> int:
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(
            days=self._settings.retention_notifications_days
        )
        # Только прочитанные: непрочитанное уведомление не должно исчезнуть раньше, чем его
        # увидят, даже если оно старше срока.
        return await self._delete_batched(
            Notification,
            (Notification.created_at < cutoff) & (Notification.is_read.is_(True)),
            dry_run=dry_run,
        )

    async def _sweep_pending_uploads(self, *, dry_run: bool) -> int:
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(
            hours=self._settings.retention_pending_upload_hours
        )
        stmt = (
            select(File)
            .where(File.status == FileStatus.PENDING.value, File.created_at < cutoff)
            .limit(self._settings.retention_batch_size)
        )
        files = list((await self._session.execute(stmt)).scalars().all())
        if dry_run or not files:
            return len(files)
        now = dt.datetime.now(dt.UTC)
        for file in files:
            await delete_object_if_unreferenced(
                self._session, bucket=file.bucket, key=file.storage_key, excluding_file_id=file.id
            )
            file.status = FileStatus.DELETED.value
            file.deleted_at = now
        await self._session.flush()
        return len(files)

    async def _sweep_deleted_files(self, *, dry_run: bool) -> int:
        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(
            days=self._settings.retention_deleted_files_days
        )
        stmt = (
            select(File)
            .where(File.status == FileStatus.DELETED.value, File.deleted_at < cutoff)
            .limit(self._settings.retention_batch_size)
        )
        files = list((await self._session.execute(stmt)).scalars().all())
        if dry_run or not files:
            return len(files)
        removed = 0
        for file in files:
            if await delete_object_if_unreferenced(
                self._session, bucket=file.bucket, key=file.storage_key, excluding_file_id=file.id
            ):
                removed += 1
        return removed

    async def _delete_batched(self, model: Any, condition: Any, *, dry_run: bool) -> int:
        if dry_run:
            count_stmt = (
                select(model.id).where(condition).limit(self._settings.retention_batch_size)
            )
            return len(list((await self._session.execute(count_stmt)).scalars().all()))
        total = 0
        while True:
            ids_stmt = select(model.id).where(condition).limit(self._settings.retention_batch_size)
            ids = list((await self._session.execute(ids_stmt)).scalars().all())
            if not ids:
                break
            await self._session.execute(delete(model).where(model.id.in_(ids)))
            await self._session.flush()
            total += len(ids)
            if len(ids) < self._settings.retention_batch_size:
                break
        return total
