"""Входящие вебхуки внешних систем (new_spec §4.14, раздел 8).

Без аутентификации сессией — вызывающая сторона не пользователь браузера
(CMS/LMS/Bitrix не имеют учётки Keycloak). Аутентификация — HMAC-подпись
тела (раздел 4.14: «с HMAC-SHA256 подписью в заголовке»). Смонтирован по
буквальному пути раздела 8 (`/api/v1/integrations/...`) — отдельно от
`/api` (BFF-сессия) и `/public` (токен-в-пути подписания), см. `app/main.py`.

Рейт-лимит — тот же `app.core.rate_limit`, что уже защищает `/public/sign/*`
и `/api/org-lookup/suggest` (раздел 4.14 явно требует его только для ПЭП,
но оставлять единственный неаутентифицированный сессией и при этом
бизнес-значимый путь без него — небрежность, которую предыдущие спринты
себе не позволяли).
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request
from sqlalchemy import select

from app.core.config import get_settings
from app.core.deps import DbSession
from app.core.errors import AppError, ErrorCode
from app.core.rate_limit import enforce as rate_limit_enforce
from app.modules.integration.bitrix import apply_inbound_change
from app.modules.integration.cms import CmsLeadService
from app.modules.integration.lms import upsert_progress
from app.modules.integration.models import InboundMessage, InboundStatus, IntegrationSourceCode
from app.modules.integration.schemas import BitrixWebhookRequest, LmsProgressPushRequest
from app.modules.integration.security import (
    record_signature_failure,
    resolve_secret,
    verify_signature,
)
from app.modules.integration.service import IntegrationSourceService

integrations_public_router = APIRouter(prefix="/v1/integrations", tags=["integrations-public"])


async def _rate_limited(request: Request, route: str) -> None:
    ip = request.client.host if request.client else "unknown"
    await rate_limit_enforce(
        ip,
        f"integrations:{route}",
        limit=get_settings().integration_webhook_rate_limit_per_min,
        window_seconds=60,
        detail="Слишком много запросов к вебхуку интеграции, повторите позже",
    )


async def _active_source(session: DbSession, code: str) -> Any:
    source = await IntegrationSourceService(session).get_by_code(code)
    if not source.is_active:
        raise AppError(
            ErrorCode.INTEGRATION_SOURCE_INACTIVE, f"Источник {code!r} отключён", status=503
        )
    return source


async def _find_existing(
    session: DbSession, source_code: str, external_id: str
) -> InboundMessage | None:
    return (
        await session.execute(
            select(InboundMessage).where(
                InboundMessage.source_code == source_code,
                InboundMessage.external_id == external_id,
            )
        )
    ).scalar_one_or_none()


@integrations_public_router.post("/cms/leads", summary="Приём лида с сайта (CMS)")
async def cms_leads(
    request: Request,
    session: DbSession,
    x_signature: Annotated[str | None, Header()] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, str]:
    await _rate_limited(request, "cms")
    if not idempotency_key:
        raise AppError(ErrorCode.VALIDATION, "Заголовок Idempotency-Key обязателен")
    raw_body = await request.body()
    try:
        payload = await request.json()
    except ValueError as exc:
        raise AppError(ErrorCode.VALIDATION, "Тело запроса — невалидный JSON") from exc

    source = await _active_source(session, IntegrationSourceCode.CMS.value)
    secret = resolve_secret(source.credentials_ref)
    message = await CmsLeadService(session).ingest(
        raw_body=raw_body,
        secret=secret,
        signature_header=x_signature,
        external_id=idempotency_key,
        payload=payload,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("User-Agent"),
    )
    return {"status": message.status, "inbound_message_id": str(message.id)}


@integrations_public_router.post("/lms/progress", summary="Приём прогресса от LMS (push)")
async def lms_progress_push(
    request: Request,
    session: DbSession,
    x_signature: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    await _rate_limited(request, "lms")
    raw_body = await request.body()
    try:
        payload = LmsProgressPushRequest.model_validate_json(raw_body)
    except ValueError as exc:
        raise AppError(ErrorCode.VALIDATION, "Тело запроса не соответствует схеме") from exc

    existing = await _find_existing(session, IntegrationSourceCode.LMS.value, payload.external_id)
    if existing is not None:
        return {"status": existing.status, "inbound_message_id": str(existing.id)}

    source = await _active_source(session, IntegrationSourceCode.LMS.value)
    secret = resolve_secret(source.credentials_ref)
    signature_valid = verify_signature(secret, raw_body, x_signature)
    message = InboundMessage(
        source_code=IntegrationSourceCode.LMS.value,
        external_id=payload.external_id,
        message_type="progress_push",
        raw_payload=payload.model_dump(),
        signature_valid=signature_valid,
        status=InboundStatus.RECEIVED.value,
    )
    session.add(message)
    await session.flush()
    if not signature_valid:
        message.status = InboundStatus.FAILED.value
        message.error = "invalid_signature"
        await record_signature_failure(
            session,
            source_code=IntegrationSourceCode.LMS.value,
            external_id=payload.external_id,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("User-Agent"),
        )
        # См. `integration.cms.CmsLeadService.ingest` — без явного commit
        # здесь `get_db_session` откатил бы этот InboundMessage/SecurityEvent
        # вместе с 401-ответом, и попытка подобрать подпись не оставила бы следов.
        await session.commit()
        raise AppError(
            ErrorCode.INTEGRATION_BAD_SIGNATURE, "Подпись вебхука недействительна", status=401
        )

    applied = 0
    for row in payload.items:
        result = await upsert_progress(session, row)
        if result is not None:
            applied += 1
    message.status = InboundStatus.PROCESSED.value
    message.processed_at = dt.datetime.now(dt.UTC)
    await session.flush()
    return {"status": message.status, "applied": applied}


@integrations_public_router.post("/bitrix/webhook", summary="Приём изменений из Bitrix24")
async def bitrix_webhook(
    request: Request,
    session: DbSession,
    x_signature: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    await _rate_limited(request, "bitrix")
    raw_body = await request.body()
    try:
        payload = BitrixWebhookRequest.model_validate_json(raw_body)
    except ValueError as exc:
        raise AppError(ErrorCode.VALIDATION, "Тело запроса не соответствует схеме") from exc

    existing = await _find_existing(
        session, IntegrationSourceCode.BITRIX24.value, payload.external_id
    )
    if existing is not None:
        return {"status": existing.status, "inbound_message_id": str(existing.id)}

    source = await _active_source(session, IntegrationSourceCode.BITRIX24.value)
    secret = resolve_secret(source.credentials_ref)
    signature_valid = verify_signature(secret, raw_body, x_signature)
    message = InboundMessage(
        source_code=IntegrationSourceCode.BITRIX24.value,
        external_id=payload.external_id,
        message_type="deal_webhook",
        raw_payload=payload.model_dump(),
        signature_valid=signature_valid,
        status=InboundStatus.RECEIVED.value,
    )
    session.add(message)
    await session.flush()
    if not signature_valid:
        message.status = InboundStatus.FAILED.value
        message.error = "invalid_signature"
        await record_signature_failure(
            session,
            source_code=IntegrationSourceCode.BITRIX24.value,
            external_id=payload.external_id,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("User-Agent"),
        )
        # См. `integration.cms.CmsLeadService.ingest` — без явного commit
        # здесь `get_db_session` откатил бы этот InboundMessage/SecurityEvent
        # вместе с 401-ответом.
        await session.commit()
        raise AppError(
            ErrorCode.INTEGRATION_BAD_SIGNATURE, "Подпись вебхука недействительна", status=401
        )

    deal, applied = await apply_inbound_change(
        session,
        bitrix_id=payload.bitrix_id,
        remote_version=payload.version,
        fields=payload.fields,
    )
    # `duplicate` здесь означает «версия не новее уже сохранённой» — обновление
    # получено, но last-write-wins (раздел 4.14) его не применило, это не
    # обязательно тот же самый `external_id` дважды (см. докстринг `bitrix.py`).
    message.status = InboundStatus.PROCESSED.value if applied else InboundStatus.DUPLICATE.value
    message.resulting_entity_type = "deal" if deal else None
    message.resulting_entity_id = deal.id if deal else None
    message.processed_at = dt.datetime.now(dt.UTC)
    await session.flush()
    return {"status": message.status, "applied": applied}
