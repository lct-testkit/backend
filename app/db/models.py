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

# --- каталог: организации, контакты, справочники (раздел 5.2/5.3) ---
from app.modules.catalog.models import (
    Contact,
    ContactChannel,
    CustomFieldDef,
    Direction,
    Holiday,
    LossReason,
    Organization,
    OrganizationBranch,
    Product,
    Region,
)

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

# --- файлы и вложения (раздел 5.6) ---
from app.modules.files.models import Attachment, File

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

# --- импорт каталогов (раздел 5.10/§4.12, спринт 5) ---
from app.modules.imports.models import ImportJob, ImportPreset, ImportRowResult

# --- ЕГРЮЛ и автоподстановка (раздел 5.11, спринт 5) ---
from app.modules.registry.models import (
    EgrulEntry,
    OrgLookupLog,
    RegistryVersion,
    UniversityRegistry,
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
    "Attachment",
    "AuditLog",
    "Base",
    "Consent",
    "Contact",
    "ContactChannel",
    "CustomFieldDef",
    "DataErasureRequest",
    "Deal",
    "DealComment",
    "DealCommentRevision",
    "DealEvent",
    "DealParticipant",
    "DealProduct",
    "DealStatusHistory",
    "Direction",
    "EgrulEntry",
    "FeatureFlag",
    "File",
    "Holiday",
    "IdempotencyKey",
    "ImportJob",
    "ImportPreset",
    "ImportRowResult",
    "LossReason",
    "Organization",
    "OrganizationBranch",
    "OrgLookupLog",
    "Product",
    "Region",
    "RegistryVersion",
    "SecurityEvent",
    "SlaRule",
    "StatusMappingJob",
    "SystemSetting",
    "Task",
    "Team",
    "UniversityRegistry",
    "User",
    "UserDelegation",
    "UserInvite",
    "Workflow",
    "WorkflowStatus",
    "WorkflowTransition",
]

target_metadata = Base.metadata
