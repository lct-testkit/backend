"""Q-1: общий поиск из шапки — сделки, организации, контакты одним запросом.

Три раздельных запроса фронтенда не могли бы решить «ничего не нашлось»: пустоту всех трёх групп
знает только тот, кто смотрит на них вместе. Здесь это одно место: если по набранному строка
пуста во всех группах, поиск повторяется со строкой в другой раскладке (`gge` → «ппу»), а ответ
несёт `corrected_query`, чтобы интерфейс написал, по какому запросу показаны результаты.

Видимость — та же, что в списках: используются `list_query` сервисов (скоуп и маскирование не
дублируются), права на группу проверяются отдельно, чужая группа просто пуста.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.layout import switch_layout
from app.core.permissions import Permission, has_permission
from app.core.security import Principal
from app.modules.catalog.models import Contact, Organization
from app.modules.catalog.service import (
    ContactFilters,
    ContactService,
    OrganizationFilters,
    OrganizationService,
)
from app.modules.crm.models import Deal
from app.modules.crm.service import DealFilters, DealService

MIN_QUERY_LENGTH = 2
MAX_QUERY_LENGTH = 200
DEFAULT_LIMIT = 5
MAX_LIMIT = 20


@dataclass(slots=True)
class SearchResult:
    query: str
    corrected_query: str | None = None
    deals: list[Deal] = field(default_factory=list)
    organizations: list[Organization] = field(default_factory=list)
    contacts: list[Contact] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.deals or self.organizations or self.contacts)


class SearchService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _rows(self, stmt: Select[Any], limit: int) -> list[Any]:
        return list((await self._session.execute(stmt.limit(limit))).scalars().all())

    async def _search_once(self, principal: Principal, text: str, limit: int) -> SearchResult:
        result = SearchResult(query=text)
        if has_permission(principal.role, Permission.DEAL_READ):
            deal_stmt = await DealService(self._session).list_query(principal, DealFilters(q=text))
            result.deals = await self._rows(
                deal_stmt.order_by(Deal.created_at.desc(), Deal.id.desc()), limit
            )
        if has_permission(principal.role, Permission.ORG_READ):
            org_stmt = await OrganizationService(self._session).list_query(
                principal, OrganizationFilters(q=text)
            )
            result.organizations = await self._rows(
                org_stmt.order_by(Organization.created_at.desc(), Organization.id.desc()), limit
            )
        if has_permission(principal.role, Permission.CONTACT_READ):
            stmt = await ContactService(self._session).list_query(principal, ContactFilters(q=text))
            result.contacts = await self._rows(
                stmt.order_by(Contact.created_at.desc(), Contact.id.desc()), limit
            )
        return result

    async def search(self, principal: Principal, text: str, *, limit: int) -> SearchResult:
        found = await self._search_once(principal, text, limit)
        if not found.empty:
            return found
        switched = switch_layout(text)
        if switched is None:
            return found
        retry = await self._search_once(principal, switched, limit)
        if retry.empty:
            return found
        retry.query = text
        retry.corrected_query = switched
        return retry
