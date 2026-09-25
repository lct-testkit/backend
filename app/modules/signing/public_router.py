"""Публичные ручки подписания и проверки (dop.md §10.4 фаза 3-4, §10.5, §10.10).

Без аутентификации: подписант-контрагент не заводит учётку ради одной
подписи (dop.md §10.4 п.6). Каждый запрос ограничен по частоте на IP
(`PUBLIC_SIGN_RATE_LIMIT_PER_MIN`) — dop.md §10.11 предлагает делать это на
Caddy, но стандартная сборка Caddy без стороннего плагина (`caddy-ratelimit`,
не подключён в `deploy/Caddyfile`) rate-limit не умеет; проверка на уровне
приложения — тем же механизмом (`app.core.rate_limit`), которым уже
защищены `/api/org-lookup/suggest` и форма смены пароля — реальный
enforcement в этом развёртывании, а не бумажное требование.

Токен не хранится в открытом виде нигде, включая логи: маршруты принимают
его только как часть пути и сразу хэшируют (`SignatureRequestService.
get_by_token`). Коммит — на `DbSession`/`get_db_session` (раздел 1: одна
транзакция на запрос, фиксируется зависимостью, не сервисом), как везде в
проекте — свой `session.commit()` здесь не нужен.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request

from app.core.config import get_settings
from app.core.deps import DbSession
from app.core.rate_limit import enforce as rate_limit_enforce
from app.modules.signing.schemas import (
    ChallengeResponse,
    RejectRequest,
    SignatureOut,
    SignatureRequestOut,
    SigningPageOut,
    SignRequest,
    VerifyResult,
)
from app.modules.signing.service import SignatureRequestService, VerifyService

public_signing_router = APIRouter(prefix="/sign", tags=["signing-public"])
public_verify_router = APIRouter(prefix="/verify", tags=["signing-public"])


async def _rate_limited(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    settings = get_settings()
    await rate_limit_enforce(
        ip,
        "public:sign",
        limit=settings.public_sign_rate_limit_per_min,
        window_seconds=60,
        detail="Слишком много запросов к странице подписания, повторите позже",
    )


async def _rate_limited_verify(request: Request) -> None:
    """`/public/verify/{signature_id}` не входит в `public_signing_router`

    (свой `APIRouter`, `public_verify_router`), поэтому оставался вне
    `_rate_limited` выше и без ограничения вовсе — единственный из пяти
    публичных маршрутов без сессионной аутентификации. Раскрывает ФИО и дату
    подписания по значению из пути: без лимита это переборный оракул по
    `signature_id` на полной скорости клиента. Отдельный бакет ключа (не
    `public:sign`) — чтение статуса подписи и сам процесс подписания не
    должны исчерпывать один и тот же счётчик друг у друга.
    """
    ip = request.client.host if request.client else "unknown"
    settings = get_settings()
    await rate_limit_enforce(
        ip,
        "public:verify",
        limit=settings.public_sign_rate_limit_per_min,
        window_seconds=60,
        detail="Слишком много запросов проверки подписи, повторите позже",
    )


@public_signing_router.get(
    "/{token}",
    summary="Страница подписания (внешний подписант)",
    description="Открытие страницы фиксирует ознакомление (viewed_at, IP, User-Agent).",
    response_model=SigningPageOut,
    dependencies=[Depends(_rate_limited)],
)
async def get_sign_page(
    request: Request,
    session: DbSession,
    token: Annotated[str, Path()],
) -> SigningPageOut:
    service = SignatureRequestService(session)
    signature_request = await service.get_by_token(token)
    client_ip = request.client.host if request.client else None
    user_agent = request.headers.get("User-Agent")
    return await service.build_signing_page(
        signature_request, mark_viewed=True, ip=client_ip, user_agent=user_agent
    )


@public_signing_router.post(
    "/{token}/challenge",
    summary="Запросить одноразовый код (внешний подписант)",
    response_model=ChallengeResponse,
    dependencies=[Depends(_rate_limited)],
)
async def public_challenge(
    request: Request,
    session: DbSession,
    token: Annotated[str, Path()],
) -> ChallengeResponse:
    service = SignatureRequestService(session)
    signature_request = await service.get_by_token(token)
    client_ip = request.client.host if request.client else None
    user_agent = request.headers.get("User-Agent")
    _otp, channel, masked, debug_code = await service.challenge(
        signature_request, ip=client_ip, user_agent=user_agent
    )
    return ChallengeResponse(
        channel=channel,
        sent_to_masked=masked,
        expires_in_seconds=get_settings().signature_otp_ttl_seconds,
        debug_code=debug_code,
    )


@public_signing_router.post(
    "/{token}/sign",
    summary="Подписать кодом подтверждения (внешний подписант)",
    response_model=SignatureOut,
    dependencies=[Depends(_rate_limited)],
)
async def public_sign(
    payload: SignRequest,
    request: Request,
    session: DbSession,
    token: Annotated[str, Path()],
) -> SignatureOut:
    service = SignatureRequestService(session)
    signature_request = await service.get_by_token(token)
    client_ip = request.client.host if request.client else None
    user_agent = request.headers.get("User-Agent")
    signature = await service.sign(
        signature_request, otp_code=payload.otp, ip=client_ip, user_agent=user_agent
    )
    return SignatureOut.model_validate(signature)


@public_signing_router.post(
    "/{token}/reject",
    summary="Отклонить документ (внешний подписант)",
    response_model=SignatureRequestOut,
    dependencies=[Depends(_rate_limited)],
)
async def public_reject(
    payload: RejectRequest,
    request: Request,
    session: DbSession,
    token: Annotated[str, Path()],
) -> SignatureRequestOut:
    service = SignatureRequestService(session)
    signature_request = await service.get_by_token(token)
    client_ip = request.client.host if request.client else None
    user_agent = request.headers.get("User-Agent")
    signature_request = await service.reject(
        signature_request, reason=payload.reason, ip=client_ip, user_agent=user_agent
    )
    return SignatureRequestOut.model_validate(signature_request)


@public_verify_router.get(
    "/{signature_id}",
    summary="Проверить подпись публично",
    description=(
        "Раскрывает только факт подписи, ФИО/снимок подписанта, дату, хэш документа и "
        "статус (действительна/отозвана/оспорена) — dop.md §10.5."
    ),
    response_model=VerifyResult,
    dependencies=[Depends(_rate_limited_verify)],
)
async def public_verify(
    session: DbSession,
    signature_id: Annotated[uuid.UUID, Path()],
) -> VerifyResult:
    result = await VerifyService(session).verify_by_id(signature_id)
    return VerifyResult(**result)
