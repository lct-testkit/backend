from __future__ import annotations

import uuid

from pydantic import BaseModel


class SearchDealOut(BaseModel):
    id: uuid.UUID
    number: str
    title: str


class SearchOrganizationOut(BaseModel):
    id: uuid.UUID
    name: str
    short_name: str | None = None
    inn: str | None = None


class SearchContactOut(BaseModel):
    id: uuid.UUID
    last_name: str
    first_name: str
    position: str | None = None


class SearchResponse(BaseModel):
    """Результат общего поиска. `corrected_query` пуст, если ищется ровно то, что набрано;
    иначе по набранному строке не нашлось, и показаны результаты для строки в другой раскладке."""

    query: str
    corrected_query: str | None = None
    deals: list[SearchDealOut]
    organizations: list[SearchOrganizationOut]
    contacts: list[SearchContactOut]
