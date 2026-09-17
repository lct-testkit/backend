"""Реестр моделей для Alembic.

Alembic сравнивает метаданные с базой, поэтому все модели должны быть
импортированы к моменту автогенерации. Модули добавляются сюда по мере
появления в спринтах.
"""

from __future__ import annotations

from app.db.base import Base

# --- admin: системные таблицы (раздел 5.10) ---
from app.modules.admin.models import (
    AdminApproval,
    FeatureFlag,
    IdempotencyKey,
    SystemSetting,
)

# --- audit (раздел 5.10) ---
from app.modules.audit.models import AuditLog

# --- сделки (раздел 5.5) ---
from app.modules.crm.models import (
    Deal,
    DealComment,
    DealCommentRevision,
    DealEvent,
    DealParticipant,
    DealProduct,
    DealStatusHistory,
    Task,
)

# --- identity (раздел 5.1) ---
from app.modules.identity.models import (
    Consent,
    DataErasureRequest,
    SecurityEvent,
    Team,
    User,
    UserDelegation,
    UserInvite,
)

# --- workflow (раздел 5.4) ---
from app.modules.workflow.models import (
    SlaRule,
    StatusMappingJob,
    Workflow,
    WorkflowStatus,
    WorkflowTransition,
)

__all__ = [
    "AdminApproval",
    "AuditLog",
    "Base",
    "Consent",
    "DataErasureRequest",
    "Deal",
    "DealComment",
    "DealCommentRevision",
    "DealEvent",
    "DealParticipant",
    "DealProduct",
    "DealStatusHistory",
    "FeatureFlag",
    "IdempotencyKey",
    "SecurityEvent",
    "SlaRule",
    "StatusMappingJob",
    "SystemSetting",
    "Task",
    "Team",
    "User",
    "UserDelegation",
    "UserInvite",
    "Workflow",
    "WorkflowStatus",
    "WorkflowTransition",
]

target_metadata = Base.metadata
