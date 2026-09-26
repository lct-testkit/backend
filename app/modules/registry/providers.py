"""Провайдерная абстракция автоподстановки по ИНН (dop.md §11.2).

Цепочка резолвинга — chain of responsibility, порядок фиксирован кодом (dop.md
допускает вынос в `system_settings`, но заводить настройку ради одной,
никогда не менявшейся на практике цепочки — заранее готовиться к требованию,
которого никто не просил): локальный реестр первым (офлайн, ~5 мс), затем
уже подтверждённые организации нашей БД, затем — только если администратор
включил флаг функции `external_org_lookup` — публичный поиск ФНС
(`registry.fns`). dop.md §11.1: закрытый контур, внешние источники — «опциональный
плагин», поэтому флаг по умолчанию выключен.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.modules.catalog.models import Organization, Region
from app.modules.registry.models import EgrulEntry, UniversityRegistry

_SUGGEST_SIMILARITY_THRESHOLD = 0.2


@dataclass(slots=True)
class OrgSuggestion:
    inn: str
    name: str
    region: str | None
    status: str
    is_liquidated: bool
    provider: str
    source_id: str | None = None  # id организации в нашей БД, если InternalCache


@dataclass(slots=True)
class OrgDetails:
    inn: str
    ogrn: str | None
    kpp: str | None
    full_name: str
    short_name: str | None
    opf_name: str | None
    status: str
    legal_address: str | None
    okved_main: str | None
    director_name: str | None
    director_position: str | None
    registration_date: dt.date | None
    registry_version_id: str | None
    provider: str
    is_accredited: bool | None = None
    accreditation_until: dt.date | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class OrgLookupProvider(Protocol):
    name: str

    async def suggest(self, query: str, limit: int) -> list[OrgSuggestion]: ...

    async def get_by_inn(self, inn: str) -> OrgDetails | None: ...

    async def health(self) -> bool: ...


class LocalRegistryProvider:
    """Основной провайдер: наша копия ЕГРЮЛ (dop.md §11.3), офлайн."""

    name = "local_registry"

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def suggest(self, query: str, limit: int) -> list[OrgSuggestion]:
        query = query.strip()
        if not query:
            return []
        if query.isdigit():
            stmt = select(EgrulEntry).where(EgrulEntry.inn.like(f"{query}%")).limit(limit)
        else:
            stmt = (
                select(EgrulEntry)
                .where(
                    or_(
                        func.similarity(EgrulEntry.short_name, query)
                        > _SUGGEST_SIMILARITY_THRESHOLD,
                        EgrulEntry.search_vector.op("@@")(func.plainto_tsquery("russian", query)),
                    )
                )
                .order_by(func.similarity(EgrulEntry.short_name, query).desc())
                .limit(limit)
            )
        rows = (await self._session.execute(stmt)).scalars().all()
        region_names = await self._region_names({r.region_code for r in rows if r.region_code})
        return [
            OrgSuggestion(
                inn=row.inn,
                name=row.short_name or row.full_name,
                # dop.md §11.5 п.3: выпадающий список показывает «город», не
                # цифровой код региона — код разрешается в имя одним батч-
                # запросом (`ix_regions_code`), а не по одному на кандидата.
                region=region_names.get(row.region_code, row.region_code),
                status=row.status,
                is_liquidated=row.status in ("liquidating", "liquidated"),
                provider=self.name,
            )
            for row in rows
        ]

    async def _region_names(self, codes: set[str]) -> dict[str, str]:
        if not codes:
            return {}
        stmt = select(Region.code, Region.name).where(Region.code.in_(codes))
        rows = (await self._session.execute(stmt)).all()
        return dict(rows)

    async def get_by_inn(self, inn: str) -> OrgDetails | None:
        entry = await self._session.get(EgrulEntry, inn)
        if entry is None:
            return None
        university = await self._session.get(UniversityRegistry, inn)
        is_accredited = None
        if university is not None:
            is_accredited = (
                university.accreditation_until is not None
                and university.accreditation_until >= dt.date.today()
            )
        return OrgDetails(
            inn=entry.inn,
            ogrn=entry.ogrn,
            kpp=entry.kpp,
            full_name=entry.full_name,
            short_name=entry.short_name,
            opf_name=entry.opf_name,
            status=entry.status,
            legal_address=entry.legal_address,
            okved_main=entry.okved_main,
            director_name=entry.director_name,
            director_position=entry.director_position,
            registration_date=entry.registration_date,
            registry_version_id=(
                str(entry.registry_version_id) if entry.registry_version_id else None
            ),
            provider=self.name,
            is_accredited=is_accredited,
            accreditation_until=university.accreditation_until if university else None,
            raw=entry.raw or {},
        )

    async def health(self) -> bool:
        return True


class InternalCacheProvider:
    """Ранее подтверждённые организации нашей же БД (dop.md §11.2, п.2) —
    ловит случаи, когда карточка заведена вручную и в локальный ЕГРЮЛ не
    попала (ИП, иностранное юрлицо, организация вне отфильтрованного набора
    ОКВЭД, см. `registry.models.is_educational_okved`)."""

    name = "internal_cache"

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def suggest(self, query: str, limit: int) -> list[OrgSuggestion]:
        query = query.strip()
        if not query:
            return []
        stmt = select(Organization).where(Organization.deleted_at.is_(None))
        if query.isdigit():
            stmt = stmt.where(Organization.inn.like(f"{query}%"))
        else:
            stmt = stmt.where(
                func.similarity(Organization.name, query) > _SUGGEST_SIMILARITY_THRESHOLD
            ).order_by(func.similarity(Organization.name, query).desc())
        rows = (await self._session.execute(stmt.limit(limit))).scalars().all()
        return [
            OrgSuggestion(
                inn=row.inn or "",
                name=row.short_name or row.name,
                region=None,
                status=row.registry_status or "active",
                is_liquidated=row.registry_status in ("liquidating", "liquidated"),
                provider=self.name,
                source_id=str(row.id),
            )
            for row in rows
            if row.inn
        ]

    async def get_by_inn(self, inn: str) -> OrgDetails | None:
        row = await self._session.scalar(
            select(Organization).where(Organization.inn == inn, Organization.deleted_at.is_(None))
        )
        if row is None:
            return None
        return OrgDetails(
            inn=row.inn or inn,
            ogrn=row.ogrn,
            kpp=row.kpp,
            full_name=row.name,
            short_name=row.short_name,
            opf_name=None,
            status=row.registry_status or "active",
            legal_address=row.legal_address,
            okved_main=None,
            director_name=None,
            director_position=None,
            registration_date=None,
            registry_version_id=str(row.registry_version_id) if row.registry_version_id else None,
            provider=self.name,
            is_accredited=row.is_accredited,
            accreditation_until=row.accreditation_until,
            raw=row.registry_snapshot or {},
        )

    async def health(self) -> bool:
        return True


class MockProvider:
    """Для тестов и CI (dop.md §11.2, п.4) — без сети и без БД. Возвращает
    фиксированный набор, полезный как самый нижний уровень цепочки, когда
    ни локальный реестр, ни внутренний кэш ничего не нашли."""

    name = "mock"

    _FIXTURES: tuple[OrgDetails, ...] = (
        OrgDetails(
            inn="7707049388",
            ogrn="1027700198767",
            kpp="770701001",
            full_name='ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО "РОСТЕЛЕКОМ"',
            short_name="ПАО «Ростелеком»",
            opf_name="Публичные акционерные общества",
            status="active",
            legal_address="г. Санкт-Петербург",
            okved_main="61.10",
            director_name=None,
            director_position=None,
            registration_date=None,
            registry_version_id=None,
            provider="mock",
        ),
    )

    async def suggest(self, query: str, limit: int) -> list[OrgSuggestion]:
        query = query.strip().lower()
        if not query:
            return []
        return [
            OrgSuggestion(
                inn=item.inn,
                name=item.short_name or item.full_name,
                region=None,
                status=item.status,
                is_liquidated=False,
                provider=self.name,
            )
            for item in self._FIXTURES
            if query in item.inn or query in (item.short_name or "").lower()
        ][:limit]

    async def get_by_inn(self, inn: str) -> OrgDetails | None:
        return next((item for item in self._FIXTURES if item.inn == inn), None)

    async def health(self) -> bool:
        return True


# Код флага функции в `feature_flags`, включающего внешний источник (см. миграцию 0021).
EXTERNAL_LOOKUP_FLAG = "external_org_lookup"


async def resolve_chain(session: AsyncSession) -> list[OrgLookupProvider]:
    """Собирает цепочку в приоритетном порядке (dop.md §11.2). Внешний источник
    (публичный поиск ФНС) попадает в неё только при включённом флаге
    `external_org_lookup`; выключенный флаг — значит его в цепочке просто нет."""
    # Импорты внутри функции: `fns` сам берёт датаклассы из этого модуля, а флаги живут в admin.
    from app.modules.admin.flags import is_feature_enabled
    from app.modules.registry.fns import FnsEgrulProvider

    settings = get_settings()
    chain: list[OrgLookupProvider] = [
        LocalRegistryProvider(session),
        InternalCacheProvider(session),
    ]
    if await is_feature_enabled(session, EXTERNAL_LOOKUP_FLAG, default=False):
        chain.append(
            FnsEgrulProvider(
                base_url=settings.fns_lookup_base_url,
                timeout_seconds=settings.fns_lookup_timeout_seconds,
            )
        )
    if settings.app_profile != "prod":
        # В тестах/деве локальный реестр обычно пуст — без mock'а
        # автоподстановка никогда бы не сработала в CI. В prod ложных
        # результатов из фикстуры быть не должно ни при каких обстоятельствах.
        chain.append(MockProvider())
    return chain
