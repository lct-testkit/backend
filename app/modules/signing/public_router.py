"""Публичные ручки подписания и проверки (dop.md §10.4 фаза 3-4, §10.5, §10.10).

Без аутентификации: подписант-контрагент не заводит учётку ради одной
подписи (dop.md §10.4 п.6). Каждый запрос ограничен по частоте — dop.md §10.11
предлагает делать это на Caddy, но стандартная сборка Caddy без стороннего
плагина (`caddy-ratelimit`, не подключён в `deploy/Caddyfile`) rate-limit не
умеет; проверка на уровне приложения — тем же механизмом
(`app.core.rate_limit`), которым уже защищены `/api/org-lookup/suggest` и форма
смены пароля — реальный enforcement в этом развёртывании, а не бумажное
требование. Лимит `PUBLIC_SIGN_RATE_LIMIT_PER_MIN` считается по токену подписи:
открытие страницы, код, неверные попытки и повторный код одного подписанта
укладываются в его квоту, а подписанты за одним NAT (офис вуза) не мешают друг
другу. Поверх него — мягкий общий лимит по IP (в `_IP_LIMIT_FACTOR` раз выше):
перебор чужих токенов каждый раз начинал бы новую квоту токена, и сдерживает его
только он.

Токен не хранится в открытом виде в БД и в ключах Redis: маршруты принимают
его только как часть пути и сразу хэшируют (`SignatureRequestService.
get_by_token`). Оговорка: в путь запроса он попадает целиком, поэтому access-лог
(`app/middleware/request_context.py`, `path=`) и лог Caddy должны писать шаблон
маршрута или маскировать сегмент — это настраивается там, а не здесь. Адрес клиента
для лимитов и доказательств подписи — из контекста запроса (`get_client()`), уже
приведённого middleware к валидному IP, а не из сырого `request.client.host`: при
`--forwarded-allow-ips '*'` он равен тому, что клиент сам написал в X-Forwarded-For.
Коммит — на `DbSession`/`get_db_session` (раздел 1: одна
транзакция на запрос, фиксируется зависимостью, не сервисом), как везде в
проекте — свой `session.commit()` здесь не нужен.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi import status as http_status

from app.core.config import get_settings
from app.core.context import get_client
from app.core.deps import DbSession
from app.core.errors import ValidationError
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
from app.modules.signing.service import (
    SignatureDocumentService,
    SignatureRequestService,
    VerifyService,
    pdf_download_headers,
    pdf_inline_headers,
)

public_signing_router = APIRouter(prefix="/sign", tags=["signing-public"])
public_verify_router = APIRouter(prefix="/verify", tags=["signing-public"])
# B7-3: ссылка «только скачать» подписанный файл/протокол — отдельный публичный роутер
# (`/public/signed/*`), не делит пространство путей/токенов с `/public/sign/*`.
public_download_router = APIRouter(prefix="/signed", tags=["signing-public"])

# B7-3: лимит multipart-загрузки для сверки файла по хэшу — тот же порядок, что у
# внутренней `POST /api/signatures/verify` (`router.py:_MAX_VERIFY_UPLOAD_BYTES`).
_MAX_PUBLIC_VERIFY_UPLOAD_BYTES = 52_428_800


# Во сколько раз общий лимит по IP мягче лимита на один токен подписи.
_IP_LIMIT_FACTOR = 10


def _client_ip(request: Request) -> str | None:
    """Валидный адрес клиента, уже нормализованный middleware (`get_client()`): в доказательство
    подписи (`signatures.ip` — `inet`) не должно попадать имя хоста или произвольная строка."""
    client = get_client()
    return client.ip if client else None


def _limit_key(request: Request) -> str:
    """Ключ лимита по IP. Без нормализованного адреса (нет прокси или он не разобрался) — сырой
    адрес соединения: лучше общий бакет, чем отсутствие лимита."""
    return _client_ip(request) or (request.client.host if request.client else "unknown")


async def _rate_limited(request: Request, token: Annotated[str, Path()]) -> None:
    ip = _limit_key(request)
    settings = get_settings()
    per_token = settings.public_sign_rate_limit_per_min
    await rate_limit_enforce(
        ip,
        "public:sign",
        limit=per_token * _IP_LIMIT_FACTOR,
        window_seconds=60,
        detail="Слишком много запросов к странице подписания, повторите позже",
        fail_closed=True,
    )
    # В ключ Redis идёт хэш, а не сам токен: токен — единственный секрет ссылки.
    await rate_limit_enforce(
        hashlib.sha256(token.encode()).hexdigest()[:32],
        "public:sign:token",
        limit=per_token,
        window_seconds=60,
        detail="Слишком много запросов по этой ссылке, повторите через минуту",
        fail_closed=True,
    )


async def _rate_limited_download(request: Request, token: Annotated[str, Path()]) -> None:
    """B7-3: тот же приём, что `_rate_limited`, но отдельный бакет — скачивание не должно
    делить счётчик с подписанием (токены разных назначений, `/public/sign/*` и
    `/public/signed/*`, не взаимозаменяемы, их лимиты тоже разведены)."""
    ip = _limit_key(request)
    settings = get_settings()
    per_token = settings.public_sign_rate_limit_per_min
    await rate_limit_enforce(
        ip,
        "public:download",
        limit=per_token * _IP_LIMIT_FACTOR,
        window_seconds=60,
        detail="Слишком много запросов на скачивание, повторите позже",
        fail_closed=True,
    )
    await rate_limit_enforce(
        hashlib.sha256(token.encode()).hexdigest()[:32],
        "public:download:token",
        limit=per_token,
        window_seconds=60,
        detail="Слишком много запросов по этой ссылке, повторите через минуту",
        fail_closed=True,
    )


async def _require_public_download_enabled() -> None:
    """Откат без деплоя (B7-3): `SIGNATURE_PUBLIC_DOWNLOAD_ENABLED=false` прячет ручки как
    несуществующий путь, не 403 — так же, как неизвестный токен."""
    if not get_settings().signature_public_download_enabled:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND)


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
    ip = _limit_key(request)
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
    client_ip = _client_ip(request)
    user_agent = request.headers.get("User-Agent")
    page = await service.build_signing_page(
        signature_request, mark_viewed=True, ip=client_ip, user_agent=user_agent
    )
    page.document.file_url = f"{get_settings().public_prefix}/sign/{token}/file"
    return page


@public_signing_router.get(
    "/{token}/file",
    summary="PDF документа для просмотра (внешний подписант)",
    description=(
        "Тот же PDF, что по `preview_url`, но с origin приложения: `inline`, без CORS и "
        "без ссылки на хранилище. По умолчанию (`variant=auto`) после подписи отдаёт файл "
        "со штампом, а не исходник (B7-2); `variant=original` — всегда исходник; "
        "`variant=protocol` — протокол подписания (только у подписанного документа). "
        "Доступ — по той же ссылке подписи, лимит — общий с ней."
    ),
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
    dependencies=[Depends(_rate_limited)],
)
async def get_sign_file(
    session: DbSession,
    token: Annotated[str, Path()],
    variant: Annotated[Literal["auto", "original", "protocol"], Query()] = "auto",
) -> Response:
    service = SignatureRequestService(session)
    signature_request = await service.get_by_token(token)
    data, filename = await service.load_document_pdf(signature_request, variant=variant)
    return Response(
        content=data, media_type="application/pdf", headers=pdf_inline_headers(filename)
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
    client_ip = _client_ip(request)
    user_agent = request.headers.get("User-Agent")
    otp, channel, masked, debug_code = await service.challenge(
        signature_request, ip=client_ip, user_agent=user_agent
    )
    return ChallengeResponse(
        channel=channel,
        sent_to_masked=masked,
        expires_in_seconds=get_settings().signature_otp_ttl_seconds,
        debug_code=debug_code,
        max_attempts=otp.max_attempts,
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
    client_ip = _client_ip(request)
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
    client_ip = _client_ip(request)
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


@public_verify_router.post(
    "/by-file",
    summary="Сверить файл по хэшу публично",
    description=(
        "multipart-файл сверяется по sha256 с сохранёнными подписями — без учётной записи "
        "(B7-3), тот же результат, что `POST /api/signatures/verify` для сотрудника."
    ),
    response_model=VerifyResult,
    dependencies=[Depends(_rate_limited_verify)],
)
async def public_verify_by_file(
    session: DbSession,
    file: Annotated[UploadFile, File()],
) -> VerifyResult:
    body = bytearray()
    while chunk := await file.read(1024 * 1024):
        body.extend(chunk)
        if len(body) > _MAX_PUBLIC_VERIFY_UPLOAD_BYTES:
            raise ValidationError("Файл слишком большой для проверки")
    result = await VerifyService(session).verify_by_file(bytes(body))
    return VerifyResult(**result)


# --- B7-3: скачивание подписанного файла/протокола по отдельной ссылке ---------------------


@public_download_router.get(
    "/{token}",
    summary="Скачать подписанный файл по ссылке",
    description=(
        "Ссылка отдельная от токена подписания: не даёт права подписывать, свой TTL "
        "(SIGNATURE_DOWNLOAD_TTL_DAYS, 30 дней по умолчанию) и лимит скачиваний. Неизвестный, "
        "просроченный и отозванный токен отвечают одинаково. Откат — "
        "SIGNATURE_PUBLIC_DOWNLOAD_ENABLED=false."
    ),
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
    dependencies=[Depends(_require_public_download_enabled), Depends(_rate_limited_download)],
)
async def get_signed_file(session: DbSession, token: Annotated[str, Path()]) -> Response:
    data, filename = await SignatureDocumentService(session).public_download(
        token, variant="signed"
    )
    return Response(
        content=data, media_type="application/pdf", headers=pdf_download_headers(filename)
    )


@public_download_router.get(
    "/{token}/protocol",
    summary="Скачать протокол подписания по ссылке",
    response_class=Response,
    responses={200: {"content": {"application/pdf": {}}}},
    dependencies=[Depends(_require_public_download_enabled), Depends(_rate_limited_download)],
)
async def get_signed_protocol(session: DbSession, token: Annotated[str, Path()]) -> Response:
    data, filename = await SignatureDocumentService(session).public_download(
        token, variant="protocol"
    )
    return Response(
        content=data, media_type="application/pdf", headers=pdf_download_headers(filename)
    )
