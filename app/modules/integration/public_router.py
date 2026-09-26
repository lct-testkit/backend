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
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, NoReturn

from fastapi import APIRouter, Header, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.core.context import get_client
from app.core.deps import DbSession
from app.core.errors import AppError, ErrorCode, FieldError, ValidationError
from app.core.rate_limit import enforce as rate_limit_enforce
from app.modules.integration.bitrix import apply_inbound_change
from app.modules.integration.cms import CmsLeadService
from app.modules.integration.json_safe import scrub_json
from app.modules.integration.lead_payload import raw_evidence
from app.modules.integration.lms import apply_progress_rows
from app.modules.integration.models import InboundMessage, InboundStatus, IntegrationSourceCode
from app.modules.integration.schemas import (
    BitrixWebhookRequest,
    CmsLeadResponse,
    LmsProgressPushRequest,
)
from app.modules.integration.security import (
    TIMESTAMP_HEADER,
    record_signature_failure,
    resolve_secret,
    signing_secret_ref,
    verify_signature,
)
from app.modules.integration.service import IntegrationSourceService

integrations_public_router = APIRouter(prefix="/v1/integrations", tags=["integrations-public"])

_IDEMPOTENCY_KEY_MAX = 255  # длина `inbound_messages.external_id`
# Заявка — маленький объект; крупнее тело — не заявка, а попытка забить память или журнал.
_CMS_MAX_BODY_BYTES = 256 * 1024
# Пачка прогресса LMS и изменение сделки Bitrix24 — тоже конечные объекты; читать без границы
# тело от кого угодно нельзя, подпись проверяется по телу целиком.
_PUSH_MAX_BODY_BYTES = 2 * 1024 * 1024
# Итоги, которые повтор доставки не пересчитывает (как у CMS).
_FINAL_PUSH_STATUSES = frozenset({InboundStatus.PROCESSED.value, InboundStatus.DUPLICATE.value})
_CLAIM_ATTEMPTS = 3


def _requires_timestamp(source: Any) -> bool:
    """Источник может потребовать подпись с меткой времени (`config.require_timestamp`)."""
    config = getattr(source, "config", None) or {}
    return isinstance(config, dict) and config.get("require_timestamp") is True


def _client_ip(request: Request) -> str | None:
    """Адрес клиента для журнала безопасности: уже приведённый middleware к валидному IP (колонка
    `security_events.ip` — `inet`, имя хоста от прокси ломало бы вставку: 500 вместо 401)."""
    client = get_client()
    return client.ip if client else None


async def _rate_limited(request: Request, route: str) -> None:
    # Нормализованный адрес из контекста, а не сырой `request.client.host`: тот при
    # `--forwarded-allow-ips '*'` равен значению, которое клиент сам вписал в X-Forwarded-For, и
    # лимит обходился бы сменой заголовка. Без прокси адрес не разобрался — берём адрес соединения.
    ip = _client_ip(request) or (request.client.host if request.client else "unknown")
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


def _idempotency_key(value: str | None) -> str:
    """`Idempotency-Key` попадает в `inbound_messages.external_id` (255 символов): длиннее — ошибка
    БД, то есть 500 на заголовке, который проверить проще, чем чинить."""

    def rejected(detail: str, reason: str) -> ValidationError:
        return ValidationError(detail, [FieldError(field="Idempotency-Key", reason=reason)])

    key = (value or "").strip()
    if not key:
        raise rejected("Заголовок Idempotency-Key обязателен", "обязательный заголовок")
    if len(key) > _IDEMPOTENCY_KEY_MAX:
        raise rejected(
            f"Idempotency-Key длиннее {_IDEMPOTENCY_KEY_MAX} символов",
            f"не длиннее {_IDEMPOTENCY_KEY_MAX} символов",
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in key):
        raise rejected(
            "Idempotency-Key содержит недопустимые символы", "недопустимые управляющие символы"
        )
    return key


async def _read_limited_body(request: Request, limit: int) -> bytes:
    """Тело запроса не больше `limit` байт: подпись проверяется по телу целиком, поэтому его надо
    прочитать, а читать неограниченный поток от кого угодно нельзя (вебхук открыт без сессии)."""
    too_large = AppError(
        ErrorCode.VALIDATION, f"Тело запроса больше {limit // 1024} КиБ", status=413
    )
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise too_large
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise too_large
        chunks.append(chunk)
    return b"".join(chunks)


_LEAD_BODY_DESCRIPTION = (
    "JSON-объект. Ключи английские (`first_name`, `last_name`, `middle_name`, `name`/`full_name`, "
    "`phone`, `email`, `product_name`/`product_code`/`program_code`/`course`, `comment`, "
    "`source_url`, `order_number`/`order_id`, `stream_number`, `amount`, `external_id`, "
    "`created_at`) либо русские, как в выгрузке «Данные оплат» (`Номер заявки`, `Курс`, "
    "`Фамилия`, `Имя`, `Отчество`, `Телефон`, `Email`, `Номер потока`, `Сумма`); регистр и "
    "пробелы в ключах не важны. Обязателен `email` или `phone`. Если есть номер заказа — нужны "
    "ещё курс, фамилия и имя."
)


@integrations_public_router.post(
    "/cms/leads",
    summary="Приём лида с сайта (CMS)",
    description=(
        "Вебхук сайта без сессии. Подпись — `X-Signature: sha256=<HMAC-SHA256 тела>`, повтор "
        "доставки узнаётся по `Idempotency-Key` (до 255 символов): с тем же ключом возвращается "
        "итог первой обработки. Без номера заказа создаётся лид (сделка в начальном статусе), "
        "с номером — оплаченный заказ (сделка «Оплата и договор оферты», идемпотентно по номеру). "
        "Один и тот же человек (email, либо телефон при той же фамилии) не дублируется. Отказ "
        "оставляет тело во входящих (`GET /api/admin/integrations/inbound-messages`) со статусом "
        "`failed`."
    ),
    response_model=CmsLeadResponse,
    responses={
        401: {"description": "Подпись неверна или отсутствует (CRM-1701)"},
        413: {"description": "Тело запроса больше 256 КиБ"},
        422: {
            "description": (
                "Нет `Idempotency-Key` или он длиннее 255 символов; тело не JSON-объект; нет email "
                "и телефона; некорректные поля (`errors[]`)"
            )
        },
        429: {"description": "Слишком много запросов"},
        503: {"description": "Источник `cms` выключен или не настроен"},
    },
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "description": _LEAD_BODY_DESCRIPTION,
                        "additionalProperties": True,
                        "example": {
                            "first_name": "Иван",
                            "last_name": "Иванов",
                            "phone": "+7 (900) 111-22-33",
                            "email": "ivanov@example.ru",
                            "product_name": "Python-разработчик с использованием инструментов ИИ",
                            "comment": "Позвоните после 18:00",
                        },
                    }
                }
            },
        }
    },
)
async def cms_leads(
    request: Request,
    session: DbSession,
    x_signature: Annotated[str | None, Header()] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> CmsLeadResponse:
    # Порядок: лимит → ключ → источник → (тело) → подпись → JSON → повтор по ключу → обработка.
    # Подпись раньше повтора: иначе чужой запрос с угаданным ключом занял бы его до настоящего.
    await _rate_limited(request, "cms")
    key = _idempotency_key(idempotency_key)
    source = await _active_source(session, IntegrationSourceCode.CMS.value)
    raw_body = await _read_limited_body(request, _CMS_MAX_BODY_BYTES)
    # Секрет: явно заданный у источника (админка), иначе из `CMS_WEBHOOK_SECRET_REF` — без
    # него настройка окружения не доходила до вебхука вовсе.
    secret = resolve_secret(signing_secret_ref(source, get_settings().cms_webhook_secret_ref))
    result = await CmsLeadService(session).ingest(
        raw_body=raw_body,
        secret=secret,
        signature_header=x_signature,
        idempotency_key=key,
        ip=_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
        timestamp=request.headers.get(TIMESTAMP_HEADER),
        require_timestamp=_requires_timestamp(source),
    )
    return CmsLeadResponse(
        status=result.message.status,
        inbound_message_id=result.message.id,
        deal_id=result.deal_id,
        contact_id=result.contact_id,
    )


async def _reject_push(
    session: DbSession,
    request: Request,
    raw_body: bytes,
    *,
    source_code: str,
    message_type: str,
) -> NoReturn:
    """Неверная подпись входящего пуша (LMS, Bitrix24).

    Раньше запись с ключом отправителя создавалась ДО проверки подписи: чужой запрос с
    угаданным `external_id` занимал ключ, и настоящая доставка получала «уже обработано».
    Теперь чужое тело остаётся под собственным `invalid:<uuid>` (улика для разбора), а ключ
    отправителя не занят. Запись фиксируется до ответа с ошибкой: транзакция запроса иначе
    откатила бы её вместе с 401."""
    message = InboundMessage(
        source_code=source_code,
        external_id=f"invalid:{uuid.uuid4()}",
        message_type=message_type,
        raw_payload=raw_evidence(raw_body),
        signature_valid=False,
        status=InboundStatus.FAILED.value,
        error="invalid_signature",
    )
    session.add(message)
    await session.flush()
    await record_signature_failure(
        session,
        source_code=source_code,
        external_id="unparsed",
        ip=_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
        details={"inbound_message_id": str(message.id)},
    )
    await session.commit()
    raise AppError(
        ErrorCode.INTEGRATION_BAD_SIGNATURE, "Подпись вебхука недействительна", status=401
    )


async def _authenticate_push(
    request: Request,
    session: DbSession,
    x_signature: str | None,
    *,
    source_code: str,
    route: str,
    message_type: str,
) -> tuple[bytes, Any]:
    """Общий вход пушей LMS и Bitrix24: лимит → тело → источник → ПОДПИСЬ; всё остальное (разбор
    тела, повтор по ключу) — только после неё, как у CMS."""
    await _rate_limited(request, route)
    raw_body = await _read_limited_body(request, _PUSH_MAX_BODY_BYTES)
    source = await _active_source(session, source_code)
    secret = resolve_secret(signing_secret_ref(source))
    valid = verify_signature(
        secret,
        raw_body,
        x_signature,
        timestamp=request.headers.get(TIMESTAMP_HEADER),
        require_timestamp=_requires_timestamp(source),
    )
    if not valid:
        await _reject_push(
            session, request, raw_body, source_code=source_code, message_type=message_type
        )
    return raw_body, source


async def _parse_push[T: BaseModel](
    session: DbSession, raw_body: bytes, model: type[T], *, source_code: str, message_type: str
) -> T:
    try:
        return model.model_validate_json(raw_body)
    except ValueError as exc:
        # Подпись верна, тело не по схеме: улика остаётся во входящих (под своим ключом).
        session.add(
            InboundMessage(
                source_code=source_code,
                external_id=f"malformed:{uuid.uuid4()}",
                message_type=message_type,
                raw_payload=raw_evidence(raw_body),
                signature_valid=True,
                status=InboundStatus.FAILED.value,
                error="body_does_not_match_schema",
            )
        )
        await session.commit()
        raise AppError(ErrorCode.VALIDATION, "Тело запроса не соответствует схеме") from exc


async def _claim_push_message(
    session: DbSession,
    *,
    source_code: str,
    external_id: str,
    message_type: str,
    raw_payload: dict[str, Any],
) -> tuple[InboundMessage, bool]:
    """Запись входящего под ключом отправителя. `True` во втором элементе — это повтор уже
    обработанной доставки (итог первой обработки возвращается как есть).

    Незавершённая запись (`failed`/`received`: например, оставшаяся от старой логики, где её
    создавал чужой запрос с неверной подписью) итогом не считается — это новая попытка на той
    же строке. Одновременная доставка с тем же ключом упирается в UNIQUE: проигравшая
    перечитывает победившую запись."""
    for _ in range(_CLAIM_ATTEMPTS):
        existing = await _find_existing(session, source_code, external_id)
        if existing is not None:
            if existing.status in _FINAL_PUSH_STATUSES:
                return existing, True
            existing.raw_payload = raw_payload
            existing.message_type = message_type
            existing.signature_valid = True
            existing.status = InboundStatus.RECEIVED.value
            existing.error = None
            existing.processed_at = None
            await session.flush()
            return existing, False
        message = InboundMessage(
            source_code=source_code,
            external_id=external_id,
            message_type=message_type,
            raw_payload=raw_payload,
            signature_valid=True,
            status=InboundStatus.RECEIVED.value,
        )
        try:
            async with session.begin_nested():
                session.add(message)
                await session.flush()
        except IntegrityError:
            continue
        return message, False
    raise AppError(
        ErrorCode.DEPENDENCY_UNAVAILABLE,
        "Доставка с этим идентификатором обрабатывается, повторите позже",
        headers={"Retry-After": "1"},
    )


async def _process_push[R](
    session: DbSession, message: InboundMessage, work: Callable[[], Awaitable[R]]
) -> R:
    """Обработка в SAVEPOINT: при сбое откатывается только она, тело остаётся во входящих
    со статусом `failed` и обобщённой причиной, а запись фиксируется до ответа с ошибкой
    (иначе транзакция запроса унесла бы её вместе с откатом)."""
    try:
        async with session.begin_nested():
            return await work()
    except Exception as exc:
        message.status = InboundStatus.FAILED.value
        message.error = exc.detail[:1000] if isinstance(exc, AppError) else "processing_error"
        message.processed_at = dt.datetime.now(dt.UTC)
        await session.commit()
        raise


@integrations_public_router.post("/lms/progress", summary="Приём прогресса от LMS (push)")
async def lms_progress_push(
    request: Request,
    session: DbSession,
    x_signature: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    # Порядок: лимит → тело → источник → подпись → разбор → повтор по ключу → обработка. Подпись
    # раньше повтора: иначе чужой запрос с угаданным ключом занимал бы его до настоящего.
    source_code = IntegrationSourceCode.LMS.value
    raw_body, _ = await _authenticate_push(
        request,
        session,
        x_signature,
        source_code=source_code,
        route="lms",
        message_type="progress_push",
    )
    payload = await _parse_push(
        session,
        raw_body,
        LmsProgressPushRequest,
        source_code=source_code,
        message_type="progress_push",
    )
    message, replay = await _claim_push_message(
        session,
        source_code=source_code,
        external_id=payload.external_id,
        message_type="progress_push",
        # `NaN`/NUL в теле JSONB не принимает: без чистки такая строка роняла бы запрос до
        # разбора, и «битые» строки не пропускались бы, а откатывали всё.
        raw_payload=scrub_json(payload.model_dump()),
    )
    if replay:
        return {"status": message.status, "inbound_message_id": str(message.id)}

    # Каждая строка — в своём SAVEPOINT: одна «битая» строка пропускается (и попадает в `skipped`),
    # а не откатывает остальные вместе со всей пачкой.
    batch = await _process_push(
        session, message, lambda: apply_progress_rows(session, payload.items)
    )
    message.status = InboundStatus.PROCESSED.value
    message.processed_at = dt.datetime.now(dt.UTC)
    if batch.skipped:
        message.error = (
            f"Пропущено строк: {batch.skipped} из {len(payload.items)}, из них с ошибкой: "
            f"{batch.failed}"
        )
    await session.flush()
    return {
        "status": message.status,
        "applied": batch.applied,
        "skipped": batch.skipped,
        "failed": batch.failed,
    }


@integrations_public_router.post("/bitrix/webhook", summary="Приём изменений из Bitrix24")
async def bitrix_webhook(
    request: Request,
    session: DbSession,
    x_signature: Annotated[str | None, Header()] = None,
) -> dict[str, Any]:
    source_code = IntegrationSourceCode.BITRIX24.value
    raw_body, _ = await _authenticate_push(
        request,
        session,
        x_signature,
        source_code=source_code,
        route="bitrix",
        message_type="deal_webhook",
    )
    payload = await _parse_push(
        session,
        raw_body,
        BitrixWebhookRequest,
        source_code=source_code,
        message_type="deal_webhook",
    )
    message, replay = await _claim_push_message(
        session,
        source_code=source_code,
        external_id=payload.external_id,
        message_type="deal_webhook",
        raw_payload=scrub_json(payload.model_dump()),
    )
    if replay:
        return {"status": message.status, "inbound_message_id": str(message.id)}

    deal, applied = await _process_push(
        session,
        message,
        lambda: apply_inbound_change(
            session,
            bitrix_id=payload.bitrix_id,
            remote_version=payload.version,
            fields=payload.fields,
        ),
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
