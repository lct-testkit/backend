"""Администрирование пользователей (раздел 6.2, new_spec §4.1, 4.4–4.8).

Здесь собраны сценарии, у которых последствия важнее самого изменения:
создание учётки (SAGA с Keycloak), смена роли (эпоха прав), блокировка
(завершение сессий), сброс пароля (аннулирование подписей), увольнение
(передача дел) и запрос на удаление (блокеры 152-ФЗ).

Правила, общие для всего файла:
  * сначала IdP, потом локальная запись — иначе остаётся запись-сирота;
  * аудит пишется в той же транзакции, что и изменение;
  * пароли и одноразовые токены не попадают ни в логи, ни в аудит;
  * операции над другим модулем идут через его сервисный интерфейс.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

import structlog
from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import invalidate_principal
from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode, FieldError, NotFoundError, VersionConflictError
from app.core.masking import mask_email
from app.core.security import Principal
from app.modules.admin.models import AdminApproval
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService, diff_changes
from app.modules.crm.service import DealService, UserWorkload, get_ownership_service
from app.modules.identity.keycloak import keycloak_client
from app.modules.identity.models import (
    DataErasureRequest,
    ErasureStatus,
    Role,
    SecurityEventType,
    Severity,
    SubjectType,
    User,
    UserStatus,
)
from app.modules.identity.service import IdentityService
from app.modules.identity.session_store import session_store
from app.modules.notification.service import (
    TPL_ACCOUNT_BLOCKED,
    TPL_ACCOUNT_UNBLOCKED,
    TPL_ERASURE_BLOCKED,
    TPL_INVITE_EXPIRED,
    TPL_OFFBOARD_SUCCESSOR,
    TPL_PASSWORD_RESET,
    TPL_ROLE_CHANGED,
    NotificationPriority,
    get_notification_service,
)
from app.modules.signing.service import (
    VOID_REASON_KEY_COMPROMISED,
    get_signing_service,
)

logger = structlog.get_logger(__name__)

ROLE_NAMES = [role.value for role in Role]

# Обязательные действия Keycloak при заведении учётки (new_spec §4.1 шаг 2).
INVITE_REQUIRED_ACTIONS = ["UPDATE_PASSWORD", "VERIFY_EMAIL"]
TOTP_ACTION = "CONFIGURE_TOTP"

# `users.status_reason` учётки, отключённой за непринятое приглашение.
INVITE_EXPIRED_REASON = "invite_expired"

OPERATION_CREATE_ADMIN = "user.create_admin"
OPERATION_ERASURE = "user.erasure"


@dataclass(slots=True)
class UserFilters:
    role: str | None = None
    team_id: uuid.UUID | None = None
    status: str | None = None
    manager_id: uuid.UUID | None = None
    region_id: uuid.UUID | None = None
    q: str | None = None


class ApprovalService:
    """Принцип «четырёх глаз» для необратимых операций (CRM-1902)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    @staticmethod
    def request_hash(operation: str, payload: dict[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(f"{operation}|{canonical}".encode()).hexdigest()

    async def require(
        self,
        *,
        operation: str,
        payload: dict[str, Any],
        principal: Principal,
        approval_id: uuid.UUID | None,
        entity_type: str | None = None,
        entity_id: uuid.UUID | None = None,
    ) -> AdminApproval:
        """Возвращает подтверждение или создаёт заявку и отвечает CRM-1902.

        Подтверждается конкретный набор параметров: хэш payload'а входит в
        заявку, поэтому «подтвердили одно — выполнили другое» невозможно.
        """
        settings = get_settings()
        digest = self.request_hash(operation, payload)

        if approval_id is not None:
            approval = (
                await self._session.execute(
                    select(AdminApproval).where(AdminApproval.id == approval_id)
                )
            ).scalar_one_or_none()
            if approval is None:
                raise NotFoundError("Подтверждение", approval_id)
            if approval.operation != operation or approval.request_hash != digest:
                raise AppError(
                    ErrorCode.SECOND_ADMIN_REQUIRED,
                    "Подтверждение выдано для других параметров операции",
                )
            if approval.status != "approved":
                raise AppError(
                    ErrorCode.SECOND_ADMIN_REQUIRED,
                    "Заявка ещё не подтверждена вторым администратором",
                    extra={"approval_id": str(approval.id), "status": approval.status},
                )
            if approval.expires_at <= dt.datetime.now(dt.UTC):
                approval.status = "expired"
                await self._session.flush()
                raise AppError(ErrorCode.SECOND_ADMIN_REQUIRED, "Срок действия подтверждения истёк")
            if approval.approved_by == principal.user_id:
                # Тот же администратор не может быть и инициатором, и
                # подтверждающим: иначе это не «четыре глаза».
                raise AppError(
                    ErrorCode.SECOND_ADMIN_REQUIRED,
                    "Подтверждение должен выдать другой администратор",
                )
            approval.status = "consumed"
            await self._session.flush()
            return approval

        approval = AdminApproval(
            operation=operation,
            request_hash=digest,
            payload=payload,
            entity_type=entity_type,
            entity_id=entity_id,
            requested_by=principal.user_id,
            expires_at=dt.datetime.now(dt.UTC)
            + dt.timedelta(seconds=settings.admin_approval_ttl_seconds),
        )
        self._session.add(approval)
        await self._session.flush()
        await self._audit.record(
            AuditAction.ADMIN_APPROVAL_REQUESTED,
            entity_type="admin_approval",
            entity_id=approval.id,
            changes={"operation": {"old": None, "new": operation}},
        )
        # `core.db.get_db_session` откатывает ВСЮ транзакцию на любом
        # исключении (раздел 1, «требование атомарности») — правильно для
        # обычных ошибок, но `raise` ниже штатный исход («нужен второй
        # администратор»), а не сбой. Без явного commit здесь `approval`
        # откатывался бы вместе с ответом: `approval_id`, отданный клиенту,
        # указывал бы на несуществующую строку, и подтвердить операцию было
        # бы структурно невозможно ни для одного вызова (включая создание
        # первого ADMIN и любой запрос на обезличивание) — сам механизм
        # «четырёх глаз» из CRM-1902 не работал бы ни разу. Тот же пробел уже
        # был закрыт этим приёмом в `integration.cms`/`public_router`
        # (sprint9-integration-implementation.md); здесь — тот самый
        # «пока не исправленный» случай, на который эти комментарии ссылались.
        await self._session.commit()
        raise AppError(
            ErrorCode.SECOND_ADMIN_REQUIRED,
            "Операция требует подтверждения вторым администратором",
            extra={"approval_id": str(approval.id), "operation": operation},
        )

    async def approve(self, approval_id: uuid.UUID, principal: Principal) -> AdminApproval:
        approval = (
            await self._session.execute(
                select(AdminApproval).where(AdminApproval.id == approval_id)
            )
        ).scalar_one_or_none()
        if approval is None:
            raise NotFoundError("Подтверждение", approval_id)
        if approval.status != "pending":
            raise AppError(
                ErrorCode.VALIDATION,
                "Заявка уже обработана",
                extra={"status": approval.status},
            )
        if approval.requested_by == principal.user_id:
            raise AppError(
                ErrorCode.SECOND_ADMIN_REQUIRED,
                "Инициатор не может подтвердить собственную заявку",
            )
        if approval.expires_at <= dt.datetime.now(dt.UTC):
            approval.status = "expired"
            await self._session.flush()
            raise AppError(ErrorCode.SECOND_ADMIN_REQUIRED, "Срок действия заявки истёк")

        approval.status = "approved"
        approval.approved_by = principal.user_id
        approval.approved_at = dt.datetime.now(dt.UTC)
        await self._session.flush()
        await self._audit.record(
            AuditAction.ADMIN_APPROVAL_GRANTED,
            entity_type="admin_approval",
            entity_id=approval.id,
            changes={"operation": {"old": None, "new": approval.operation}},
        )
        return approval

    async def reject(
        self, approval_id: uuid.UUID, principal: Principal, *, reason: str | None
    ) -> AdminApproval:
        approval = (
            await self._session.execute(
                select(AdminApproval).where(AdminApproval.id == approval_id)
            )
        ).scalar_one_or_none()
        if approval is None:
            raise NotFoundError("Подтверждение", approval_id)
        if approval.status not in ("pending", "approved"):
            raise AppError(ErrorCode.VALIDATION, "Заявка уже обработана")
        approval.status = "rejected"
        approval.approved_by = principal.user_id
        approval.approved_at = dt.datetime.now(dt.UTC)
        approval.reason = reason
        await self._session.flush()
        await self._audit.record(
            AuditAction.ADMIN_APPROVAL_REJECTED,
            entity_type="admin_approval",
            entity_id=approval.id,
            changes={"reason": {"old": None, "new": reason}},
        )
        return approval


class AdminUserService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)
        self._identity = IdentityService(session)
        self._approvals = ApprovalService(session)

    # --- Чтение ----------------------------------------------------------

    def list_query(self, filters: UserFilters) -> Select[tuple[User]]:
        stmt = select(User).where(User.deleted_at.is_(None))
        if filters.role:
            stmt = stmt.where(User.role == filters.role)
        if filters.status:
            stmt = stmt.where(User.status == filters.status)
        if filters.team_id:
            stmt = stmt.where(User.team_id == filters.team_id)
        if filters.manager_id:
            stmt = stmt.where(User.manager_id == filters.manager_id)
        if filters.region_id:
            # Регион живёт на команде: фильтр по нему — это фильтр по дереву команд.
            from app.modules.identity.models import Team

            stmt = stmt.where(
                User.team_id.in_(select(Team.id).where(Team.region_id == filters.region_id))
            )
        if filters.q:
            pattern = f"%{filters.q.strip().lower()}%"
            stmt = stmt.where(
                or_(
                    func.lower(User.full_name).like(pattern),
                    func.lower(User.email).like(pattern),
                    func.lower(func.coalesce(User.display_name, "")).like(pattern),
                )
            )
        return stmt

    async def get_or_404(self, user_id: uuid.UUID) -> User:
        user = await self._identity.get_by_id(user_id)
        if user is None or user.deleted_at is not None:
            raise NotFoundError("Пользователь", user_id)
        return user

    # --- Создание --------------------------------------------------------

    async def create_user(
        self,
        *,
        principal: Principal,
        full_name: str,
        email: str,
        role: str,
        team_id: uuid.UUID | None,
        manager_id: uuid.UUID | None,
        position: str | None,
        require_totp: bool,
        approval_id: uuid.UUID | None,
        base_url: str,
    ) -> tuple[User, str, dt.datetime, bool]:
        """Создаёт учётку: сначала в Keycloak, затем локально.

        Возвращает пользователя, одноразовый токен приглашения, срок его
        действия и признак того, ушло ли письмо (в закрытом контуре SMTP
        может не быть — тогда ссылку передаёт администратор).
        """
        settings = get_settings()
        email = email.strip().lower()

        existing = await self._identity.get_by_email(email)
        if existing is not None:
            raise AppError(
                ErrorCode.DUPLICATE,
                "Пользователь с таким email уже существует",
                extra={"user_id": str(existing.id), "status": existing.status},
            )

        await self._validate_team_and_manager(role=role, team_id=team_id, manager_id=manager_id)

        if role == Role.ADMIN.value:
            # Создание администратора — операция «четырёх глаз» (§4.1). Заявка
            # открывается только после проверок выше: иначе второй администратор
            # подтверждал бы операцию, повтор которой заведомо упадёт.
            await self._approvals.require(
                operation=OPERATION_CREATE_ADMIN,
                payload={"email": email, "role": role, "full_name": full_name},
                principal=principal,
                approval_id=approval_id,
                entity_type="user",
            )

        required_actions = list(INVITE_REQUIRED_ACTIONS)
        if require_totp or role in (Role.ADMIN.value, Role.HEAD.value):
            # Для привилегированных ролей TOTP обязателен (new_spec §4.4.C).
            required_actions.append(TOTP_ACTION)

        keycloak_id = await keycloak_client.create_user(
            email=email,
            full_name=full_name,
            required_actions=required_actions,
            attributes={"perm_epoch": 1},
        )

        try:
            await keycloak_client.set_realm_role(keycloak_id, role=role, known_roles=ROLE_NAMES)

            user = User(
                keycloak_id=keycloak_id,
                email=email,
                full_name=full_name,
                position=position,
                role=role,
                team_id=team_id,
                manager_id=manager_id,
                status=UserStatus.INVITED.value,
                must_change_password=True,
            )
            self._session.add(user)
            await self._session.flush()

            token = await self._identity.issue_invite(user, created_by=principal.user_id)
            email_sent = await keycloak_client.execute_actions_email(
                keycloak_id,
                required_actions,
                lifespan_seconds=settings.invite_ttl_hours * 3600,
            )

            await self._audit.record(
                AuditAction.USER_CREATED,
                entity_type="user",
                entity_id=user.id,
                changes={
                    "email": {"old": None, "new": email},
                    "role": {"old": None, "new": role},
                    "team_id": {"old": None, "new": str(team_id) if team_id else None},
                    "require_totp": {"old": None, "new": TOTP_ACTION in required_actions},
                },
            )
        except Exception:
            # Компенсация SAGA: локальной записи нет, значит и в Keycloak
            # пользователя быть не должно.
            logger.warning("user_create_rollback", keycloak_id=keycloak_id)
            try:
                await keycloak_client.delete_user(keycloak_id)
            except Exception:  # noqa: BLE001 — исходная ошибка важнее
                logger.exception("user_create_rollback_failed", keycloak_id=keycloak_id)
            raise

        expires_at = dt.datetime.now(dt.UTC) + dt.timedelta(hours=settings.invite_ttl_hours)
        invite_url = f"{base_url.rstrip('/')}/invite/{token}"
        return user, invite_url, expires_at, email_sent

    async def _validate_team_and_manager(
        self, *, role: str, team_id: uuid.UUID | None, manager_id: uuid.UUID | None
    ) -> None:
        from app.modules.identity.models import Team

        if team_id is not None:
            team = (
                await self._session.execute(select(Team).where(Team.id == team_id))
            ).scalar_one_or_none()
            if team is None or team.deleted_at is not None:
                raise NotFoundError("Команда", team_id)
        if manager_id is not None:
            manager = await self._identity.get_by_id(manager_id)
            if manager is None or manager.deleted_at is not None:
                raise NotFoundError("Руководитель", manager_id)
        if role == Role.HEAD.value and team_id is None:
            # «Повышение до HEAD без команды бессмысленно» (new_spec §4.6).
            raise AppError(
                ErrorCode.VALIDATION,
                "Для роли HEAD нужно указать команду, которую он возглавляет",
            )

    # --- Изменение -------------------------------------------------------

    async def patch_user(
        self,
        *,
        user: User,
        principal: Principal,
        expected_version: int,
        updates: dict[str, Any],
    ) -> User:
        if user.version != expected_version:
            raise VersionConflictError(
                user.version,
                {"role": user.role, "status": user.status, "team_id": str(user.team_id or "")},
            )

        before = {
            "role": user.role,
            "team_id": str(user.team_id) if user.team_id else None,
            "manager_id": str(user.manager_id) if user.manager_id else None,
            "status": user.status,
            "display_name": user.display_name,
            "phone": user.phone,
            "position": user.position,
            "locale": user.locale,
            "timezone": user.timezone,
        }

        new_role = updates.get("role")
        if new_role and new_role != user.role:
            await self._identity.ensure_not_last_admin(user)
        if updates.get("status") and user.status in (
            UserStatus.TERMINATED.value,
            UserStatus.ANONYMIZED.value,
        ):
            raise AppError(
                ErrorCode.VALIDATION,
                "Учётная запись уволенного или обезличенного пользователя не восстанавливается",
            )
        if updates.get("status") and user.status == UserStatus.BLOCKED.value:
            # new_spec §4.5: разблокировка — не правка поля, а процедура со
            # своими последствиями (Keycloak enabled=true, снятие
            # owner_unavailable у сделок, уведомление, USER_UNBLOCKED вместо
            # USER_UPDATED). Без этой проверки PATCH со `status: "active"`
            # молча переводил бы локальную запись в active, оставляя учётку
            # выключенной в Keycloak и сделки — помеченными недоступными.
            raise AppError(
                ErrorCode.VALIDATION,
                "Разблокировать можно только через POST /admin/users/{id}/unblock",
            )
        new_status = updates.get("status")
        if new_status is not None and new_status != user.status:
            # Активация — не правка поля: `activated_at`, `USER_ACTIVATED` и погашение
            # приглашения происходят при первом входе пользователя
            # (`IdentityService.provision_from_claims`). Перевод `invited → active`
            # отсюда обходил бы вход и оставлял учётку без `activated_at`.
            reason = (
                "активация происходит при первом входе пользователя"
                if new_status == UserStatus.ACTIVE.value
                else "вернуть учётную запись в статус invited нельзя"
            )
            raise AppError(
                ErrorCode.VALIDATION,
                f"Статус не меняется правкой поля: {reason}",
                errors=[FieldError(field="status", reason=reason)],
            )

        target_role = new_role or user.role
        target_team = updates.get("team_id", user.team_id)
        await self._validate_team_and_manager(
            role=target_role,
            team_id=target_team,
            manager_id=updates.get("manager_id", user.manager_id),
        )

        for field_name, value in updates.items():
            setattr(user, field_name, value)
        user.version += 1
        await self._session.flush()

        if new_role and new_role != before["role"]:
            # Keycloak — источник истины: роль должна измениться и там,
            # иначе следующий вход вернёт старую.
            if user.keycloak_id:
                await keycloak_client.set_realm_role(
                    user.keycloak_id, role=new_role, known_roles=ROLE_NAMES
                )
            await self._identity.bump_perm_epoch(user)
            if user.keycloak_id:
                await keycloak_client.set_attribute(user.keycloak_id, "perm_epoch", user.perm_epoch)
            await get_notification_service().notify_user(
                self._session,
                recipient_id=user.id,
                template_code=TPL_ROLE_CHANGED,
                payload={"old_role": before["role"], "new_role": new_role},
            )

        after = {
            "role": user.role,
            "team_id": str(user.team_id) if user.team_id else None,
            "manager_id": str(user.manager_id) if user.manager_id else None,
            "status": user.status,
            "display_name": user.display_name,
            "phone": user.phone,
            "position": user.position,
            "locale": user.locale,
            "timezone": user.timezone,
        }
        changes = diff_changes(before, after)
        await invalidate_principal(user.id, keycloak_id=user.keycloak_id)
        await self._audit.record(
            AuditAction.USER_ROLE_CHANGED if new_role else AuditAction.USER_UPDATED,
            entity_type="user",
            entity_id=user.id,
            changes=changes,
        )
        return user

    # --- Блокировка ------------------------------------------------------

    async def block(
        self, *, user: User, principal: Principal, reason: str, auto_unblock_at: dt.datetime | None
    ) -> int:
        if user.id == principal.user_id:
            raise AppError(ErrorCode.VALIDATION, "Нельзя заблокировать собственную учётную запись")
        await self._identity.ensure_not_last_admin(user)

        old_status = user.status
        user.status = UserStatus.BLOCKED.value
        user.status_reason = reason
        user.blocked_at = dt.datetime.now(dt.UTC)
        user.auto_unblock_at = _utc(auto_unblock_at)
        user.version += 1
        await self._session.flush()

        if user.keycloak_id:
            await keycloak_client.set_enabled(user.keycloak_id, enabled=False)
            await keycloak_client.logout_all_sessions(user.keycloak_id)
        terminated = await session_store.delete_all_for_user(user.id)
        await invalidate_principal(user.id, keycloak_id=user.keycloak_id)

        # Сделки не переназначаются, но помечаются как «требуют внимания».
        await get_ownership_service().mark_owner_unavailable(
            self._session, user.id, unavailable=True
        )

        await self._identity.record_security_event(
            SecurityEventType.PERMISSION_DENIED,
            user_id=user.id,
            severity=Severity.WARNING,
            details={"event": "account_blocked", "auto_unblock_at": _iso(auto_unblock_at)},
        )
        await get_notification_service().notify_user(
            self._session,
            recipient_id=user.id,
            template_code=TPL_ACCOUNT_BLOCKED,
            priority=NotificationPriority.HIGH,
            payload={"reason": reason},
        )
        await self._audit.record(
            AuditAction.USER_BLOCKED,
            entity_type="user",
            entity_id=user.id,
            changes={
                "status": {"old": old_status, "new": user.status},
                "reason": {"old": None, "new": reason},
                "auto_unblock_at": {"old": None, "new": _iso(auto_unblock_at)},
                "sessions_terminated": {"old": None, "new": terminated},
            },
        )
        return terminated

    async def unblock(self, *, user: User, reason: str | None) -> None:
        if user.status != UserStatus.BLOCKED.value:
            raise AppError(
                ErrorCode.VALIDATION,
                "Разблокировать можно только заблокированную учётную запись",
                extra={"status": user.status},
            )
        user.status = UserStatus.ACTIVE.value
        user.status_reason = reason
        user.blocked_at = None
        user.auto_unblock_at = None
        user.version += 1
        await self._session.flush()

        if user.keycloak_id:
            await keycloak_client.set_enabled(user.keycloak_id, enabled=True)
        await invalidate_principal(user.id, keycloak_id=user.keycloak_id)
        await get_ownership_service().mark_owner_unavailable(
            self._session, user.id, unavailable=False
        )
        await get_notification_service().notify_user(
            self._session,
            recipient_id=user.id,
            template_code=TPL_ACCOUNT_UNBLOCKED,
            payload={"reason": reason},
        )
        await self._audit.record(
            AuditAction.USER_UNBLOCKED,
            entity_type="user",
            entity_id=user.id,
            changes={
                "status": {"old": UserStatus.BLOCKED.value, "new": user.status},
                **({"reason": {"old": None, "new": reason}} if reason else {}),
            },
        )

    async def expire_invite(self, user: User) -> bool:
        """new_spec §4.1: не вошёл по приглашению за 30 дней — `INVITE_EXPIRED`.

        Учётка отключается (`blocked` с причиной `invite_expired`, в Keycloak —
        `enabled=false`), неиспользованные ссылки отзываются, администраторам
        уходит уведомление. Вернуть её можно повторным приглашением
        (`POST /admin/users/{id}/invite`). Сначала IdP, потом локальная запись:
        если Keycloak недоступен, учётка остаётся `invited` до следующего тика.
        """
        if user.status != UserStatus.INVITED.value or user.last_login_at is not None:
            return False  # успел войти, пока задача выбирала кандидатов
        if user.keycloak_id:
            await keycloak_client.set_enabled(user.keycloak_id, enabled=False)
        user.status = UserStatus.BLOCKED.value
        user.status_reason = INVITE_EXPIRED_REASON
        user.blocked_at = dt.datetime.now(dt.UTC)
        user.version += 1
        await self._session.flush()
        await self._identity.revoke_invites(user.id)
        await invalidate_principal(user.id, keycloak_id=user.keycloak_id)
        await self._audit.record(
            AuditAction.USER_INVITE_EXPIRED,
            entity_type="user",
            entity_id=user.id,
            changes={
                "status": {"old": UserStatus.INVITED.value, "new": user.status},
                "reason": {"old": None, "new": INVITE_EXPIRED_REASON},
            },
        )
        admin_ids = (
            (
                await self._session.execute(
                    select(User.id).where(
                        User.role == Role.ADMIN.value,
                        User.status == UserStatus.ACTIVE.value,
                        User.deleted_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        for admin_id in admin_ids:
            await get_notification_service().notify_user(
                self._session,
                recipient_id=admin_id,
                template_code=TPL_INVITE_EXPIRED,
                entity_type="user",
                entity_id=user.id,
                payload={"full_name": user.full_name, "email": mask_email(user.email)},
            )
        return True

    async def reopen_expired_invite(self, user: User) -> None:
        """Повторное приглашение снимает `INVITE_EXPIRED`: учётка снова `invited`."""
        if user.keycloak_id:
            await keycloak_client.set_enabled(user.keycloak_id, enabled=True)
        user.status = UserStatus.INVITED.value
        user.status_reason = None
        user.blocked_at = None
        user.version += 1
        await self._session.flush()
        await invalidate_principal(user.id, keycloak_id=user.keycloak_id)

    # --- Сброс пароля ----------------------------------------------------

    async def reset_password(
        self, *, user: User, reason: str, suspect_compromise: bool
    ) -> dict[str, int]:
        """Принудительный сброс: все сессии завершаются без исключений."""
        if user.keycloak_id:
            await keycloak_client.set_required_actions(
                user.keycloak_id, INVITE_REQUIRED_ACTIONS[:1]
            )
            await keycloak_client.execute_actions_email(
                user.keycloak_id,
                INVITE_REQUIRED_ACTIONS[:1],
                lifespan_seconds=900,
            )
            await keycloak_client.logout_all_sessions(user.keycloak_id)

        user.must_change_password = True
        await self._session.flush()

        terminated = await session_store.delete_all_for_user(user.id)
        await invalidate_principal(user.id, keycloak_id=user.keycloak_id)

        signing = get_signing_service()
        voided = await signing.void_pending_for_user(
            self._session, user.id, reason=VOID_REASON_KEY_COMPROMISED
        )
        disputed = 0
        if suspect_compromise:
            disputed = await signing.mark_disputed_since(
                self._session,
                user.id,
                since=dt.datetime.now(dt.UTC) - dt.timedelta(hours=24),
            )
            await self._identity.record_security_event(
                SecurityEventType.SIGNATURE_KEY_COMPROMISED,
                user_id=user.id,
                severity=Severity.CRITICAL,
                details={"disputed_signatures": disputed},
            )

        await self._identity.record_security_event(
            SecurityEventType.PASSWORD_RESET,
            user_id=user.id,
            severity=Severity.WARNING,
            details={"reason": reason, "sessions_terminated": terminated},
        )
        await get_notification_service().notify_user(
            self._session,
            recipient_id=user.id,
            template_code=TPL_PASSWORD_RESET,
            priority=NotificationPriority.HIGH,
        )
        await self._audit.record(
            AuditAction.PASSWORD_RESET,
            entity_type="user",
            entity_id=user.id,
            changes={
                "reason": {"old": None, "new": reason},
                "sessions_terminated": {"old": None, "new": terminated},
                "signature_requests_voided": {"old": None, "new": voided},
                "signatures_disputed": {"old": None, "new": disputed},
            },
        )
        return {
            "sessions_terminated": terminated,
            "signature_requests_voided": voided,
            "signatures_disputed": disputed,
        }

    # --- Увольнение ------------------------------------------------------

    async def offboard_preview(self, user: User) -> tuple[UserWorkload, int]:
        workload = await get_ownership_service().collect_workload(self._session, user.id)
        pending_signatures = await get_signing_service().count_pending_requests(
            self._session, user.id
        )
        return workload, pending_signatures

    async def offboard_confirm(
        self,
        *,
        user: User,
        principal: Principal,
        successor_id: uuid.UUID,
        reason: str,
        deal_successors: dict[uuid.UUID, uuid.UUID] | None = None,
    ) -> dict[str, Any]:
        await self._ensure_successor(user, successor_id)
        await self._identity.ensure_not_last_admin(user)

        # Сделки с назначенным преемником уходят первыми обычным переназначением
        # сделки; `reassign_all` потом забирает у увольняемого всё, что осталось.
        individually = await self._reassign_individually(
            user, principal, deal_successors or {}, reason=reason
        )
        bulk = await get_ownership_service().reassign_all(
            self._session, user.id, successor_id=successor_id, reason=reason
        )
        reassigned = [*individually, *bulk]
        signatures = await get_signing_service().reassign_pending(
            self._session, user.id, successor_id=successor_id
        )

        user.status = UserStatus.TERMINATED.value
        user.status_reason = reason
        user.version += 1
        await self._session.flush()

        if user.keycloak_id:
            await keycloak_client.set_enabled(user.keycloak_id, enabled=False)
            await keycloak_client.logout_all_sessions(user.keycloak_id)
        terminated = await session_store.delete_all_for_user(user.id)
        await invalidate_principal(user.id, keycloak_id=user.keycloak_id)
        await self._identity.revoke_invites(user.id)

        await get_notification_service().notify_user(
            self._session,
            recipient_id=successor_id,
            template_code=TPL_OFFBOARD_SUCCESSOR,
            priority=NotificationPriority.HIGH,
            payload={"deals": [str(d) for d in bulk], "from_user": str(user.id)},
        )
        await self._audit.record(
            AuditAction.USER_OFFBOARDED,
            entity_type="user",
            entity_id=user.id,
            changes={
                "successor_id": {"old": None, "new": str(successor_id)},
                "reason": {"old": None, "new": reason},
                "deals_reassigned": {"old": None, "new": len(reassigned)},
                "signature_requests_reassigned": {"old": None, "new": signatures},
            },
        )
        await self._audit.record(
            AuditAction.USER_TERMINATED,
            entity_type="user",
            entity_id=user.id,
            changes={"status": {"old": UserStatus.ACTIVE.value, "new": user.status}},
        )
        return {
            "reassigned_deals": reassigned,
            "signature_requests_reassigned": signatures,
            "sessions_terminated": terminated,
        }

    async def _ensure_successor(self, user: User, successor_id: uuid.UUID) -> User:
        if successor_id == user.id:
            raise AppError(ErrorCode.VALIDATION, "Преемник не может совпадать с увольняемым")
        successor = await self.get_or_404(successor_id)
        if successor.status != UserStatus.ACTIVE.value:
            raise AppError(
                ErrorCode.VALIDATION,
                "Преемник должен быть активным пользователем",
                extra={"status": successor.status, "user_id": str(successor_id)},
            )
        return successor

    async def _reassign_individually(
        self,
        user: User,
        principal: Principal,
        deal_successors: dict[uuid.UUID, uuid.UUID],
        *,
        reason: str,
    ) -> list[uuid.UUID]:
        """Переназначает названные сделки их преемникам. Любая ошибка (чужая
        сделка, неактивный преемник) откатывает весь увольнение целиком."""
        if not deal_successors:
            return []
        deals = DealService(self._session)
        moved: list[uuid.UUID] = []
        for deal_id, deal_successor_id in deal_successors.items():
            await self._ensure_successor(user, deal_successor_id)
            deal = await deals.get_or_404(deal_id, principal)
            if deal.owner_id != user.id:
                raise AppError(
                    ErrorCode.VALIDATION,
                    "Сделка не принадлежит увольняемому сотруднику",
                    errors=[FieldError(field="deal_successors", reason=str(deal_id))],
                )
            await deals.reassign(
                deal,
                principal,
                owner_id=deal_successor_id,
                reason=reason,
                expected_version=deal.version,
            )
            moved.append(deal_id)
        return moved

    # --- 152-ФЗ: запрос на удаление --------------------------------------

    async def create_erasure_request(
        self,
        *,
        user: User,
        principal: Principal,
        mode: str,
        reason: str,
        legal_basis: str,
        comment: str | None,
        approval_id: uuid.UUID | None,
    ) -> tuple[DataErasureRequest, list[dict[str, Any]]]:
        """Создаёт запрос и сразу отдаёт блокеры (new_spec §4.8.4 шаг 2)."""
        settings = get_settings()
        if user.id == principal.user_id:
            # «Не позволять удалить собственную учётку администратору» (§4.8.6).
            raise AppError(
                ErrorCode.VALIDATION, "Нельзя запросить удаление собственной учётной записи"
            )

        await self._approvals.require(
            operation=OPERATION_ERASURE,
            payload={"user_id": str(user.id), "mode": mode},
            principal=principal,
            approval_id=approval_id,
            entity_type="user",
            entity_id=user.id,
        )

        blockers = await self.collect_erasure_blockers(user)
        status = ErasureStatus.BLOCKED.value if blockers else ErasureStatus.PENDING.value
        # new_spec §4.8.4 шаг 4: отсрочка режима A начинается сразу, как
        # только блокеров нет — не при отдельном «подтверждении», которого
        # раньше не существовало. Раньше это поле вычислялось только в
        # ответе роутера и никуда не сохранялось: сборщику (`identity.tasks.
        # sweep_erasure_requests`) было не по чему выбирать просроченные
        # запросы.
        grace_until = (
            None
            if blockers
            else dt.datetime.now(dt.UTC) + dt.timedelta(days=settings.erasure_grace_days)
        )

        request = DataErasureRequest(
            subject_type=SubjectType.USER.value,
            subject_id=user.id,
            reason=reason,
            legal_basis=legal_basis,
            requested_by=principal.user_id,
            deadline_at=dt.datetime.now(dt.UTC)
            + dt.timedelta(days=settings.erasure_subject_deadline_days),
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
                "subject_id": {"old": None, "new": str(user.id)},
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
                payload={"subject_type": "user", "blockers": [b["code"] for b in blockers]},
            )
        return request, blockers

    async def hard_delete_eligible(self, user: User) -> bool:
        """new_spec §4.8.2, режим C: «разрешён только если у сущности нет ни

        одной зависимой записи (проверяется явным подсчётом, а не надеждой
        на ON DELETE)». `collect_erasure_blockers` уже проверяет активные
        сделки/задачи/подписи — здесь строже: и завершённые тоже, потому что
        `ON DELETE RESTRICT` не различает «активная» и «закрытая» запись, и
        руководство другим пользователем (`users.manager_id`), которое
        обезличивание не блокирует, а жёсткое удаление — обязано.
        """
        from app.modules.crm.models import Deal, DealComment, Task
        from app.modules.signing.models import SignatureRequest

        checks = (
            select(Deal.id).where((Deal.owner_id == user.id) | (Deal.created_by == user.id)),
            select(DealComment.id).where(DealComment.author_id == user.id),
            select(Task.id).where(Task.assignee_id == user.id),
            select(SignatureRequest.id).where(SignatureRequest.signer_user_id == user.id),
            select(User.id).where(User.manager_id == user.id),
        )
        for stmt in checks:
            if (await self._session.scalar(stmt.limit(1))) is not None:
                return False
        return True

    async def collect_erasure_blockers(self, user: User) -> list[dict[str, Any]]:
        """Блокеры из new_spec §4.8.3 и dop §10.7."""
        blockers: list[dict[str, Any]] = []

        if (
            user.role == Role.ADMIN.value
            and await self._identity.count_active_admins(exclude=user.id) == 0
        ):
            blockers.append(
                {
                    "code": "last_admin",
                    "detail": "Пользователь — последний активный администратор",
                    "count": 1,
                }
            )

        workload = await get_ownership_service().collect_workload(self._session, user.id)
        if workload.active_deals:
            blockers.append(
                {
                    "code": "active_deals",
                    "detail": "Есть активные сделки: требуется передача дел",
                    "count": workload.active_deals,
                }
            )
        if workload.open_tasks:
            blockers.append(
                {
                    "code": "open_tasks",
                    "detail": "Есть незакрытые задачи",
                    "count": workload.open_tasks,
                }
            )

        signatures = await get_signing_service().count_signatures(self._session, user.id)
        if signatures:
            # dop §10.7: подписи не обезличиваются никогда.
            blockers.append(
                {
                    "code": "has_signatures",
                    "detail": (
                        "Подписи не обезличиваются: подпись без идентификации подписанта "
                        "теряет юридическую силу"
                    ),
                    "count": signatures,
                    "legal_basis": "ст. 6 ч. 1 п. 5 и п. 7 152-ФЗ",
                }
            )

        if user.status not in (UserStatus.TERMINATED.value, UserStatus.BLOCKED.value):
            blockers.append(
                {
                    "code": "account_active",
                    "detail": "Учётная запись активна: сначала блокировка или увольнение",
                    "count": 1,
                }
            )
        return blockers


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None


def _utc(value: dt.datetime | None) -> dt.datetime | None:
    """Время без пояса из тела запроса считаем UTC: иначе его нечем сравнить с
    временем фоновой задачи."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=dt.UTC)
