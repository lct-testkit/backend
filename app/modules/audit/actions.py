"""Перечень действий аудита из раздела 18 спецификации.

Список закрытый: бекендер не придумывает названия действий на месте,
иначе журнал становится непригодным для фильтрации и отчётности.
"""

from __future__ import annotations

from enum import StrEnum


class AuditAction(StrEnum):
    # --- Пользователи ---
    USER_CREATED = "USER_CREATED"
    USER_INVITED = "USER_INVITED"
    USER_ACTIVATED = "USER_ACTIVATED"
    # Раздел 18 перечисляет минимум; изменение профиля без смены роли тоже
    # обязано быть видно в журнале, иначе правка команды или руководителя
    # остаётся неотслеженной.
    USER_UPDATED = "USER_UPDATED"
    USER_BLOCKED = "USER_BLOCKED"
    USER_UNBLOCKED = "USER_UNBLOCKED"
    USER_ROLE_CHANGED = "USER_ROLE_CHANGED"
    USER_OFFBOARDED = "USER_OFFBOARDED"
    USER_TERMINATED = "USER_TERMINATED"
    # new_spec §4.1: не вошёл по приглашению за 30 дней — учётка отключена.
    USER_INVITE_EXPIRED = "USER_INVITE_EXPIRED"
    PASSWORD_CHANGED = "PASSWORD_CHANGED"
    PASSWORD_RESET = "PASSWORD_RESET"
    CONSENT_ACCEPTED = "CONSENT_ACCEPTED"
    SESSIONS_TERMINATED = "SESSIONS_TERMINATED"

    TEAM_CREATED = "TEAM_CREATED"
    TEAM_UPDATED = "TEAM_UPDATED"
    # C-22: мягкое удаление, только если нет активных сотрудников и живых
    # дочерних команд. См. `identity.router_admin.delete_team`.
    TEAM_DELETED = "TEAM_DELETED"

    # --- Сделки ---
    DEAL_CREATED = "DEAL_CREATED"
    DEAL_UPDATED = "DEAL_UPDATED"
    DEAL_STATUS_CHANGED = "DEAL_STATUS_CHANGED"
    DEAL_OWNER_CHANGED = "DEAL_OWNER_CHANGED"
    DEAL_CLOSED = "DEAL_CLOSED"
    DEAL_REASSIGNED_BULK = "DEAL_REASSIGNED_BULK"
    PARTICIPANT_ADDED = "PARTICIPANT_ADDED"
    PARTICIPANT_REMOVED = "PARTICIPANT_REMOVED"

    # Раздел 18 перечисляет минимум и не называет задачи отдельно, но
    # `tasks` — такая же изменяемая сущность со своими ручками (раздел 6.6),
    # и требование «100% изменяющих данные действий — в аудите» (часть 0,
    # критерий 2) распространяется и на неё. Тот же принцип, что уже дал
    # `USER_UPDATED` сверх минимума.
    TASK_CREATED = "TASK_CREATED"
    TASK_UPDATED = "TASK_UPDATED"
    TASK_COMPLETED = "TASK_COMPLETED"

    # --- Комментарии и файлы ---
    COMMENT_CREATED = "COMMENT_CREATED"
    COMMENT_UPDATED = "COMMENT_UPDATED"
    COMMENT_DELETED = "COMMENT_DELETED"
    FILE_UPLOAD_INTENT = "FILE_UPLOAD_INTENT"
    FILE_COMMITTED = "FILE_COMMITTED"
    FILE_INFECTED = "FILE_INFECTED"
    # Раздел 3.7: превышение лимита размера, обнаруженное постфактум по
    # факту в S3, — не то же самое событие, что несовпадение magic
    # bytes/хэша (FILE_INFECTED). Разные причины отклонения полезно
    # различать в журнале, а не сваливать в одну корзину.
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    FILE_DOWNLOADED = "FILE_DOWNLOADED"
    FILE_DELETED = "FILE_DELETED"
    ATTACHMENT_CREATED = "ATTACHMENT_CREATED"
    ATTACHMENT_DELETED = "ATTACHMENT_DELETED"

    # --- Импорт и отчёты ---
    IMPORT_STARTED = "IMPORT_STARTED"
    # Раздел 18 не называет отдельно фазу dry-run и сохранение пресета
    # маппинга, но тот же принцип «100% изменяющих действий — в аудите»
    # (часть 0, критерий 2), что уже дал `STATUS_MAPPING_STARTED`/
    # `WORKFLOW_VALIDATED` — dry-run пишет `import_row_results` и счётчики
    # задания, это не чисто вычислительный предпросмотр.
    IMPORT_VALIDATED = "IMPORT_VALIDATED"
    IMPORT_APPLIED = "IMPORT_APPLIED"
    IMPORT_ROLLBACK = "IMPORT_ROLLBACK"
    IMPORT_PRESET_CREATED = "IMPORT_PRESET_CREATED"
    REPORT_EXPORTED = "REPORT_EXPORTED"
    REPORT_FAILED = "REPORT_FAILED"

    # --- Интеграции (спринт 9, §4.14/§7.8) ---
    INTEGRATION_SOURCE_UPDATED = "INTEGRATION_SOURCE_UPDATED"
    # Ручной возврат события outbox из failed/dead в очередь доставки.
    INTEGRATION_OUTBOX_RETRIED = "INTEGRATION_OUTBOX_RETRIED"

    # --- Отчётность и дашборды (спринт 8, §4.13/§7.9) ---
    DASHBOARD_CREATED = "DASHBOARD_CREATED"
    DASHBOARD_UPDATED = "DASHBOARD_UPDATED"
    DASHBOARD_DELETED = "DASHBOARD_DELETED"
    DASHBOARD_WIDGET_ADDED = "DASHBOARD_WIDGET_ADDED"
    DASHBOARD_WIDGET_UPDATED = "DASHBOARD_WIDGET_UPDATED"
    DASHBOARD_WIDGET_REMOVED = "DASHBOARD_WIDGET_REMOVED"

    # --- Workflow ---
    WORKFLOW_CREATED = "WORKFLOW_CREATED"
    # П4: удаление разрешено только для черновика (`core.errors.ErrorCode.
    # WORKFLOW_NOT_DRAFT` блокирует остальное) — см. `workflow.service.
    # WorkflowService.delete_draft`.
    WORKFLOW_DELETED = "WORKFLOW_DELETED"
    # `PATCH /workflows/{id}`: имя и воронка по умолчанию (граф правит `PUT .../graph`).
    WORKFLOW_UPDATED = "WORKFLOW_UPDATED"
    WORKFLOW_PUBLISHED = "WORKFLOW_PUBLISHED"
    WORKFLOW_VALIDATED = "WORKFLOW_VALIDATED"
    STATUS_ARCHIVED = "STATUS_ARCHIVED"
    STATUS_MAPPING_STARTED = "STATUS_MAPPING_STARTED"
    STATUS_MAPPING_COMPLETED = "STATUS_MAPPING_COMPLETED"
    # Перенос сделок остановился: часть сделок перенести нельзя, статус не архивирован.
    STATUS_MAPPING_FAILED = "STATUS_MAPPING_FAILED"

    # --- Организации и ЕГРЮЛ ---
    ORGANIZATION_CREATED = "ORGANIZATION_CREATED"
    ORGANIZATION_UPDATED = "ORGANIZATION_UPDATED"
    ORGANIZATION_DUPLICATE_FOUND = "ORGANIZATION_DUPLICATE_FOUND"
    ORG_REGISTRY_IMPORTED = "ORG_REGISTRY_IMPORTED"
    ORG_DRIFT_APPLIED = "ORG_DRIFT_APPLIED"
    ORG_LIQUIDATION_DETECTED = "ORG_LIQUIDATION_DETECTED"
    # П4: удаление снэпшота/выгрузки ЕГРЮЛ — только не последней активной
    # версии (раздел 4: «систему нельзя оставить без реестра»). См.
    # `registry.service.RegistryImportService.delete`.
    REGISTRY_VERSION_DELETED = "REGISTRY_VERSION_DELETED"

    # --- Контакты и остальной каталог (раздел 5.2/5.3) ---
    # Раздел 18 перечисляет минимум и не называет контакты/справочники
    # отдельно, но тот же принцип, что уже дал `TASK_CREATED` в спринте 3:
    # «100% изменяющих данные действий — в аудите» (часть 0, критерий 2)
    # распространяется на любую изменяемую сущность, не только на явно
    # перечисленные в разделе 18.
    CONTACT_CREATED = "CONTACT_CREATED"
    CONTACT_UPDATED = "CONTACT_UPDATED"
    # Ответственный за продукт (каталог «Вендоры»): связь контакта с продуктом ставится и снимается
    # вручную (`PUT`/`DELETE /products/{id}/contacts/{contact_id}`). Запись в аудите привязана к
    # продукту — «кто отвечает за продукт» читается с его карточки.
    CONTACT_PRODUCT_LINKED = "CONTACT_PRODUCT_LINKED"
    CONTACT_PRODUCT_UNLINKED = "CONTACT_PRODUCT_UNLINKED"
    PRODUCT_CREATED = "PRODUCT_CREATED"
    PRODUCT_UPDATED = "PRODUCT_UPDATED"
    DIRECTION_CREATED = "DIRECTION_CREATED"
    DIRECTION_UPDATED = "DIRECTION_UPDATED"
    # П4: мягкое удаление (`deleted_at`) — только если нет дочерних
    # направлений и ничего на него не ссылается (продукты/сделки), иначе
    # 409 `ENTITY_IN_USE`. См. `catalog.service.DirectionService.delete`.
    DIRECTION_DELETED = "DIRECTION_DELETED"
    LOSS_REASON_CREATED = "LOSS_REASON_CREATED"
    LOSS_REASON_UPDATED = "LOSS_REASON_UPDATED"
    # П4: жёсткое удаление (таблица без `deleted_at` — «деактивируется, не
    # удаляется» было верно до этого пункта) — только если не используется
    # ни в одной сделке. См. `catalog.service.LossReasonService.delete`.
    LOSS_REASON_DELETED = "LOSS_REASON_DELETED"
    HOLIDAY_CREATED = "HOLIDAY_CREATED"
    HOLIDAY_UPDATED = "HOLIDAY_UPDATED"
    CUSTOM_FIELD_DEF_CREATED = "CUSTOM_FIELD_DEF_CREATED"
    CUSTOM_FIELD_DEF_UPDATED = "CUSTOM_FIELD_DEF_UPDATED"

    # --- Уведомления (раздел 5.7, спринт 7) ---
    NOTIFICATION_TEMPLATE_CREATED = "NOTIFICATION_TEMPLATE_CREATED"
    NOTIFICATION_TEMPLATE_UPDATED = "NOTIFICATION_TEMPLATE_UPDATED"
    # П4: можно всегда (шаблон текста, не бизнес-сущность с историей) — см.
    # `notification.service.NotificationTemplateService.delete`.
    NOTIFICATION_TEMPLATE_DELETED = "NOTIFICATION_TEMPLATE_DELETED"

    # --- ПЭП ---
    SIGNATURE_DOCUMENT_CREATED = "SIGNATURE_DOCUMENT_CREATED"
    SIGNATURE_DOCUMENT_SENT = "SIGNATURE_DOCUMENT_SENT"
    SIGNATURE_VIEWED = "SIGNATURE_VIEWED"
    SIGNATURE_CHALLENGED = "SIGNATURE_CHALLENGED"
    SIGNATURE_SIGNED = "SIGNATURE_SIGNED"
    SIGNATURE_REJECTED = "SIGNATURE_REJECTED"
    SIGNATURE_VOID = "SIGNATURE_VOID"
    SIGNATURE_VERIFIED = "SIGNATURE_VERIFIED"
    SIGNATURE_OTP_FAILED = "SIGNATURE_OTP_FAILED"
    SIGNATURE_KEY_COMPROMISED = "SIGNATURE_KEY_COMPROMISED"
    EDM_AGREEMENT_CREATED = "EDM_AGREEMENT_CREATED"
    EDM_AGREEMENT_REVOKED = "EDM_AGREEMENT_REVOKED"
    # Срок действия вышел (`valid_to`): статус `expired` выставляет фоновая задача.
    EDM_AGREEMENT_EXPIRED = "EDM_AGREEMENT_EXPIRED"
    # Инициатор получил новую ссылку внешнему подписанту; прежняя перестала работать.
    SIGNATURE_LINK_REISSUED = "SIGNATURE_LINK_REISSUED"

    # --- 152-ФЗ ---
    ERASURE_REQUEST_CREATED = "ERASURE_REQUEST_CREATED"
    ERASURE_REQUEST_BLOCKED = "ERASURE_REQUEST_BLOCKED"
    ERASURE_REQUEST_APPROVED = "ERASURE_REQUEST_APPROVED"
    ERASURE_REQUEST_REJECTED = "ERASURE_REQUEST_REJECTED"
    # Восстановление в период отсрочки (new_spec §4.8.4 шаг 4, кнопка
    # «Восстановить») — отдельное от ERASURE_REQUEST_REJECTED действие: одно
    # значит «отказано по существу», другое — «передумали, пока не поздно».
    # В отчёте по 152-ФЗ это разные события, даже если оба переводят запрос
    # в терминальный статус `rejected`.
    ERASURE_REQUEST_RESTORED = "ERASURE_REQUEST_RESTORED"
    ERASURE_EXECUTED = "ERASURE_EXECUTED"
    PII_ACCESS = "PII_ACCESS"
    PII_REVEALED = "PII_REVEALED"
    MASS_EXPORT = "MASS_EXPORT"

    # --- Система ---
    LOGIN_SUCCEEDED = "LOGIN_SUCCEEDED"
    LOGIN_FAILED = "LOGIN_FAILED"
    LOGOUT = "LOGOUT"
    SESSION_TERMINATED = "SESSION_TERMINATED"
    AUDIT_EXPORTED = "AUDIT_EXPORTED"
    # Принцип «четырёх глаз» (CRM-1902): заявка, подтверждение и отказ.
    ADMIN_APPROVAL_REQUESTED = "ADMIN_APPROVAL_REQUESTED"
    ADMIN_APPROVAL_GRANTED = "ADMIN_APPROVAL_GRANTED"
    ADMIN_APPROVAL_REJECTED = "ADMIN_APPROVAL_REJECTED"
    FEATURE_FLAG_CHANGED = "FEATURE_FLAG_CHANGED"
    SYSTEM_SETTING_CHANGED = "SYSTEM_SETTING_CHANGED"
    ACCESS_DENIED = "ACCESS_DENIED"
