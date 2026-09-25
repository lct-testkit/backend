"""Исполнение запросов на удаление/обезличивание (new_spec §4.8.4-4.8.5,
dop.md §10.7) — спринт 10.

До этого спринта `app/modules/identity/admin_service.py` умел только
создавать `DataErasureRequest` и считать блокеры (`AdminUserService.
create_erasure_request`/`collect_erasure_blockers`): `ERASURE_EXECUTED`/
`ERASURE_REQUEST_APPROVED`/`ERASURE_REQUEST_REJECTED` существовали как
значения `AuditAction`, но ни разу не вызывались, а `grace_until` вычислялся
роутером только для ответа API и нигде не сохранялся. Этот файл — вторая
половина сценария: пересчёт блокеров после их устранения, отказ, восстановление
в период отсрочки и, наконец, само исполнение — обезличивание или жёсткое
удаление, с актом об уничтожении ПДн (dop.md §10.4 фаза 6 п.19 по аналогии,
new_spec §4.8.4 шаг 6).

Оркестрация субъект-независимая, конкретика — нет: за «что значит обезличить»
для пользователя отвечает `AdminUserService` (уже владеет `collect_erasure_
blockers`/`hard_delete_eligible` для users), для контакта — `catalog.service.
ContactService` (тот же спринт). Этот файл только решает, КОГДА и ПО КАКОМУ
режиму это вызвать, и оформляет результат — акт, аудит, уведомление.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode, NotFoundError
from app.core.ids import uuid7
from app.core.security import Principal
from app.core.storage import ensure_bucket, upload_object_bytes
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.catalog.models import Contact, ContactChannel, Organization
from app.modules.catalog.service import ContactService, OrganizationService
from app.modules.files.models import File, FileStatus
from app.modules.identity.admin_service import AdminUserService, ApprovalService
from app.modules.identity.keycloak import keycloak_client
from app.modules.identity.models import (
    DataErasureRequest,
    ErasureStatus,
    SubjectType,
    User,
    UserStatus,
)
from app.modules.notification.service import (
    TPL_ERASURE_BLOCKED,
    TPL_ERASURE_COMPLETED,
    get_notification_service,
)
from app.modules.signing.rendering import render_erasure_act_pdf

logger = structlog.get_logger(__name__)

OPERATION_ERASURE_CONTACT = "contact.erasure"
OPERATION_ERASURE_ORGANIZATION = "organization.erasure"

# new_spec §7.10-стиль обоснование: категория, которая переживает любое
# удаление/обезличивание субъекта — бизнес-история компании, не ПДн субъекта.
_RETAINED_BUSINESS_HISTORY = {
    "category": "Бизнес-история (авторство сделок, комментариев, действий)",
    "legal_basis": "ст. 6 ч. 1 п. 7 152-ФЗ — обязанности учёта и отчётности",
}


class ErasureExecutionService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)
        self._approvals = ApprovalService(session)

    async def get_or_404(self, request_id: uuid.UUID) -> DataErasureRequest:
        request = await self._session.get(DataErasureRequest, request_id)
        if request is None:
            raise NotFoundError("Запрос на удаление", request_id)
        return request

    async def subject_displays(self, requests: list[DataErasureRequest]) -> dict[uuid.UUID, str]:
        """Имена субъектов запросов одним запросом на тип субъекта; ключ —
        `subject_id`. Нет строки (субъект удалён физически) — нет и ключа."""
        ids: dict[str, set[uuid.UUID]] = {"user": set(), "contact": set(), "organization": set()}
        for request in requests:
            ids.setdefault(request.subject_type, set()).add(request.subject_id)

        names: dict[uuid.UUID, str] = {}
        if ids["user"]:
            rows = await self._session.execute(
                select(User.id, User.display_name, User.full_name).where(User.id.in_(ids["user"]))
            )
            for row in rows:
                names[row.id] = row.display_name or row.full_name
        if ids["contact"]:
            rows = await self._session.execute(
                select(
                    Contact.id, Contact.last_name, Contact.first_name, Contact.middle_name
                ).where(Contact.id.in_(ids["contact"]))
            )
            for row in rows:
                names[row.id] = " ".join(
                    part for part in (row.last_name, row.first_name, row.middle_name) if part
                )
        if ids["organization"]:
            rows = await self._session.execute(
                select(Organization.id, Organization.name).where(
                    Organization.id.in_(ids["organization"])
                )
            )
            for row in rows:
                names[row.id] = row.name
        return names

    # --- Контакт: создание запроса (new_spec §4.8.5) -----------------------

    async def create_contact_request(
        self,
        *,
        contact: Contact,
        principal: Principal,
        mode: str,
        reason: str,
        legal_basis: str,
        comment: str | None,
        approval_id: uuid.UUID | None,
    ) -> tuple[DataErasureRequest, list[dict[str, Any]]]:
        """Зеркало `AdminUserService.create_erasure_request` для субъекта

        B2C/представителя вуза — то же «четыре глаза» (CRM-1902), тот же
        принцип «блокеры сразу», но короче: у контакта нет «увольнения» как
        предварительного шага (§4.8.5 проще §4.8.1-4.8.4 ровно на это).
        """
        settings = get_settings()
        await self._approvals.require(
            operation=OPERATION_ERASURE_CONTACT,
            payload={"contact_id": str(contact.id), "mode": mode},
            principal=principal,
            approval_id=approval_id,
            entity_type="contact",
            entity_id=contact.id,
        )

        contact_service = ContactService(self._session)
        blockers = await contact_service.collect_erasure_blockers(contact)
        status = ErasureStatus.BLOCKED.value if blockers else ErasureStatus.PENDING.value
        grace_until = (
            None
            if blockers
            else dt.datetime.now(dt.UTC) + dt.timedelta(days=settings.erasure_grace_days)
        )

        request = DataErasureRequest(
            subject_type=SubjectType.CONTACT.value,
            subject_id=contact.id,
            reason=reason,
            legal_basis=legal_basis,
            requested_by=principal.user_id,
            # new_spec §4.8.1: срок именно для запроса контакта (ст. 21)
            # короче, чем для сотрудника — см. docstring `erasure_contact_
            # deadline_days` в `app/core/config.py`.
            deadline_at=dt.datetime.now(dt.UTC)
            + dt.timedelta(days=settings.erasure_contact_deadline_days),
            status=status,
            blockers={"mode": mode, "comment": comment, "items": blockers},
            grace_until=grace_until,
        )
        self._session.add(request)
        await self._session.flush()

        await self._audit.record(
            AuditAction.ERASURE_REQUEST_CREATED,
            entity_type="data_erasure_request",
            entity_id=request.id,
            changes={
                "subject_id": {"old": None, "new": str(contact.id)},
                "subject_type": {"old": None, "new": "contact"},
                "mode": {"old": None, "new": mode},
                "legal_basis": {"old": None, "new": legal_basis},
            },
        )
        if blockers:
            await self._audit.record(
                AuditAction.ERASURE_REQUEST_BLOCKED,
                entity_type="data_erasure_request",
                entity_id=request.id,
                changes={"blockers": {"old": None, "new": [b["code"] for b in blockers]}},
            )
            await get_notification_service().notify_user(
                self._session,
                recipient_id=principal.user_id,
                template_code=TPL_ERASURE_BLOCKED,
                payload={"subject_type": "contact", "blockers": [b["code"] for b in blockers]},
            )
        return request, blockers

    # --- Организация (ИП): создание запроса (dop.md §11.8) -----------------

    async def create_organization_request(
        self,
        *,
        organization: Organization,
        principal: Principal,
        mode: str,
        reason: str,
        legal_basis: str,
        comment: str | None,
        approval_id: uuid.UUID | None,
    ) -> tuple[DataErasureRequest, list[dict[str, Any]]]:
        """Зеркало `create_contact_request` для ИП (`org_type=

        'individual_entrepreneur'`) — dop.md §11.8: это ПДн физлица, не
        сведения о юрлице, значит субъект удаления наравне с контактом.
        Компании/вузы отсекаются раньше, в `OrganizationService.
        collect_erasure_blockers` (`_ensure_erasure_applicable`), а не здесь
        — там же живёт единственное место, которое решает, применим ли
        152-ФЗ-режим к этому `org_type`.
        """
        settings = get_settings()
        await self._approvals.require(
            operation=OPERATION_ERASURE_ORGANIZATION,
            payload={"organization_id": str(organization.id), "mode": mode},
            principal=principal,
            approval_id=approval_id,
            entity_type="organization",
            entity_id=organization.id,
        )

        org_service = OrganizationService(self._session)
        blockers = await org_service.collect_erasure_blockers(organization)
        status = ErasureStatus.BLOCKED.value if blockers else ErasureStatus.PENDING.value
        grace_until = (
            None
            if blockers
            else dt.datetime.now(dt.UTC) + dt.timedelta(days=settings.erasure_grace_days)
        )

        request = DataErasureRequest(
            subject_type=SubjectType.ORGANIZATION.value,
            subject_id=organization.id,
            reason=reason,
            legal_basis=legal_basis,
            requested_by=principal.user_id,
            deadline_at=dt.datetime.now(dt.UTC)
            + dt.timedelta(days=settings.erasure_contact_deadline_days),
            status=status,
            blockers={"mode": mode, "comment": comment, "items": blockers},
            grace_until=grace_until,
        )
        self._session.add(request)
        await self._session.flush()

        await self._audit.record(
            AuditAction.ERASURE_REQUEST_CREATED,
            entity_type="data_erasure_request",
            entity_id=request.id,
            changes={
                "subject_id": {"old": None, "new": str(organization.id)},
                "subject_type": {"old": None, "new": "organization"},
                "mode": {"old": None, "new": mode},
                "legal_basis": {"old": None, "new": legal_basis},
            },
        )
        if blockers:
            await self._audit.record(
                AuditAction.ERASURE_REQUEST_BLOCKED,
                entity_type="data_erasure_request",
                entity_id=request.id,
                changes={"blockers": {"old": None, "new": [b["code"] for b in blockers]}},
            )
            await get_notification_service().notify_user(
                self._session,
                recipient_id=principal.user_id,
                template_code=TPL_ERASURE_BLOCKED,
                payload={
                    "subject_type": "organization",
                    "blockers": [b["code"] for b in blockers],
                },
            )
        return request, blockers

    # --- Пересчёт / отказ / восстановление ---------------------------------

    async def recheck(
        self, request: DataErasureRequest, principal: Principal
    ) -> list[dict[str, Any]]:
        """Раньше блокеры проверялись только один раз, при создании — если

        админ решал проблему (передавал дела, снимал подпись и т.д.), запрос
        оставался в `blocked` навсегда: не было ручки, которая пересчитала
        бы их заново. Возвращает оставшиеся блокеры (пусто — запрос ушёл в
        отсрочку).
        """
        if request.status != ErasureStatus.BLOCKED.value:
            raise AppError(
                ErrorCode.VALIDATION,
                "Пересчитать блокеры можно только у заблокированного запроса",
                extra={"status": request.status},
            )
        blockers = await self._collect_blockers_for(request)
        stored = dict(request.blockers or {})
        stored["items"] = blockers
        request.blockers = stored

        if blockers:
            await self._session.flush()
            await self._audit.record(
                AuditAction.ERASURE_REQUEST_BLOCKED,
                entity_type="data_erasure_request",
                entity_id=request.id,
                changes={"blockers": {"old": None, "new": [b["code"] for b in blockers]}},
            )
            return blockers

        settings = get_settings()
        request.status = ErasureStatus.APPROVED.value
        request.grace_until = dt.datetime.now(dt.UTC) + dt.timedelta(
            days=settings.erasure_grace_days
        )
        await self._session.flush()
        await self._audit.record(
            AuditAction.ERASURE_REQUEST_APPROVED,
            entity_type="data_erasure_request",
            entity_id=request.id,
            changes={"grace_until": {"old": None, "new": request.grace_until.isoformat()}},
        )
        return []

    async def reject(self, request: DataErasureRequest, *, reason: str) -> None:
        """new_spec §4.8.4 шаг 2: «система обязана позволять обоснованно

        отказать» — например, действующий договор (§4.8.5). Терминально:
        отклонённый запрос не возобновляется, субъект подаёт новый.
        """
        if request.status in (ErasureStatus.COMPLETED.value, ErasureStatus.REJECTED.value):
            raise AppError(
                ErrorCode.VALIDATION,
                "Запрос уже в терминальном статусе",
                extra={"status": request.status},
            )
        request.status = ErasureStatus.REJECTED.value
        request.rejection_reason = reason
        request.grace_until = None
        await self._session.flush()
        await self._audit.record(
            AuditAction.ERASURE_REQUEST_REJECTED,
            entity_type="data_erasure_request",
            entity_id=request.id,
            changes={"reason": {"old": None, "new": reason}},
        )

    async def restore(self, request: DataErasureRequest) -> None:
        """Кнопка «Восстановить» из new_spec §4.8.4 шаг 4 — отменяет запрос,

        пока не истекла отсрочка. Не трогает `user.status`/`contact.
        is_anonymized`: разблокировка учётной записи — отдельное действие
        (`POST /admin/users/{id}/unblock`), уже существующее и не требующее
        дублирования здесь.
        """
        if request.status not in (ErasureStatus.PENDING.value, ErasureStatus.APPROVED.value):
            raise AppError(
                ErrorCode.VALIDATION,
                "Восстановить можно только запрос в периоде отсрочки",
                extra={"status": request.status},
            )
        if request.grace_until is not None and request.grace_until <= dt.datetime.now(dt.UTC):
            raise AppError(ErrorCode.VALIDATION, "Период отсрочки уже истёк")
        request.status = ErasureStatus.REJECTED.value
        request.rejection_reason = "Восстановлено администратором в период отсрочки"
        request.grace_until = None
        await self._session.flush()
        await self._audit.record(
            AuditAction.ERASURE_REQUEST_RESTORED,
            entity_type="data_erasure_request",
            entity_id=request.id,
            changes={},
        )

    async def _collect_blockers_for(self, request: DataErasureRequest) -> list[dict[str, Any]]:
        if request.subject_type == SubjectType.USER.value:
            user = await self._session.get(User, request.subject_id)
            if user is None:
                return [{"code": "subject_missing", "detail": "Пользователь не найден", "count": 1}]
            return await AdminUserService(self._session).collect_erasure_blockers(user)
        if request.subject_type == SubjectType.ORGANIZATION.value:
            organization = await self._session.get(Organization, request.subject_id)
            if organization is None:
                return [{"code": "subject_missing", "detail": "Организация не найдена", "count": 1}]
            return await OrganizationService(self._session).collect_erasure_blockers(organization)
        contact = await self._session.get(Contact, request.subject_id)
        if contact is None:
            return [{"code": "subject_missing", "detail": "Контакт не найден", "count": 1}]
        return await ContactService(self._session).collect_erasure_blockers(contact)

    # --- Исполнение (new_spec §4.8.4 шаг 5) ---------------------------------

    async def execute(self, request: DataErasureRequest) -> dict[str, Any]:
        """Вызывается только сборщиком (`identity.tasks.

        sweep_erasure_requests`) по истечении `grace_until`, никогда
        напрямую администратором: отсрочка режима A обязательна («режим A
        ... обязательный первый шаг любого удаления», §4.8.2), и ручка
        «исполнить сейчас» подрывала бы весь смысл grace period.
        """
        if request.status not in (ErasureStatus.PENDING.value, ErasureStatus.APPROVED.value):
            raise AppError(
                ErrorCode.VALIDATION,
                "Исполнить можно только запрос в периоде отсрочки",
                extra={"status": request.status},
            )

        # Блокеры проверялись при создании/пересчёте — со страховкой:
        # что-то могло измениться за дни отсрочки (появилась новая сделка,
        # контакт что-то подписал). Молча исполнять в этом случае нельзя.
        blockers = await self._collect_blockers_for(request)
        if blockers:
            request.status = ErasureStatus.BLOCKED.value
            request.grace_until = None
            stored = dict(request.blockers or {})
            stored["items"] = blockers
            request.blockers = stored
            await self._session.flush()
            await self._audit.record(
                AuditAction.ERASURE_REQUEST_BLOCKED,
                entity_type="data_erasure_request",
                entity_id=request.id,
                changes={
                    "blockers": {"old": None, "new": [b["code"] for b in blockers]},
                    "detected_at": {"old": None, "new": "execute"},
                },
            )
            return {"executed": False, "blockers": [b["code"] for b in blockers]}

        mode = (request.blockers or {}).get("mode", "anonymize")
        if request.subject_type == SubjectType.USER.value:
            result = await self._execute_user(request, mode=mode)
        elif request.subject_type == SubjectType.ORGANIZATION.value:
            result = await self._execute_organization(request, mode=mode)
        else:
            result = await self._execute_contact(request, mode=mode)

        request.status = ErasureStatus.COMPLETED.value
        request.executed_at = dt.datetime.now(dt.UTC)
        request.grace_until = None
        await self._session.flush()
        await self._audit.record(
            AuditAction.ERASURE_EXECUTED,
            entity_type="data_erasure_request",
            entity_id=request.id,
            changes={"mode": {"old": None, "new": mode}, **result.get("changes", {})},
        )
        if request.requested_by:
            await get_notification_service().notify_user(
                self._session,
                recipient_id=request.requested_by,
                template_code=TPL_ERASURE_COMPLETED,
                payload={"subject_type": request.subject_type, "mode": mode},
            )
        return {"executed": True, "mode": mode}

    async def _execute_user(self, request: DataErasureRequest, *, mode: str) -> dict[str, Any]:
        user = await self._session.get(User, request.subject_id)
        if user is None:
            raise NotFoundError("Пользователь", request.subject_id)
        subject_display = user.effective_name
        admin_service = AdminUserService(self._session)

        if mode == "hard_delete":
            if not await admin_service.hard_delete_eligible(user):
                raise AppError(
                    ErrorCode.ERASURE_BLOCKED,
                    "Жёсткое удаление невозможно: есть зависимые записи",
                    extra={"user_id": str(user.id)},
                )
            if user.keycloak_id:
                await keycloak_client.delete_user(user.keycloak_id)
            act_file_id = await self._issue_act(
                request,
                subject_type="user",
                subject_display=subject_display,
                categories_erased=["Учётная запись целиком (ФИО, email, телефон, должность)"],
                categories_retained=[],
            )
            request.act_file_id = act_file_id
            await self._session.delete(user)
            return {"changes": {"mode": {"old": None, "new": "hard_delete"}}}

        # Режим B — обезличивание (new_spec §4.8.2).
        short_id = str(user.id)[:8]
        if user.avatar_file_id:
            avatar = await self._session.get(File, user.avatar_file_id)
            if avatar is not None and avatar.refcount > 0:
                avatar.refcount -= 1
            user.avatar_file_id = None
        if user.keycloak_id:
            await keycloak_client.delete_user(user.keycloak_id)

        user.full_name = f"Пользователь #{short_id}"
        user.display_name = None
        user.email = None
        user.phone = None
        user.position = None
        user.keycloak_id = None
        user.status = UserStatus.ANONYMIZED.value
        user.anonymized_at = dt.datetime.now(dt.UTC)
        user.version += 1
        await self._session.flush()

        act_file_id = await self._issue_act(
            request,
            subject_type="user",
            subject_display=subject_display,
            categories_erased=[
                "ФИО",
                "email",
                "телефон",
                "должность",
                "аватар",
                "учётная запись Keycloak",
            ],
            categories_retained=[_RETAINED_BUSINESS_HISTORY],
        )
        request.act_file_id = act_file_id
        return {"changes": {"mode": {"old": None, "new": "anonymize"}}}

    async def _execute_contact(self, request: DataErasureRequest, *, mode: str) -> dict[str, Any]:
        contact = await self._session.get(Contact, request.subject_id)
        if contact is None:
            raise NotFoundError("Контакт", request.subject_id)
        contact_service = ContactService(self._session)
        subject_display = f"{contact.last_name} {contact.first_name}".strip() or str(contact.id)

        if mode == "hard_delete":
            if not await contact_service.hard_delete_eligible(contact):
                raise AppError(
                    ErrorCode.ERASURE_BLOCKED,
                    "Жёсткое удаление невозможно: есть зависимые записи",
                    extra={"contact_id": str(contact.id)},
                )
            await self._notify_external_erasure("contact", contact)
            act_file_id = await self._issue_act(
                request,
                subject_type="contact",
                subject_display=subject_display,
                categories_erased=[
                    "ФИО",
                    "email",
                    "телефон",
                    "каналы связи",
                    "запись контакта целиком",
                ],
                categories_retained=[],
            )
            request.act_file_id = act_file_id
            await self._session.execute(
                ContactChannel.__table__.delete().where(ContactChannel.contact_id == contact.id)
            )
            await self._session.delete(contact)
            return {"changes": {"mode": {"old": None, "new": "hard_delete"}}}

        # Режим B: обезличивание — делегируется `ContactService.anonymize`
        # (та же реализация, которую использовал бы прямой вызов
        # администратором, не только сборщик).
        await self._notify_external_erasure("contact", contact)
        await contact_service.anonymize(contact)
        act_file_id = await self._issue_act(
            request,
            subject_type="contact",
            subject_display=subject_display,
            categories_erased=["ФИО", "email", "телефон", "каналы связи"],
            categories_retained=[_RETAINED_BUSINESS_HISTORY],
        )
        request.act_file_id = act_file_id
        return {"changes": {"mode": {"old": None, "new": "anonymize"}}}

    async def _execute_organization(
        self, request: DataErasureRequest, *, mode: str
    ) -> dict[str, Any]:
        """Тот же сценарий, что `_execute_contact` — dop.md §11.8 делает ИП

        субъектом 152-ФЗ наравне с контактом, только источник конкретики —
        `OrganizationService`, а не `ContactService`.
        """
        organization = await self._session.get(Organization, request.subject_id)
        if organization is None:
            raise NotFoundError("Организация", request.subject_id)
        org_service = OrganizationService(self._session)
        subject_display = organization.name

        if mode == "hard_delete":
            if not await org_service.hard_delete_eligible(organization):
                raise AppError(
                    ErrorCode.ERASURE_BLOCKED,
                    "Жёсткое удаление невозможно: есть зависимые записи",
                    extra={"organization_id": str(organization.id)},
                )
            await self._notify_external_erasure("organization", organization)
            act_file_id = await self._issue_act(
                request,
                subject_type="organization",
                subject_display=subject_display,
                categories_erased=[
                    "ФИО (наименование ИП)",
                    "адрес регистрации",
                    "телефон",
                    "email",
                    "запись организации целиком",
                ],
                categories_retained=[],
            )
            request.act_file_id = act_file_id
            await self._session.delete(organization)
            return {"changes": {"mode": {"old": None, "new": "hard_delete"}}}

        await self._notify_external_erasure("organization", organization)
        await org_service.anonymize(organization)
        act_file_id = await self._issue_act(
            request,
            subject_type="organization",
            subject_display=subject_display,
            categories_erased=["ФИО (наименование ИП)", "адрес регистрации", "телефон", "email"],
            categories_retained=[_RETAINED_BUSINESS_HISTORY],
        )
        request.act_file_id = act_file_id
        return {"changes": {"mode": {"old": None, "new": "anonymize"}}}

    async def _notify_external_erasure(
        self, aggregate_type: str, entity: Contact | Organization
    ) -> None:
        """new_spec §4.8.3, строка «Внешние системы»: обязанность уведомить

        об уничтожении распространяется на всех, кому передавали (ст. 21 ч.
        4 152-ФЗ) — если у субъекта есть `external_ids` (пришёл из CMS,
        синхронизирован в Bitrix, или у организации — ЕГРЮЛ/ЕГРИП), каждый
        источник получает `ERASURE_REQUESTED` через тот же outbox, которым
        уже пользуются crm/signing. Контакты и организации оба несут
        `external_ids jsonb` — общий метод, а не дублирование на каждый тип.
        """
        if not entity.external_ids:
            return
        from app.modules.integration.service import get_outbox_service

        for source_code in entity.external_ids:
            await get_outbox_service().publish(
                self._session,
                aggregate_type=aggregate_type,
                aggregate_id=entity.id,
                event_type="ERASURE_REQUESTED",
                payload={"source": source_code},
                target=source_code,
            )

    async def _issue_act(
        self,
        request: DataErasureRequest,
        *,
        subject_type: str,
        subject_display: str,
        categories_erased: list[str],
        categories_retained: list[dict[str, str]],
    ) -> uuid.UUID:
        """Акт об уничтожении ПДн (new_spec §4.8.4 шаг 6, dop.md §10.7) —

        обязательный документ, который заказчик обязан предъявить при
        проверке Роскомнадзора; хранится в S3 бессрочно (`act_file_id`,
        `ON DELETE RESTRICT`, см. docstring `DataErasureRequest`).
        """
        pdf_bytes = render_erasure_act_pdf(
            subject_type=subject_type,
            subject_display=subject_display,
            request_id=str(request.id),
            legal_basis=request.legal_basis or "не указано",
            executed_at_iso=dt.datetime.now(dt.UTC).isoformat(),
            responsible_display="система (плановое исполнение по истечении отсрочки)",
            categories_erased=categories_erased,
            categories_retained=categories_retained,
        )
        return await self._store_pdf(pdf_bytes, filename=f"erasure-act-{request.id}.pdf")

    async def _store_pdf(self, pdf_bytes: bytes, *, filename: str) -> uuid.UUID:
        """Тот же приём, что `signing.service.SignatureDocumentService.

        _store_generated_pdf` — сервер сам формирует файл, поэтому
        presigned-upload (раздел 3.7) не нужен: загружать себе самому через
        HTTP было бы избыточным кругом.
        """
        settings = get_settings()
        bucket = settings.s3_bucket_signatures
        await ensure_bucket(bucket)
        file_id = uuid7()
        storage_key = f"erasure-acts/{file_id}/{filename}"
        await upload_object_bytes(
            bucket=bucket, key=storage_key, body=pdf_bytes, content_type="application/pdf"
        )
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
        return file_id
