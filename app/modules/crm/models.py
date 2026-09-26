"""Модели домена сделок (раздел 5.5, new_spec §4.9).

`Deal.organization_id`, `Deal.contact_id`, `Deal.loss_reason_id` и
`DealProduct.product_id` получили настоящий `ForeignKey` в спринте 4, когда
появился каталог (`app/modules/catalog/models.py`) — миграция
`0005_catalog_sprint.py` добавляет ограничения поверх уже существующих
колонок из `0004_deals_sprint.py`. `Deal.active_signature_document_id`
получил свой FK тем же приёмом в спринте 6 (`0007_signing_sprint.py`), когда
появился модуль ПЭП (`app/modules/signing/models.py`).

`deal_status_history` и `deal_events` — журналы, не редактируются: как и
`audit_log`, они получают `REVOKE UPDATE, DELETE` в миграции (см.
`migrations/versions/0004_deals_sprint.py`).
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
    Interval,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Money, SoftDeleteMixin, TimestampMixin, UuidPkMixin, VersionMixin


class DealType(StrEnum):
    B2B = "b2b"
    B2C = "b2c"


class SlaState(StrEnum):
    OK = "ok"
    WARNING = "warning"
    BREACHED = "breached"
    PAUSED = "paused"


class SignatureStatus(StrEnum):
    NONE = "none"
    PENDING = "pending"
    PARTIALLY_SIGNED = "partially_signed"
    SIGNED = "signed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    VOID = "void"


class Priority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


class ParticipantRole(StrEnum):
    WATCHER = "watcher"
    CO_OWNER = "co_owner"
    LAWYER = "lawyer"
    METHODIST = "methodist"


class HistoryReason(StrEnum):
    MANUAL = "manual"
    WORKFLOW_MIGRATION = "workflow_migration"
    INTEGRATION = "integration"
    AUTO = "auto"
    SIGNATURE_REJECTED = "signature_rejected"


class DealEventType(StrEnum):
    CREATED = "CREATED"
    OWNER_CHANGED = "OWNER_CHANGED"
    FIELD_CHANGED = "FIELD_CHANGED"
    FILE_ADDED = "FILE_ADDED"
    SIGNATURE_REQUESTED = "SIGNATURE_REQUESTED"
    SIGNATURE_SIGNED = "SIGNATURE_SIGNED"
    SIGNATURE_REJECTED = "SIGNATURE_REJECTED"
    IMPORT_APPLIED = "IMPORT_APPLIED"
    WORKFLOW_MIGRATION = "WORKFLOW_MIGRATION"


class TaskStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    CANCELLED = "cancelled"


#: Статусы задач, которые считаются «незакрытыми» для guard-условия
#: `tasks.open_count` (раздел 8 DSL) и для чек-листа перед закрытием сделки.
OPEN_TASK_STATUSES = frozenset({TaskStatus.OPEN.value, TaskStatus.IN_PROGRESS.value})


class Deal(UuidPkMixin, TimestampMixin, VersionMixin, SoftDeleteMixin, Base):
    __tablename__ = "deals"
    __table_args__ = (
        UniqueConstraint("number", name="uq_deals_number"),
        Index("ix_deals_owner", "owner_id"),
        Index("ix_deals_status", "status_id"),
        Index("ix_deals_workflow", "workflow_id"),
        Index("ix_deals_organization", "organization_id"),
        Index("ix_deals_contact", "contact_id"),
        # SLA-скан бьёт только по незакрытым сделкам (new_spec §4.10): частичный
        # индекс держит запрос быстрым независимо от общего объёма сделок.
        Index(
            "ix_deals_sla_due_open",
            "sla_due_at",
            postgresql_where=text("closed_at IS NULL AND deleted_at IS NULL"),
        ),
        CheckConstraint("deal_type IN ('b2b','b2c')", name="deals_deal_type_valid"),
        CheckConstraint(
            "deal_type <> 'b2b' OR organization_id IS NOT NULL",
            name="deals_b2b_requires_organization",
        ),
        CheckConstraint(
            "deal_type <> 'b2c' OR contact_id IS NOT NULL", name="deals_b2c_requires_contact"
        ),
        CheckConstraint(
            "sla_state IN ('ok','warning','breached','paused')", name="deals_sla_state_valid"
        ),
        CheckConstraint(
            "priority IN ('low','normal','high','critical')", name="deals_priority_valid"
        ),
        CheckConstraint(
            "signature_status IN "
            "('none','pending','partially_signed','signed','rejected','expired','void')",
            name="deals_signature_status_valid",
        ),
        # Внешний «Номер заявки» (оплата с сайта): повторная загрузка или доставка того же заказа
        # не должна плодить сделки — на уровне БД, а не только проверкой в коде (два запроса
        # одновременно проходят проверку оба).
        Index(
            "uq_deals_order_number",
            "order_number",
            unique=True,
            postgresql_where=text("order_number IS NOT NULL AND deleted_at IS NULL"),
        ),
    )

    number: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    deal_type: Mapped[str] = mapped_column(String(8), nullable=False)
    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflows.id", ondelete="RESTRICT"), nullable=False
    )
    status_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_statuses.id", ondelete="RESTRICT"), nullable=False
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="RESTRICT"), nullable=True
    )
    contact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="RESTRICT"), nullable=True
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )

    amount: Mapped[Money | None] = mapped_column(Numeric(14, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, server_default="RUB")
    students_planned: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_close_date: Mapped[dt.date | None] = mapped_column(nullable=True)

    status_changed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    sla_due_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sla_state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="ok")
    # Когда по этой сделке ушла эскалация нарушения SLA (`sla_rules.escalate_*`): пока статус тот
    # же, повторно не шлём. Сбрасывается при входе в новый статус.
    sla_escalated_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Накопленное время в статусах типа parked — не таймер, а счётчик для
    # отчётности (new_spec §4.10): «сколько мы в сумме прождали вуз».
    sla_paused_total: Mapped[dt.timedelta] = mapped_column(
        Interval, nullable=False, server_default=text("interval '0'")
    )

    priority: Mapped[str] = mapped_column(String(16), nullable=False, server_default="normal")
    loss_reason_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("loss_reasons.id", ondelete="RESTRICT"), nullable=True
    )
    closed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    custom_fields: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    external_ids: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # «Номер заявки» из внешней системы (сайт, файл оплат), не путать с `number` (D-2026-000431).
    order_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner_unavailable: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    signature_status: Mapped[str] = mapped_column(String(20), nullable=False, server_default="none")
    # Модуль ПЭП (спринт 6) — FK на `signature_documents`, а не на живой
    # `signature_status` guard-условия: документ может быть voided/expired,
    # пока `signature_status` ещё отражает последнее известное состояние.
    active_signature_document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("signature_documents.id", ondelete="SET NULL"), nullable=True
    )


class DealParticipant(UuidPkMixin, Base):
    """Дополнительные участники сделки помимо владельца (раздел 5.5)."""

    __tablename__ = "deal_participants"
    __table_args__ = (
        UniqueConstraint(
            "deal_id", "user_id", "role_in_deal", name="uq_deal_participants_deal_user_role"
        ),
        Index("ix_deal_participants_deal", "deal_id"),
        Index("ix_deal_participants_user", "user_id"),
        CheckConstraint(
            "role_in_deal IN ('watcher','co_owner','lawyer','methodist')",
            name="deal_participants_role_valid",
        ),
    )

    deal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("deals.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    role_in_deal: Mapped[str] = mapped_column(String(16), nullable=False)
    added_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    added_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class DealStatusHistory(UuidPkMixin, Base):
    """Append-only история переходов (раздел 5.5). Не редактируется никогда."""

    __tablename__ = "deal_status_history"
    __table_args__ = (
        Index("ix_deal_status_history_deal_changed", "deal_id", "changed_at"),
        CheckConstraint(
            "reason IN ('manual','workflow_migration','integration','auto','signature_rejected')",
            name="deal_status_history_reason_valid",
        ),
    )

    deal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("deals.id", ondelete="CASCADE"), nullable=False
    )
    # NULL у самой первой записи: сделка ещё не была ни в каком статусе.
    from_status_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workflow_statuses.id", ondelete="RESTRICT"), nullable=True
    )
    to_status_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_statuses.id", ondelete="RESTRICT"), nullable=False
    )
    # NULL у системных переносов (миграция при архивировании статуса).
    changed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    transition_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workflow_transitions.id", ondelete="RESTRICT"), nullable=True
    )
    reason: Mapped[str] = mapped_column(String(24), nullable=False, server_default="manual")
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_in_prev: Mapped[dt.timedelta | None] = mapped_column(Interval, nullable=True)
    sla_state_at_change: Mapped[str | None] = mapped_column(String(16), nullable=True)
    changed_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False, index=True
    )


class DealEvent(UuidPkMixin, Base):
    """Лента активности сделки (раздел 5.5)."""

    __tablename__ = "deal_events"
    __table_args__ = (
        Index("ix_deal_events_deal_created", "deal_id", "created_at"),
        CheckConstraint(
            "event_type IN ('CREATED','OWNER_CHANGED','FIELD_CHANGED','FILE_ADDED',"
            "'SIGNATURE_REQUESTED','SIGNATURE_SIGNED','SIGNATURE_REJECTED',"
            "'IMPORT_APPLIED','WORKFLOW_MIGRATION')",
            name="deal_events_type_valid",
        ),
    )

    deal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("deals.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False, index=True
    )


class DealProduct(UuidPkMixin, TimestampMixin, Base):
    """Продукты сделки (раздел 5.5)."""

    __tablename__ = "deal_products"
    __table_args__ = (
        Index("ix_deal_products_deal", "deal_id"),
        Index("ix_deal_products_product", "product_id"),
        CheckConstraint("quantity > 0", name="deal_products_quantity_positive"),
        CheckConstraint("discount_pct BETWEEN 0 AND 100", name="deal_products_discount_valid"),
        CheckConstraint(
            "stream_number IS NULL OR stream_number > 0",
            name="deal_products_stream_number_positive",
        ),
    )

    deal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("deals.id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("products.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    price: Mapped[Money | None] = mapped_column(Numeric(14, 2), nullable=True)
    discount_pct: Mapped[Any] = mapped_column(
        Numeric(5, 2), nullable=False, server_default=text("0")
    )
    total: Mapped[Money | None] = mapped_column(Numeric(14, 2), nullable=True)
    # Номер потока курса («Номер потока» из данных оплат): один и тот же курс идёт несколькими
    # параллельными потоками, и отчёт «сколько учится на потоке 2» без этого поля невозможен.
    stream_number: Mapped[int | None] = mapped_column(Integer, nullable=True)


class DealComment(UuidPkMixin, TimestampMixin, SoftDeleteMixin, Base):
    __tablename__ = "deal_comments"
    __table_args__ = (
        Index("ix_deal_comments_deal_created", "deal_id", "created_at"),
        CheckConstraint("body_format IN ('plain','markdown')", name="deal_comments_format_valid"),
    )

    deal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("deals.id", ondelete="CASCADE"), nullable=False
    )
    # NULL у чисто системных записей без действующего актора.
    author_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("deal_comments.id", ondelete="RESTRICT"), nullable=True
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    body_format: Mapped[str] = mapped_column(String(16), nullable=False, server_default="plain")
    mentions: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    is_system: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    is_internal: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    edited_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DealCommentRevision(UuidPkMixin, Base):
    """Ревизия комментария (раздел 6.6): правка не стирает исходный текст."""

    __tablename__ = "deal_comment_revisions"
    __table_args__ = (Index("ix_deal_comment_revisions_comment", "comment_id"),)

    comment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("deal_comments.id", ondelete="CASCADE"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    edited_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    edited_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )


class Task(UuidPkMixin, TimestampMixin, SoftDeleteMixin, Base):
    """Задача, привязанная к сделке (раздел 5.5). Может быть создана вручную
    или действием перехода `create_task` (раздел 8 DSL)."""

    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_deal", "deal_id"),
        Index("ix_tasks_assignee", "assignee_id"),
        # Чек-лист «дочерние задачи закрыты» (new_spec §4.9 п.5) сканирует
        # именно незакрытые задачи сделки — частичный индекс держит его быстрым.
        Index(
            "ix_tasks_deal_open",
            "deal_id",
            postgresql_where=text("status IN ('open','in_progress') AND deleted_at IS NULL"),
        ),
        Index("ix_tasks_due_open", "due_at", postgresql_where=text("status = 'open'")),
        CheckConstraint(
            "status IN ('open','in_progress','done','cancelled')", name="tasks_status_valid"
        ),
        CheckConstraint(
            "priority IN ('low','normal','high','critical')", name="tasks_priority_valid"
        ),
    )

    deal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("deals.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    assignee_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    due_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="open")
    priority: Mapped[str] = mapped_column(String(16), nullable=False, server_default="normal")
    auto_created_by_transition_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workflow_transitions.id", ondelete="RESTRICT"), nullable=True
    )
