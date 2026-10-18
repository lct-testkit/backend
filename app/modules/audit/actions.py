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
    # B4-5: ограничения видимости по вузам/направлениям (`access_scope:manage`, только
    # ADMIN) — `changes` несёт конфиги `before`/`after` и причину.
    ACCESS_RULES_CHANGED = "ACCESS_RULES_CHANGED"
    USER_OFFBOARDED = "USER_OFFBOARDED"
    USER_TERMINATED = "USER_TERMINATED"
    # new_spec §4.1: не вошёл по приглашению за 30 дней — учётка отключена.
    USER_INVITE_EXPIRED = "USER_INVITE_EXPIRED"
    PASSWORD_CHANGED = "PASSWORD_CHANGED"
    PASSWORD_RESET = "PASSWORD_RESET"
    # B6-10: ссылка на самостоятельную установку пароля (приглашение — покрыто `USER_INVITED`;
    # сброс — отдельное действие, т.к. `PASSWORD_RESET` уже означает «сброс запущен»).
    PASSWORD_RESET_LINK_ISSUED = "PASSWORD_RESET_LINK_ISSUED"

    # --- Инциденты безопасности (B6-12, 152-ФЗ ст. 21 ч. 3.1) ---
    INCIDENT_CREATED = "INCIDENT_CREATED"
    INCIDENT_CONFIRMED = "INCIDENT_CONFIRMED"
    INCIDENT_UPDATED = "INCIDENT_UPDATED"
    INCIDENT_RKN_NOTICE_GENERATED = "INCIDENT_RKN_NOTICE_GENERATED"
    INCIDENT_RKN_NOTICE_SENT = "INCIDENT_RKN_NOTICE_SENT"
    INCIDENT_SUBJECTS_NOTICE_GENERATED = "INCIDENT_SUBJECTS_NOTICE_GENERATED"
    INCIDENT_CLOSED = "INCIDENT_CLOSED"
    INCIDENT_DEADLINE_OVERDUE = "INCIDENT_DEADLINE_OVERDUE"
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
    # B4-7: AUDITOR читает комментарии сделки через comment:audit_read — доступ к свободному
    # тексту с возможными ПДн, того же класса, что PII_ACCESS, но именованный под конкретную
    # операцию, чтобы в журнале было видно, что именно смотрели, а не только что «обращались к ПДн».
    DEAL_COMMENTS_VIEWED = "DEAL_COMMENTS_VIEWED"
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
    # B9-8 (OPEN-ISSUES B-15): `DELETE /api/import-presets/{id}` не было вовсе — созданный
    # пресет маппинга нельзя было убрать из списка иначе, чем прямым SQL.
    IMPORT_PRESET_DELETED = "IMPORT_PRESET_DELETED"
    REPORT_EXPORTED = "REPORT_EXPORTED"
    REPORT_FAILED = "REPORT_FAILED"

    # --- Интеграции (спринт 9, §4.14/§7.8) ---
    INTEGRATION_SOURCE_UPDATED = "INTEGRATION_SOURCE_UPDATED"
    # Ручной возврат события outbox из failed/dead в очередь доставки.
    INTEGRATION_OUTBOX_RETRIED = "INTEGRATION_OUTBOX_RETRIED"
    # B5-5: «Забрать сейчас» — ручной запрос забора по API вне расписания cron.
    INTEGRATION_PULL_REQUESTED = "INTEGRATION_PULL_REQUESTED"
    # B5-3: настраиваемый маппинг «утверждённых полей».
    INTEGRATION_MAPPING_UPDATED = "INTEGRATION_MAPPING_UPDATED"
    INTEGRATION_MAPPING_RESET = "INTEGRATION_MAPPING_RESET"
    # B5-4: маршрутизация (воронка/тип сделки на источник, правила по значению поля).
    INTEGRATION_ROUTING_UPDATED = "INTEGRATION_ROUTING_UPDATED"
    INTEGRATION_ROUTING_RESET = "INTEGRATION_ROUTING_RESET"
    # B5-9: универсальные webhook-подписки на исходящие события. `changes` никогда не несёт
    # секрет — ни сырой, ни зашифрованный конверт (см. `integration.webhooks`).
    WEBHOOK_SUBSCRIPTION_CREATED = "WEBHOOK_SUBSCRIPTION_CREATED"
    WEBHOOK_SUBSCRIPTION_UPDATED = "WEBHOOK_SUBSCRIPTION_UPDATED"
    WEBHOOK_SUBSCRIPTION_DELETED = "WEBHOOK_SUBSCRIPTION_DELETED"
    WEBHOOK_SUBSCRIPTION_SECRET_ROTATED = "WEBHOOK_SUBSCRIPTION_SECRET_ROTATED"
    WEBHOOK_DELIVERY_RETRIED = "WEBHOOK_DELIVERY_RETRIED"
    # B5-10: API-ключи сервисных учёток роли INTEGRATION.
    API_KEY_CREATED = "API_KEY_CREATED"
    API_KEY_REVOKED = "API_KEY_REVOKED"

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
    # B9-7: архивация воронки целиком (B-4), в отличие от STATUS_ARCHIVED ниже (один статус).
    WORKFLOW_ARCHIVED = "WORKFLOW_ARCHIVED"
    # B2-5: HEAD переименовывает статус (workflow:rename_status) без публикации новой версии.
    WORKFLOW_STATUS_RENAMED = "WORKFLOW_STATUS_RENAMED"
    STATUS_ARCHIVED = "STATUS_ARCHIVED"
    STATUS_MAPPING_STARTED = "STATUS_MAPPING_STARTED"
    STATUS_MAPPING_COMPLETED = "STATUS_MAPPING_COMPLETED"
    # Перенос сделок остановился: часть сделок перенести нельзя, статус не архивирован.
    STATUS_MAPPING_FAILED = "STATUS_MAPPING_FAILED"
    # B2-3: массовый перенос открытых сделок с одной воронки на другую (CLI `python -m app.
    # modules.workflow.migrate_deals`) — одна запись на партию (откуда, куда, число сделок).
    WORKFLOW_DEALS_MIGRATED = "WORKFLOW_DEALS_MIGRATED"

    # --- Организации и ЕГРЮЛ ---
    ORGANIZATION_CREATED = "ORGANIZATION_CREATED"
    ORGANIZATION_UPDATED = "ORGANIZATION_UPDATED"
    ORGANIZATION_DUPLICATE_FOUND = "ORGANIZATION_DUPLICATE_FOUND"
    ORGANIZATION_RESTORED = "ORGANIZATION_RESTORED"
    # B4-1: отдельно от ORGANIZATION_UPDATED — иначе смену ответственного за
    # вуз (ТЗ Функц.11) не отличить в журнале от правки адреса или сайта.
    ORGANIZATION_OWNER_CHANGED = "ORGANIZATION_OWNER_CHANGED"
    # B4-3: мягкое удаление вуза и сделки (A-16).
    ORGANIZATION_DELETED = "ORGANIZATION_DELETED"
    DEAL_DELETED = "DEAL_DELETED"
    DEAL_RESTORED = "DEAL_RESTORED"
    # B4-12 (A-18): запрос временного доступа к чужой организации.
    ACCESS_REQUESTED = "ACCESS_REQUESTED"
    ACCESS_GRANTED = "ACCESS_GRANTED"
    ACCESS_REJECTED = "ACCESS_REJECTED"
    ACCESS_REVOKED = "ACCESS_REVOKED"
    ORG_REGISTRY_IMPORTED = "ORG_REGISTRY_IMPORTED"
    ORG_DRIFT_APPLIED = "ORG_DRIFT_APPLIED"
    # B4-11 (A-22): «Оставить как есть» — отклонить расхождение без изменения карточки.
    ORG_DRIFT_DISMISSED = "ORG_DRIFT_DISMISSED"
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
    # B-9: мягкое удаление (`deleted_at`, тот же приём, что у направлений) —
    # только если продукт не указан ни в одной сделке. См.
    # `catalog.service.ProductService.delete`.
    PRODUCT_DELETED = "PRODUCT_DELETED"
    DIRECTION_CREATED = "DIRECTION_CREATED"
    DIRECTION_UPDATED = "DIRECTION_UPDATED"
    # П4: мягкое удаление (`deleted_at`) — только если нет дочерних
    # направлений и ничего на него не ссылается (продукты/сделки), иначе
    # 409 `ENTITY_IN_USE`. См. `catalog.service.DirectionService.delete`.
    DIRECTION_DELETED = "DIRECTION_DELETED"
    # B3-6: CRUD лицензий вуз-вендор-ПО (до этой задачи — только импорт).
    # `manager_full_name`/`responsible_contacts` в `changes` идут через
    # `mask_name`/`mask_contacts_text` — журнал аудита не должен стать
    # хранилищем ПДн, которые на выдаче и так маскируются без
    # `contact:reveal` (см. `OrganizationLicenseService`).
    LICENSE_CREATED = "LICENSE_CREATED"
    LICENSE_UPDATED = "LICENSE_UPDATED"
    LICENSE_DELETED = "LICENSE_DELETED"
    LOSS_REASON_CREATED = "LOSS_REASON_CREATED"
    LOSS_REASON_UPDATED = "LOSS_REASON_UPDATED"
    # П4: жёсткое удаление (таблица без `deleted_at` — «деактивируется, не
    # удаляется» было верно до этого пункта) — только если не используется
    # ни в одной сделке. См. `catalog.service.LossReasonService.delete`.
    LOSS_REASON_DELETED = "LOSS_REASON_DELETED"
    HOLIDAY_CREATED = "HOLIDAY_CREATED"
    HOLIDAY_UPDATED = "HOLIDAY_UPDATED"
    # B-34: жёсткое удаление (таблица без `deleted_at`) — ничего не
    # ссылается на `holidays` по FK, проверять использование нечего. См.
    # `catalog.service.HolidayService.delete`.
    HOLIDAY_DELETED = "HOLIDAY_DELETED"
    CUSTOM_FIELD_DEF_CREATED = "CUSTOM_FIELD_DEF_CREATED"
    CUSTOM_FIELD_DEF_UPDATED = "CUSTOM_FIELD_DEF_UPDATED"
    # B-9: жёсткое удаление, только если ни у одной сущности этого типа нет
    # непустого значения по коду поля (значения лежат в её `custom_fields
    # jsonb`, не в отдельной таблице). См.
    # `catalog.service.CustomFieldDefService.delete`.
    CUSTOM_FIELD_DEF_DELETED = "CUSTOM_FIELD_DEF_DELETED"

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
    # B7-3: скачивание подписанного файла/протокола по публичной ссылке (не токену подписания).
    SIGNATURE_DOWNLOADED = "SIGNATURE_DOWNLOADED"
    # B7-1: напоминание подписанту о приближающемся дедлайне.
    SIGNATURE_REMINDED = "SIGNATURE_REMINDED"
    # B7-4: автопереход воронки после подписания всех сторон.
    SIGNATURE_AUTO_ADVANCED = "SIGNATURE_AUTO_ADVANCED"

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
    # B6-6: срок хранения — категория, порог, число удалённых (без значений удалённых полей).
    RETENTION_APPLIED = "RETENTION_APPLIED"

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
