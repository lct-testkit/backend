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
    Integer,
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
    # B6-5: расхождение сигнатуры файла с заявленным расширением (`.pdf`, а внутри не PDF) —
    # не обязательно вредонос (переименованный файл), отдельный уровень тревоги от
    # MALWARE_DETECTED, чтобы не путать «подозрительно» с «обнаружен вредонос».
    FILE_REJECTED = "FILE_REJECTED"
    MASS_EXPORT = "MASS_EXPORT"
    SIGNATURE_OTP_FAILED = "SIGNATURE_OTP_FAILED"
    # B7-5: не меньше 5 неверных токенов подписи с одного IP за 10 минут — перебор ссылок,
    # главный вектор атаки на публичные ручки ПЭП. Строка без CHECK (String(48) без
    # ограничения значений) — новый тип не требует миграции.
    SIGNATURE_TOKEN_PROBE = "SIGNATURE_TOKEN_PROBE"
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
    # B4-5: есть хоть одна строка в `user_access_rules` — скоуп роли (KAM/HEAD) сужается
    # SQL-условием по вузам/направлениям (B4-6, `app/core/access_rules.py`). Денормализация
    # ради 99% пользователей без ограничений: ни одного лишнего запроса на список.
    access_restricted: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))

    team: Mapped[Team | None] = relationship(lazy="noload")

    @property
    def is_active(self) -> bool:
        return self.status == UserStatus.ACTIVE and self.deleted_at is None

    @property
    def effective_name(self) -> str:
        return self.display_name or self.full_name

    @property
    def is_system(self) -> bool:
        """B4-13 (I16): служебная учётка без входа (роль INTEGRATION — `integration@
        system.local` — либо исторический фантом ADMIN на уже развёрнутых стендах,
        `reporting.seed.remediate_phantom_admin`). Обычный сотрудник заводится только через
        Keycloak-инвайт, `keycloak_id` у него не бывает NULL, кроме как после обезличивания
        (туда он уже не попадёт: такой пользователь не активен и не виден в пикерах)."""
        return self.keycloak_id is None


class UserAccessRule(UuidPkMixin, Base):
    """B4-5: ограничение видимости пользователя по вузу или направлению (ТЗ Функц.11:
    «разграничение пользователей по доступу к данным»).

    Одна строка — один вуз ИЛИ одно направление (ровно одно поле, CHECK ниже), не обе
    размерности сразу. `effect`: `allow` — показывать только перечисленное в этой
    размерности, `deny` — показывать всё, кроме. Смешивать `allow` и `deny` в одной
    размерности для одного пользователя запрещено на уровне API
    (`identity.access_rules.AccessRulesService`), не здесь: проверка требует агрегата по
    пользователю, а не ограничения на строку. FK на `organizations`/`directions` —
    строкой на имя таблицы (как `Consent.signature_id` выше): модуль `identity` не
    импортирует модели `catalog`, SQLAlchemy резолвит внешний ключ по `Base.metadata`."""

    __tablename__ = "user_access_rules"
    __table_args__ = (
        Index(
            "uq_user_access_rules_org",
            "user_id",
            "organization_id",
            unique=True,
            postgresql_where=text("organization_id IS NOT NULL"),
        ),
        Index(
            "uq_user_access_rules_dir",
            "user_id",
            "direction_id",
            unique=True,
            postgresql_where=text("direction_id IS NOT NULL"),
        ),
        Index("ix_user_access_rules_user", "user_id"),
        CheckConstraint("effect IN ('allow','deny')", name="user_access_rules_effect_valid"),
        CheckConstraint(
            "(organization_id IS NOT NULL)::int + (direction_id IS NOT NULL)::int = 1",
            name="user_access_rules_one_target",
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    effect: Mapped[str] = mapped_column(String(8), nullable=False)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True
    )
    direction_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("directions.id", ondelete="CASCADE"), nullable=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


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
        CheckConstraint("purpose IN ('invite','reset')", name="user_invites_purpose_valid"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # B6-10: одна таблица обслуживает и приглашение нового сотрудника ('invite'), и
    # самостоятельную установку пароля по ссылке сброса ('reset') — тот же токен-одноразовость,
    # та же проверка `is_active`, но `revoke_invites`/`issue_invite` различают назначение.
    purpose: Mapped[str] = mapped_column(String(16), nullable=False, server_default="invite")
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
        # B6-2: `GET /admin/security-events?min_severity=warning` — основной путь просмотра,
        # частичный индекс не раздувается записями `info`, которых большинство.
        Index(
            "ix_security_events_severity_created",
            "severity",
            "created_at",
            postgresql_where=text("severity <> 'info'"),
        ),
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
    # B6-9: ПЭП ответственного лица делает акт доказательством — связывает акт, подписанную
    # копию и протокол (`SignatureDocument.manifest`). `RESTRICT`, как у `act_signature_id`.
    act_document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("signature_documents.id", ondelete="RESTRICT"), nullable=True
    )


class IncidentStatus(StrEnum):
    """B6-12: цикл реестра инцидентов. `candidate` — создан автоматически по событию
    безопасности, срок РКН ещё не идёт (его запускает подтверждение человеком)."""

    CANDIDATE = "candidate"
    OPEN = "open"
    RKN_24_SENT = "rkn_24_sent"
    RKN_72_SENT = "rkn_72_sent"
    CLOSED = "closed"
    DISMISSED = "dismissed"


class SecurityIncident(UuidPkMixin, TimestampMixin, VersionMixin, Base):
    """Реестр инцидентов безопасности (B6-12, 152-ФЗ ст. 21 ч. 3.1, сроки РКН 24/72 ч).

    Кандидаты создаются автоматически из событий `security_events` (`MALWARE_DETECTED`,
    `BRUTE_FORCE_LOCK`, `SESSION_HIJACK_SUSPECT`, `SIGNATURE_KEY_COMPROMISED`) —
    `IncidentService.suggest`, вызывается из `IdentityService.record_security_event` внутри
    SAVEPOINT (сбой создания кандидата не должен ронять запись самого события). Сроки РКН
    считаются от `confirm` (решение человека, что это инцидент), не от момента создания
    кандидата. `number` (`INC-{год}-{NNNN}`) строится в Python из `nextval('security_
    incident_seq')` перед вставкой — тот же приём, что `crm.service.DealService._next_
    number` уже использует для номеров сделок (`deal_number_seq`)."""

    __tablename__ = "security_incidents"
    __table_args__ = (
        CheckConstraint(
            "status IN ('candidate','open','rkn_24_sent','rkn_72_sent','closed','dismissed')",
            name="security_incidents_status_valid",
        ),
        CheckConstraint("source IN ('event','manual')", name="security_incidents_source_valid"),
        CheckConstraint(
            "severity IN ('info','warning','critical')", name="security_incidents_severity_valid"
        ),
        Index("ix_security_incidents_status_rkn24", "status", "rkn_24_due_at"),
        Index("uq_security_incidents_number", "number", unique=True),
    )

    number: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=IncidentStatus.CANDIDATE.value
    )
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=Severity.WARNING.value
    )
    detected_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    # id событий security_events, из которых собран кандидат (один или несколько — пачка).
    event_ids: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Категории ПДн, затронутые инцидентом (ст. 21 ч. 3.1 требует указать это РКН).
    data_categories: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    subjects_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    causes: Mapped[str | None] = mapped_column(Text, nullable=True)
    harm: Mapped[str | None] = mapped_column(Text, nullable=True)
    measures: Mapped[str | None] = mapped_column(Text, nullable=True)
    responsible_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    rkn_24_due_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rkn_72_due_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rkn_24_sent_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rkn_24_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    rkn_72_sent_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rkn_72_reference: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subjects_notified_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    investigation_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Сканер напоминаний (`sweep_incident_deadlines`) не должен слать повторные
    # напоминания об одном и том же пороге — отметка, какие уже отправлены.
    deadline_alerts_sent: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
