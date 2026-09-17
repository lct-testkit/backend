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

__all__ = [
    "AdminApproval",
    "AuditLog",
    "Base",
    "Consent",
    "DataErasureRequest",
    "FeatureFlag",
    "IdempotencyKey",
    "SecurityEvent",
    "SystemSetting",
    "Team",
    "User",
    "UserDelegation",
    "UserInvite",
]

target_metadata = Base.metadata
