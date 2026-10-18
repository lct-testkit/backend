"""Реестр инцидентов безопасности (B6-12, 152-ФЗ ст. 21 ч. 3.1).

Кандидаты заводятся автоматически из `security_events` (`MALWARE_DETECTED` — B6-5,
`BRUTE_FORCE_LOCK` — B6-3, `SESSION_HIJACK_SUSPECT` — B6-11, `SIGNATURE_KEY_COMPROMISED`),
но срок РКН (24/72 ч) не идёт, пока человек не подтвердит кандидат (`confirm`) — именно
решение человека, что произошедшее действительно инцидент, запускает отсчёт. Сама подача в
РКН делается человеком на портале регулятора: система только ведёт реестр, считает сроки,
генерирует тексты уведомлений и фиксирует факт отправки (`rkn_24_reference`/`rkn_72_reference`
— номер или ссылка подачи, которую указывает пользователь).

Серия `INTEGRATION_SIGNATURE_INVALID` (вебхуки с неверной подписью) НЕ создаёт кандидатов в
этой версии: единичное событие слишком шумно (сканер/партнёр с опечаткой в секрете даёт
десятки событий в минуту), а обнаружение всплеска — отдельная задача по объёму, не успевшая
войти в этот спринт (см. отчёт)."""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import run_after_rollback
from app.core.errors import AppError, ErrorCode, NotFoundError
from app.core.ids import uuid7
from app.core.optimistic import claim_version
from app.core.storage import delete_object, ensure_bucket, upload_object_bytes
from app.modules.admin.models import SystemSetting
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.files.models import File, FileStatus
from app.modules.identity.models import (
    IncidentStatus,
    Role,
    SecurityEvent,
    SecurityEventType,
    SecurityIncident,
    User,
    UserStatus,
)
from app.modules.notification.service import NotificationPriority, get_notification_service
from app.modules.signing.rendering import RenderError, html_to_pdf, render_template_html

logger = structlog.get_logger(__name__)

TPL_SECURITY_INCIDENT_CANDIDATE = "SECURITY_INCIDENT_CANDIDATE"
TPL_SECURITY_INCIDENT_DEADLINE = "SECURITY_INCIDENT_DEADLINE"

#: Типы событий, из которых автоматически заводится кандидат в инцидент (`suggest`).
INCIDENT_CANDIDATE_TRIGGERS = frozenset(
    {
        SecurityEventType.MALWARE_DETECTED.value,
        SecurityEventType.BRUTE_FORCE_LOCK.value,
        SecurityEventType.SESSION_HIJACK_SUSPECT.value,
        SecurityEventType.SIGNATURE_KEY_COMPROMISED.value,
    }
)

_RKN_CONTACT_SETTING_KEY = "pdn.rkn_contact"
_OPERATOR_NAME = "ИТ Школа Ростелекома"

_TEMPLATES_DIR = (
    Path(__file__).resolve().parent.parent.parent.parent / "docs" / "security" / "templates"
)

_STAGE_TEMPLATES = {
    "24h": "rkn-notice-24h.md.j2",
    "72h": "rkn-notice-72h.md.j2",
}


def _template_source(filename: str) -> str:
    return (_TEMPLATES_DIR / filename).read_text(encoding="utf-8")


@dataclass(slots=True)
class IncidentFilters:
    status: list[str] | None = None


class IncidentService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    # --- Создание ----------------------------------------------------------

    async def _next_number(self) -> str:
        from sqlalchemy import text

        seq = await self._session.scalar(text("SELECT nextval('security_incident_seq')"))
        year = dt.datetime.now(dt.UTC).year
        return f"INC-{year}-{int(seq):04d}"

    async def create_manual(
        self,
        *,
        title: str,
        severity: str,
        description: str | None,
        data_categories: list[str],
        subjects_estimate: int | None,
        created_by: uuid.UUID | None,
    ) -> SecurityIncident:
        incident = SecurityIncident(
            number=await self._next_number(),
            title=title,
            status=IncidentStatus.CANDIDATE.value,
            severity=severity,
            source="manual",
            description=description,
            data_categories=data_categories or None,
            subjects_estimate=subjects_estimate,
            created_by=created_by,
        )
        self._session.add(incident)
        await self._session.flush()
        await self._audit.record(
            AuditAction.INCIDENT_CREATED,
            entity_type="security_incident",
            entity_id=incident.id,
            changes={
                "number": {"old": None, "new": incident.number},
                "source": {"old": None, "new": "manual"},
            },
        )
        await self._notify_candidate(incident)
        return incident

    async def suggest(self, event: SecurityEvent) -> SecurityIncident | None:
        """Кандидат из события безопасности (B6-3/B6-5/B6-11 и т.п.).

        Вызывается из `IdentityService.record_security_event` ВНУТРИ SAVEPOINT — сбой здесь
        (редактор шаблона, недоступный Redis для уведомления) не должен откатить запись
        самого события безопасности, которая важнее."""
        if event.event_type not in INCIDENT_CANDIDATE_TRIGGERS:
            return None
        title = f"Кандидат из события {event.event_type}"
        incident = SecurityIncident(
            number=await self._next_number(),
            title=title,
            status=IncidentStatus.CANDIDATE.value,
            severity=event.severity,
            source="event",
            event_ids=[str(event.id)],
            description=f"Автоматически создан по событию {event.event_type} (id={event.id}).",
            created_by=None,
        )
        self._session.add(incident)
        await self._session.flush()
        await self._audit.record(
            AuditAction.INCIDENT_CREATED,
            entity_type="security_incident",
            entity_id=incident.id,
            changes={
                "number": {"old": None, "new": incident.number},
                "source": {"old": None, "new": "event"},
                "event_type": {"old": None, "new": event.event_type},
            },
        )
        await self._notify_candidate(incident)
        return incident

    async def _notify_candidate(self, incident: SecurityIncident) -> None:
        admin_ids = list(
            (
                await self._session.execute(
                    select(User.id).where(
                        User.role == Role.ADMIN.value,
                        User.status == UserStatus.ACTIVE.value,
                        User.deleted_at.is_(None),
                    )
                )
            ).scalars()
        )
        for admin_id in admin_ids:
            await get_notification_service().notify_user(
                self._session,
                recipient_id=admin_id,
                template_code=TPL_SECURITY_INCIDENT_CANDIDATE,
                payload={"number": incident.number, "event_type": incident.description or ""},
                priority=NotificationPriority.HIGH,
                entity_type="security_incident",
                entity_id=incident.id,
            )

    # --- Чтение --------------------------------------------------------

    def list_query(self, filters: IncidentFilters) -> Select[tuple[SecurityIncident]]:
        stmt = select(SecurityIncident)
        if filters.status:
            stmt = stmt.where(SecurityIncident.status.in_(filters.status))
        return stmt

    async def get_or_404(self, incident_id: uuid.UUID) -> SecurityIncident:
        incident = await self._session.get(SecurityIncident, incident_id)
        if incident is None:
            raise NotFoundError("Инцидент", incident_id)
        return incident

    # --- Переходы статуса ------------------------------------------------

    async def confirm(
        self,
        incident: SecurityIncident,
        *,
        detected_at: dt.datetime,
        data_categories: list[str],
        subjects_estimate: int | None,
        causes: str | None,
        harm: str | None,
        measures: str | None,
        responsible_user_id: uuid.UUID | None,
        expected_version: int,
    ) -> SecurityIncident:
        if incident.status != IncidentStatus.CANDIDATE.value:
            raise AppError(
                ErrorCode.VALIDATION,
                "Подтвердить можно только кандидата",
                extra={"status": incident.status},
            )
        incident.status = IncidentStatus.OPEN.value
        incident.detected_at = detected_at
        incident.data_categories = data_categories or None
        incident.subjects_estimate = subjects_estimate
        incident.causes = causes
        incident.harm = harm
        incident.measures = measures
        incident.responsible_user_id = responsible_user_id
        settings = get_settings()
        incident.rkn_24_due_at = detected_at + dt.timedelta(hours=settings.incident_rkn_24_hours)
        incident.rkn_72_due_at = detected_at + dt.timedelta(hours=settings.incident_rkn_72_hours)
        await claim_version(self._session, incident, expected_version)
        await self._session.flush()
        await self._audit.record(
            AuditAction.INCIDENT_CONFIRMED,
            entity_type="security_incident",
            entity_id=incident.id,
            changes={
                "status": {"old": IncidentStatus.CANDIDATE.value, "new": incident.status},
                "detected_at": {"old": None, "new": detected_at.isoformat()},
                "rkn_24_due_at": {"old": None, "new": incident.rkn_24_due_at.isoformat()},
                "rkn_72_due_at": {"old": None, "new": incident.rkn_72_due_at.isoformat()},
            },
        )
        return incident

    async def update(
        self, incident: SecurityIncident, updates: dict[str, Any], expected_version: int
    ) -> SecurityIncident:
        before = {k: getattr(incident, k) for k in updates}
        for field_name, value in updates.items():
            setattr(incident, field_name, value)
        await claim_version(self._session, incident, expected_version)
        await self._session.flush()
        await self._audit.record(
            AuditAction.INCIDENT_UPDATED,
            entity_type="security_incident",
            entity_id=incident.id,
            changes={
                k: {"old": _jsonable(before[k]), "new": _jsonable(v)} for k, v in updates.items()
            },
        )
        return incident

    async def close(
        self, incident: SecurityIncident, *, reason: str | None, expected_version: int
    ) -> SecurityIncident:
        if incident.status in (IncidentStatus.CLOSED.value, IncidentStatus.DISMISSED.value):
            raise AppError(
                ErrorCode.VALIDATION,
                "Инцидент уже закрыт",
                extra={"status": incident.status},
            )
        old_status = incident.status
        incident.status = (
            IncidentStatus.DISMISSED.value
            if old_status == IncidentStatus.CANDIDATE.value
            else IncidentStatus.CLOSED.value
        )
        await claim_version(self._session, incident, expected_version)
        await self._session.flush()
        await self._audit.record(
            AuditAction.INCIDENT_CLOSED,
            entity_type="security_incident",
            entity_id=incident.id,
            changes={
                "status": {"old": old_status, "new": incident.status},
                "reason": {"old": None, "new": reason},
            },
        )
        return incident

    # --- Уведомления РКН --------------------------------------------------

    async def _rkn_contact(self) -> str:
        setting = await self._session.get(SystemSetting, _RKN_CONTACT_SETTING_KEY)
        if setting is not None and isinstance(setting.value, str) and setting.value.strip():
            return setting.value
        return "не указано (заполните pdn.rkn_contact в системных настройках)"

    def _common_fields(self, incident: SecurityIncident, *, rkn_contact: str) -> dict[str, Any]:
        return {
            "number": incident.number,
            "detected_at": _iso(incident.detected_at),
            "operator_name": _OPERATOR_NAME,
            "rkn_contact": rkn_contact,
            "incident_summary": incident.description or incident.title,
            "probable_causes": incident.causes or "не указано",
            "probable_harm": incident.harm or "не указано",
            "measures": incident.measures or "не указано",
            "data_categories": ", ".join(incident.data_categories or []) or "не указано",
            "subjects_estimate": incident.subjects_estimate,
            "generated_at": _iso(dt.datetime.now(dt.UTC)),
        }

    async def generate_rkn_notice(
        self, incident: SecurityIncident, *, stage: str
    ) -> tuple[File, dict[str, Any]]:
        if incident.status not in (
            IncidentStatus.OPEN.value,
            IncidentStatus.RKN_24_SENT.value,
        ):
            raise AppError(
                ErrorCode.VALIDATION,
                "Уведомление РКН доступно только для подтверждённого инцидента",
                extra={"status": incident.status},
            )
        if stage == "72h" and incident.status != IncidentStatus.RKN_24_SENT.value:
            raise AppError(
                ErrorCode.VALIDATION,
                "Уведомление на 72 часа формируется после отметки об отправке 24-часового",
                extra={"status": incident.status},
            )
        rkn_contact = await self._rkn_contact()
        fields = self._common_fields(incident, rkn_contact=rkn_contact)
        if stage == "72h":
            fields["rkn_24_sent_at"] = _iso(incident.rkn_24_sent_at)
            fields["investigation_summary"] = incident.investigation_summary or "не указано"
        try:
            html = render_template_html(_template_source(_STAGE_TEMPLATES[stage]), fields)
            pdf_bytes = html_to_pdf(html)
        except RenderError as exc:
            raise AppError(
                ErrorCode.VALIDATION, f"Не удалось сформировать документ: {exc}"
            ) from exc
        file = await self._store_pdf(
            pdf_bytes, filename=f"rkn-notice-{stage}.pdf", incident=incident
        )
        action = AuditAction.INCIDENT_RKN_NOTICE_GENERATED
        await self._audit.record(
            action,
            entity_type="security_incident",
            entity_id=incident.id,
            changes={
                "stage": {"old": None, "new": stage},
                "file_id": {"old": None, "new": str(file.id)},
            },
        )
        due_at = incident.rkn_24_due_at if stage == "24h" else incident.rkn_72_due_at
        return file, {**fields, "stage": stage, "due_at": _iso(due_at), "file_id": str(file.id)}

    async def mark_sent(
        self,
        incident: SecurityIncident,
        *,
        stage: str,
        sent_at: dt.datetime | None,
        reference: str,
        expected_version: int,
    ) -> SecurityIncident:
        sent_at = sent_at or dt.datetime.now(dt.UTC)
        expected_statuses = {
            "24h": IncidentStatus.OPEN.value,
            "72h": IncidentStatus.RKN_24_SENT.value,
        }
        already_sent_status = {
            "24h": IncidentStatus.RKN_24_SENT.value,
            "72h": IncidentStatus.RKN_72_SENT.value,
        }
        if incident.status == already_sent_status[stage]:
            raise AppError(ErrorCode.DUPLICATE, f"Отправка {stage} уже отмечена")
        if incident.status != expected_statuses[stage]:
            raise AppError(
                ErrorCode.VALIDATION,
                f"Неверный статус инцидента для отметки отправки {stage}",
                extra={"status": incident.status},
            )
        # Отдельно от статуса: сам документ должен быть сгенерирован хоть раз
        # (POST .../rkn-notice/{stage}) — тот же детерминированный ключ, что `_store_pdf`.
        notice_key = f"incidents/{incident.id}/rkn-notice-{stage}.pdf"
        notice_exists = await self._session.scalar(
            select(File.id).where(
                File.storage_key == notice_key, File.status == FileStatus.READY.value
            )
        )
        if notice_exists is None:
            raise AppError(
                ErrorCode.VALIDATION,
                f"Сначала сгенерируйте уведомление (POST .../rkn-notice/{stage})",
            )
        if stage == "24h":
            incident.status = IncidentStatus.RKN_24_SENT.value
            incident.rkn_24_sent_at = sent_at
            incident.rkn_24_reference = reference
        else:
            incident.status = IncidentStatus.RKN_72_SENT.value
            incident.rkn_72_sent_at = sent_at
            incident.rkn_72_reference = reference
        await claim_version(self._session, incident, expected_version)
        await self._session.flush()
        await self._audit.record(
            AuditAction.INCIDENT_RKN_NOTICE_SENT,
            entity_type="security_incident",
            entity_id=incident.id,
            changes={
                "stage": {"old": None, "new": stage},
                "reference": {"old": None, "new": reference},
                "sent_at": {"old": None, "new": sent_at.isoformat()},
            },
        )
        return incident

    async def generate_subjects_notice(self, incident: SecurityIncident) -> File:
        # Кандидат ещё не признан инцидентом (возможна ложная тревога), отклонённый — не был им
        # вовсе. Уведомление субъектам фиксирует `subjects_notified_at` и повторно не
        # формируется — составлять его по пустой карточке нельзя.
        if incident.status in (IncidentStatus.CANDIDATE.value, IncidentStatus.DISMISSED.value):
            raise AppError(
                ErrorCode.VALIDATION,
                "Уведомление субъектам формируется только для подтверждённого инцидента",
                extra={"status": incident.status},
            )
        rkn_contact = await self._rkn_contact()
        fields = self._common_fields(incident, rkn_contact=rkn_contact)
        fields["subject_display"] = "субъект персональных данных"
        try:
            html = render_template_html(_template_source("subjects-notice.md.j2"), fields)
            pdf_bytes = html_to_pdf(html)
        except RenderError as exc:
            raise AppError(
                ErrorCode.VALIDATION, f"Не удалось сформировать документ: {exc}"
            ) from exc
        file = await self._store_pdf(pdf_bytes, filename="subjects-notice.pdf", incident=incident)
        incident.subjects_notified_at = dt.datetime.now(dt.UTC)
        await self._session.flush()
        await self._audit.record(
            AuditAction.INCIDENT_SUBJECTS_NOTICE_GENERATED,
            entity_type="security_incident",
            entity_id=incident.id,
            changes={"file_id": {"old": None, "new": str(file.id)}},
        )
        return file

    async def _store_pdf(
        self, pdf_bytes: bytes, *, filename: str, incident: SecurityIncident
    ) -> File:
        """Тот же приём, что `erasure_service._store_pdf` — сервер формирует файл сам, без
        presigned-upload; ключ детерминирован по инциденту и имени, повторная генерация
        перезаписывает тот же объект."""
        settings = get_settings()
        bucket = settings.s3_bucket_signatures
        await ensure_bucket(bucket)
        file_id = uuid7()
        storage_key = f"incidents/{incident.id}/{filename}"
        await upload_object_bytes(
            bucket=bucket, key=storage_key, body=pdf_bytes, content_type="application/pdf"
        )

        async def _cleanup_orphan() -> None:
            await delete_object(bucket=bucket, key=storage_key)

        run_after_rollback(self._session, _cleanup_orphan)
        file = File(
            id=file_id,
            storage_key=storage_key,
            bucket=bucket,
            original_filename=filename,
            mime_type="application/pdf",
            size_bytes=len(pdf_bytes),
            sha256=hashlib.sha256(pdf_bytes).hexdigest(),
            status=FileStatus.READY.value,
            uploaded_by=None,
        )
        self._session.add(file)
        await self._session.flush()
        return file


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None


def _jsonable(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dt.datetime):
        return value.isoformat()
    return value
