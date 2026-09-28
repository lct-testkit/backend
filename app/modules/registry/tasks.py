"""Фоновая обработка реестра ЕГРЮЛ: импорт выгрузки и сверка дрейфа реквизитов.

Импорт запускается тем же периодическим сканом, что и `workflow.tasks`
(см. его docstring про гонку видимости транзакции): `POST
/api/admin/registry/import` коммитит строку `registry_versions` в статусе
`pending` в конце HTTP-запроса, а эта задача подхватывает её по расписанию,
а не по прямой постановке в очередь arq из хендлера.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.metrics import track_task
from app.core.optimistic import claim_version
from app.core.storage import download_object_to_file
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.catalog.models import Organization
from app.modules.crm.models import Deal
from app.modules.crm.models import Task as DealTask
from app.modules.crm.service import drop_deal_card_after_commit
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

# 21 столбец × 1500 строк = 31 500 параметров: предел протокола asyncpg — 32 767 на запрос.
# Раньше партия была 5000 строк (105 тысяч параметров), и любая реальная выгрузка падала на
# первой же партии.
_ENTRY_BATCH_SIZE = 1500
_DRIFT_FIELDS = ("full_name", "short_name", "kpp", "ogrn", "legal_address", "status")

# Версия, застрявшая в `running` дольше этого (воркер упал посреди импорта), помечается сбойной:
# иначе она блокировала бы следующую выгрузку навсегда.
_STALE_RUNNING_AFTER = dt.timedelta(hours=12)


async def _upsert_batch(session: AsyncSession, batch: list[ParsedEntry], version_id: Any) -> int:
    """Записывает партию и возвращает число записанных строк (уникальных ИНН)."""
    if not batch:
        return 0
    # Один ИНН дважды в партии: `ON CONFLICT DO UPDATE` не может затронуть строку второй раз,
    # партия падала целиком. Побеждает последняя запись.
    unique = list({e.inn: e for e in batch}.values())
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
        for e in unique
    ]
    stmt = pg_insert(EgrulEntry).values(rows)
    update_cols = {col: getattr(stmt.excluded, col) for col in rows[0] if col != "inn"}
    stmt = stmt.on_conflict_do_update(index_elements=[EgrulEntry.inn], set_=update_cols)
    await session.execute(stmt)
    return len(rows)


@dataclass(slots=True)
class _ImportOutcome:
    status: str
    entries_count: int = 0
    checksum: str | None = None
    error: str | None = None


def _next_batch(entries: Iterator[ParsedEntry], counters: dict[str, int]) -> list[ParsedEntry]:
    """До `_ENTRY_BATCH_SIZE` образовательных записей из потока. Разбор XML — синхронный и
    тяжёлый, поэтому вызывается в потоке (`asyncio.to_thread`), а не в цикле событий воркера."""
    batch: list[ParsedEntry] = []
    for entry in entries:
        counters["parsed"] += 1
        if not entry.is_educational:
            continue
        batch.append(entry)
        if len(batch) >= _ENTRY_BATCH_SIZE:
            break
    return batch


async def _import_file(version_id: Any, file: File) -> _ImportOutcome:
    """Скачивает выгрузку потоком во временный файл (sha256 считается по ходу) и загружает записи
    партиями, каждая партия — в собственной транзакции: сбой на N-й партии не откатывает
    предыдущие, а в одной гигантской транзакции держать миллионы строк нельзя.

    Файл, в котором нет ни одной записи ЮЛ или нет образовательных организаций, — сбой, а не
    «успешный импорт нуля строк»; ошибка разбора XML тоже сбой (без режима восстановления lxml)."""
    with tempfile.TemporaryDirectory(prefix="egrul-") as tmp:
        path = os.path.join(tmp, "dump.xml")
        try:
            checksum = await download_object_to_file(
                bucket=file.bucket, key=file.storage_key, path=path
            )
        except Exception as exc:  # noqa: BLE001 — недоступность хранилища не должна ронять воркер
            return _ImportOutcome(
                RegistryImportStatus.FAILED.value,
                error=f"Не удалось скачать файл: {type(exc).__name__}",
            )

        counters = {"parsed": 0}
        total = 0
        stream = await asyncio.to_thread(open, path, "rb")
        try:
            entries = iter_entries(stream)
            while True:
                batch = await asyncio.to_thread(_next_batch, entries, counters)
                if not batch:
                    break
                async with session_scope() as session:
                    total += await _upsert_batch(session, batch, version_id)
        except Exception as exc:  # noqa: BLE001 — битый файл не должен ронять воркер
            return _ImportOutcome(
                RegistryImportStatus.FAILED.value,
                entries_count=total,
                checksum=checksum,
                error=f"Ошибка разбора: {type(exc).__name__}: {exc}",
            )
        finally:
            await asyncio.to_thread(stream.close)

        if counters["parsed"] == 0:
            return _ImportOutcome(
                RegistryImportStatus.FAILED.value,
                checksum=checksum,
                error="В файле нет ни одной записи юридического лица (СвЮЛ): проверьте формат",
            )
        if total == 0:
            return _ImportOutcome(
                RegistryImportStatus.FAILED.value,
                checksum=checksum,
                error=(
                    f"В файле {counters['parsed']} записей, но ни одна не относится к "
                    "образовательным организациям (ОКВЭД 85.*): загружать нечего"
                ),
            )
        return _ImportOutcome(
            RegistryImportStatus.COMPLETED.value, entries_count=total, checksum=checksum
        )


async def _claim_pending_version() -> tuple[Any, str, Any] | None:
    """Берёт одну версию в работу и фиксирует `running` отдельной короткой транзакцией.

    Раньше статус лишь флашился и коммитился в конце всего импорта: следующий тик (крон раз в
    минуту) видел версию ещё `pending` и запускал импорт повторно. `SKIP LOCKED` исключает и
    гонку двух воркеров."""
    async with session_scope() as session:
        version = await session.scalar(
            select(RegistryVersion)
            .where(RegistryVersion.status == RegistryImportStatus.PENDING.value)
            .order_by(RegistryVersion.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if version is None:
            return None
        version.status = RegistryImportStatus.RUNNING.value
        return version.id, version.source, version.file_id


async def _fail_stale_running_versions() -> int:
    cutoff = dt.datetime.now(dt.UTC) - _STALE_RUNNING_AFTER
    async with session_scope() as session:
        stale = (
            await session.scalars(
                select(RegistryVersion)
                .where(
                    RegistryVersion.status == RegistryImportStatus.RUNNING.value,
                    RegistryVersion.updated_at < cutoff,
                )
                .with_for_update(skip_locked=True)
            )
        ).all()
        for version in stale:
            version.status = RegistryImportStatus.FAILED.value
            version.error = "Импорт прерван: воркер остановился до завершения"
        return len(stale)


@track_task
async def sweep_registry_imports(ctx: dict[str, Any]) -> dict[str, int]:
    """Обрабатывает не более одной версии реестра за тик — импорт запускается
    вручную администратором и редко идёт параллельно (см. `RegistryImportService.
    start_import`, которая и так не даёт создать вторую `pending` версию)."""
    await _fail_stale_running_versions()
    claimed = await _claim_pending_version()
    if claimed is None:
        return {"processed": 0}
    version_id, source, file_id = claimed

    if source != RegistrySource.FNS_EGRUL.value:
        # dop.md §11.3: реестр вузов Рособрнадзора помечен как «дополнительно»
        # и не имеет описанного формата выгрузки — `egrul_xml` разбирает
        # только схему ЕГРЮЛ (`СвЮЛ`). Честный отказ вместо тихого «0 строк
        # импортировано»: source в модели/API принимается, но парсера для
        # него нет, и молчаливый успех с нулём записей выглядел бы как баг,
        # а не как незавершённая (сознательно отложенная) функциональность.
        outcome = _ImportOutcome(
            RegistryImportStatus.FAILED.value,
            error=(
                f"Импорт источника {source!r} не реализован: разбирается "
                "только выгрузка ЕГРЮЛ ФНС (fns_egrul)"
            ),
        )
    else:
        async with session_scope() as session:
            file = await session.get(File, file_id)
            if file is not None:
                session.expunge(file)
        if file is None:
            outcome = _ImportOutcome(
                RegistryImportStatus.FAILED.value, error="Файл выгрузки не найден"
            )
        else:
            outcome = await _import_file(version_id, file)

    async with session_scope() as session:
        version = await session.get(RegistryVersion, version_id)
        if version is not None:
            version.status = outcome.status
            version.error = outcome.error
            version.entries_count = outcome.entries_count
            if outcome.checksum:
                version.checksum = outcome.checksum
            if outcome.status == RegistryImportStatus.COMPLETED.value:
                version.imported_at = dt.datetime.now(dt.UTC)
                version.published_at = version.published_at or version.imported_at
            await AuditService(session).record(
                AuditAction.ORG_REGISTRY_IMPORTED,
                entity_type="registry_version",
                entity_id=version.id,
                changes={
                    "status": {"old": None, "new": version.status},
                    "entries_count": {"old": None, "new": version.entries_count},
                },
            )

    logger.info("registry_import_processed", status=outcome.status)
    return {"processed": 1}


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
        # Задача меняет счётчик открытых задач в карточке, а `version` сделки не растёт: без сброса
        # кэш карточки показывал бы старое число до конца TTL (10 минут).
        await drop_deal_card_after_commit(session, deal.id)
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


@track_task
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
        organization_ids = list(
            (
                await session.scalars(
                    select(Organization.id)
                    .where(
                        Organization.inn.is_not(None),
                        Organization.deleted_at.is_(None),
                        (Organization.registry_checked_at.is_(None))
                        | (Organization.registry_checked_at < threshold),
                    )
                    .order_by(Organization.id)
                )
            ).all()
        )

    # Каждая организация — в своей транзакции. Одна общая держала бы глобальный advisory-лок
    # цепочки аудита (его берёт первая же запись) на весь проход по тысячам организаций и
    # останавливала бы запись аудита во всей системе.
    for organization_id in organization_ids:
        async with session_scope() as session:
            organization = await session.get(Organization, organization_id)
            if organization is None or organization.deleted_at is not None:
                continue
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
            if not drift:
                continue
            old_drift = organization.requisites_drift
            merged_drift = {**(organization.requisites_drift or {}), **drift}
            if "registry_status" in drift:
                # Зеркало статуса реестра нужно, чтобы переход в ликвидацию ловился один раз.
                organization.registry_status = entry.status
            if merged_drift == (old_drift or {}):
                # Расхождение то же, что уже записано и ещё не принято: раз в интервал сверки
                # оно заново поднимало версию карточки, писало аудит и слало уведомление.
                continue
            organization.requisites_drift = merged_drift
            await claim_version(session, organization)
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

    if checked:
        logger.info(
            "registry_drift_swept", checked=checked, drifted=drifted, liquidations=liquidations
        )
    return {"checked": checked, "drifted": drifted, "liquidations": liquidations}
