"""LMS: pull прогресса по расписанию + push зачисления (new_spec §4.14).

Два независимых потока делят один HTTP-клиент (`LmsClient`) и одну функцию
апсерта (`upsert_progress`): раздел 8 перечисляет и pull (наш `GET
{lms_base_url}/students/progress`, инициируется нами по cron —
`integration.tasks.sweep_lms_progress_pull`), и push-приём (`POST
/api/v1/integrations/lms/progress`, для LMS, которые сами шлют обновления,
не дожидаясь опроса) под одним контуром — обе ветки в итоге приводят к
одной и той же строке `learning_progress`.

**Контракт сопоставления (решение этого спринта, не продиктовано ни одной
из спек — реального LMS-API нет ни у кого):** прогресс сопоставляется со
сделкой по `deal_id` — **нашему собственному** UUID, а не отдельному
LMS-идентификатору. При отправке зачисления (`push_enrollment`) мы сообщаем
LMS `deal_id` сделки; вернувшиеся строки прогресса обязаны содержать то же
значение. Это устраняет нужду в дополнительной таблице сопоставления
(`external_refs` уже занят Bitrix, где внешний id действительно чужой и
непредсказуемый) — здесь идентификатор задаём мы сами, значит сверять
не с чем.

Push зачисления (`LEARNING_ENROLLMENT_SENT`/`LEARNING_TRANSFER_REQUESTED`,
воронка `crm.workflow.seed`) вызывается из `integration.tasks.
sweep_outbox_events`, не отсюда напрямую — outbox публикуется синхронно с
переходом сделки, доставка асинхронна (раздел 3.6).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.crm.models import Deal, DealProduct
from app.modules.integration.models import LearningProgress, SyncCursor

logger = structlog.get_logger(__name__)

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class LmsClient:
    def __init__(self, base_url: str, auth_token: str | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth_token = auth_token

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        return headers

    async def pull_progress(self, *, updated_since: str | None) -> list[dict[str, Any]]:
        params = {"updated_since": updated_since} if updated_since else {}
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(
                f"{self._base_url}/students/progress", params=params, headers=self._headers()
            )
            response.raise_for_status()
            body = response.json()
            return body.get("items", []) if isinstance(body, dict) else body

    async def push_enrollment(self, payload: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                f"{self._base_url}/enrollments", json=payload, headers=self._headers()
            )
            response.raise_for_status()
            return response.json() if response.content else {}


def _parse_dt(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


async def upsert_progress(session: AsyncSession, row: dict[str, Any]) -> LearningProgress | None:
    """Один элемент ответа LMS → строка `learning_progress`. Строки без
    распознаваемого `deal_id`/`course_external_id` пропускаются с
    предупреждением, а не роняют весь батч — тот же приём честного
    частичного отказа, что `registry.tasks._process_version` уже применяет
    к отдельным «битым» записям выгрузки."""
    deal_id_raw = row.get("deal_id")
    course_external_id = row.get("course_external_id")
    if not deal_id_raw or not course_external_id:
        logger.warning("lms_progress_row_unmatched", row=row)
        return None

    try:
        deal_id = uuid.UUID(str(deal_id_raw))
    except ValueError:
        logger.warning("lms_progress_row_bad_deal_id", deal_id_raw=deal_id_raw)
        return None

    deal = await session.get(Deal, deal_id)
    if deal is None or deal.deleted_at is not None:
        logger.warning("lms_progress_deal_not_found", deal_id=str(deal_id))
        return None

    product_id = (
        (
            await session.execute(
                select(DealProduct.product_id).where(DealProduct.deal_id == deal.id).limit(1)
            )
        )
        .scalars()
        .first()
    )

    existing = (
        await session.execute(
            select(LearningProgress).where(
                LearningProgress.deal_id == deal.id,
                LearningProgress.external_course_id == str(course_external_id),
            )
        )
    ).scalar_one_or_none()

    fields = {
        "contact_id": deal.contact_id,
        "deal_id": deal.id,
        "product_id": product_id,
        "external_course_id": str(course_external_id),
        "enrolled_at": _parse_dt(row.get("enrolled_at")),
        "progress_pct": row.get("progress_pct"),
        "score": row.get("score"),
        "completed_at": _parse_dt(row.get("completed_at")),
        "last_activity_at": _parse_dt(row.get("last_activity_at")),
        "raw": row,
        "synced_at": dt.datetime.now(dt.UTC),
    }
    if existing is None:
        existing = LearningProgress(**fields)
        session.add(existing)
    else:
        for key, value in fields.items():
            setattr(existing, key, value)
    await session.flush()
    return existing


async def get_cursor(session: AsyncSession, *, source_code: str, resource: str) -> str | None:
    cursor = (
        await session.execute(
            select(SyncCursor).where(
                SyncCursor.source_code == source_code, SyncCursor.resource == resource
            )
        )
    ).scalar_one_or_none()
    return cursor.cursor_value if cursor else None


async def set_cursor(session: AsyncSession, *, source_code: str, resource: str, value: str) -> None:
    cursor = (
        await session.execute(
            select(SyncCursor).where(
                SyncCursor.source_code == source_code, SyncCursor.resource == resource
            )
        )
    ).scalar_one_or_none()
    if cursor is None:
        session.add(SyncCursor(source_code=source_code, resource=resource, cursor_value=value))
    else:
        cursor.cursor_value = value
    await session.flush()
