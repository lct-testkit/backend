"""Сервисы автоподстановки и администрирования реестра ЕГРЮЛ (раздел 6/5.11).

`OrgLookupService` — пользовательская автоподстановка (rate-limited, логирует
каждый запрос). `RegistryImportService` — создание версии реестра из
загруженного файла (раздел 6: "`POST /api/admin/registry/import`"), сам
разбор — в `registry.tasks` (фоновая задача, dop.md §11.3). `DriftService` —
периодическая сверка реквизитов организаций с локальным реестром (dop.md
§11.7).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

from sqlalchemy import Select, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import rate_limit
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode, NotFoundError, ValidationError
from app.core.masking import mask_inn
from app.core.security import Principal
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.catalog.models import Organization
from app.modules.catalog.service import organization_scope_clause
from app.modules.catalog.validators import validate_requisite
from app.modules.files.models import File, FileStatus
from app.modules.registry.models import (
    OrgLookupLog,
    RegistryImportStatus,
    RegistryVersion,
)
from app.modules.registry.providers import OrgDetails, OrgSuggestion, resolve_chain

_SUGGEST_RATE_LIMIT_ROUTE = "org_lookup:suggest"
_SUGGEST_RATE_WINDOW_SECONDS = 60


def _mask_query(query: str) -> str:
    """Раздел 5.11: `query_masked` — для ИНН-подобного ввода используем
    `mask_inn`, для текста маскируем середину, чтобы в логах не оседали
    полные названия организаций пользовательского ввода без необходимости."""
    stripped = query.strip()
    if stripped.isdigit():
        return mask_inn(stripped) or "***"
    if len(stripped) <= 4:
        return "***"
    return f"{stripped[:2]}***{stripped[-2:]}"


@dataclass(slots=True)
class SuggestResult:
    items: list[OrgSuggestion]
    provider: str


class OrgLookupService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    async def suggest(self, principal: Principal, *, query: str, limit: int) -> SuggestResult:
        settings = get_settings()
        await rate_limit.enforce(
            str(principal.user_id),
            _SUGGEST_RATE_LIMIT_ROUTE,
            limit=settings.org_lookup_rate_limit_per_min,
            window_seconds=_SUGGEST_RATE_WINDOW_SECONDS,
            detail="Слишком много запросов автоподстановки, повторите позже",
        )

        started = time.perf_counter()
        provider_name = "none"
        items: list[OrgSuggestion] = []
        for provider in await resolve_chain(self._session):
            found = await provider.suggest(query, limit)
            if found:
                items, provider_name = found, provider.name
                break
        items = await self._attach_organization_ids(principal, items)
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        self._session.add(
            OrgLookupLog(
                user_id=principal.user_id,
                query_masked=_mask_query(query),
                provider=provider_name,
                result_count=len(items),
                matched_inn=items[0].inn if len(items) == 1 else None,
                response_ms=elapsed_ms,
            )
        )
        await self._session.flush()
        return SuggestResult(items=items, provider=provider_name)

    async def _attach_organization_ids(
        self, principal: Principal, items: list[OrgSuggestion]
    ) -> list[OrgSuggestion]:
        """B4-11 (A-21): если подсказка указывает на уже заведённую у нас организацию и она в
        скоупе вызывающего (B4-5/B4-6 — ограничения сильнее выданного доступа), заполняет
        `organization_id`, чтобы фронтенд мог перейти к карточке вместо дублирующего создания.

        `internal_cache` — элемент УЖЕ наша организация (`source_id`): вне скоупа убираем сам
        элемент целиком (как `check-duplicate`, который не раскрывает чужие реквизиты) — иначе
        подсказка молча подтверждала бы существование чужого вуза. Остальные провайдеры
        (ЕГРЮЛ) несут публичные сведения — элемент остаётся, только `organization_id` не
        заполняется для организаций вне скоупа."""
        inns = {item.inn for item in items if item.inn}
        internal_ids = {
            uuid.UUID(item.source_id)
            for item in items
            if item.provider == "internal_cache" and item.source_id
        }
        conditions = []
        if inns:
            conditions.append(Organization.inn.in_(inns))
        if internal_ids:
            conditions.append(Organization.id.in_(internal_ids))
        if not conditions:
            return items

        # Одним запросом — организации всех подсказок, одним же — какие из них в скоупе
        # вызывающего: проверка скоупа по одной (`organization_in_scope`) давала запрос на
        # каждую подсказку, а автоподстановка вызывается на каждое нажатие клавиши.
        orgs = (
            (
                await self._session.execute(
                    select(Organization).where(Organization.deleted_at.is_(None), or_(*conditions))
                )
            )
            .scalars()
            .all()
        )
        scope_clause = await organization_scope_clause(self._session, principal)
        if scope_clause is None:
            visible_ids = {org.id for org in orgs}
        else:
            visible_ids = set(
                (
                    await self._session.execute(
                        select(Organization.id).where(
                            Organization.id.in_([org.id for org in orgs]), scope_clause
                        )
                    )
                )
                .scalars()
                .all()
            )
        by_id = {org.id: org for org in orgs}
        by_inn = {org.inn: org for org in orgs if org.inn}

        kept: list[OrgSuggestion] = []
        for item in items:
            if item.provider == "internal_cache" and item.source_id:
                org = by_id.get(uuid.UUID(item.source_id))
                if org is None or org.id not in visible_ids:
                    continue
                item.organization_id = org.id
            else:
                org = by_inn.get(item.inn)
                if org is not None and org.id in visible_ids:
                    item.organization_id = org.id
            kept.append(item)
        return kept

    async def get_by_inn(self, principal: Principal, inn: str) -> OrgDetails:
        check = validate_requisite("inn", inn)
        if not check.ok:
            raise ValidationError(check.reason or "Некорректный ИНН")

        started = time.perf_counter()
        details: OrgDetails | None = None
        provider_name = "none"
        for provider in await resolve_chain(self._session):
            details = await provider.get_by_inn(inn)
            if details is not None:
                provider_name = provider.name
                break
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        self._session.add(
            OrgLookupLog(
                user_id=principal.user_id,
                query_masked=mask_inn(inn) or "***",
                provider=provider_name,
                result_count=1 if details else 0,
                matched_inn=inn if details else None,
                response_ms=elapsed_ms,
            )
        )
        await self._session.flush()

        if details is None:
            raise NotFoundError("Организация в реестре ЕГРЮЛ", inn)
        return details


def validate_requisite_value(kind: str, value: str) -> tuple[bool, str | None]:
    result = validate_requisite(kind, value)
    return result.ok, result.reason


class RegistryImportService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    async def list_query(self) -> Select[tuple[RegistryVersion]]:
        return select(RegistryVersion)

    async def get_or_404(self, version_id: uuid.UUID) -> RegistryVersion:
        version = await self._session.get(RegistryVersion, version_id)
        if version is None:
            raise NotFoundError("Версия реестра", version_id)
        return version

    async def delete(self, version: RegistryVersion) -> None:
        """П4, раздел 4: «систему нельзя оставить без реестра». Нельзя
        удалить версию, которая сейчас импортируется (`running` —
        `registry.tasks._process_version` обращается к строке по id прямо
        сейчас), и нельзя удалить последнюю успешно завершённую версию:
        после удаления должна остаться хотя бы одна `completed`, иначе
        локальный реестр и автоподстановка по ИНН (`OrgLookupService`)
        останутся без данных. `EgrulEntry.registry_version_id`/
        `Organization.registry_version_id` — `ON DELETE SET NULL`, поэтому
        сам DELETE ничего не блокирует на уровне FK — проверка целиком
        бизнес-логическая, ниже."""
        if version.status == RegistryImportStatus.RUNNING.value:
            raise AppError(
                ErrorCode.ENTITY_IN_USE,
                "Версия реестра сейчас импортируется — дождитесь завершения",
            )
        if version.status == RegistryImportStatus.COMPLETED.value:
            other_completed = await self._session.scalar(
                select(RegistryVersion.id)
                .where(
                    RegistryVersion.status == RegistryImportStatus.COMPLETED.value,
                    RegistryVersion.id != version.id,
                )
                .limit(1)
            )
            if other_completed is None:
                raise AppError(
                    ErrorCode.ENTITY_IN_USE,
                    "Это последняя завершённая версия реестра — систему нельзя "
                    "оставить без реестра",
                )

        version_id, source = version.id, version.source
        await self._session.delete(version)
        await self._session.flush()
        await self._audit.record(
            AuditAction.REGISTRY_VERSION_DELETED,
            entity_type="registry_version",
            entity_id=version_id,
            changes={"source": {"old": source, "new": None}},
        )

    async def start_import(
        self, principal: Principal, *, file_id: uuid.UUID, source: str
    ) -> RegistryVersion:
        file = await self._session.get(File, file_id)
        if file is None or file.deleted_at is not None:
            raise NotFoundError("Файл", file_id)
        if file.status != FileStatus.READY.value:
            raise AppError(ErrorCode.VALIDATION, "Файл ещё не прошёл проверку")

        # Раздел 6.5/6.12: одна незавершённая версия реестра за раз — иначе
        # два параллельных импорта одной выгрузки затирали бы друг друга.
        pending = await self._session.scalar(
            select(RegistryVersion).where(
                RegistryVersion.status.in_(
                    (RegistryImportStatus.PENDING.value, RegistryImportStatus.RUNNING.value)
                )
            )
        )
        if pending is not None:
            raise AppError(
                ErrorCode.VALIDATION,
                "Уже есть незавершённый импорт реестра, дождитесь его окончания",
            )

        version = RegistryVersion(
            source=source,
            file_id=file_id,
            status=RegistryImportStatus.PENDING.value,
            imported_by=principal.user_id,
        )
        self._session.add(version)
        await self._session.flush()
        await self._audit.record(
            AuditAction.IMPORT_STARTED,
            entity_type="registry_version",
            entity_id=version.id,
            changes={
                "source": {"old": None, "new": source},
                "file_id": {"old": None, "new": str(file_id)},
            },
        )
        return version
