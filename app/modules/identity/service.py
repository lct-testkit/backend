"""Сервис identity: provisioning, согласия, жизненный цикл учётной записи.

Ключевые правила (new_spec §4.1–4.8):
  * самостоятельной регистрации нет — учётку заводит администратор;
  * Keycloak является источником истины по роли, но решение о доступе
    принимает API;
  * любое изменение роли, статуса или прав инвалидирует кэш и, если нужно,
    поднимает `perm_epoch`, обесценивая ранее выданные токены;
  * пароли и коды не логируются и не попадают в аудит.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import secrets
import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import invalidate_principal
from app.core.config import get_settings
from app.core.context import get_client
from app.core.errors import AppError, ErrorCode
from app.core.redis_client import get_redis
from app.core.security import TokenClaims
from app.modules.admin.models import SystemSetting
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
    UserInvite,
    UserStatus,
)

logger = structlog.get_logger(__name__)

# Ключ системной настройки с действующей редакцией политики обработки ПДн.
POLICY_SETTING_KEY = "pdn_policy"
_POLICY_CACHE_KEY = "cache:catalog:pdn_policy"
_POLICY_CACHE_TTL = 60


@dataclass(frozen=True, slots=True)
class PolicyVersion:
    """Действующая редакция политики обработки ПДн."""

    version: str
    text_hash: str | None = None


class IdentityService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    # --- Provisioning ----------------------------------------------------

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        result = await self._session.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

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

        Самостоятельной регистрации нет (new_spec §4.1). Поэтому запись
        создаётся только если администратор уже что-то сделал для этого
        пользователя: либо завёл локальное приглашение по email, либо выдал
        ему роль CRM в Keycloak. Учётная запись realm'а без роли CRM доступа
        не получает — иначе любой аккаунт каталога становился бы КАМом с
        правом видеть ПДн контактов.
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
                # Приглашение одноразовое: после входа оно должно погаснуть.
                await self.mark_invites_used(invited.id)
                await self._audit.record(
                    AuditAction.USER_ACTIVATED,
                    entity_type="user",
                    entity_id=invited.id,
                    changes={"keycloak_id": {"old": None, "new": claims.subject}},
                    actor_id=invited.id,
                    actor_role=invited.role,
                )
                await self._sync_role(invited, claims)
                return invited

        role = claims.crm_role
        if role is None:
            # Роль CRM не выдана — значит, администратор эту учётку в CRM не
            # заводил. Фиксируем попытку: это может быть и разведка доступа.
            await self.record_security_event(
                SecurityEventType.PERMISSION_DENIED,
                severity=Severity.WARNING,
                details={"reason": "no_crm_role", "subject": claims.subject},
            )
            raise AppError(
                ErrorCode.FORBIDDEN,
                "Учётная запись не заведена в CRM: обратитесь к администратору",
            )

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
        """Роль в Keycloak — источник истины, при рассинхроне побеждает токен.

        `perm_epoch` здесь не трогаем: токен уже содержит новую роль, а
        инкремент эпохи на каждом запросе означал бы бесконечное
        «обновите токен» и запись в аудит на каждый GET.
        """
        kc_role = claims.crm_role
        if not kc_role or kc_role == user.role:
            return
        old_role = user.role
        user.role = kc_role
        await self._session.flush()
        await invalidate_principal(user.id, keycloak_id=user.keycloak_id)
        await self._audit.record(
            AuditAction.USER_ROLE_CHANGED,
            entity_type="user",
            entity_id=user.id,
            changes={
                "role": {"old": old_role, "new": kc_role},
                "source": {"old": None, "new": "keycloak_sync"},
            },
            actor_id=user.id,
            actor_role=kc_role,
        )

    async def mark_login(self, user: User) -> None:
        user.last_login_at = dt.datetime.now(dt.UTC)
        await self._session.flush()

    # --- Политика и согласия ---------------------------------------------

    async def current_policy(self) -> PolicyVersion:
        """Действующая редакция политики обработки ПДн.

        Публикуется через `system_settings.pdn_policy`, чтобы новая версия
        поднимала `consent_required` у всех пользователей без передеплоя
        (раздел 6.1). Значения из окружения — запасной вариант.
        """
        settings = get_settings()
        fallback = PolicyVersion(
            version=settings.consent_policy_version,
            text_hash=settings.consent_policy_text_hash,
        )

        try:
            cached = await get_redis().get(_POLICY_CACHE_KEY)
            if cached:
                payload = json.loads(cached)
                return PolicyVersion(
                    version=payload["version"], text_hash=payload.get("text_hash")
                )
        except Exception:  # noqa: BLE001 — кэш не источник истины
            cached = None

        row = (
            await self._session.execute(
                select(SystemSetting.value).where(SystemSetting.key == POLICY_SETTING_KEY)
            )
        ).scalar_one_or_none()

        policy = fallback
        if isinstance(row, dict) and row.get("version"):
            policy = PolicyVersion(version=str(row["version"]), text_hash=row.get("text_hash"))

        try:
            await get_redis().setex(
                _POLICY_CACHE_KEY,
                _POLICY_CACHE_TTL,
                json.dumps({"version": policy.version, "text_hash": policy.text_hash}),
            )
        except Exception:  # noqa: BLE001
            pass
        return policy

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

    async def consent_required(self, user: User) -> bool:
        """Новая версия политики снова поднимает признак согласия."""
        policy = await self.current_policy()
        return user.consent_version != policy.version

    async def accept_consent(
        self, user: User, *, policy_version: str, policy_text_hash: str
    ) -> Consent:
        """Фиксирует согласие вместе с доказательством редакции текста."""
        policy = await self.current_policy()
        if policy_version != policy.version:
            raise AppError(
                ErrorCode.VALIDATION,
                "Версия политики не совпадает с действующей",
                extra={"expected": policy.version},
            )
        if policy.text_hash and policy.text_hash != policy_text_hash:
            # Хэш текста — единственное доказательство того, с чем именно
            # согласился человек. Принимать чужой хэш бессмысленно.
            raise AppError(
                ErrorCode.VALIDATION,
                "Хэш текста политики не совпадает с опубликованным",
                extra={"policy_version": policy.version},
            )

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
        await invalidate_principal(user.id, keycloak_id=user.keycloak_id)

        await self._audit.record(
            AuditAction.CONSENT_ACCEPTED,
            entity_type="consent",
            entity_id=consent.id,
            changes={"policy_version": {"old": None, "new": policy_version}},
        )
        return consent

    # --- Приглашения ------------------------------------------------------

    async def issue_invite(self, user: User, *, created_by: uuid.UUID | None) -> str:
        """Создаёт одноразовое приглашение и возвращает сам токен.

        Токен возвращается ровно один раз — в ответе администратору. В базе
        остаётся только sha256, поэтому восстановить ссылку из дампа нельзя.
        """
        settings = get_settings()
        token = secrets.token_urlsafe(32)
        invite = UserInvite(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=settings.invite_ttl_hours),
            created_by=created_by,
        )
        self._session.add(invite)
        user.invited_at = dt.datetime.now(dt.UTC)
        await self._session.flush()
        await self._audit.record(
            AuditAction.USER_INVITED,
            entity_type="user",
            entity_id=user.id,
            changes={"expires_at": {"old": None, "new": invite.expires_at.isoformat()}},
        )
        return token

    async def mark_invites_used(self, user_id: uuid.UUID) -> int:
        """Гасит приглашения после первого входа."""
        return await self._close_invites(user_id, used=True)

    async def revoke_invites(self, user_id: uuid.UUID) -> int:
        """Отзывает неиспользованные приглашения — например, при перевыпуске."""
        return await self._close_invites(user_id, used=False)

    async def _close_invites(self, user_id: uuid.UUID, *, used: bool) -> int:
        now = dt.datetime.now(dt.UTC)
        invites = (
            (
                await self._session.execute(
                    select(UserInvite).where(
                        UserInvite.user_id == user_id,
                        UserInvite.used_at.is_(None),
                        UserInvite.revoked_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        for invite in invites:
            if used:
                invite.used_at = now
            else:
                invite.revoked_at = now
        await self._session.flush()
        return len(invites)

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

    # --- Скоуп руководителя ----------------------------------------------

    async def team_subtree_ids(self, team_id: uuid.UUID) -> list[uuid.UUID]:
        """Идентификаторы команды и всех вложенных команд.

        Раздел 4: скоуп HEAD рекурсивен по `teams.parent_id`. Обход делает
        БД через `WITH RECURSIVE`, а не приложение циклом запросов.
        """
        rows = await self._session.execute(
            text(
                """
                WITH RECURSIVE subtree AS (
                    SELECT id FROM teams WHERE id = :root AND deleted_at IS NULL
                    UNION ALL
                    SELECT t.id
                    FROM teams t
                    JOIN subtree s ON t.parent_id = s.id
                    WHERE t.deleted_at IS NULL
                )
                SELECT id FROM subtree
                """
            ),
            {"root": team_id},
        )
        return [row[0] for row in rows.all()]

    async def team_member_ids(self, team_id: uuid.UUID) -> list[uuid.UUID]:
        """Пользователи команды и подчинённых команд."""
        subtree = await self.team_subtree_ids(team_id)
        if not subtree:
            return []
        rows = await self._session.execute(
            select(User.id).where(User.team_id.in_(subtree), User.deleted_at.is_(None))
        )
        return [row[0] for row in rows.all()]

    # --- Ограничения целостности ролей -----------------------------------

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

    async def ensure_not_last_admin(self, user: User) -> None:
        """Блокирует понижение, блокировку и удаление последнего админа."""
        if user.role != Role.ADMIN.value:
            return
        if await self.count_active_admins(exclude=user.id) == 0:
            raise AppError(
                ErrorCode.LAST_ADMIN,
                "Это последний активный администратор: операция заблокирована",
                extra={"user_id": str(user.id)},
            )

    async def bump_perm_epoch(self, user: User) -> None:
        """Мгновенно обесценивает ранее выданные токены (new_spec §4.6)."""
        user.perm_epoch += 1
        await self._session.flush()
        await invalidate_principal(user.id, keycloak_id=user.keycloak_id)


def hash_token(token: str) -> str:
    """sha256 одноразового токена: в базе не хранится сам секрет."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def policy_text_hash(text: str) -> str:
    """Хэш текста политики: доказывает, с какой именно редакцией согласился субъект."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
