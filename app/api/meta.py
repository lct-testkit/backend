"""Справочник кодов ошибок (B9-4, НФР3): `GET /api/meta/error-codes`.

В `openapi.json` до этой задачи были упомянуты лишь коды, попавшие в примеры ответов на
конкретных ручках (25 из 51) — остальные клиент узнавал только перебором. Справочник строится из
`core.errors.ERROR_CATALOG` на каждый запрос, а не хранится отдельным текстом: разойтись с кодом
он не может физически.

Без аутентификации — тот же приём, что `/health/*`: справочник статичен и не содержит данных
пользователей, организаций или сделок, поэтому закрывать его правом смысла не имеет.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.core.errors import ERROR_CATALOG, ErrorCode

router = APIRouter(prefix="/meta", tags=["meta"])


class ErrorCodeOut(BaseModel):
    code: str = Field(description="Код из каталога, `CRM-XXYY`.")
    name: str = Field(description="Имя значения `ErrorCode`, например `VERSION_CONFLICT`.")
    http_status: int = Field(description="HTTP-статус, с которым код обычно отдаётся.")
    title: str = Field(description="Короткое название ошибки.")
    description: str = Field(description="Что означает код и когда он возникает.")
    hint: str = Field(description="Что сделать клиенту, чтобы продолжить.")


class ErrorCodeListResponse(BaseModel):
    items: list[ErrorCodeOut]


@router.get(
    "/error-codes",
    summary="Справочник кодов ошибок",
    description=(
        "Все коды `CRM-XXYY` из каталога ошибок API: статус, заголовок, описание и подсказка "
        "«что сделать». Строится из `core.errors.ERROR_CATALOG` на каждый запрос — отставание "
        "от кода исключено. Роль: доступно без аутентификации (справочник не несёт данных "
        "пользователей)."
    ),
    response_model=ErrorCodeListResponse,
)
async def list_error_codes() -> ErrorCodeListResponse:
    items = [
        ErrorCodeOut(
            code=code.value,
            name=code.name,
            http_status=spec.status,
            title=spec.title,
            description=spec.description,
            hint=spec.hint,
        )
        for code, spec in ERROR_CATALOG.items()
    ]
    # Порядок стабилен и человекочитаем (как объявлены в ErrorCode), а не порядок dict — так
    # справочник воспроизводим между запусками и его проще сверять глазами с документацией.
    order = {code: index for index, code in enumerate(ErrorCode)}
    items.sort(key=lambda item: order[ErrorCode(item.code)])
    return ErrorCodeListResponse(items=items)
