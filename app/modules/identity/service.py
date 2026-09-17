"""Сервис identity: just-in-time provisioning и согласия.

При первом входе пользователя создаётся локальная запись `users` на основе
claims токена. Роль берётся из Keycloak, но финальное решение по доступу
принимает API (раздел 4).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.context import get_client
from app.core.security import TokenClaims
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.identity.models import (
    Consent,
    Role,
    SecurityEvent,
    SecurityEventType,
    Severity,
    SubjectType,
    User,
    UserStatus,
)

logger = structlog.get_logger(__name__)


class IdentityService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    # --- Provisioning ----------------------------------------------------

    async def get_by_keycloak_id(self, keycloak_id: str) -> User | None:
        result = await self._session.execute(
            select(User).where(User.keycloak_id == keycloak_id, User.deleted_at.is_(None))
        )
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str) -> User | None:
        result = await self._session.execute(
            select(User).where(
                func.lower(User.email) == email.lower(), User.deleted_at.is_(None)
            )
        )
        return result.scalar_one_or_none()

    async def provision_from_claims(self, claims: TokenClaims) -> User:
        """JIT provisioning: находит или создаёт локальную запись пользователя.

        Если запись была создана заранее приглашением (по email, без
        keycloak_id), она связывается с учёткой Keycloak и активируется.
        """
        user = await self.get_by_keycloak_id(claims.subject)
        if user:
            await self._sync_role(user, claims)
            return user

        if claims.email:
            invited = await self.get_by_email(claims.email)
            if invited and not invited.keycloak_id:
                invited.keycloak_id = claims.subject
                if invited.status == UserStatus.INVITED:
                    invited.status = UserStatus.ACTIVE
                    invited.activated_at = dt.datetime.now(dt.UTC)
                await self._session.flush()
                await self._audit.record(
                    AuditAction.USER_ACTIVATED,
                    entity_type="user",
                    entity_id=invited.id,
                    changes={"keycloak_id": {"old": None, "new": claims.subject}},
                )
                await self._sync_role(invited, claims)
                return invited

        role = claims.crm_role or Role.KAM.value
        now = dt.datetime.now(dt.UTC)
        user = User(
            keycloak_id=claims.subject,
            email=claims.email,
            full_name=claims.full_name or claims.preferred_username or "Без имени",
            role=role,
            status=UserStatus.ACTIVE,
            activated_at=now,
            last_login_at=now,
        )
        self._session.add(user)
        await self._session.flush()

        # Актор ещё не положен в контекст: пользователь создаётся в процессе
        # собственной аутентификации. Указываем его явно, иначе запись
        # осталась бы без actor_id и actor_role.
        await self._audit.record(
            AuditAction.USER_CREATED,
            entity_type="user",
            entity_id=user.id,
            changes={
                "source": {"old": None, "new": "jit_provisioning"},
                "role": {"old": None, "new": role},
                "email": {"old": None, "new": claims.email},
            },
            actor_id=user.id,
            actor_role=role,
        )
        logger.info("user_provisioned", user_id=str(user.id), role=role)
        return user

    async def _sync_role(self, user: User, claims: TokenClaims) -> None:
        """Роль в Keycloak — источник истины, поэтому расхождение фиксируем."""
        kc_role = claims.crm_role
        if kc_role and kc_role != user.role:
            old_role = user.role
            user.role = kc_role
            user.perm_epoch += 1
            await self._session.flush()
            await self._audit.record(
                AuditAction.USER_ROLE_CHANGED,
                entity_type="user",
                entity_id=user.id,
                changes={
                    "role": {"old": old_role, "new": kc_role},
                    "source": {"old": None, "new": "keycloak_sync"},
                },
            )

    async def mark_login(self, user: User) -> None:
        user.last_login_at = dt.datetime.now(dt.UTC)
        await self._session.flush()

    # --- Согласия --------------------------------------------------------

    async def latest_consent(self, user_id: uuid.UUID) -> Consent | None:
        result = await self._session.execute(
            select(Consent)
            .where(
                Consent.subject_type == SubjectType.USER.value,
                Consent.subject_id == user_id,
                Consent.revoked_at.is_(None),
                Consent.accepted_at.is_not(None),
            )
            .order_by(Consent.accepted_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    def consent_required(self, user: User) -> bool:
        """Новая версия политики снова поднимает признак согласия."""
        return user.consent_version != get_settings().consent_policy_version

    async def accept_consent(
        self, user: User, *, policy_version: str, policy_text_hash: str
    ) -> Consent:
        client = get_client()
        consent = Consent(
            subject_type=SubjectType.USER.value,
            subject_id=user.id,
            policy_version=policy_version,
            policy_text_hash=policy_text_hash,
            accepted_at=dt.datetime.now(dt.UTC),
            ip=client.ip if client else None,
            user_agent=client.user_agent if client else None,
        )
        self._session.add(consent)
        user.consent_version = policy_version
        await self._session.flush()

        await self._audit.record(
            AuditAction.CONSENT_ACCEPTED,
            entity_type="consent",
            entity_id=consent.id,
            changes={"policy_version": {"old": None, "new": policy_version}},
        )
        return consent

    # --- События безопасности -------------------------------------------

    async def record_security_event(
        self,
        event_type: SecurityEventType,
        *,
        user_id: uuid.UUID | None = None,
        severity: Severity = Severity.INFO,
        details: dict[str, object] | None = None,
    ) -> SecurityEvent:
        client = get_client()
        event = SecurityEvent(
            user_id=user_id,
            event_type=event_type.value,
            severity=severity.value,
            ip=client.ip if client else None,
            user_agent=client.user_agent if client else None,
            details=details,  # type: ignore[arg-type]
        )
        self._session.add(event)
        await self._session.flush()
        logger.info(
            "security_event",
            event_type=event_type.value,
            severity=severity.value,
            user_id=str(user_id) if user_id else None,
        )
        return event

    async def count_active_admins(self, *, exclude: uuid.UUID | None = None) -> int:
        """Нужно для запрета понижения последнего администратора (CRM-1903)."""
        stmt = select(func.count(User.id)).where(
            User.role == Role.ADMIN.value,
            User.status == UserStatus.ACTIVE.value,
            User.deleted_at.is_(None),
        )
        if exclude:
            stmt = stmt.where(User.id != exclude)
        return int((await self._session.execute(stmt)).scalar_one())


def policy_text_hash(text: str) -> str:
    """Хэш текста политики: доказывает, с какой именно редакцией согласился субъект."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
