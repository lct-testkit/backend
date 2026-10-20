"""B4-12 (A-18): запрос временного доступа к чужой записи.

Менеджер пытается завести вуз, который уже ведёт коллега (`POST /organizations` даёт 409
CRM-1302 с `organization_id`, `GET /organizations/check-duplicate` — `accessible=false`), и
упирается в тупик: ни способа попросить доступ, ни следа в журнале. Этот модуль даёт временный
доступ (до `expires_at`), БЕЗ переноса владения — `organizations.owner_id` не меняется.
`catalog.service.organization_scope_clause_for_rules` добавляет отдельную ветку по этой таблице,
а ограничения B4-5/B4-6 применяются поверх через AND — одобренный доступ не пробивает `deny`.

Пока только `entity_type='organization'` (CHECK в модели оставлен шире на будущее). Для сделок
доступ выдаётся уже существующим участником (`POST /deals/{id}/participants`).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access_rules import load_access_rules, user_organization_allowed
from app.core.errors import AppError, ErrorCode, FieldError, NotFoundError, ValidationError
from app.core.pagination import Page, PageParams, keyset_before
from app.core.security import Principal
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.catalog.models import AccessRequest, AccessRequestStatus, Organization
from app.modules.catalog.service import OrganizationService, organization_in_scope
from app.modules.identity.models import Role, Team, User
from app.modules.notification.service import NotificationPriority, get_notification_service

#: Раздел 3 B4-12: ожидающие дольше этого считаются просроченными ЛЕНИВО, в самих запросах —
#: отдельного cron не требуется (строка остаётся в БД в статусе `pending`, просто не
#: действует и не считается «ожидающей» для лимита/списков).
_PENDING_TTL_DAYS = 14
_MAX_PENDING_PER_USER = 10
_DEFAULT_GRANT_DAYS = 30
_MAX_GRANT_DAYS = 365


def _notification_payload(request: AccessRequest) -> dict[str, str]:
    """N-1: уведомления о заявке несут её id — фронт открывает именно эту заявку
    (`/access-requests?box=…&highlight=<id>`), а не карточку вуза. `entity_*` остаются вузом."""
    return {"access_request_id": str(request.id)}


def _pending_cutoff() -> dt.datetime:
    return dt.datetime.now(dt.UTC) - dt.timedelta(days=_PENDING_TTL_DAYS)


@dataclass(slots=True)
class AccessRequestPage:
    items: list[AccessRequest]
    next_cursor: str | None


class AccessRequestService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    async def get_or_404(self, request_id: uuid.UUID) -> AccessRequest:
        request = await self._session.get(AccessRequest, request_id)
        if request is None:
            raise NotFoundError("Запрос доступа", request_id)
        return request

    # Порядок проверок в решениях: блокировка → объектное право → статус. Статус раньше права
    # отдавал бы чужому менеджеру состояние запроса (`extra.status`) по одному его id.

    async def _lock(self, request: AccessRequest) -> None:
        """Строка запроса под `FOR UPDATE` перед любым переходом статуса. Решение — переход
        `pending → …`, и без блокировки два одновременных (два одобряющих, одобрение и отмена
        автором) оба проходили `_ensure_pending` по статусу, прочитанному ДО блокировки: аудит и
        уведомление дублировались, `decided_by` затирался, а отменённый запрос мог остаться
        выданным доступом. Перечитывание после блокировки отдаёт уже актуальный статус."""
        await self._session.refresh(request, with_for_update=True)

    # --- Создание ------------------------------------------------------------------------

    async def create(
        self, principal: Principal, *, entity_id: uuid.UUID, reason: str
    ) -> AccessRequest:
        organization = await self._session.get(Organization, entity_id)
        if organization is None or organization.deleted_at is not None:
            raise NotFoundError("Организация", entity_id)

        # Q-3: вуз, видимый лишь через собственную сделку, открыт для чтения, но не для правки —
        # просить доступ к нему можно. «Уже есть» — когда вызывающий и так вправе его править.
        if await organization_in_scope(
            self._session, principal, organization
        ) and await OrganizationService(self._session).can_edit(principal, organization):
            raise ValidationError(
                "Доступ к этому вузу уже есть",
                [FieldError(field="entity_id", reason="организация уже в вашем скоупе")],
            )
        if organization.owner_id is None:
            raise ValidationError(
                "У вуза нет ответственного — заведите сделку, чтобы получить к нему доступ",
                [FieldError(field="entity_id", reason="вуз без ответственного")],
            )
        rules = await load_access_rules(self._session, principal)
        if rules is not None:
            requester = await self._session.get(User, principal.user_id)
            if requester is not None and not await user_organization_allowed(
                self._session, requester, entity_id
            ):
                raise ValidationError(
                    "Этот вуз вне ваших ограничений видимости — запрос доступа не поможет",
                    [FieldError(field="entity_id", reason="вуз вне ограничения доступа")],
                )

        pending_count = (
            await self._session.scalar(
                select(func.count(AccessRequest.id)).where(
                    AccessRequest.requester_id == principal.user_id,
                    AccessRequest.status == AccessRequestStatus.PENDING.value,
                    AccessRequest.created_at >= _pending_cutoff(),
                )
            )
        ) or 0
        if pending_count >= _MAX_PENDING_PER_USER:
            raise ValidationError(
                "Слишком много ожидающих запросов доступа",
                [
                    FieldError(
                        field="entity_id", reason=f"не более {_MAX_PENDING_PER_USER} ожидающих"
                    )
                ],
            )

        existing_pending = await self._session.scalar(
            select(AccessRequest).where(
                AccessRequest.requester_id == principal.user_id,
                AccessRequest.entity_type == "organization",
                AccessRequest.entity_id == entity_id,
                AccessRequest.status == AccessRequestStatus.PENDING.value,
            )
        )
        if existing_pending is not None:
            if existing_pending.created_at >= _pending_cutoff():
                raise AppError(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "Запрос доступа к этому вузу уже ожидает решения",
                )
            # Просрочен лениво (раздел 3): строка остаётся в БД как pending, но
            # уникальный индекс (requester_id, entity_type, entity_id) WHERE status='pending'
            # не даст вставить новую, пока старая не сменит статус — закрываем её здесь.
            existing_pending.status = AccessRequestStatus.CANCELLED.value

        request = AccessRequest(
            requester_id=principal.user_id,
            entity_type="organization",
            entity_id=entity_id,
            reason=reason,
            status=AccessRequestStatus.PENDING.value,
        )
        self._session.add(request)
        await self._session.flush()
        await self._audit.record(
            AuditAction.ACCESS_REQUESTED,
            entity_type="organization",
            entity_id=entity_id,
            changes={"reason": {"old": None, "new": reason}, "request_id": str(request.id)},
        )
        await self._notify_decision_makers(organization, request)
        return request

    async def _notify_decision_makers(
        self, organization: Organization, request: AccessRequest
    ) -> None:
        recipients: set[uuid.UUID] = set()
        if organization.owner_id is not None:
            recipients.add(organization.owner_id)
            owner = await self._session.get(User, organization.owner_id)
            if owner is not None and owner.team_id is not None:
                team = await self._session.get(Team, owner.team_id)
                if team is not None and team.head_id is not None:
                    recipients.add(team.head_id)
        for recipient_id in recipients:
            await get_notification_service().notify_user(
                self._session,
                recipient_id=recipient_id,
                template_code="ACCESS_REQUEST_CREATED",
                payload=_notification_payload(request),
                priority=NotificationPriority.NORMAL,
                entity_type="organization",
                entity_id=organization.id,
            )

    # --- Списки ---------------------------------------------------------------------------

    async def list_mine(self, principal: Principal, page: PageParams) -> AccessRequestPage:
        stmt = select(AccessRequest).where(AccessRequest.requester_id == principal.user_id)
        return await self._paged(stmt, page)

    async def list_inbox(
        self, principal: Principal, page: PageParams, *, status: str | None
    ) -> AccessRequestPage:
        """Запросы, которые ЭТОТ пользователь может решить: он — владелец вуза, либо
        руководитель команды владельца, либо ADMIN."""
        owned_orgs = select(Organization.id).where(Organization.owner_id == principal.user_id)
        led_team_member_orgs = (
            select(Organization.id)
            .join(User, User.id == Organization.owner_id)
            .join(Team, Team.id == User.team_id)
            .where(Team.head_id == principal.user_id)
        )
        conditions = [AccessRequest.entity_id.in_(owned_orgs)]
        if principal.role in (Role.HEAD.value, Role.ADMIN.value):
            conditions.append(AccessRequest.entity_id.in_(led_team_member_orgs))
        if principal.is_admin:
            stmt = select(AccessRequest)
        else:
            stmt = select(AccessRequest).where(
                AccessRequest.entity_type == "organization", or_(*conditions)
            )
        if status:
            stmt = stmt.where(AccessRequest.status == status)
            if status == AccessRequestStatus.PENDING.value:
                # Просроченный лениво запрос решить уже нельзя (`_ensure_pending`), а «ожидающими»
                # он не считается и в лимите — на решение его не предлагаем.
                stmt = stmt.where(AccessRequest.created_at >= _pending_cutoff())
        return await self._paged(stmt, page)

    async def _paged(
        self, stmt: Select[tuple[AccessRequest]], page: PageParams
    ) -> AccessRequestPage:
        ordered = stmt.order_by(AccessRequest.created_at.desc(), AccessRequest.id.desc())
        if page.decoded_cursor:
            ordered = ordered.where(
                keyset_before(AccessRequest.created_at, AccessRequest.id, page.decoded_cursor)
            )
        rows = list((await self._session.execute(ordered.limit(page.fetch_limit))).scalars().all())
        built: Page[AccessRequest] = Page.build(rows, limit=page.limit)
        return AccessRequestPage(items=list(built.items), next_cursor=built.next_cursor)

    # --- Решения ---------------------------------------------------------------------------

    async def _ensure_can_decide(self, principal: Principal, organization: Organization) -> None:
        if principal.is_admin:
            return
        if organization.owner_id == principal.user_id:
            return
        if principal.role == Role.HEAD.value and organization.owner_id is not None:
            owner = await self._session.get(User, organization.owner_id)
            if owner is not None and owner.team_id is not None:
                team = await self._session.get(Team, owner.team_id)
                if team is not None and team.head_id == principal.user_id:
                    return
        raise AppError(
            ErrorCode.FORBIDDEN,
            "Решать по этому запросу может только владелец вуза или его руководитель",
        )

    async def approve(
        self, request: AccessRequest, principal: Principal, *, days: int | None, comment: str | None
    ) -> AccessRequest:
        await self._lock(request)
        organization = await self._get_entity_or_404(request)
        await self._ensure_can_decide(principal, organization)
        self._ensure_pending(request)
        ttl_days = min(days or _DEFAULT_GRANT_DAYS, _MAX_GRANT_DAYS)
        request.status = AccessRequestStatus.APPROVED.value
        request.decided_by = principal.user_id
        request.decided_at = dt.datetime.now(dt.UTC)
        request.decision_comment = comment
        request.expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(days=ttl_days)
        await self._session.flush()
        await self._audit.record(
            AuditAction.ACCESS_GRANTED,
            entity_type="organization",
            entity_id=request.entity_id,
            changes={
                "status": {
                    "old": AccessRequestStatus.PENDING.value,
                    "new": AccessRequestStatus.APPROVED.value,
                },
                "expires_at": {"old": None, "new": request.expires_at.isoformat()},
            },
        )
        await get_notification_service().notify_user(
            self._session,
            recipient_id=request.requester_id,
            template_code="ACCESS_REQUEST_APPROVED",
            payload=_notification_payload(request),
            priority=NotificationPriority.NORMAL,
            entity_type="organization",
            entity_id=request.entity_id,
        )
        return request

    async def reject(
        self, request: AccessRequest, principal: Principal, *, comment: str
    ) -> AccessRequest:
        await self._lock(request)
        organization = await self._get_entity_or_404(request)
        await self._ensure_can_decide(principal, organization)
        self._ensure_pending(request)
        request.status = AccessRequestStatus.REJECTED.value
        request.decided_by = principal.user_id
        request.decided_at = dt.datetime.now(dt.UTC)
        request.decision_comment = comment
        await self._session.flush()
        await self._audit.record(
            AuditAction.ACCESS_REJECTED,
            entity_type="organization",
            entity_id=request.entity_id,
            changes={
                "status": {
                    "old": AccessRequestStatus.PENDING.value,
                    "new": AccessRequestStatus.REJECTED.value,
                },
                "comment": {"old": None, "new": comment},
            },
        )
        await get_notification_service().notify_user(
            self._session,
            recipient_id=request.requester_id,
            template_code="ACCESS_REQUEST_REJECTED",
            payload=_notification_payload(request),
            priority=NotificationPriority.NORMAL,
            entity_type="organization",
            entity_id=request.entity_id,
        )
        return request

    async def revoke(self, request: AccessRequest, principal: Principal) -> AccessRequest:
        await self._lock(request)
        organization = await self._get_entity_or_404(request)
        await self._ensure_can_decide(principal, organization)
        if request.status != AccessRequestStatus.APPROVED.value:
            raise AppError(
                ErrorCode.VALIDATION, "Отозвать можно только одобренный и действующий доступ"
            )
        request.status = AccessRequestStatus.REVOKED.value
        await self._session.flush()
        await self._audit.record(
            AuditAction.ACCESS_REVOKED,
            entity_type="organization",
            entity_id=request.entity_id,
            changes={
                "status": {
                    "old": AccessRequestStatus.APPROVED.value,
                    "new": AccessRequestStatus.REVOKED.value,
                }
            },
        )
        return request

    async def cancel(self, request: AccessRequest, principal: Principal) -> AccessRequest:
        if request.requester_id != principal.user_id:
            raise AppError(ErrorCode.FORBIDDEN, "Отменить можно только свой запрос")
        await self._lock(request)
        self._ensure_pending(request)
        request.status = AccessRequestStatus.CANCELLED.value
        await self._session.flush()
        return request

    def _ensure_pending(self, request: AccessRequest) -> None:
        if (
            request.status != AccessRequestStatus.PENDING.value
            or request.created_at < _pending_cutoff()
        ):
            raise AppError(
                ErrorCode.VALIDATION,
                "Решение возможно только по ожидающему запросу",
                extra={"status": request.status},
            )

    async def _get_entity_or_404(self, request: AccessRequest) -> Organization:
        organization = await self._session.get(Organization, request.entity_id)
        if organization is None:
            raise NotFoundError("Организация", request.entity_id)
        return organization
