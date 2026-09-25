"""Коннектор Bitrix24 (new_spec §4.14, «козырь»).

Запросы построены по **официальной документации** Bitrix24 REST API,
проверенной в этом спринте (не «best-effort по памяти», как egrul_xml
в спринте 5 — там реальной выгрузки ФНС не было вообще, здесь публичная
документация доступна и прочитана напрямую):

* Аутентификация — входящий локальный вебхук:
  https://apidocs.bitrix24.com/local-integrations/local-webhooks.html
  Секрет — это **сам URL**: `https://{портал}/rest/{user_id}/{webhook_code}/`.
  Отдельного заголовка/параметра авторизации нет и не нужно — поэтому
  `BitrixClient` не принимает `auth_token` отдельно, весь префикс URL
  целиком резолвится из `credentials_ref` (раздел 7.8: секрет — по ссылке,
  не в БД открытым текстом; здесь секрет — это URL целиком, а не токен
  рядом с публичным адресом).
* Создание/обновление — **не** `crm.deal.add`/`crm.deal.update`
  (документация Bitrix прямо помечает их устаревшими: "the development of
  this method has been halted"), а универсальные `crm.item.add`/
  `crm.item.update` с `entityTypeId=2` (сделка):
  https://apidocs.bitrix24.com/api-reference/crm/universal/crm-item-add.html
  https://apidocs.bitrix24.com/api-reference/crm/universal/crm-item-update.html
  Запрос — `{"entityTypeId": 2, "fields": {...}}` (add) /
  `{"entityTypeId": 2, "id": <id>, "fields": {...}}` (update). Ответ —
  `{"result": {"item": {"id": ..., ...}}}`, не голый `{"result": <id>}`,
  как в старом `crm.deal.*`.

**Что сознательно не отправляется, и почему.** `stageId`/`assignedById`/
`companyId`/`contactId` в реальном Bitrix — это ID сущностей **на стороне
портала** (стадия конкретной воронки, сотрудник, компания, контакт), не
наши. Сопоставление `workflow_statuses.id ↔ stageId`,
`users.id ↔ Bitrix-сотрудник`, `organizations.id/contacts.id ↔
companyId/contactId` — отдельная таблица соответствия, которую не задаёт
ни одна из спецификаций и нельзя надёжно построить без доступа к реальному
порталу (какие стадии и в какой воронке вообще есть — это конфигурация
конкретного клиента). Раздел 4.14 описывает демонстрируемый эффект
буквально как «создаём сделку у нас — она появляется в Bitrix» — этого
достаточно с `title`/`opportunity`/`currencyId`/`sourceDescription`, без
выдуманных ID, которые на реальном портале либо будут отклонены API, либо
привяжутся не к тому.

Направление «мы → Bitrix» идёт через outbox (`integration.tasks.
sweep_outbox_events`, `target='bitrix24'`). Направление «Bitrix → мы» —
`POST /api/v1/integrations/bitrix/webhook` со **своим**, упрощённым
контрактом (`integration.schemas.BitrixWebhookRequest`), а не разбором
реального события `ONCRMDEALUPDATE`: настоящая подписка на события
(`event.bind`) требует зарегистрированного OAuth/локального приложения на
портале — не входящего вебхук-ключа — и здесь не поднимается за неимением
реального портала для регистрации. Честно задокументированный, а не
скрытый разрыв — тот же принцип, что уже применён к NTP/SMS-шлюзу в
спринте 6.

`external_refs.synced_version` — сравнение «last-write-wins» (раздел 4.14:
«журнал расхождений для ручного разбора»): входящее обновление принимается,
только если его версия строго новее уже сохранённой; более старая или
равная — фиксируется в логе и не применяется, вебхук не падает.

Симметрично и для исходящего направления: `push_deal` перед `update_deal`
сверяет `crm.item.get`'s `updatedTime` с нашим `external_refs.last_synced_at`
(`_reject_if_bitrix_moved_ahead`). У Bitrix нет счётчика версий, сравнимого
с нашим `synced_version` (проверено по официальной документации
`crm.item.get` — только `createdTime`/`updatedTime`/`movedTime`), поэтому
сравнение идёт по временной метке, а не по выдуманному полю. Без этой
проверки push вслепую перезаписывал бы правку, сделанную прямо в портале
между нашими синхронизациями, — ровно тот «конфликт», который раздел 4.14
называет явно, но который раньше не обнаруживался в эту сторону вообще.
Отдельного «журнала» под исходящее направление не заводится — конфликт
поднимается как обычная ошибка доставки и попадает в уже существующий
retry/backoff/dead-letter цикл outbox (`integration.tasks`), тот же путь
ручного разбора, что `GET /admin/integrations/outbox-events?status=dead`
уже даёт для любых недоставленных событий. Компромисс: между нашей
проверкой и последующим `update_deal` остаётся узкое TOCTOU-окно (Bitrix
не даёт compare-and-swap/ETag на `crm.item.update`) — честно принятое
ограничение best-effort last-write-wins, не скрытое.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.crm.models import Deal
from app.modules.integration.models import ExternalRef, IntegrationSourceCode, SyncDirection

logger = structlog.get_logger(__name__)

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

# https://apidocs.bitrix24.com/api-reference/crm/universal/crm-item-add.html
# Системные идентификаторы типов CRM-объектов: Lead=1, Deal=2, Contact=3,
# Company=4, Invoice=31 — здесь используется только Deal.
_DEAL_ENTITY_TYPE_ID = 2


class BitrixClient:
    """`webhook_base_url` — это **весь** секретный префикс
    `https://{портал}/rest/{user_id}/{webhook_code}` целиком (без хвостового
    `/`), резолвится из `credentials_ref`. Метод дописывает `/{method}.json`
    — суффикс `.json` из примера в официальной документации на входящие
    вебхуки."""

    def __init__(self, webhook_base_url: str) -> None:
        self._base_url = webhook_base_url.rstrip("/")

    async def _call(self, method: str, body: dict[str, Any]) -> dict[str, Any]:
        path = f"{self._base_url}/{method}.json"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(path, json=body)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # `raise_for_status()` вшивает `response.url` в текст сообщения —
            # а URL целиком и есть секрет (докстринг класса выше), поэтому
            # `str(exc)` сюда не пробрасывается, только статус ответа.
            raise RuntimeError(
                f"bitrix24 {method} http error: "
                f"{exc.response.status_code} {exc.response.reason_phrase}"
            ) from None
        except httpx.HTTPError as exc:
            # Остальные httpx-исключения (таймаут, обрыв соединения…) тоже
            # могут нести URL в тексте — не полагаемся на конкретный формат,
            # сообщаем только тип ошибки.
            raise RuntimeError(f"bitrix24 {method} request failed: {type(exc).__name__}") from None

        data = response.json()
        if "error" in data:
            raise RuntimeError(
                f"bitrix24 {method} error: {data['error']}: {data.get('error_description', '')}"
            )
        return data

    async def add_deal(self, fields: dict[str, Any]) -> dict[str, Any]:
        return await self._call(
            "crm.item.add", {"entityTypeId": _DEAL_ENTITY_TYPE_ID, "fields": fields}
        )

    async def update_deal(self, bitrix_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        return await self._call(
            "crm.item.update",
            {"entityTypeId": _DEAL_ENTITY_TYPE_ID, "id": bitrix_id, "fields": fields},
        )

    async def get_deal(self, bitrix_id: str) -> dict[str, Any]:
        """https://apidocs.bitrix24.com/api-reference/crm/universal/crm-item-get.html
        — тот же конверт ответа `{"result": {"item": {...}}}`, что и у
        add/update. Используется только для проверки конфликта перед push
        (`_reject_if_bitrix_moved_ahead`), не для обычного чтения."""
        return await self._call(
            "crm.item.get", {"entityTypeId": _DEAL_ENTITY_TYPE_ID, "id": bitrix_id}
        )


def _deal_to_bitrix_fields(deal: Deal) -> dict[str, Any]:
    """Поля — буквально из документации `crm.item.add`/`crm.item.update`
    (`title`, `opportunity`, `currencyId`, `sourceId`/`sourceDescription`),
    camelCase — не путать со старым `crm.deal.*`, где те же понятия звались
    `TITLE`/`OPPORTUNITY`/`CURRENCY_ID` заглавными. `sourceId` — код из
    встроенного справочника источников Bitrix (настраивается на портале);
    `"OTHER"` — безопасный дефолт, реальный источник уходит текстом в
    `sourceDescription`, где ограничений на значения нет."""
    return {
        "title": deal.title,
        "opportunity": str(deal.amount) if deal.amount is not None else None,
        "currencyId": deal.currency,
        "sourceId": "OTHER",
        "sourceDescription": f"CRM #{deal.number}" + (f" ({deal.source})" if deal.source else ""),
    }


def _parse_bitrix_time(raw: str | None) -> dt.datetime | None:
    if not raw:
        return None
    return dt.datetime.fromisoformat(raw)


async def _reject_if_bitrix_moved_ahead(client: BitrixClient, ref: ExternalRef) -> None:
    """Зеркало проверки в `apply_inbound_change`: там входящее обновление
    отклоняется, если его версия не новее уже сохранённой у нас; здесь —
    наоборот, исходящий push отклоняется, если сторона Bitrix успела
    измениться ПОСЛЕ нашей последней синхронизации (см. докстринг модуля).

    `ref.last_synced_at is None` не должно происходить в обычном потоке
    (push_deal всегда его проставляет), но раз `ExternalRef.last_synced_at`
    в модели `nullable=True` — трактуем «нет базы для сравнения» как
    «конфликт доказать нечем», а не падаем."""
    if ref.last_synced_at is None:
        return
    response = await client.get_deal(ref.external_id)
    bitrix_updated_at = _parse_bitrix_time(response["result"]["item"].get("updatedTime"))
    if bitrix_updated_at is None:
        return
    if bitrix_updated_at > ref.last_synced_at:
        logger.warning(
            "bitrix_push_conflict",
            bitrix_id=ref.external_id,
            entity_id=str(ref.entity_id),
            bitrix_updated_at=bitrix_updated_at.isoformat(),
            local_synced_at=ref.last_synced_at.isoformat(),
        )
        raise RuntimeError(
            f"bitrix push conflict: deal {ref.entity_id} (bitrix_id="
            f"{ref.external_id}) changed in Bitrix at "
            f"{bitrix_updated_at.isoformat()}, after our last sync at "
            f"{ref.last_synced_at.isoformat()} — needs manual review"
        )


async def push_deal(
    session: AsyncSession, client: BitrixClient, *, deal_id: uuid.UUID
) -> ExternalRef:
    deal = await session.get(Deal, deal_id)
    if deal is None:
        raise ValueError(f"deal {deal_id} not found")

    ref = (
        await session.execute(
            select(ExternalRef).where(
                ExternalRef.source_code == IntegrationSourceCode.BITRIX24.value,
                ExternalRef.entity_type == "deal",
                ExternalRef.entity_id == deal_id,
            )
        )
    ).scalar_one_or_none()

    if ref is not None:
        await _reject_if_bitrix_moved_ahead(client, ref)

    fields = _deal_to_bitrix_fields(deal)
    if ref is not None:
        response = await client.update_deal(ref.external_id, fields)
    else:
        response = await client.add_deal(fields)
    # Раздел «Response JSON Structure» crm.item.add/update: `result.item.id`,
    # не голый `result`, как у устаревшего `crm.deal.*`.
    item = response["result"]["item"]
    bitrix_id = str(item["id"])
    # `updatedTime` из ответа Bitrix, а не наши часы — иначе рассинхрон часов
    # между нашим сервером и порталом дал бы ложные срабатывания
    # `_reject_if_bitrix_moved_ahead` на следующем push при отсутствии
    # реальной правки на стороне Bitrix.
    synced_at = _parse_bitrix_time(item.get("updatedTime")) or dt.datetime.now(dt.UTC)

    if ref is None:
        ref = ExternalRef(
            entity_type="deal",
            entity_id=deal_id,
            source_code=IntegrationSourceCode.BITRIX24.value,
            external_id=bitrix_id,
            synced_version=1,
            last_synced_at=synced_at,
            sync_direction=SyncDirection.OUTBOUND.value,
        )
        session.add(ref)
    else:
        ref.synced_version += 1
        ref.last_synced_at = synced_at
        ref.sync_direction = SyncDirection.OUTBOUND.value
    await session.flush()
    return ref


async def apply_inbound_change(
    session: AsyncSession, *, bitrix_id: str, remote_version: int, fields: dict[str, Any]
) -> tuple[Deal | None, bool]:
    """Возвращает `(сделка или None, применено ли изменение)`. `fields` —
    контракт нашего собственного `/bitrix/webhook` (см. докстринг модуля),
    поэтому ключи — те же camelCase-имена, что `crm.item.*` использует для
    исходящего направления (`title`), а не сырой конверт `ONCRMDEALUPDATE`."""
    ref = (
        await session.execute(
            select(ExternalRef).where(
                ExternalRef.source_code == IntegrationSourceCode.BITRIX24.value,
                ExternalRef.entity_type == "deal",
                ExternalRef.external_id == bitrix_id,
            )
        )
    ).scalar_one_or_none()
    if ref is None:
        logger.warning("bitrix_webhook_unknown_deal", bitrix_id=bitrix_id)
        return None, False

    if remote_version <= ref.synced_version:
        logger.info(
            "bitrix_webhook_stale_version",
            bitrix_id=bitrix_id,
            remote_version=remote_version,
            local_version=ref.synced_version,
        )
        return None, False

    deal = await session.get(Deal, ref.entity_id)
    if deal is None or deal.deleted_at is not None:
        return None, False

    title = fields.get("title")
    if title:
        deal.title = title

    ref.synced_version = remote_version
    ref.last_synced_at = dt.datetime.now(dt.UTC)
    ref.sync_direction = SyncDirection.INBOUND.value
    await session.flush()
    return deal, True
