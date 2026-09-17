"""Схемы автоподстановки по ИНН и администрирования реестра (dop.md §11.10)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.masking import mask_name

RequisiteKind = Literal["inn", "kpp", "ogrn", "ogrnip"]
RegistrySourceLiteral = Literal["fns_egrul", "rosobrnadzor", "manual"]


class OrgSuggestionOut(BaseModel):
    inn: str
    name: str
    region: str | None = None
    status: str
    is_liquidated: bool
    provider: str


class OrgSuggestResponse(BaseModel):
    items: list[OrgSuggestionOut] = Field(default_factory=list)


class OrgDetailsOut(BaseModel):
    """`director_name` — ПДн руководителя (dop.md §11.8): маскируется до
    инициалов даже в ответе автоподстановки, не только в логах."""

    inn: str
    ogrn: str | None = None
    kpp: str | None = None
    full_name: str
    short_name: str | None = None
    opf_name: str | None = None
    status: str
    legal_address: str | None = None
    okved_main: str | None = None
    director_name: str | None = None
    director_position: str | None = None
    registration_date: dt.date | None = None
    registry_version_id: str | None = None
    provider: str
    is_accredited: bool | None = None
    accreditation_until: dt.date | None = None

    @classmethod
    def from_details(cls, details: object) -> OrgDetailsOut:
        return cls(
            inn=details.inn,  # type: ignore[attr-defined]
            ogrn=details.ogrn,  # type: ignore[attr-defined]
            kpp=details.kpp,  # type: ignore[attr-defined]
            full_name=details.full_name,  # type: ignore[attr-defined]
            short_name=details.short_name,  # type: ignore[attr-defined]
            opf_name=details.opf_name,  # type: ignore[attr-defined]
            status=details.status,  # type: ignore[attr-defined]
            legal_address=details.legal_address,  # type: ignore[attr-defined]
            okved_main=details.okved_main,  # type: ignore[attr-defined]
            director_name=mask_name(details.director_name),  # type: ignore[attr-defined]
            director_position=details.director_position,  # type: ignore[attr-defined]
            registration_date=details.registration_date,  # type: ignore[attr-defined]
            registry_version_id=details.registry_version_id,  # type: ignore[attr-defined]
            provider=details.provider,  # type: ignore[attr-defined]
            is_accredited=details.is_accredited,  # type: ignore[attr-defined]
            accreditation_until=details.accreditation_until,  # type: ignore[attr-defined]
        )


class ValidateRequisiteRequest(BaseModel):
    kind: RequisiteKind
    value: str


class ValidateRequisiteResponse(BaseModel):
    ok: bool
    reason: str | None = None


class RegistryImportRequest(BaseModel):
    file_id: uuid.UUID
    source: RegistrySourceLiteral = "fns_egrul"


class RegistryVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source: str
    file_id: uuid.UUID
    status: str
    published_at: dt.datetime | None = None
    imported_at: dt.datetime | None = None
    entries_count: int
    imported_by: uuid.UUID | None = None
    checksum: str | None = None
    error: str | None = None
    created_at: dt.datetime


class RegistryVersionListResponse(BaseModel):
    items: list[RegistryVersionOut] = Field(default_factory=list)
    next_cursor: str | None = None
