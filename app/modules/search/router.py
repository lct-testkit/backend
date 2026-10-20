"""Q-1: `GET /api/search` — общий поиск из шапки (сделки, организации, контакты)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from app.core.deps import ConsentedUser, DbSession
from app.core.errors import ForbiddenError
from app.core.permissions import Permission, has_permission
from app.modules.search.schemas import (
    SearchContactOut,
    SearchDealOut,
    SearchOrganizationOut,
    SearchResponse,
)
from app.modules.search.service import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    MAX_QUERY_LENGTH,
    MIN_QUERY_LENGTH,
    SearchService,
)

router = APIRouter(prefix="/search", tags=["search"])

_SEARCHABLE = (Permission.DEAL_READ, Permission.ORG_READ, Permission.CONTACT_READ)


@router.get(
    "",
    summary="Общий поиск",
    description=(
        "Сделки (название, номер, вуз), организации (название, ИНН) и контакты (ФИО) одним "
        "запросом; в каждой группе не больше `limit` записей, видимость — как в списках. "
        "Группа, на которую у роли нет права чтения, пуста. Если по набранной строке ничего не "
        "нашлось ни в одной группе, поиск повторяется со строкой в другой раскладке "
        "(`gge` → «ппу», «ппу» → `gge`): тогда `corrected_query` — та строка, по которой показаны "
        "результаты, иначе он `null`. Роль: любая с правом чтения сделок, организаций или "
        "контактов."
    ),
    response_model=SearchResponse,
)
async def search(
    session: DbSession,
    principal: ConsentedUser,
    q: Annotated[str, Query(min_length=MIN_QUERY_LENGTH, max_length=MAX_QUERY_LENGTH)],
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> SearchResponse:
    if not any(has_permission(principal.role, p) for p in _SEARCHABLE):
        raise ForbiddenError("Поиск недоступен для вашей роли")
    text = q.strip()
    if len(text) < MIN_QUERY_LENGTH:
        return SearchResponse(query=text, deals=[], organizations=[], contacts=[])
    found = await SearchService(session).search(principal, text, limit=limit)
    return SearchResponse(
        query=found.query,
        corrected_query=found.corrected_query,
        deals=[SearchDealOut(id=d.id, number=d.number, title=d.title) for d in found.deals],
        organizations=[
            SearchOrganizationOut(id=o.id, name=o.name, short_name=o.short_name, inn=o.inn)
            for o in found.organizations
        ],
        contacts=[
            SearchContactOut(
                id=c.id, last_name=c.last_name, first_name=c.first_name, position=c.position
            )
            for c in found.contacts
        ],
    )
