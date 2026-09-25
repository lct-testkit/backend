"""Фоновая обработка реестра ЕГРЮЛ: импорт выгрузки и сверка дрейфа реквизитов.

Импорт запускается тем же периодическим сканом, что и `workflow.tasks`
(см. его docstring про гонку видимости транзакции): `POST
/api/admin/registry/import` коммитит строку `registry_versions` в статусе
`pending` в конце HTTP-запроса, а эта задача подхватывает её по расписанию,
а не по прямой постановке в очередь arq из хендлера.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.metrics import background_tasks_total
from app.core.storage import download_object_bytes
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.catalog.models import Organization
from app.modules.crm.models import Deal
from app.modules.crm.models import Task as DealTask
from app.modules.files.models import File
from app.modules.notification.service import NotificationPriority, get_notification_service
from app.modules.registry.egrul_xml import ParsedEntry, iter_entries
from app.modules.registry.models import (
    EgrulEntry,
    EgrulStatus,
    RegistryImportStatus,
    RegistrySource,
    RegistryVersion,
)

logger = structlog.get_logger(__name__)

_ENTRY_BATCH_SIZE = 5000
_DRIFT_FIELDS = ("full_name", "short_name", "kpp", "ogrn", "legal_address", "status")


async def _upsert_batch(session: AsyncSession, batch: list[ParsedEntry], version_id: Any) -> None:
    if not batch:
        return
    rows = [
        {
            "inn": e.inn,
            "ogrn": e.ogrn,
            "kpp": e.kpp,
            "full_name": e.full_name,
            "short_name": e.short_name,
            "opf_code": e.opf_code,
            "opf_name": e.opf_name,
            "status": e.status,
            "registration_date": e.registration_date,
            "termination_date": e.termination_date,
            "region_code": e.region_code,
            "legal_address": e.legal_address,
            "address_parts": e.address_parts,
            "okved_main": e.okved_main,
            "okved_extra": e.okved_extra,
            "director_name": e.director_name,
            "director_position": e.director_position,
            "capital": e.capital,
            "is_educational": e.is_educational,
            "registry_version_id": version_id,
            "raw": e.raw,
        }
        for e in batch
    ]
    stmt = pg_insert(EgrulEntry).values(rows)
    update_cols = {col: getattr(stmt.excluded, col) for col in rows[0] if col != "inn"}
    stmt = stmt.on_conflict_do_update(index_elements=[EgrulEntry.inn], set_=update_cols)
    await session.execute(stmt)


async def _process_version(session: AsyncSession, version: RegistryVersion) -> None:
    if version.source != RegistrySource.FNS_EGRUL.value:
        # dop.md §11.3: реестр вузов Рособрнадзора помечен как «дополнительно»
        # и не имеет описанного формата выгрузки — `egrul_xml` разбирает
        # только схему ЕГРЮЛ (`СвЮЛ`). Честный отказ вместо тихого «0 строк
        # импортировано»: source в модели/API принимается, но парсера для
        # него нет, и молчаливый успех с нулём записей выглядел бы как баг,
        # а не как незавершённая (сознательно отложенная) функциональность.
        version.status = RegistryImportStatus.FAILED.value
        version.error = (
            f"Импорт источника {version.source!r} не реализован: разбирается "
            "только выгрузка ЕГРЮЛ ФНС (fns_egrul)"
        )
        return

    file = await session.get(File, version.file_id)
    if file is None:
        version.status = RegistryImportStatus.FAILED.value
        version.error = "Файл выгрузки не найден"
        return

    try:
        content = await download_object_bytes(bucket=file.bucket, key=file.storage_key)
    except Exception as exc:  # noqa: BLE001 — недоступность хранилища не должна ронять воркер
        version.status = RegistryImportStatus.FAILED.value
        version.error = f"Не удалось скачать файл: {type(exc).__name__}"
        return

    version.checksum = hashlib.sha256(content).hexdigest()

    # dop.md §11.3 п.4: рекомендованный для закрытого контура вариант —
    # держать только образовательные ОКВЭД (реестр сжимается с миллионов до
    # тысяч записей). Не режим отладки, а постоянное поведение импортёра.
    total = 0
    batch: list[ParsedEntry] = []
    try:
        for entry in iter_entries(io.BytesIO(content)):
            if not entry.is_educational:
                continue
            batch.append(entry)
            if len(batch) >= _ENTRY_BATCH_SIZE:
                await _upsert_batch(session, batch, version.id)
                total += len(batch)
                await session.flush()
                batch = []
        if batch:
            await _upsert_batch(session, batch, version.id)
            total += len(batch)
    except Exception as exc:  # noqa: BLE001 — битый файл не должен ронять воркер
        version.status = RegistryImportStatus.FAILED.value
        version.error = f"Ошибка разбора: {type(exc).__name__}: {exc}"
        return

    version.entries_count = total
    version.status = RegistryImportStatus.COMPLETED.value
    version.imported_at = dt.datetime.now(dt.UTC)
    version.published_at = version.published_at or version.imported_at


async def sweep_registry_imports(ctx: dict[str, Any]) -> dict[str, int]:
    """Обрабатывает не более одной версии реестра за тик — импорт запускается
    вручную администратором и редко идёт параллельно (см. `RegistryImportService.
    start_import`, которая и так не даёт создать вторую `pending` версию)."""
    processed = 0
    async with session_scope() as session:
        version = await session.scalar(
            select(RegistryVersion)
            .where(RegistryVersion.status == RegistryImportStatus.PENDING.value)
            .order_by(RegistryVersion.created_at)
            .limit(1)
        )
        if version is not None:
            version.status = RegistryImportStatus.RUNNING.value
            await session.flush()
            await _process_version(session, version)
            await AuditService(session).record(
                AuditAction.ORG_REGISTRY_IMPORTED,
                entity_type="registry_version",
                entity_id=version.id,
                changes={
                    "status": {"old": None, "new": version.status},
                    "entries_count": {"old": None, "new": version.entries_count},
                },
            )
            processed = 1

    background_tasks_total.labels(task="sweep_registry_imports", result="success").inc()
    if processed:
        logger.info("registry_import_processed")
    return {"processed": processed}


# =============================================================================
# Сверка дрейфа реквизитов (dop.md §11.7)
# =============================================================================


async def _notify_drift(
    session: AsyncSession, organization: Organization, drift: dict[str, Any]
) -> None:
    if organization.owner_id is None:
        return
    await get_notification_service().notify_user(
        session,
        recipient_id=organization.owner_id,
        template_code="ORG_REQUISITES_DRIFT_DETECTED",
        priority=NotificationPriority.NORMAL,
        entity_type="organization",
        entity_id=organization.id,
        payload={"fields": list(drift.keys())},
    )


async def _handle_liquidation(session: AsyncSession, organization: Organization) -> None:
    """dop.md §11.7: смена статуса на ликвидируется/ликвидирована при
    активной сделке — критичное событие. В системе нет отдельной роли
    «юрист» (роли фиксированы `identity.models.Role`), поэтому задача на
    правовую проверку заводится на владельца каждой активной сделки этой
    организации — тот, кто реально видит сделку и может её остановить,
    осознанный выбор вместо несуществующей очереди юристов."""
    deals = list(
        (
            await session.execute(
                select(Deal).where(
                    Deal.organization_id == organization.id,
                    Deal.deleted_at.is_(None),
                    Deal.closed_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if not deals:
        return

    for deal in deals:
        title = (
            f"Юридическая проверка: {organization.name} — статус «{organization.registry_status}»"
        )
        session.add(
            DealTask(
                deal_id=deal.id,
                title=title,
                description=(
                    "Реестр ЕГРЮЛ показывает изменение статуса организации на "
                    f"«{organization.registry_status}». Подписание документов с "
                    "юрлицом в процессе ликвидации — юридический риск, требуется проверка "
                    "перед дальнейшими действиями по сделке."
                ),
                assignee_id=deal.owner_id,
                created_by=None,
                due_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=2),
                priority="critical",
            )
        )
        await get_notification_service().notify_user(
            session,
            recipient_id=deal.owner_id,
            template_code="ORG_LIQUIDATION_DETECTED",
            priority=NotificationPriority.HIGH,
            entity_type="deal",
            entity_id=deal.id,
        )

    await AuditService(session).record(
        AuditAction.ORG_LIQUIDATION_DETECTED,
        entity_type="organization",
        entity_id=organization.id,
        changes={"registry_status": {"old": None, "new": organization.registry_status}},
    )


def _compute_drift(organization: Organization, entry: EgrulEntry) -> dict[str, dict[str, Any]]:
    candidates = {
        "name": entry.full_name,
        "short_name": entry.short_name,
        "kpp": entry.kpp,
        "ogrn": entry.ogrn,
        "legal_address": entry.legal_address,
    }
    drift: dict[str, dict[str, Any]] = {}
    for field, new_value in candidates.items():
        if field in organization.manual_overrides:
            continue
        if not new_value:
            continue
        old_value = getattr(organization, field, None)
        if old_value != new_value:
            drift[field] = {"old": old_value, "new": new_value}
    if organization.registry_status != entry.status:
        drift["registry_status"] = {"old": organization.registry_status, "new": entry.status}
    return drift


async def sweep_registry_drift(ctx: dict[str, Any]) -> dict[str, int]:
    """Раз в сутки (dop.md §11.7 говорит «раз в 30 дней или при обновлении
    реестра» — здесь дневной cron с проверкой `registry_checked_at`, тот же
    приём, что и у `password_change_lock_seconds`: расписание чаще, чем
    минимально нужно, дёшево, а недельный/месячный cron сложнее тестировать
    и восстанавливать после простоя воркера)."""
    settings = get_settings()
    threshold = dt.datetime.now(dt.UTC) - dt.timedelta(days=settings.registry_drift_interval_days)
    checked = drifted = liquidations = 0

    async with session_scope() as session:
        organizations = list(
            (
                await session.execute(
                    select(Organization).where(
                        Organization.inn.is_not(None),
                        Organization.deleted_at.is_(None),
                        (Organization.registry_checked_at.is_(None))
                        | (Organization.registry_checked_at < threshold),
                    )
                )
            )
            .scalars()
            .all()
        )
        for organization in organizations:
            entry = await session.get(EgrulEntry, organization.inn)
            organization.registry_checked_at = dt.datetime.now(dt.UTC)
            checked += 1
            if entry is None:
                continue

            drift = _compute_drift(organization, entry)
            was_liquidating = organization.registry_status in (
                EgrulStatus.LIQUIDATING.value,
                EgrulStatus.LIQUIDATED.value,
            )
            if drift:
                old_drift = organization.requisites_drift
                merged_drift = {**(organization.requisites_drift or {}), **drift}
                organization.requisites_drift = merged_drift
                if "registry_status" in drift:
                    organization.registry_status = entry.status
                organization.version += 1
                await session.flush()
                await AuditService(session).record(
                    AuditAction.ORGANIZATION_UPDATED,
                    entity_type="organization",
                    entity_id=organization.id,
                    changes={"requisites_drift": {"old": old_drift, "new": merged_drift}},
                )
                await _notify_drift(session, organization, drift)
                drifted += 1

                now_liquidating = entry.status in (
                    EgrulStatus.LIQUIDATING.value,
                    EgrulStatus.LIQUIDATED.value,
                )
                if now_liquidating and not was_liquidating:
                    await _handle_liquidation(session, organization)
                    liquidations += 1

    background_tasks_total.labels(task="sweep_registry_drift", result="success").inc()
    if checked:
        logger.info(
            "registry_drift_swept", checked=checked, drifted=drifted, liquidations=liquidations
        )
    return {"checked": checked, "drifted": drifted, "liquidations": liquidations}
