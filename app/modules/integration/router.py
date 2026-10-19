"""Административный контур интеграций (раздел 5: «Настройки интеграций» —
только ADMIN). Списки `outbox_events`/`inbound_messages`/`external_refs` —
не из раздела 8 буквально, но без них включение `bitrix_connector_enabled`
на живом стенде нечем было бы проверить, кроме прямых SQL-запросов — тот же
довод, что уже оправдал чтение `import_row_results`/`signature_documents`
через API в предыдущих спринтах.

Списки отдают массив, как и раньше, а продолжение — курсором: `cursor` в запросе и
заголовок `X-Next-Cursor` в ответе (его нет на последней странице). Так форма тела
не меняется; порядок — от новых к старым, границы периода `from` (включительно) и
`to` (исключительно) — по времени создания/получения/синхронизации записи."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Response
from sqlalchemy import DateTime, func, literal, select

from app.core.config import get_settings
from app.core.deps import DbSession, require_permission
from app.core.errors import AppError, ErrorCode
from app.core.masking import mask_email, mask_phone
from app.core.pagination import Cursor, Page, keyset_before
from app.core.permissions import Permission
from app.core.rate_limit import enforce as rate_limit_enforce
from app.core.security import Principal
from app.modules.catalog.lookup import ProductIndex
from app.modules.integration.api_keys import ApiKeyService
from app.modules.integration.check import check_source_connection
from app.modules.integration.cms_client import CmsClient
from app.modules.integration.field_mapping import TARGETS, FieldMapping, apply_mapping
from app.modules.integration.lead_payload import parse_lead_payload
from app.modules.integration.models import (
    ExternalRef,
    InboundMessage,
    OutboxEvent,
    WebhookDelivery,
)
from app.modules.integration.routing import Routing
from app.modules.integration.schemas import (
    ApiKeyCreatedOut,
    ApiKeyCreateRequest,
    ApiKeyOut,
    ExternalRefOut,
    InboundMessageOut,
    IntegrationSourceOut,
    IntegrationSourceUpdateRequest,
    IntegrationStatusOut,
    MappingGetResponse,
    MappingPreviewRequest,
    MappingPreviewResponse,
    MappingValidationErrorOut,
    OutboxEventOut,
    PullRequestBody,
    PullRequestResponse,
    RoutingGetResponse,
    SourceCheckOut,
    TargetOut,
    WebhookDeliveryOut,
    WebhookSubscriptionCreatedOut,
    WebhookSubscriptionCreateRequest,
    WebhookSubscriptionOut,
    WebhookSubscriptionUpdateRequest,
    WebhookTestResultOut,
)
from app.modules.integration.security import check_outbound_url_resolved, resolve_secret
from app.modules.integration.service import IntegrationSourceService, OutboxEventService
from app.modules.integration.webhooks import WebhookSubscriptionService

IntegrationAdminPerm = Annotated[
    Principal, Depends(require_permission(Permission.INTEGRATION_ADMIN))
]

integration_admin_router = APIRouter(prefix="/admin/integrations", tags=["integrations-admin"])

NEXT_CURSOR_HEADER = "X-Next-Cursor"
# У записи без синхронизации нет времени: в сортировке она идёт последней.
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
_PAGED: dict[int | str, dict[str, Any]] = {
    200: {
        "headers": {
            NEXT_CURSOR_HEADER: {
                "description": "Курсор следующей страницы; на последней странице заголовка нет.",
                "schema": {"type": "string"},
            }
        }
    }
}

CursorParam = Annotated[str | None, Query(description="Курсор из `X-Next-Cursor` прошлой страницы")]
FromParam = Annotated[
    dt.datetime | None, Query(alias="from", description="Не раньше (включительно)")
]
ToParam = Annotated[dt.datetime | None, Query(alias="to", description="Раньше (исключительно)")]
LimitParam = Annotated[int, Query(ge=1, le=200)]


def _page(
    response: Response, rows: list[Any], limit: int, sort_value: Callable[[Any], Any]
) -> list[Any]:
    """Отрезает лишнюю строку и кладёт курсор следующей страницы в заголовок."""
    built: Page = Page.build(rows, limit=limit, cursor_value=sort_value)
    if built.next_cursor:
        response.headers[NEXT_CURSOR_HEADER] = built.next_cursor
    return list(built.items)


@integration_admin_router.get(
    "/sources",
    summary="Список источников интеграций",
    description=(
        "Настроенные источники (Bitrix24, LMS, оплаты и т.п.): адрес, тип авторизации, "
        "признак активности, время последней синхронизации и последняя ошибка. "
        "Роль: ADMIN."
    ),
    response_model=list[IntegrationSourceOut],
)
async def list_sources(
    session: DbSession, _principal: IntegrationAdminPerm
) -> list[IntegrationSourceOut]:
    sources = await IntegrationSourceService(session).list_all()
    return [IntegrationSourceOut.model_validate(s) for s in sources]


@integration_admin_router.patch(
    "/sources/{code}",
    summary="Изменить источник интеграции",
    description=(
        "Правка настроек источника по его коду: адрес, тип авторизации, ссылка на переменную "
        "окружения с учётными данными (`credentials_ref`, не само значение секрета), "
        "активность, произвольная конфигурация. CRM-9004, если источник с таким кодом не "
        "настроен. Роль: ADMIN."
    ),
    response_model=IntegrationSourceOut,
)
async def update_source(
    session: DbSession,
    _principal: IntegrationAdminPerm,
    code: Annotated[str, Path()],
    payload: IntegrationSourceUpdateRequest,
) -> IntegrationSourceOut:
    source = await IntegrationSourceService(session).update(code, payload)
    return IntegrationSourceOut.model_validate(source)


# =============================================================================
# B5-3: настраиваемый маппинг «утверждённых полей»
# =============================================================================


@integration_admin_router.get(
    "/sources/{code}/mapping",
    summary="Текущий маппинг полей и каталог целей",
    description=(
        "B5-3: сохранённый маппинг полей источника (пустой — действует прежнее поведение приёма "
        "без маппинга) и каталог целей, на которые можно сопоставить поля заявки. Роль: "
        "администратор интеграций."
    ),
    response_model=MappingGetResponse,
)
async def get_source_mapping(
    session: DbSession, _principal: IntegrationAdminPerm, code: Annotated[str, Path()]
) -> MappingGetResponse:
    mapping = await IntegrationSourceService(session).get_mapping(code)
    targets = [
        TargetOut(code=spec.code, label=spec.label) for spec in TARGETS.get(code, {}).values()
    ]
    return MappingGetResponse(mapping=mapping, targets=targets)


@integration_admin_router.put(
    "/sources/{code}/mapping",
    summary="Сохранить маппинг полей",
    description=(
        "B5-3: администратор задаёт, какие поля источника принимаются, куда они ложатся и как "
        "преобразуются, без релиза. 422 с errors[] — неизвестная цель, лишние правила, нет цели "
        "email/phone, custom_fields.<код> без активного определения."
    ),
    response_model=FieldMapping,
)
async def put_source_mapping(
    session: DbSession,
    principal: IntegrationAdminPerm,
    code: Annotated[str, Path()],
    payload: FieldMapping,
) -> FieldMapping:
    return await IntegrationSourceService(session).set_mapping(
        code, payload, approved_by=principal.user_id
    )


@integration_admin_router.delete(
    "/sources/{code}/mapping",
    summary="Вернуть прежнее поведение (без маппинга)",
    description=(
        "B5-3: удаляет сохранённый маппинг — источник возвращается к прежнему приёму заявок, "
        "поля берутся как есть. Ответ 204. Роль: администратор интеграций."
    ),
    status_code=204,
)
async def delete_source_mapping(
    session: DbSession, _principal: IntegrationAdminPerm, code: Annotated[str, Path()]
) -> None:
    await IntegrationSourceService(session).reset_mapping(code)


@integration_admin_router.post(
    "/sources/{code}/mapping/preview",
    summary="Предпросмотр маппинга без записи в БД",
    description=(
        "B5-3: как запись источника (`record` либо первая запись удалённого источника при "
        "`from_remote`) превратится в заявку по переданному `mapping` или по сохранённому. "
        "Телефон и email в ответе маскированы, в БД ничего не пишется; ошибки маппинга — в "
        "`errors[]`. Роль: администратор интеграций."
    ),
    response_model=MappingPreviewResponse,
)
async def preview_source_mapping(
    session: DbSession,
    _principal: IntegrationAdminPerm,
    code: Annotated[str, Path()],
    payload: MappingPreviewRequest,
) -> MappingPreviewResponse:
    record = payload.record
    if payload.from_remote:
        settings = get_settings()
        base_url = settings.cms_base_url if code == "cms" else None
        if not base_url:
            raise AppError(
                ErrorCode.INTEGRATION_SOURCE_INACTIVE,
                "Адрес источника не настроен — from_remote недоступен",
            )
        base_url = await check_outbound_url_resolved(base_url, strict=settings.is_prod)
        client = CmsClient(base_url, resolve_secret(settings.cms_auth_ref))
        page = await client.list_leads(updated_since=None, cursor=None, limit=1)
        if not page.items:
            raise AppError(ErrorCode.NOT_FOUND, "Источник не вернул ни одной записи")
        record = page.items[0]
    if record is None:
        # Недостижимо: `MappingPreviewRequest` требует ровно один источник образца, а оба пути
        # выше либо присваивают `record`, либо бросают ошибку раньше.
        raise AppError(ErrorCode.VALIDATION, "Нужен либо record, либо from_remote: true")

    mapping = payload.mapping
    if mapping is None:
        mapping = await IntegrationSourceService(session).get_mapping(code)

    mapped = apply_mapping(record, mapping, code)
    errors: list[MappingValidationErrorOut] = []
    product: dict[str, Any] | None = None
    try:
        lead = parse_lead_payload(mapped.payload)
    except AppError as exc:
        errors = [
            MappingValidationErrorOut(field=error.field, reason=error.reason)
            for error in exc.errors
        ]
    else:
        for candidate in lead.course_candidates:
            found = await ProductIndex(session).get(candidate)
            if found is not None:
                product = {"code": found.code, "name": found.name}
                break

    masked_record = dict(record)
    if "phone" in masked_record:
        masked_record["phone"] = mask_phone(str(masked_record["phone"]))
    if "email" in masked_record:
        masked_record["email"] = mask_email(str(masked_record["email"]))

    return MappingPreviewResponse(
        record=masked_record,
        payload=mapped.payload,
        ignored_keys=mapped.ignored_keys,
        product=product,
        errors=errors,
        warnings=mapped.warnings,
    )


@integration_admin_router.get(
    "/sources/{code}/routing",
    summary="Текущая маршрутизация (воронка/тип сделки на источник)",
    description=(
        "B5-4: сохранённые правила выбора воронки, типа сделки и ответственного для заявок "
        "источника и маршрут по умолчанию. Роль: администратор интеграций."
    ),
    response_model=RoutingGetResponse,
)
async def get_source_routing(
    session: DbSession, _principal: IntegrationAdminPerm, code: Annotated[str, Path()]
) -> RoutingGetResponse:
    routing = await IntegrationSourceService(session).get_routing(code)
    return RoutingGetResponse(routing=routing)


@integration_admin_router.put(
    "/sources/{code}/routing",
    summary="Сохранить маршрутизацию",
    description=(
        "B5-4 (BUG-15): правила «если поле заявки равно/в списке — вот воронка, тип сделки и "
        "ответственный», плюс маршрут по умолчанию. 422 с errors[] — воронка не найдена/не "
        "опубликована/не того deal_type, статус не найден в опубликованном графе, "
        "fixed_owner_id не найден или неактивен, больше 10 правил."
    ),
    response_model=Routing,
)
async def put_source_routing(
    session: DbSession,
    _principal: IntegrationAdminPerm,
    code: Annotated[str, Path()],
    payload: Routing,
) -> Routing:
    return await IntegrationSourceService(session).set_routing(code, payload)


@integration_admin_router.delete(
    "/sources/{code}/routing",
    summary="Вернуть прежнее поведение (без маршрутизации)",
    description=(
        "B5-4: удаляет сохранённую маршрутизацию — заявки источника снова идут по прежнему "
        "маршруту. Ответ 204. Роль: администратор интеграций."
    ),
    status_code=204,
)
async def delete_source_routing(
    session: DbSession, _principal: IntegrationAdminPerm, code: Annotated[str, Path()]
) -> None:
    await IntegrationSourceService(session).reset_routing(code)


# =============================================================================
# B5-5: ручной запуск «Забрать сейчас» и статус синхронизации
# =============================================================================

_PULL_RATE_LIMIT_ROUTE = "integration_pull"
_PULL_RATE_LIMIT_PER_MIN = 6
_PULL_RATE_WINDOW_SECONDS = 60


@integration_admin_router.post(
    "/sources/{code}/pull",
    summary="Забрать сейчас",
    description=(
        "Ручной запуск забора по API вне расписания cron — следующий тик (не больше минуты, "
        "`worker/main.py`) выполнит его независимо от `pull_enabled` и интервала источника. "
        "Источник выключен или адрес не настроен — 409 (CRM-1703); запрос уже стоит в очереди "
        "— 409 (CRM-1705). Лимит 6 запросов в минуту на пользователя."
    ),
    response_model=PullRequestResponse,
    status_code=202,
)
async def request_source_pull(
    session: DbSession,
    principal: IntegrationAdminPerm,
    code: Annotated[str, Path()],
    payload: PullRequestBody,
) -> PullRequestResponse:
    await rate_limit_enforce(
        str(principal.user_id),
        _PULL_RATE_LIMIT_ROUTE,
        limit=_PULL_RATE_LIMIT_PER_MIN,
        window_seconds=_PULL_RATE_WINDOW_SECONDS,
        detail="Слишком много запросов «Забрать сейчас», повторите позже",
    )
    pull_request = await IntegrationSourceService(session).request_pull(
        code, principal, updated_since=payload.updated_since
    )
    return PullRequestResponse(
        status="queued", requested_at=dt.datetime.fromisoformat(pull_request["requested_at"])
    )


_CHECK_RATE_LIMIT_ROUTE = "integration_check"
_CHECK_RATE_LIMIT_PER_MIN = 20


@integration_admin_router.post(
    "/sources/{code}/check",
    summary="Проверить соединение с источником",
    description=(
        "Один безопасный запрос к внешней системе (чтение одной записи; у Bitrix24 — "
        "`server.time`) тем же способом, каким её опрашивают рабочие задачи. Отвечает 200, "
        "если проверка состоялась: `ok=true` — система ответила и приняла запрос, "
        "`ok=false` — причина в `message` по-русски (таймаут, нет соединения, отказ в "
        "доступе, неверный адрес), `duration_ms` — время проверки, `http_status` — ответ "
        "системы, если он был. Адрес и секрет в ответ не попадают. Ничего не меняет у "
        "источника и в журнале синхронизации. Источник выключен — 409 (CRM-1703), "
        "источника с таким кодом нет — 404 (CRM-9004). Лимит 20 проверок в минуту на "
        "пользователя. Роль: ADMIN."
    ),
    response_model=SourceCheckOut,
)
async def check_source(
    session: DbSession, principal: IntegrationAdminPerm, code: Annotated[str, Path()]
) -> SourceCheckOut:
    await rate_limit_enforce(
        str(principal.user_id),
        _CHECK_RATE_LIMIT_ROUTE,
        limit=_CHECK_RATE_LIMIT_PER_MIN,
        window_seconds=_PULL_RATE_WINDOW_SECONDS,
        detail="Слишком много проверок соединения, повторите позже",
    )
    source = await IntegrationSourceService(session).get_by_code(code)
    if not source.is_active:
        raise AppError(
            ErrorCode.INTEGRATION_SOURCE_INACTIVE,
            "Источник выключен: включите его, чтобы проверить соединение",
        )
    result = await check_source_connection(source, get_settings())
    return SourceCheckOut(
        code=source.code,
        ok=result.ok,
        message=result.message,
        duration_ms=result.duration_ms,
        http_status=result.http_status,
        checked_at=dt.datetime.now(dt.UTC),
    )


@integration_admin_router.get(
    "/sources/{code}/status",
    summary="Статус синхронизации источника",
    description=(
        "Настройки забора, курсоры, итог последнего прогона (`last_run`) и живые счётчики за "
        "последние 24 часа по журналу входящих и очереди исходящих."
    ),
    response_model=IntegrationStatusOut,
)
async def get_source_status(
    session: DbSession, _principal: IntegrationAdminPerm, code: Annotated[str, Path()]
) -> IntegrationStatusOut:
    status = await IntegrationSourceService(session).get_status(code)
    return IntegrationStatusOut(**status)


@integration_admin_router.get(
    "/outbox-events",
    summary="Исходящие события",
    description=(
        "Очередь доставки, новые первыми. Фильтры: `status`, период `from`/`to` по "
        "`created_at`. Продолжение — `cursor` из заголовка `X-Next-Cursor`."
    ),
    response_model=list[OutboxEventOut],
    responses=_PAGED,
)
async def list_outbox_events(
    session: DbSession,
    response: Response,
    _principal: IntegrationAdminPerm,
    status: Annotated[str | None, Query()] = None,
    date_from: FromParam = None,
    date_to: ToParam = None,
    limit: LimitParam = 50,
    cursor: CursorParam = None,
) -> list[OutboxEventOut]:
    stmt = select(OutboxEvent).order_by(OutboxEvent.created_at.desc(), OutboxEvent.id.desc())
    if status:
        stmt = stmt.where(OutboxEvent.status == status)
    if date_from:
        stmt = stmt.where(OutboxEvent.created_at >= date_from)
    if date_to:
        stmt = stmt.where(OutboxEvent.created_at < date_to)
    if cursor:
        stmt = stmt.where(
            keyset_before(OutboxEvent.created_at, OutboxEvent.id, Cursor.decode(cursor))
        )
    rows = list((await session.execute(stmt.limit(limit + 1))).scalars().all())
    visible = _page(response, rows, limit, lambda r: r.created_at)
    return [OutboxEventOut.model_validate(r) for r in visible]


@integration_admin_router.post(
    "/outbox-events/{event_id}/retry",
    summary="Повторить доставку события",
    description=(
        "Возвращает событие из `failed`/`dead` в очередь: статус `pending`, счётчик "
        "попыток и ошибка сброшены, доставка — на ближайшем тике воркера. Для "
        "остальных статусов — 409 (CRM-1704). Причина сбоя должна быть устранена до "
        "повтора, например включён источник. Пишется в аудит. Роль: ADMIN."
    ),
    response_model=OutboxEventOut,
)
async def retry_outbox_event(
    session: DbSession,
    _principal: IntegrationAdminPerm,
    event_id: Annotated[uuid.UUID, Path()],
) -> OutboxEventOut:
    event = await OutboxEventService(session).retry(event_id)
    return OutboxEventOut.model_validate(event)


@integration_admin_router.get(
    "/inbound-messages",
    summary="Входящие сообщения",
    description=(
        "Журнал входящих вызовов, новые первыми. Фильтры: `source_code`, период `from`/`to` "
        "по `received_at`. Продолжение — `cursor` из заголовка `X-Next-Cursor`."
    ),
    response_model=list[InboundMessageOut],
    responses=_PAGED,
)
async def list_inbound_messages(
    session: DbSession,
    response: Response,
    _principal: IntegrationAdminPerm,
    source_code: Annotated[str | None, Query()] = None,
    date_from: FromParam = None,
    date_to: ToParam = None,
    limit: LimitParam = 50,
    cursor: CursorParam = None,
) -> list[InboundMessageOut]:
    stmt = select(InboundMessage).order_by(
        InboundMessage.received_at.desc(), InboundMessage.id.desc()
    )
    if source_code:
        stmt = stmt.where(InboundMessage.source_code == source_code)
    if date_from:
        stmt = stmt.where(InboundMessage.received_at >= date_from)
    if date_to:
        stmt = stmt.where(InboundMessage.received_at < date_to)
    if cursor:
        stmt = stmt.where(
            keyset_before(InboundMessage.received_at, InboundMessage.id, Cursor.decode(cursor))
        )
    rows = list((await session.execute(stmt.limit(limit + 1))).scalars().all())
    visible = _page(response, rows, limit, lambda r: r.received_at)
    return [InboundMessageOut.model_validate(r) for r in visible]


@integration_admin_router.get(
    "/external-refs",
    summary="Связи с внешними системами",
    description=(
        "Соответствие «наша сущность — id во внешней системе», недавно синхронизированные "
        "первыми (несинхронизированные — в конце). Фильтры: `source_code`, `entity_type`, "
        "период `from`/`to` по `last_synced_at`. Продолжение — `cursor` из заголовка "
        "`X-Next-Cursor`."
    ),
    response_model=list[ExternalRefOut],
    responses=_PAGED,
)
async def list_external_refs(
    session: DbSession,
    response: Response,
    _principal: IntegrationAdminPerm,
    source_code: Annotated[str | None, Query()] = None,
    entity_type: Annotated[str | None, Query()] = None,
    date_from: FromParam = None,
    date_to: ToParam = None,
    limit: LimitParam = 50,
    cursor: CursorParam = None,
) -> list[ExternalRefOut]:
    synced = func.coalesce(ExternalRef.last_synced_at, literal(_EPOCH, DateTime(timezone=True)))
    stmt = select(ExternalRef).order_by(synced.desc(), ExternalRef.id.desc())
    if source_code:
        stmt = stmt.where(ExternalRef.source_code == source_code)
    if entity_type:
        stmt = stmt.where(ExternalRef.entity_type == entity_type)
    if date_from:
        stmt = stmt.where(ExternalRef.last_synced_at >= date_from)
    if date_to:
        stmt = stmt.where(ExternalRef.last_synced_at < date_to)
    if cursor:
        stmt = stmt.where(keyset_before(synced, ExternalRef.id, Cursor.decode(cursor)))
    rows = list((await session.execute(stmt.limit(limit + 1))).scalars().all())
    visible = _page(response, rows, limit, lambda r: r.last_synced_at or _EPOCH)
    return [ExternalRefOut.model_validate(r) for r in visible]


# =============================================================================
# B5-9: универсальные webhook-подписки на исходящие события
# =============================================================================


@integration_admin_router.get(
    "/webhooks",
    summary="Список webhook-подписок",
    description=(
        "Подписки на исходящие события (DEAL_CREATED, DEAL_STATUS_CHANGED, DOCUMENT_SIGNED, "
        "LEARNING_ENROLLMENT_SENT, LEARNING_TRANSFER_REQUESTED). Секрет не отдаётся. Роль: ADMIN."
    ),
    response_model=list[WebhookSubscriptionOut],
)
async def list_webhook_subscriptions(
    session: DbSession, _principal: IntegrationAdminPerm
) -> list[WebhookSubscriptionOut]:
    rows = await WebhookSubscriptionService(session).list_all()
    return [WebhookSubscriptionOut.model_validate(r) for r in rows]


@integration_admin_router.post(
    "/webhooks",
    summary="Создать webhook-подписку",
    description=(
        "Администратор задаёт URL, список событий и необязательные фильтры (по значениям "
        "нагрузки, например deal_type) без релиза. Секрет генерирует сервер и показывает РОВНО "
        "ОДИН РАЗ в этом ответе — сохраните его сразу. В prod без SETTINGS_ENCRYPTION_KEY — "
        "422 CRM-1706. Адрес проверяется на SSRF (как base_url источников интеграций)."
    ),
    response_model=WebhookSubscriptionCreatedOut,
    status_code=201,
)
async def create_webhook_subscription(
    payload: WebhookSubscriptionCreateRequest,
    session: DbSession,
    principal: IntegrationAdminPerm,
) -> WebhookSubscriptionCreatedOut:
    subscription, secret = await WebhookSubscriptionService(session).create(principal, payload)
    return WebhookSubscriptionCreatedOut(
        **WebhookSubscriptionOut.model_validate(subscription).model_dump(), secret=secret
    )


@integration_admin_router.post(
    "/webhooks/deliveries/{delivery_id}/retry",
    summary="Повторить доставку webhook",
    description=(
        "Возвращает доставку из failed/dead в очередь: статус pending, счётчик попыток и "
        "ошибка сброшены. Для остальных статусов — 409 CRM-1704. Роль: ADMIN."
    ),
    response_model=WebhookDeliveryOut,
)
async def retry_webhook_delivery(
    session: DbSession,
    _principal: IntegrationAdminPerm,
    delivery_id: Annotated[uuid.UUID, Path()],
) -> WebhookDeliveryOut:
    delivery = await WebhookSubscriptionService(session).retry_delivery(delivery_id)
    return WebhookDeliveryOut.model_validate(delivery)


@integration_admin_router.patch(
    "/webhooks/{subscription_id}",
    summary="Изменить webhook-подписку",
    description=(
        "Правка названия, URL, событий, фильтров или активности. Включение ранее выключенной "
        "подписки сбрасывает счётчик дохлых попыток. Роль: ADMIN."
    ),
    response_model=WebhookSubscriptionOut,
)
async def update_webhook_subscription(
    payload: WebhookSubscriptionUpdateRequest,
    session: DbSession,
    _principal: IntegrationAdminPerm,
    subscription_id: Annotated[uuid.UUID, Path()],
) -> WebhookSubscriptionOut:
    service = WebhookSubscriptionService(session)
    subscription = await service.get_or_404(subscription_id)
    subscription = await service.update(subscription, payload)
    return WebhookSubscriptionOut.model_validate(subscription)


@integration_admin_router.delete(
    "/webhooks/{subscription_id}",
    summary="Удалить webhook-подписку",
    description="Удаляет подписку и всю её очередь доставки (CASCADE). Роль: ADMIN.",
    status_code=204,
)
async def delete_webhook_subscription(
    session: DbSession,
    _principal: IntegrationAdminPerm,
    subscription_id: Annotated[uuid.UUID, Path()],
) -> None:
    service = WebhookSubscriptionService(session)
    subscription = await service.get_or_404(subscription_id)
    await service.delete(subscription)


@integration_admin_router.post(
    "/webhooks/{subscription_id}/rotate-secret",
    summary="Сменить секрет подписки",
    description=(
        "Новый секрет показывается РОВНО ОДИН РАЗ в этом ответе; прежний перестаёт "
        "действовать немедленно. Роль: ADMIN."
    ),
    response_model=WebhookSubscriptionCreatedOut,
)
async def rotate_webhook_secret(
    session: DbSession,
    _principal: IntegrationAdminPerm,
    subscription_id: Annotated[uuid.UUID, Path()],
) -> WebhookSubscriptionCreatedOut:
    service = WebhookSubscriptionService(session)
    subscription = await service.get_or_404(subscription_id)
    secret = await service.rotate_secret(subscription)
    return WebhookSubscriptionCreatedOut(
        **WebhookSubscriptionOut.model_validate(subscription).model_dump(), secret=secret
    )


@integration_admin_router.post(
    "/webhooks/{subscription_id}/test",
    summary="Тестовая отправка (PING)",
    description=(
        "Синхронно отправляет подписанное событие PING на адрес подписки, минуя очередь "
        "доставки: код ответа и время — в самом ответе. Не считается попыткой подписки. "
        "Роль: ADMIN."
    ),
    response_model=WebhookTestResultOut,
)
async def ping_webhook_subscription(
    session: DbSession,
    _principal: IntegrationAdminPerm,
    subscription_id: Annotated[uuid.UUID, Path()],
) -> WebhookTestResultOut:
    service = WebhookSubscriptionService(session)
    subscription = await service.get_or_404(subscription_id)
    result = await service.test_ping(subscription)
    return WebhookTestResultOut(**result)


@integration_admin_router.get(
    "/webhooks/{subscription_id}/deliveries",
    summary="Доставки подписки",
    description=(
        "Журнал доставок конкретной подписки, новые первыми. Фильтр `status`. Продолжение — "
        "`cursor` из заголовка X-Next-Cursor. Роль: ADMIN."
    ),
    response_model=list[WebhookDeliveryOut],
    responses=_PAGED,
)
async def list_webhook_deliveries(
    session: DbSession,
    response: Response,
    _principal: IntegrationAdminPerm,
    subscription_id: Annotated[uuid.UUID, Path()],
    status: Annotated[str | None, Query()] = None,
    limit: LimitParam = 50,
    cursor: CursorParam = None,
) -> list[WebhookDeliveryOut]:
    await WebhookSubscriptionService(session).get_or_404(subscription_id)
    stmt = (
        select(WebhookDelivery)
        .where(WebhookDelivery.subscription_id == subscription_id)
        .order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.id.desc())
    )
    if status:
        stmt = stmt.where(WebhookDelivery.status == status)
    if cursor:
        stmt = stmt.where(
            keyset_before(WebhookDelivery.created_at, WebhookDelivery.id, Cursor.decode(cursor))
        )
    rows = list((await session.execute(stmt.limit(limit + 1))).scalars().all())
    visible = _page(response, rows, limit, lambda r: r.created_at)
    return [WebhookDeliveryOut.model_validate(r) for r in visible]


# =============================================================================
# B5-10: API-ключи (X-API-Key, только роль INTEGRATION)
# =============================================================================


@integration_admin_router.get(
    "/api-keys",
    summary="Список API-ключей",
    description=(
        "Ключи сервисных учёток роли INTEGRATION для доступа по заголовку X-API-Key "
        "(без OAuth2 client_credentials). Сам ключ не отдаётся, только key_prefix. Роль: ADMIN."
    ),
    response_model=list[ApiKeyOut],
)
async def list_api_keys(session: DbSession, _principal: IntegrationAdminPerm) -> list[ApiKeyOut]:
    rows = await ApiKeyService(session).list_all()
    return [ApiKeyOut.model_validate(r) for r in rows]


@integration_admin_router.post(
    "/api-keys",
    summary="Создать API-ключ",
    description=(
        "`user_id` — активная учётка роли INTEGRATION (иначе 422). `expires_in_days` — срок "
        "жизни, по умолчанию 90 дней. Ключ показывается РОВНО ОДИН РАЗ в этом ответе — "
        "сохраните его сразу, в БД хранится только хэш. Роль: ADMIN."
    ),
    response_model=ApiKeyCreatedOut,
    status_code=201,
)
async def create_api_key(
    payload: ApiKeyCreateRequest, session: DbSession, principal: IntegrationAdminPerm
) -> ApiKeyCreatedOut:
    api_key, raw_key = await ApiKeyService(session).create(principal, payload)
    return ApiKeyCreatedOut(**ApiKeyOut.model_validate(api_key).model_dump(), key=raw_key)


@integration_admin_router.delete(
    "/api-keys/{api_key_id}",
    summary="Отозвать API-ключ",
    description="Ключ немедленно перестаёт проходить аутентификацию. Роль: ADMIN.",
    response_model=ApiKeyOut,
)
async def revoke_api_key(
    session: DbSession,
    _principal: IntegrationAdminPerm,
    api_key_id: Annotated[uuid.UUID, Path()],
) -> ApiKeyOut:
    service = ApiKeyService(session)
    api_key = await service.get_or_404(api_key_id)
    api_key = await service.revoke(api_key)
    return ApiKeyOut.model_validate(api_key)
