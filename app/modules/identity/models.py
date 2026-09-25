"""Модели модуля identity (раздел 5.1).

`users` — локальная проекция пользователя Keycloak. Источник истины по роли
Keycloak, но локальное поле нужно для быстрых фильтров и SQL-скоупов.
Уникальность `email` и `keycloak_id` действует только для активных записей:
после обезличивания они очищаются, а запись остаётся ради связности истории.
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import (
    Base,
    IpAddressType,
    SoftDeleteMixin,
    TimestampMixin,
    UuidPkMixin,
    VersionMixin,
)


class Role(StrEnum):
    KAM = "KAM"
    HEAD = "HEAD"
    ADMIN = "ADMIN"
    AUDITOR = "AUDITOR"
    INTEGRATION = "INTEGRATION"


class UserStatus(StrEnum):
    INVITED = "invited"
    ACTIVE = "active"
    BLOCKED = "blocked"
    TERMINATED = "terminated"
    ANONYMIZED = "anonymized"


class SubjectType(StrEnum):
    USER = "user"
    CONTACT = "contact"
    # dop.md §11.8: данные ИП (`organizations.org_type='individual_
    # entrepreneur'`) — ПДн физлица, а не сведения о юрлице, значит подпадают
    # под тот же режим удаления/обезличивания, что contact (new_spec §4.8).
    # Компании/вузы этим субъектом никогда не являются — их данные не ПДн, и
    # `catalog.service.OrganizationService` отказывает в запросе для любого
    # другого `org_type`.
    ORGANIZATION = "organization"


class SecurityEventType(StrEnum):
    LOGIN_FAILED = "LOGIN_FAILED"
    BRUTE_FORCE_LOCK = "BRUTE_FORCE_LOCK"
    PASSWORD_CHANGED = "PASSWORD_CHANGED"
    PASSWORD_RESET = "PASSWORD_RESET"
    SESSION_HIJACK_SUSPECT = "SESSION_HIJACK_SUSPECT"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    MALWARE_DETECTED = "MALWARE_DETECTED"
    MASS_EXPORT = "MASS_EXPORT"
    SIGNATURE_OTP_FAILED = "SIGNATURE_OTP_FAILED"
    SIGNATURE_KEY_COMPROMISED = "SIGNATURE_KEY_COMPROMISED"
    # Раздел 4.14: HMAC-подпись входящего вебхука (CMS/LMS/Bitrix) не сошлась —
    # тот же класс события, что и неверный OTP при подписании, только для
    # входящей интеграции вместо интерактивного пользователя.
    INTEGRATION_SIGNATURE_INVALID = "INTEGRATION_SIGNATURE_INVALID"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class ErasureStatus(StrEnum):
    PENDING = "pending"
    BLOCKED = "blocked"
    APPROVED = "approved"
    REJECTED = "rejected"
    COMPLETED = "completed"


class Team(UuidPkMixin, TimestampMixin, SoftDeleteMixin, VersionMixin, Base):
    """Команда. Иерархия рекурсивная: HEAD видит команды вниз по дереву."""

    __tablename__ = "teams"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("teams.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    # head_id без FK на users: иначе получаем циклическую зависимость таблиц.
    head_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    region_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)

    parent: Mapped[Team | None] = relationship(remote_side="Team.id", lazy="noload")


class User(UuidPkMixin, TimestampMixin, SoftDeleteMixin, VersionMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        # Уникальность только среди живых записей: soft delete не должен
        # блокировать повторное создание пользователя с тем же email.
        # Сравнение регистронезависимое — «Ivanov@rt.ru» и «ivanov@rt.ru»
        # это один человек.
        Index(
            "uq_users_email_lower_active",
            text("lower(email)"),
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND email IS NOT NULL"),
        ),
        Index(
            "uq_users_keycloak_id_active",
            "keycloak_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND keycloak_id IS NOT NULL"),
        ),
        Index("ix_users_team_role", "team_id", "role"),
        Index("ix_users_status", "status", postgresql_where=text("deleted_at IS NULL")),
        # Под запрос `identity.tasks.sweep_user_lifecycle`: блокировки со сроком.
        Index(
            "ix_users_auto_unblock_due",
            "auto_unblock_at",
            postgresql_where=text("status = 'blocked' AND auto_unblock_at IS NOT NULL"),
        ),
        # Поиск по ФИО в админке — по триграммам, а не LIKE '%…%' по таблице.
        Index(
            "ix_users_full_name_trgm",
            "full_name",
            postgresql_using="gin",
            postgresql_ops={"full_name": "gin_trgm_ops"},
        ),
        CheckConstraint(
            "role IN ('KAM','HEAD','ADMIN','AUDITOR','INTEGRATION')",
            name="users_role_valid",
        ),
        CheckConstraint(
            "status IN ('invited','active','blocked','terminated','anonymized')",
            name="users_status_valid",
        ),
    )

    # Обнуляется при обезличивании, поэтому nullable.
    keycloak_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    position: Mapped[str | None] = mapped_column(String(255), nullable=True)

    role: Mapped[str] = mapped_column(String(32), nullable=False, server_default="KAM")
    team_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("teams.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    manager_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="invited")
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    avatar_file_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)

    locale: Mapped[str] = mapped_column(String(8), nullable=False, server_default="ru")
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="Europe/Moscow"
    )

    # Растёт при смене роли: токен со старой эпохой отклоняется как CRM-1103.
    perm_epoch: Mapped[int] = mapped_column(nullable=False, server_default=text("1"))

    password_changed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Невыполненное обязательное действие Keycloak `UPDATE_PASSWORD`
    # (new_spec §4.4, ситуация B). Access-токен его не содержит, поэтому
    # признак держим локально: API обязан отвергать бизнес-запросы, пока
    # пароль не сменён, иначе действие обходится прямым вызовом API.
    must_change_password: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    last_login_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consent_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    invited_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    activated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    blocked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Срок блокировки из `POST /admin/users/{id}/block`: по его наступлении
    # задача `sweep_user_lifecycle` снимает блокировку. Пусто — без срока.
    auto_unblock_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    anonymized_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    team: Mapped[Team | None] = relationship(lazy="noload")

    @property
    def is_active(self) -> bool:
        return self.status == UserStatus.ACTIVE and self.deleted_at is None

    @property
    def effective_name(self) -> str:
        return self.display_name or self.full_name


class UserDelegation(UuidPkMixin, Base):
    """Замещение на время отпуска. Право подписи по умолчанию не делегируется."""

    __tablename__ = "user_delegations"
    __table_args__ = (
        CheckConstraint("ends_at > starts_at", name="delegation_period_valid"),
        Index("ix_user_delegations_to_user_period", "to_user_id", "starts_at", "ends_at"),
    )

    from_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    to_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    # Что именно делегируется: просмотр, переходы, назначение.
    scope: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    starts_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class UserInvite(UuidPkMixin, Base):
    """Одноразовое приглашение (new_spec §4.1, шаг 4).

    В закрытом контуре SMTP может отсутствовать, поэтому ссылка отдаётся
    администратору в ответе на создание. В базе хранится только sha256
    токена: утечка дампа не должна давать возможность войти.
    """

    __tablename__ = "user_invites"
    __table_args__ = (
        Index("ix_user_invites_user_active", "user_id", "expires_at"),
        Index("uq_user_invites_token_hash", "token_hash", unique=True),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    @property
    def is_active(self) -> bool:
        return (
            self.used_at is None
            and self.revoked_at is None
            and self.expires_at > dt.datetime.now(dt.UTC)
        )


class Consent(UuidPkMixin, Base):
    """Согласие на обработку ПДн. Хранит версию политики и хэш её текста,
    чтобы можно было доказать, с чем именно согласился субъект."""

    __tablename__ = "consents"
    __table_args__ = (
        CheckConstraint("subject_type IN ('user','contact')", name="consent_subject_valid"),
        Index("ix_consents_subject", "subject_type", "subject_id", "accepted_at"),
    )

    subject_type: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    policy_text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    accepted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip: Mapped[str | None] = mapped_column(IpAddressType(), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Заполняется, если согласие подписано ПЭП. FK — строкой на имя таблицы:
    # `signatures` живёт в модуле `signing` (спринт 6), кросс-модульный
    # Python-импорт моделей не нужен, SQLAlchemy резолвит по `Base.metadata`
    # (тот же приём, что `Deal.organization_id` для `catalog.Organization`).
    signature_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("signatures.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class SecurityEvent(UuidPkMixin, Base):
    __tablename__ = "security_events"
    __table_args__ = (
        Index("ix_security_events_type_created", "event_type", "created_at"),
        Index("ix_security_events_user_created", "user_id", "created_at"),
    )

    # Без FK: событие может касаться несуществующего или удалённого субъекта.
    user_id: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, server_default="info")
    ip: Mapped[str | None] = mapped_column(IpAddressType(), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False, index=True
    )


class DataErasureRequest(UuidPkMixin, TimestampMixin, Base):
    """Запрос субъекта на удаление или обезличивание (152-ФЗ).

    `blockers` заполняется сразу при создании: активные сделки, незакрытые
    задачи, роль последнего администратора, подписи, файлы с ПДн.
    """

    __tablename__ = "data_erasure_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','blocked','approved','rejected','completed')",
            name="erasure_status_valid",
        ),
        CheckConstraint(
            "subject_type IN ('user','contact','organization')", name="erasure_subject_valid"
        ),
        Index("ix_data_erasure_requests_subject", "subject_type", "subject_id"),
    )

    subject_type: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    legal_basis: Mapped[str | None] = mapped_column(String(255), nullable=True)
    requested_by: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    requested_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    deadline_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    blockers: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Конец отсрочки режима A (new_spec §4.8.4 шаг 4, 30 дней по умолчанию).
    # Устанавливается, когда блокеров нет (при создании или после `recheck`);
    # `identity.tasks.sweep_erasure_requests` исполняет запрос, как только
    # это время наступает, и снимается — не устанавливается заново — при
    # `restore`, чем «отсрочка» отличается от `rejected` по причине отказа.
    grace_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Акт об уничтожении ПДн и его ПЭП хранятся бессрочно — `RESTRICT`, не
    # `SET NULL`: ссылка на доказательство уничтожения не должна тихо
    # обнуляться. `signatures` и так `INSERT`/`SELECT`-only и никогда не
    # удаляется (см. `app/modules/signing/models.py`), поэтому для
    # `act_signature_id` это ограничение работает как документация инварианта,
    # а не как что-то реально способное сработать.
    act_file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("files.id", ondelete="RESTRICT"), nullable=True
    )
    act_signature_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("signatures.id", ondelete="RESTRICT"), nullable=True
    )
