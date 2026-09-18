"""Права и скоупы (раздел 4).

Проверка идёт на трёх уровнях: маршрут и базовое право, объект, и SQL-фильтр
списка. Здесь живёт первый уровень — сопоставление роли и набора прав, а
также перечень прав, на которые опираются зависимости роутеров.

Третий уровень (SQL-скоуп) реализуется в репозиториях модулей: даже если
проверка объекта забыта, запрос не должен вернуть чужие данные.
"""

from __future__ import annotations

from enum import StrEnum

from app.modules.identity.models import Role


class Permission(StrEnum):
    # --- Сделки ---
    DEAL_READ = "deal:read"
    DEAL_CREATE = "deal:create"
    DEAL_UPDATE = "deal:update"
    DEAL_TRANSITION = "deal:transition"
    DEAL_REASSIGN = "deal:reassign"
    DEAL_REASSIGN_BULK = "deal:reassign_bulk"

    # --- Организации и контакты ---
    ORG_READ = "organization:read"
    ORG_WRITE = "organization:write"
    # dop.md §11.8: данные ИП (ФИО, адрес) — ПДн физлица, хоть и из
    # общедоступного источника. `OrganizationOut` маскирует телефон/email для
    # `org_type='individual_entrepreneur'` тем же приёмом, что `ContactOut` —
    # отдельное право на раскрытие, не совпадающее с `ORG_WRITE`.
    ORG_REVEAL = "organization:reveal"
    CONTACT_READ = "contact:read"
    CONTACT_WRITE = "contact:write"
    CONTACT_REVEAL = "contact:reveal"

    # --- Каталоги ---
    CATALOG_READ = "catalog:read"
    CATALOG_WRITE = "catalog:write"

    # --- Воронки ---
    WORKFLOW_READ = "workflow:read"
    WORKFLOW_WRITE = "workflow:write"
    WORKFLOW_PUBLISH = "workflow:publish"

    # --- Файлы ---
    FILE_UPLOAD = "file:upload"
    FILE_DOWNLOAD = "file:download"
    FILE_DELETE = "file:delete"

    # --- Импорт и отчёты ---
    IMPORT_RUN = "import:run"
    IMPORT_ROLLBACK = "import:rollback"
    REPORT_READ = "report:read"
    REPORT_CREATE = "report:create"

    # --- ЕГРЮЛ и автоподстановка (dop.md §11.10, new_spec §3.2 добавления) ---
    ORG_LOOKUP_USE = "org_lookup:use"
    REGISTRY_IMPORT = "registry:import"

    # --- Интеграции ---
    INTEGRATION_INGEST = "integration:ingest"
    INTEGRATION_ADMIN = "integration:admin"

    # --- ПЭП ---
    SIGNATURE_CREATE = "signature:create"
    SIGNATURE_SIGN = "signature:sign"
    SIGNATURE_VOID = "signature:void"
    EDM_ADMIN = "edm:admin"
    # dop.md §13 матрица прав: «Управление соглашениями ЭДО» — ADMIN пишет,
    # AUDITOR только читает (KAM/HEAD — ни то, ни другое). Отдельное право,
    # а не проверка роли на роутере: раздел 4 требует, чтобы даже первый
    # уровень проверки (маршрут) шёл через `Permission`, а не через прямое
    # сравнение `principal.role`.
    EDM_READ = "edm:read"

    # --- Уведомления ---
    # `GET /api/notifications`, `.../read` и `/api/me/notification-prefs` не
    # проверяют отдельное право — это самообслуживание своих же уведомлений,
    # доступное любой аутентифицированной роли (см. `core.deps.ConsentedUser`).
    # Права нужны только на административный контур шаблонов (dop.md §13:
    # «шаблоны администрируются через отдельный административный контур»).
    NOTIFICATION_TEMPLATE_MANAGE = "notification_template:manage"

    # --- Администрирование ---
    USER_READ = "user:read"
    USER_WRITE = "user:write"
    AUDIT_READ = "audit:read"
    AUDIT_EXPORT = "audit:export"
    SETTINGS_WRITE = "settings:write"
    ERASURE_MANAGE = "erasure:manage"


# KAM — менеджер по вузам: только свои сделки и связанные с ними сущности.
_KAM: frozenset[Permission] = frozenset(
    {
        Permission.DEAL_READ,
        Permission.DEAL_CREATE,
        Permission.DEAL_UPDATE,
        Permission.DEAL_TRANSITION,
        Permission.ORG_READ,
        Permission.ORG_WRITE,
        Permission.ORG_REVEAL,
        Permission.ORG_LOOKUP_USE,
        Permission.CONTACT_READ,
        Permission.CONTACT_WRITE,
        Permission.CONTACT_REVEAL,
        Permission.CATALOG_READ,
        Permission.WORKFLOW_READ,
        Permission.FILE_UPLOAD,
        Permission.FILE_DOWNLOAD,
        Permission.REPORT_READ,
        Permission.REPORT_CREATE,
        Permission.SIGNATURE_CREATE,
        Permission.SIGNATURE_SIGN,
    }
)

# HEAD — руководитель: всё как у KAM, плюс команда, переназначение и импорт
# каталогов (раздел 5: матрица прав отдаёт «Импорт каталогов» HEAD и ADMIN,
# не KAM).
_HEAD: frozenset[Permission] = _KAM | {
    Permission.DEAL_REASSIGN,
    Permission.DEAL_REASSIGN_BULK,
    Permission.AUDIT_READ,
    Permission.SIGNATURE_VOID,
    Permission.FILE_DELETE,
    Permission.IMPORT_RUN,
    Permission.IMPORT_ROLLBACK,
}

# AUDITOR — только журнал, плюс явное «чтение» соглашений ЭДО из матрицы
# dop.md §13 (разделение обязанностей: видит, но не управляет).
_AUDITOR: frozenset[Permission] = frozenset(
    {Permission.AUDIT_READ, Permission.AUDIT_EXPORT, Permission.EDM_READ}
)

# INTEGRATION — сервисная учётка. new_spec часть 5, матрица прав: переход по
# статусу для INTEGRATION отмечен «✔ (огранич.)» — входящий лид создаёт
# сделку и должен уметь провести её по автоматическим переходам (например,
# зачисление в LMS по данным вебхука), не только создать саму запись.
_INTEGRATION: frozenset[Permission] = frozenset(
    {
        Permission.INTEGRATION_INGEST,
        Permission.DEAL_CREATE,
        Permission.DEAL_TRANSITION,
        Permission.CONTACT_WRITE,
        Permission.ORG_READ,
    }
)

# ADMIN — все права.
_ADMIN: frozenset[Permission] = frozenset(Permission)

ROLE_PERMISSIONS: dict[str, frozenset[Permission]] = {
    Role.KAM.value: _KAM,
    Role.HEAD.value: _HEAD,
    Role.ADMIN.value: _ADMIN,
    Role.AUDITOR.value: _AUDITOR,
    Role.INTEGRATION.value: _INTEGRATION,
}


def permissions_for(role: str) -> frozenset[Permission]:
    return ROLE_PERMISSIONS.get(role, frozenset())


def scopes_for(role: str) -> list[str]:
    """Плоский список прав для GET /api/me — фронтенд рисует по нему интерфейс."""
    return sorted(p.value for p in permissions_for(role))


def has_permission(role: str, permission: Permission) -> bool:
    return permission in permissions_for(role)


class DealScope(StrEnum):
    """Какой SQL-фильтр применяет репозиторий сделок для роли (раздел 4)."""

    OWN = "own"  # KAM: owner_id = me OR участник
    TEAM = "team"  # HEAD: рекурсивно по дереву команд
    ALL = "all"  # ADMIN
    SOURCE = "source"  # INTEGRATION: только созданное этим источником
    NONE = "none"  # AUDITOR: пустой скоуп по сделкам и контактам


ROLE_DEAL_SCOPE: dict[str, DealScope] = {
    Role.KAM.value: DealScope.OWN,
    Role.HEAD.value: DealScope.TEAM,
    Role.ADMIN.value: DealScope.ALL,
    Role.INTEGRATION.value: DealScope.SOURCE,
    Role.AUDITOR.value: DealScope.NONE,
}


def deal_scope_for(role: str) -> DealScope:
    return ROLE_DEAL_SCOPE.get(role, DealScope.NONE)
