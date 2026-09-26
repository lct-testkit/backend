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
    # Три файла заказчика о людях: ответственные за продукты вендоров и данные учащихся LMS.
    ContactLearnerProfile,
    ContactProduct,
    CustomFieldDef,
    Direction,
    Holiday,
    LossReason,
    Organization,
    OrganizationBranch,
    # П3 (rtk_requiriments.md разд. 4, Треб.1): лицензии/договоры
    # вуз↔вендор↔ПО.
    OrganizationLicense,
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

# --- интеграции: транспортный слой, шесть таблиц раздела 7.8 (спринт 9) ---
from app.modules.integration.models import (
    ExternalRef,
    InboundMessage,
    IntegrationSource,
    LearningProgress,
    OutboxEvent,
    SyncCursor,
)

# --- уведомления (раздел 5.7, спринт 7) ---
from app.modules.notification.models import (
    Notification,
    NotificationDelivery,
    NotificationTemplate,
    UserNotificationPref,
)

# --- ЕГРЮЛ и автоподстановка (раздел 5.11, спринт 5) ---
from app.modules.registry.models import (
    EgrulEntry,
    OrgLookupLog,
    RegistryVersion,
    UniversityRegistry,
)

# --- отчётность и дашборды (§4.13/§7.9, спринт 8) ---
from app.modules.reporting.models import (
    Dashboard,
    DashboardWidget,
    ReportJob,
    ReportTemplate,
)

# --- ПЭП (раздел 5.12, спринт 6) ---
from app.modules.signing.models import (
    EdmAgreement,
    Signature,
    SignatureDocument,
    SignatureOtpCode,
    SignatureRequest,
    SignatureTemplate,
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
    "ContactLearnerProfile",
    "ContactProduct",
    "CustomFieldDef",
    "Dashboard",
    "DashboardWidget",
    "DataErasureRequest",
    "Deal",
    "DealComment",
    "DealCommentRevision",
    "DealEvent",
    "DealParticipant",
    "DealProduct",
    "DealStatusHistory",
    "Direction",
    "EdmAgreement",
    "EgrulEntry",
    "ExternalRef",
    "FeatureFlag",
    "File",
    "Holiday",
    "IdempotencyKey",
    "ImportJob",
    "ImportPreset",
    "ImportRowResult",
    "InboundMessage",
    "IntegrationSource",
    "LearningProgress",
    "LossReason",
    "Notification",
    "NotificationDelivery",
    "NotificationTemplate",
    "OrgLookupLog",
    "Organization",
    "OrganizationBranch",
    "OrganizationLicense",
    "OutboxEvent",
    "Product",
    "Region",
    "RegistryVersion",
    "ReportJob",
    "ReportTemplate",
    "SecurityEvent",
    "Signature",
    "SignatureDocument",
    "SignatureOtpCode",
    "SignatureRequest",
    "SignatureTemplate",
    "SlaRule",
    "StatusMappingJob",
    "SyncCursor",
    "SystemSetting",
    "Task",
    "Team",
    "UniversityRegistry",
    "User",
    "UserDelegation",
    "UserInvite",
    "UserNotificationPref",
    "Workflow",
    "WorkflowStatus",
    "WorkflowTransition",
]

target_metadata = Base.metadata
