"""B5-2: забор лидов с сайта по API (Функц.5 ТЗ: «забирать по API информацию из веб-сайта»).

`CmsPullService.run()` — по образцу `integration.tasks.sweep_lms_progress_pull`, но с двумя
отличиями, которых у LMS нет:

* блокировка между тиками (`distributed_lock`): прогон может занять дольше минутного интервала
  cron, а у LMS-забора такой защиты нет (известный пробел, не в этой задаче);
* постраничный разбор ответа (`next_cursor`) — мок сайта отдаёт лидов страницами.

Каждый лид обрабатывается в своей `session_scope()`: сбой одного не откатывает остальные, а
`CmsLeadService._fail`/`_fail_unexpected` коммитят неудачу сами (тот же приём, что у вебхука).
Курсор — наибольший `updated_at` среди обработанных лидов, но не дальше самого раннего лида с
ВРЕМЕННЫМ сбоем (исключение вне `AppError` или `AppError` со статусом 5xx): такую строку
следующий тик получит снова. Лид с бизнес-ошибкой (4xx — нет телефона/почты и т.п.) курсор не
держит: он остаётся `failed` во входящих и виден администратору, а ручной повтор («Забрать
сейчас», B5-5) с `updated_since` в прошлом подбирает такие ключи заново (`_claim` обрабатывает
записи со статусом `failed` как новую попытку).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any, Literal

import structlog
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.errors import AppError
from app.core.redis_client import distributed_lock
from app.modules.integration.cms import CmsLeadService
from app.modules.integration.cms_client import CmsClient
from app.modules.integration.lms import get_cursor, set_cursor
from app.modules.integration.models import (
    InboundStatus,
    IntegrationSource,
    IntegrationSourceCode,
)
from app.modules.integration.security import check_outbound_url_resolved, resolve_secret

logger = structlog.get_logger(__name__)

_SOURCE = IntegrationSourceCode.CMS.value
_CURSOR_RESOURCE = "leads_updated_since"
_LOCK_RESOURCE = "integration:cms_pull"
_LOCK_TTL = 600
_LOCK_WAIT = 0.5

# B5-2 шаг 6: «утверждённые поля» мока сайта не совпадают с алиасами `lead_payload._ALIASES`
# (`lead_id`, `product_interest`, `submitted_at` сайт не присылает ни одним из распознаваемых
# имён) — временный (до настраиваемого маппинга B5-3) перевод в уже распознаваемые цели.
_INTERIM_FIELD_MAP: dict[str, str] = {
    "lead_id": "external_id",
    "product_interest": "product_code",
    "submitted_at": "created_at",
}


def apply_interim_mapping(record: dict[str, Any]) -> dict[str, Any]:
    mapped = dict(record)
    for source_key, target_key in _INTERIM_FIELD_MAP.items():
        if source_key in mapped and target_key not in mapped:
            mapped[target_key] = mapped.pop(source_key)
    return mapped


def _lead_key(record: dict[str, Any]) -> str:
    raw = record.get("lead_id") or record.get("external_id")
    return str(raw) if raw else uuid.uuid4().hex


def _updated_at(record: dict[str, Any]) -> str | None:
    value = record.get("updated_at") or record.get("submitted_at")
    return str(value) if value else None


def advance_cursor(cursor: str | None, rows: list[tuple[str | None, bool]]) -> str | None:
    """Та же логика, что `integration.lms.advance_cursor` (B5-2 переиспользует приём, не код —
    форма строки другая): курсор не идёт дальше самой ранней строки с временным сбоем, а
    строки с постоянной (бизнес-) ошибкой курсор не держат. `rows` — `(updated_at, временный_ли)`
    для каждого обработанного лида, в порядке получения."""
    ceiling: str | None = None
    for updated_at, temporary in rows:
        if not temporary:
            continue
        if updated_at is None:
            return cursor  # место сбойной строки неизвестно: не рискуем перешагнуть её
        ceiling = updated_at if ceiling is None else min(ceiling, updated_at)

    latest = cursor
    for updated_at, temporary in rows:
        if temporary or updated_at is None:
            continue
        if ceiling is not None and updated_at >= ceiling:
            continue
        if latest is None or updated_at > latest:
            latest = updated_at
    return latest


@dataclass(slots=True)
class CmsPullResult:
    status: Literal["ok", "busy", "skipped", "error"] = "ok"
    fetched: int = 0
    created: int = 0
    duplicates: int = 0
    failed: int = 0
    skipped: int = 0
    error: str | None = None


class CmsPullService:
    async def run(self, trigger: str) -> CmsPullResult:
        """`trigger` — `cron` или `manual` (B5-5): попадает в `integration_sources.last_run`."""
        settings = get_settings()
        async with session_scope() as session:
            source = await session.scalar(
                select(IntegrationSource).where(IntegrationSource.code == _SOURCE)
            )
            if source is None or not source.is_active or not source.pull_enabled:
                return CmsPullResult(status="skipped")
            base_url = source.base_url or settings.cms_base_url
            if not base_url:
                return CmsPullResult(status="skipped")
            try:
                base_url = await check_outbound_url_resolved(base_url, strict=settings.is_prod)
            except ValueError as exc:
                await self._record_run(trigger, CmsPullResult(status="error", error=str(exc)))
                return CmsPullResult(status="error", error=str(exc))
            token = resolve_secret(settings.cms_auth_ref)

        async with distributed_lock(_LOCK_RESOURCE, ttl=_LOCK_TTL, blocking_timeout=_LOCK_WAIT) as (
            acquired
        ):
            if not acquired:
                return CmsPullResult(status="busy")
            result = await self._run_locked(base_url, token, settings)
        await self._record_run(trigger, result)
        return result

    async def _run_locked(self, base_url: str, token: str | None, settings: Any) -> CmsPullResult:
        client = CmsClient(base_url, token)
        async with session_scope() as session:
            cursor = await get_cursor(session, source_code=_SOURCE, resource=_CURSOR_RESOURCE)
        started_cursor = cursor

        result = CmsPullResult()
        rows: list[tuple[str | None, bool]] = []
        page_cursor: str | None = None

        for _page in range(settings.integration_pull_max_pages):
            try:
                page = await client.list_leads(
                    updated_since=cursor,
                    cursor=page_cursor,
                    limit=settings.integration_pull_page_size,
                )
            except Exception as exc:  # noqa: BLE001 — как sweep_registry_drift: не роняем cron
                logger.warning("cms_leads_pull_failed", error=str(exc))
                result.status = "error"
                result.error = str(exc)
                return result

            if not page.items:
                break
            for record in page.items:
                result.fetched += 1
                if not isinstance(record, dict):
                    result.skipped += 1
                    continue
                key = _lead_key(record)
                updated_at = _updated_at(record)
                outcome, temporary = await self._ingest_one(record, key)
                rows.append((updated_at, temporary))
                if outcome == "created":
                    result.created += 1
                elif outcome == "duplicate":
                    result.duplicates += 1
                else:
                    result.failed += 1

            page_cursor = page.next_cursor
            if not page_cursor:
                break

        final_cursor = advance_cursor(started_cursor, rows)
        if final_cursor and final_cursor != started_cursor:
            async with session_scope() as session:
                await set_cursor(
                    session, source_code=_SOURCE, resource=_CURSOR_RESOURCE, value=final_cursor
                )
        return result

    async def _ingest_one(self, record: dict[str, Any], key: str) -> tuple[str, bool]:
        """`(исход, временный_ли_сбой)`: `исход` — `created`/`duplicate`/`failed`. `duplicate` —
        и повтор уже обработанного ключа (идемпотентность, например тот же лид во второй
        странице или во втором прогоне), и бизнес-дубль (`_open_duplicate`: открытая сделка
        того же контакта с тем же продуктом) — счётчику прогона эта разница не важна, обеим
        не соответствует новая сделка."""
        mapped = apply_interim_mapping(record)
        async with session_scope() as session:
            try:
                ingested = await CmsLeadService(session).ingest_pulled(mapped, key)
            except AppError as exc:
                return "failed", exc.status >= 500
            except Exception:  # noqa: BLE001 — одна битая строка не должна ронять прогон
                logger.exception("cms_lead_pull_row_failed", key=key)
                return "failed", True
        is_duplicate = (
            ingested.replayed or ingested.message.status == InboundStatus.DUPLICATE.value
        )
        return ("duplicate" if is_duplicate else "created"), False

    async def _record_run(self, trigger: str, result: CmsPullResult) -> None:
        async with session_scope() as session:
            source = await session.scalar(
                select(IntegrationSource).where(IntegrationSource.code == _SOURCE)
            )
            if source is None:
                return
            now = dt.datetime.now(dt.UTC)
            source.last_run = {
                "trigger": trigger,
                "finished_at": now.isoformat(),
                "fetched": result.fetched,
                "created": result.created,
                "duplicates": result.duplicates,
                "failed": result.failed,
                "skipped": result.skipped,
                "error": result.error,
                "status": result.status,
            }
            if result.status != "error":
                source.last_sync_at = now
                source.last_error = None
            else:
                source.last_error = result.error
