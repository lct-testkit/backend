"""Письмо-уведомление: к тексту админского шаблона добавляются кнопка и таблица деталей.

Текст шаблона из `notification_templates` (то, что админ правит в интерфейсе) — абзац письма, тема
шаблона — заголовок. Остальное собирается здесь: кнопка ведёт на страницу сущности (`entity_type` /
`entity_id` уведомления, маршруты веб-клиента), а таблица «ключ — значение» берёт данные из самой
сущности (номер и вуз сделки, название документа) и из `payload` (причина, время, срок).

Любой сбой выборки деталей даёт письмо без них, а не потерянную доставку: деталь — украшение, текст
письма и так уже отрисован из шаблона.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.catalog.models import Organization
from app.modules.crm.models import Deal, Task
from app.modules.notification.mail_render import (
    ROLE_LABELS,
    RenderedMail,
    msk_datetime,
    public_ip,
    public_url,
    render_notice,
    ru_elapsed,
)
from app.modules.signing.models import SignatureDocument, SignatureRequest
from app.modules.workflow.models import WorkflowStatus

logger = structlog.get_logger(__name__)

# entity_type уведомления → (маршрут веб-клиента, подпись кнопки). Маршруты — из
# `frontend/src/routes`; тип без маршрута кнопки не получает (письма о безопасности учётной
# записи без ссылок: по ссылке из письма пароль не вводят).
_ROUTES: dict[str, tuple[str, str]] = {
    "deal": ("/deals/{id}", "Открыть сделку"),
    "organization": ("/organizations/{id}", "Открыть организацию"),
    "contact": ("/contacts/{id}", "Открыть контакт"),
    "task": ("/tasks?task={id}", "Открыть задачу"),
    "signature_request": ("/signing/requests/{id}", "Открыть запрос на подпись"),
    "signature_document": ("/signing/{id}", "Открыть документ"),
    "user": ("/admin/users/{id}", "Открыть сотрудника"),
    "data_erasure_request": ("/admin/erasure/{id}", "Открыть запрос"),
    "import_job": ("/imports/{id}", "Открыть импорт"),
}

_SLA_CODES = frozenset({"DEAL_SLA_WARNING", "DEAL_SLA_BREACHED"})

Rows = list[tuple[str, Any]]


def notice_link(entity_type: str | None, entity_id: uuid.UUID | None) -> tuple[str, str] | None:
    """(абсолютная ссылка, подпись кнопки) или `None`, если вести некуда."""
    if not entity_type or entity_id is None:
        return None
    route = _ROUTES.get(entity_type)
    if route is None:
        return None
    path, label = route
    return public_url(path.format(id=entity_id)), label


async def _deal_rows(session: AsyncSession, deal_id: uuid.UUID, code: str) -> Rows:
    deal = await session.get(Deal, deal_id)
    if deal is None:
        return []
    organization = (
        await session.get(Organization, deal.organization_id) if deal.organization_id else None
    )
    status = await session.get(WorkflowStatus, deal.status_id)
    rows: Rows = [
        ("Сделка", deal.number),
        ("Название", deal.title),
        ("Вуз", organization.short_name or organization.name if organization else None),
        ("Статус", status.name if status else None),
    ]
    if code in _SLA_CODES and deal.sla_due_at is not None:
        rows.append(("Срок статуса", msk_datetime(deal.sla_due_at)))
        overdue = dt.datetime.now(dt.UTC) - deal.sla_due_at
        if overdue > dt.timedelta(0):
            rows.append(("Просрочено на", ru_elapsed(overdue)))
    return rows


async def _document_rows(session: AsyncSession, document_id: uuid.UUID) -> Rows:
    document = await session.get(SignatureDocument, document_id)
    if document is None:
        return []
    return [
        ("Документ", document.title),
        ("Срок подписания", msk_datetime(document.deadline_at) if document.deadline_at else None),
    ]


async def _entity_rows(
    session: AsyncSession, entity_type: str | None, entity_id: uuid.UUID | None, code: str
) -> Rows:
    if not entity_type or entity_id is None:
        return []
    if entity_type == "deal":
        return await _deal_rows(session, entity_id, code)
    if entity_type == "organization":
        organization = await session.get(Organization, entity_id)
        return [("Организация", organization.name)] if organization else []
    if entity_type == "signature_document":
        return await _document_rows(session, entity_id)
    if entity_type == "signature_request":
        request = await session.get(SignatureRequest, entity_id)
        return await _document_rows(session, request.document_id) if request else []
    if entity_type == "task":
        task = await session.get(Task, entity_id)
        if task is None:
            return []
        return [
            ("Задача", task.title),
            ("Срок", msk_datetime(task.due_at) if task.due_at else None),
        ]
    return []


def _role(value: Any) -> str | None:
    return ROLE_LABELS.get(str(value), str(value)) if value else None


def _payload_rows(payload: dict[str, Any]) -> Rows:
    """Детали из `payload`. Даты показываются по Москве, внутренний IP не показывается вовсе."""
    when = payload.get("changed_at")
    deadline = payload.get("deadline_at")
    due = payload.get("due_at")
    return [
        ("Причина", payload.get("reason")),
        ("Сотрудник", payload.get("escalated_from_name") or payload.get("full_name")),
        ("Когда", msk_datetime(when) if when else None),
        ("IP-адрес", public_ip(payload.get("ip"))),
        ("Документ", payload.get("title")),
        ("Срок подписания", msk_datetime(deadline) if deadline else None),
        ("Срок", msk_datetime(due) if due else None),
        ("Номер", payload.get("number")),
        ("Было", _role(payload.get("old_role"))),
        ("Стало", _role(payload.get("new_role"))),
    ]


async def build_notice_mail(
    session: AsyncSession,
    *,
    code: str,
    subject: str,
    body: str,
    payload: dict[str, Any],
    entity_type: str | None,
    entity_id: uuid.UUID | None,
) -> RenderedMail:
    """Письмо по уведомлению: тема и текст уже отрисованы из шаблона канала."""
    try:
        entity = await _entity_rows(session, entity_type, entity_id, code)
    except Exception:  # noqa: BLE001 — детали не должны ронять доставку
        logger.warning("notice_details_failed", code=code, entity_type=entity_type)
        entity = []
    seen: set[str] = set()
    rows: Rows = []
    for label, value in [*entity, *_payload_rows(payload)]:
        if value in (None, "") or label in seen:
            continue
        seen.add(label)
        rows.append((label, value))
    link = notice_link(entity_type, entity_id)
    return render_notice(
        subject=subject,
        body=body,
        rows=rows,
        url=link[0] if link else None,
        button_label=link[1] if link else None,
    )
