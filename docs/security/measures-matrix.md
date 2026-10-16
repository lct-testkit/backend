# Матрица мер ФСТЭК № 117 и 152-ФЗ (B6-1)

Дата сверки: 2026-10-16 (коммит эпика 1, см. `git log`). Эта таблица — честная сверка «мера →
где в коде → что осталось», а не декларация: статус «есть» ставится только тогда, когда в
коде есть что показать (ручка, тест, файл), статус подтверждается чтением кода на дату сверки.
Группы названы по структуре приказов ФСТЭК (ИАФ, УПД, АУД, АВЗ, ОЦЛ, ЗНИ, ЗИС, ИНЦ, УКФ, ОДТ) —
номера конкретных мер (ИАФ.1 и далее) не сверялись с действующей у заказчика редакцией приказа
№ 117, это открытый вопрос (см. «Вопрос владельцу» ниже).

Статусы: **есть** (работает и подтверждено тестом или чтением кода на эту дату), **частично**,
**нет**.

| Группа и мера | Статус | Где в коде | Что дописать |
| --- | --- | --- | --- |
| ИАФ: парольная политика, блокировка перебора | частично | `deploy/keycloak/realm-crm.json:20,24` (`bruteForceProtected: true`, `failureFactor: 5`) | Блокировка не попадает в журнал CRM — опрос Keycloak Admin API (B6-3) не успел войти в объём эпика |
| ИАФ: второй фактор для привилегированных | частично | `app/modules/identity/admin_service.py:74` (`TOTP_ACTION = "CONFIGURE_TOTP"`, выставляется только при заведении ADMIN/HEAD) | Не добавляется при повышении роли, нет списка «кто без TOTP»: B6-11 |
| ИАФ: сессии, idle-таймаут, CSRF | есть | `app/core/config.py:128` (`session_idle_timeout`), `app/core/csrf.py:27-47` (`new_csrf_token`/`set_csrf_cookie`) | Ротация refresh в realm — B6-11 |
| УПД: роли и права (три уровня проверки) | есть | `app/core/permissions.py:179-198` (`ROLE_PERMISSIONS`, `permissions_for`, `has_permission`), `app/core/deps.py:258` (`require_permission`) | Индивидуальные ограничения по вузам — отдельный спринт (не B6) |
| УПД: разделение обязанностей (AUDITOR без бизнес-прав, 4 глаза) | есть | `app/modules/identity/admin_service.py:96` (`ApprovalService`), `app/core/permissions.py` (роль AUDITOR без `deal:*`/`workflow:write`) | — |
| АУД: неизменяемый журнал с цепочкой хэшей | есть | `app/modules/audit/service.py:175-401` (`AuditService`, `_chain_head`, `record`, `verify_chain`) | HMAC-ключ необязателен: B6-8; разделение прав outbox/sealer — B6-16 (эпик 3) |
| АУД: события безопасности — запись | частично | `app/modules/identity/service.py` (`record_security_event`, маскирует `details` через `mask_mapping`), типы — `app/modules/identity/models.py` (`SecurityEventType`), пишутся: `PERMISSION_DENIED`, `PASSWORD_CHANGED`, `PASSWORD_RESET`, `SIGNATURE_KEY_COMPROMISED`, `MASS_EXPORT`, `MALWARE_DETECTED`, `FILE_REJECTED` (B6-5) | `LOGIN_FAILED`, `BRUTE_FORCE_LOCK` из Keycloak нигде не создаются: B6-3 не успел войти в объём эпика |
| АУД: события безопасности — чтение (API) | есть | `app/modules/admin/router.py:469` (`GET /admin/security-events`, `/summary`, `/export`), право `app/core/permissions.py:110` (`SECURITY_EVENT_READ`), `tests/test_security_events_api.py` | Экран в интерфейсе — спринт F5, не входит в объём backend-эпика |
| АУД: срок хранения журнала | нет | — | B6-6 (не входит в объём эпика — P1, не успели) |
| АВЗ: антивирусная защита | частично | `app/modules/files/antivirus.py` (`ClamAvScanner`, протокол INSTREAM, собственный asyncio-клиент), регистрация — `app/main.py`/`app/worker/main.py`, решения `commit` — `app/modules/files/service.py` (ветки `scan.error`/`scan.clean`/квота `clamav_sync_max_bytes`) | Выключен по умолчанию (`CLAMAV_ENABLED=false`, сервис `clamav` в compose/Helm — спринт D); воркер `sweep_quarantined_files` для крупных файлов и проверка в `/health/ready` не реализованы |
| АВЗ: карантин и событие MALWARE_DETECTED | есть | `app/modules/files/service.py:545` (`SecurityEventType.MALWARE_DETECTED`), `app/modules/files/service.py:561` (`copy_object` в `s3_bucket_quarantine` перед удалением исходного), `tests/test_files_hardening.py::test_a_virus_verdict_is_recorded` | Уведомление ADMIN по шаблону — B6-5 п.3, не входит в объём эпика |
| ОЦЛ: sha256 и сигнатуры файлов по факту содержимого | есть | `app/modules/files/service.py:46-59,194-201` (`_MAGIC_SIGNATURES`, `_check_magic_bytes`), `app/core/storage.py:130` (`inspect_object`) | Проверка doc/xls по содержимому OLE/zip (а не только по расширению) — B2-9, не входит в этот эпик |
| ОЦЛ: файлы в статус воронки (целостность пути документа) | есть | `app/modules/files/models.py:122,125` (`Attachment.status_id`/`status_history_id`), `app/modules/crm/service.py:2208` (`DealService._bind_transition_attachments`), миграция `migrations/versions/0026_b2_attachment_status_link.py` | — (B2-6/B2-7/B2-8 этого эпика) |
| ОЦЛ: подписанный документ, а не исходник, после ПЭП | есть | `app/modules/signing/service.py:1440` (`load_document_pdf`, параметр `variant`) | Публичная выдача подписанного файла по отдельной ссылке — B7-3 (не входит в объём, который успел эпик) |
| ЗНИ: защита при хранении (шифрование секретов/ПДн) | частично | `app/modules/admin/setting_crypto.py:81` (Fernet только для секретов системных настроек) | СНИЛС/паспорт/адрес учащегося открытым текстом: B6-7 (не успели) |
| ЗИС: TLS, HSTS, ограничение частоты | есть | `deploy/Caddyfile:55` (`Strict-Transport-Security`), `app/core/rate_limit.py` (`get_redis`/`key_rate_limit`) | `sslRequired: none` в realm — отдельный спринт D |
| ЗИС: публичные диагностические адреса не раскрывают деталей | частично | `app/api/health.py:52-99` (`live`/`ready` отдают версию, профиль и список зависимостей без токена) | B6-8 (не успели) |
| ИНЦ: реагирование, оповещение, сроки РКН | нет | — | B6-12, B6-14 (не успели) |
| ОДТ: резервное копирование | нет (в этом репозитории) | — | Скрипты бэкапа — отдельный deploy-репозиторий, не проверялись из этого эпика |
| УКФ: управление уязвимостями в CI | есть | `.github/workflows/ci.yml:165-180` (pip-audit, Trivy, hadolint) | Еженедельный аудит, gitleaks по истории, тест прав на маршрутах — B6-15 (не успели) |
| 152-ФЗ ст. 9: согласие на ПДн | есть | `app/modules/identity/service.py:51-52,350-355` (`pdn_policy`, `consent_required`, `accept_consent`) | — |
| 152-ФЗ: маскирование и раскрытие с аудитом | есть | `app/core/masking.py:21,84,96,216` (`LEARNER_PII_KEYS`, `mask_phone`, `mask_email`, `mask_mapping`), `app/modules/catalog/service.py:636,1283,1734` (`reveal`, аудит `PII_REVEALED`) | — |
| 152-ФЗ ст. 21: уничтожение и обезличивание | частично | `app/modules/identity/erasure_service.py:687-708` (`_issue_act`, `responsible_display="система..."`) | Акт без подписи ответственного лица, сирота в S3 при откате: B6-9 (не успели) |
| 152-ФЗ: срок хранения ПДн | нет | — | B6-6 (не успели) |
| 152-ФЗ ст. 18.1: организационные документы (политика, перечень ПДн, модель угроз, приказ) | нет | — | B6-14 (не успели) |
| 152-ФЗ ст. 21 ч. 3.1: уведомление РКН об утечке | нет | — | B6-12 (не успели) |
| Обезличенные данные на тестовых стендах | нет | — | B6-13 (не успели, P2) |

## Как читать эту таблицу

Статус «частично» или «нет» рядом с реально отсутствующей мерой — не ошибка документа, а
честная фиксация текущего состояния: эпик 1 спринта B6 успел закрыть P0-задачи B6-1 (эта
матрица) и начал, но не закончил B6-2…B6-5 в рамках выделенного времени (см. финальный отчёт
агента эпика). Каждая задача при закрытии меняет статус строки в этом же PR — колонка «Что
дописать» хранит номер задачи, которая это сделает.

## Вопрос владельцу/организаторам

Какой уровень защищённости ИСПДн принят у оператора (`.github/docs/ACCOMPANYING-DOCUMENTATION.md`
пишет «УЗ-3 предположительно») и есть ли акт классификации — от этого зависит обязательный
состав мер приказа № 117. Номера мер (ИАФ.1 и далее) в этой таблице не сверялись с текстом
приказа в редакции, действующей у заказчика.
