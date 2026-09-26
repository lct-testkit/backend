"""Модели конструктора воронок (раздел 5.4).

Граф воронки — это данные, а не код: статусы, переходы, условия и действия
хранятся в таблицах, редактируются администратором и валидируются перед
публикацией.

Одно осознанное расширение относительно раздела 5.4: у `workflows` есть
колонка `published_graph`. Без неё правка черновика немедленно меняла бы
правила для сделок, которые уже идут по воронке, и `graph_hash` с кэшем
`cache:wf:{id}` не имели бы смысла — они описывали бы граф, который уже
изменился. Публикация делает снимок графа, и сделки живут по снимку, пока
администратор не опубликует следующий.
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
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UuidPkMixin, VersionMixin


class WorkflowState(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    ARCHIVED = "archived"


class StatusType(StrEnum):
    INITIAL = "initial"
    INTERMEDIATE = "intermediate"
    WON = "won"
    LOST = "lost"
    PARKED = "parked"


#: Терминальные типы: сделка в них закрыта (`closed_at`) и дальше не идёт. `parked` сюда НЕ
#: входит: «заморозка» — пауза (SLA стоит, время копится в `sla_paused_total`), из неё сделку
#: возобновляют обычным переходом; раньше она закрывалась и оживить её было нечем.
TERMINAL_TYPES = frozenset({StatusType.WON, StatusType.LOST})


class MappingJobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class Workflow(UuidPkMixin, TimestampMixin, VersionMixin, Base):
    __tablename__ = "workflows"
    __table_args__ = (
        UniqueConstraint("code", name="uq_workflows_code"),
        # Воронка по умолчанию — ровно одна на тип сделки, иначе создание
        # сделки без явного `workflow_id` становится неоднозначным.
        Index(
            "uq_workflows_default_per_deal_type",
            "deal_type",
            unique=True,
            postgresql_where=text("is_default AND state = 'published'"),
        ),
        Index("ix_workflows_deal_type_state", "deal_type", "state"),
        CheckConstraint("deal_type IN ('b2b','b2c')", name="workflows_deal_type_valid"),
        CheckConstraint("state IN ('draft','published','archived')", name="workflows_state_valid"),
        # Опубликованная воронка обязана иметь снимок графа и его хэш.
        CheckConstraint(
            "state <> 'published' OR (published_graph IS NOT NULL AND graph_hash IS NOT NULL)",
            name="workflows_published_has_graph",
        ),
    )

    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    deal_type: Mapped[str] = mapped_column(String(8), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="draft")
    is_default: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))

    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    published_by: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    # sha256 канонизированного снимка: по нему фронтенд и сделки понимают,
    # что правила изменились.
    graph_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Снимок графа на момент публикации (см. docstring модуля).
    published_graph: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class WorkflowStatus(UuidPkMixin, TimestampMixin, Base):
    __tablename__ = "workflow_statuses"
    __table_args__ = (
        UniqueConstraint("workflow_id", "code", name="uq_workflow_statuses_workflow_id_code"),
        # Раздел 5.4 требует ровно один начальный статус на воронку.
        Index(
            "uq_workflow_statuses_initial",
            "workflow_id",
            unique=True,
            postgresql_where=text("type = 'initial'"),
        ),
        Index("ix_workflow_statuses_workflow_sort", "workflow_id", "sort_order"),
        CheckConstraint(
            "type IN ('initial','intermediate','won','lost','parked')",
            name="workflow_statuses_type_valid",
        ),
        # Архивный статус обязан нести отметку времени: без неё нельзя
        # отличить «архивирован давно» от незавершённой миграции.
        CheckConstraint(
            "is_archived = false OR archived_at IS NOT NULL",
            name="workflow_statuses_archived_at_present",
        ),
        CheckConstraint("replaced_by_status_id <> id", name="workflow_statuses_replacement_self"),
    )

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False, server_default="intermediate")
    color: Mapped[str | None] = mapped_column(String(16), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # Поля сделки, обязательные для входа в статус (проверяются при переходе).
    required_fields: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    is_archived: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    archived_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Куда переехали сделки при архивировании — нужно для чтения истории.
    replaced_by_status_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workflow_statuses.id", ondelete="RESTRICT"), nullable=True
    )


class WorkflowTransition(UuidPkMixin, TimestampMixin, Base):
    __tablename__ = "workflow_transitions"
    __table_args__ = (
        UniqueConstraint(
            "workflow_id",
            "from_status_id",
            "to_status_id",
            name="uq_workflow_transitions_workflow_id_from_status_id_to_status_id",
        ),
        Index("ix_workflow_transitions_from", "from_status_id"),
        CheckConstraint("from_status_id <> to_status_id", name="workflow_transitions_no_self_loop"),
    )

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_status_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_statuses.id", ondelete="CASCADE"), nullable=False
    )
    to_status_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_statuses.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # Пустой список = переход разрешён всем ролям, у которых есть право
    # `deal:transition`. Иначе — только перечисленным.
    allowed_roles: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    conditions: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    actions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    requires_comment: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


class SlaRule(UuidPkMixin, TimestampMixin, Base):
    __tablename__ = "sla_rules"
    __table_args__ = (
        # Одно активное правило на статус: иначе непонятно, чей срок считать.
        Index(
            "uq_sla_rules_status_active",
            "status_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        CheckConstraint(
            "warn_threshold_pct BETWEEN 1 AND 100", name="sla_rules_warn_threshold_valid"
        ),
        CheckConstraint(
            "escalate_threshold_pct BETWEEN 100 AND 1000", name="sla_rules_escalate_threshold_valid"
        ),
        CheckConstraint("max_duration > interval '0'", name="sla_rules_duration_positive"),
    )

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_statuses.id", ondelete="CASCADE"), nullable=False
    )
    max_duration: Mapped[dt.timedelta] = mapped_column(Interval, nullable=False)
    warn_threshold_pct: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("80")
    )
    # Доля срока (в процентах), после которой нарушение SLA эскалируется (`escalate_to_*`).
    # Не меньше 100: эскалация — продолжение нарушения, а не предупреждения.
    escalate_threshold_pct: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default=text("150")
    )
    escalate_to_role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    escalate_to_user_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    channels: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[\"in_app\"]'::jsonb")
    )
    count_business_days: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))
    is_active: Mapped[bool] = mapped_column(nullable=False, server_default=text("true"))


class StatusMappingJob(UuidPkMixin, Base):
    """Миграция сделок при архивировании статуса (раздел 6.5).

    Статус с живыми сделками нельзя просто выключить: сделки остались бы в
    статусе, из которого нет выхода. Поэтому архивирование всегда идёт через
    задачу сопоставления, а её отчёт остаётся в журнале.
    """

    __tablename__ = "status_mapping_jobs"
    __table_args__ = (
        Index("ix_status_mapping_jobs_workflow_status", "workflow_id", "from_status_id"),
        # Один незавершённый перенос на статус: параллельные задачи по одному
        # и тому же статусу переносили бы одни и те же сделки дважды.
        Index(
            "uq_status_mapping_jobs_active",
            "from_status_id",
            unique=True,
            postgresql_where=text("status IN ('pending','running')"),
        ),
        CheckConstraint(
            "status IN ('pending','running','completed','failed')",
            name="status_mapping_jobs_status_valid",
        ),
    )

    workflow_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False
    )
    from_status_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workflow_statuses.id", ondelete="CASCADE"), nullable=False
    )
    # {"target_status_id": ..., "fallback_status_id": ..., "rules": [...],
    #  "sla_mode": "recalculate"}
    mapping_rules: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    affected_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    processed_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    initiated_by: Mapped[uuid.UUID | None] = mapped_column(PgUUID(as_uuid=True), nullable=True)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    report: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False, index=True
    )
