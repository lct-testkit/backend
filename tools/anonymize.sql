-- B6-13: обезличивание ВРЕМЕННОЙ базы (восстановленной из боевого бэкапа) перед выгрузкой
-- для dev/тестовых стендов. Запускается только через `tools/anonymized_dump.sh`, суперпользователем,
-- внутри базы, которую сам скрипт создал и которую он же удалит — никогда на боевой базе.
--
--     psql -v salt='<случайная строка>' -f tools/anonymize.sql -d crm_anon_20261018
--
-- Соль обязательна (`\set salt` ниже падает, если переменная не задана `-v`) и никуда не
-- сохраняется — она участвует только в sha256 внутри этого сеанса psql. Один и тот же `seed`
-- (id строки или id связанного пользователя/контакта) с одной солью всегда даёт один и тот же
-- псевдоним — так контакт, подписант в `signatures` и автор системного комментария получают
-- одинаковое вымышленное имя. Разная соль — разные имена (проверено `tests/test_anonymize.py`).
--
-- Только встроенные функции PostgreSQL (`sha256(bytea)` есть с версии 11) — без расширений
-- (`postgresql_anonymizer` нет в образе `postgres:16-alpine`, и это ещё один пункт в офлайн-бандл).
--
-- Названия вузов (`organizations` с `org_type <> 'individual_entrepreneur'`) НЕ обезличиваются — решение владельца
-- зафиксировать (вопрос в отчёте спринта B6-13): это не ПДн физлица. Названия и ИНН ИП —
-- обезличиваются (ст. 11.8 dop.md, ИП ведёт деятельность под собственным именем).

\set ON_ERROR_STOP on
\if :{?salt}
\else
    \echo 'anonymize.sql: переменная psql "salt" обязательна, например -v salt=$(openssl rand -hex 16)'
    SELECT 1/0; -- psql \quit не возвращает код выхода (проверено, PostgreSQL 16) — только так
\endif

-- =============================================================================
-- 1. Псевдонимы: функции, живущие только в этой временной базе
-- =============================================================================

-- sha256(salt || seed) в hex (64 символа) — источник всех производных значений ниже.
CREATE OR REPLACE FUNCTION anon_token(seed uuid, salt text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT encode(sha256((salt || seed::text)::bytea), 'hex');
$$;

-- Беззнаковое 32-битное число из 8 hex-символов токена, начиная с позиции `pos` (1-based) —
-- `('x' || hex)::bit(32)::bigint` ровно для этого и существует: хватает на modulo по длине
-- любого из массивов ниже без переполнения.
CREATE OR REPLACE FUNCTION anon_bits(token text, pos int) RETURNS bigint
LANGUAGE sql IMMUTABLE AS $$
    SELECT ('x' || substr(token, pos, 8))::bit(32)::bigint;
$$;

CREATE OR REPLACE FUNCTION anon_is_male(seed uuid, salt text) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
    SELECT anon_bits(anon_token(seed, salt), 1) % 2 = 0;
$$;

CREATE OR REPLACE FUNCTION anon_last_name(seed uuid, salt text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT (ARRAY[
        'Иванов','Смирнов','Кузнецов','Попов','Соколов','Лебедев','Новиков','Морозов',
        'Волков','Зайцев','Павлов','Семёнов','Голубев','Виноградов','Богданов','Воробьёв',
        'Фёдоров','Михайлов','Беляев','Тарасов','Белов','Комаров','Орлов','Киселёв'
    ])[(anon_bits(anon_token(seed, salt), 9) % 24) + 1];
$$;

CREATE OR REPLACE FUNCTION anon_first_name(seed uuid, salt text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN anon_is_male(seed, salt) THEN
        (ARRAY['Александр','Дмитрий','Максим','Сергей','Андрей','Алексей','Иван','Михаил',
               'Никита','Евгений','Артём','Денис'])[(anon_bits(anon_token(seed, salt), 17) % 12) + 1]
    ELSE
        (ARRAY['Елена','Ольга','Наталья','Татьяна','Ирина','Светлана','Анна','Мария',
               'Екатерина','Юлия','Виктория','Дарья'])[(anon_bits(anon_token(seed, salt), 17) % 12) + 1]
    END;
$$;

CREATE OR REPLACE FUNCTION anon_middle_name(seed uuid, salt text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN anon_is_male(seed, salt) THEN
        (ARRAY['Александрович','Дмитриевич','Сергеевич','Андреевич','Алексеевич','Иванович',
               'Михайлович','Петрович'])[(anon_bits(anon_token(seed, salt), 25) % 8) + 1]
    ELSE
        (ARRAY['Александровна','Дмитриевна','Сергеевна','Андреевна','Алексеевна','Ивановна',
               'Михайловна','Петровна'])[(anon_bits(anon_token(seed, salt), 25) % 8) + 1]
    END;
$$;

CREATE OR REPLACE FUNCTION anon_full_name(seed uuid, salt text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT anon_last_name(seed, salt) || ' ' || anon_first_name(seed, salt)
        || ' ' || anon_middle_name(seed, salt);
$$;

-- `u<токен>@example.invalid` — example.invalid зарезервирован RFC 2606, никогда не резолвится.
CREATE OR REPLACE FUNCTION anon_email(seed uuid, salt text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT 'u' || substr(anon_token(seed, salt), 1, 16) || '@example.invalid';
$$;

-- +7 900 XXX-XXXX — диапазон 900 не выдаётся реальным абонентам ни одним российским
-- оператором как часть массовой нумерации федеральных номеров (тестовый блок).
CREATE OR REPLACE FUNCTION anon_phone(seed uuid, salt text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT '+7 900 ' || lpad((anon_bits(anon_token(seed, salt), 1) % 1000)::text, 3, '0')
        || '-' || lpad((anon_bits(anon_token(seed, salt), 9) % 10000)::text, 4, '0');
$$;

-- 11 цифр СНИЛС с ГАРАНТИРОВАННО корректной контрольной суммой: `normalize_snils`
-- (`app/modules/catalog/learner.py`) проверяет контрольное число только для номеров
-- старше 001-001-998 — держим первые 9 цифр строго внутри этого диапазона
-- (1 000 001..1 001 998), тогда последние 2 цифры могут быть любыми.
CREATE OR REPLACE FUNCTION anon_snils(seed uuid, salt text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT lpad((1000000 + (anon_bits(anon_token(seed, salt), 1) % 1998) + 1)::text, 9, '0')
        || lpad((anon_bits(anon_token(seed, salt), 9) % 100)::text, 2, '0');
$$;

-- Серия/номер паспорта и код подразделения — синтетические, без претензии на валидность
-- (паспорт РФ не несёт контрольной суммы, в отличие от СНИЛС).
CREATE OR REPLACE FUNCTION anon_passport_series(seed uuid, salt text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$ SELECT lpad((anon_bits(anon_token(seed, salt), 1) % 10000)::text, 4, '0'); $$;

CREATE OR REPLACE FUNCTION anon_passport_number(seed uuid, salt text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$ SELECT lpad((anon_bits(anon_token(seed, salt), 9) % 1000000)::text, 6, '0'); $$;

CREATE OR REPLACE FUNCTION anon_dept_code(seed uuid, salt text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$ SELECT lpad((anon_bits(anon_token(seed, salt), 17) % 1000)::text, 3, '0'); $$;

-- =============================================================================
-- 2. Пользователи и контакты — ФИО/email/телефон по псевдониму собственного id
-- =============================================================================

UPDATE users SET
    full_name = anon_full_name(id, :'salt'),
    display_name = anon_first_name(id, :'salt') || ' ' || anon_last_name(id, :'salt'),
    email = anon_email(id, :'salt'),
    phone = CASE WHEN phone IS NOT NULL THEN anon_phone(id, :'salt') END,
    keycloak_id = NULL,
    status = 'active',
    status_reason = NULL
WHERE true;

UPDATE contacts SET
    last_name = anon_last_name(id, :'salt'),
    first_name = anon_first_name(id, :'salt'),
    middle_name = CASE WHEN middle_name IS NOT NULL THEN anon_middle_name(id, :'salt') END,
    email = CASE WHEN email IS NOT NULL THEN anon_email(id, :'salt') END,
    phone = CASE WHEN phone IS NOT NULL THEN anon_phone(id, :'salt') END
WHERE true;

-- `value` несёт сам email/телефон/мессенджер-идентификатор независимо от `channel_type` —
-- переписываем единообразно по email-псевдониму того же контакта: тип значения в dev не
-- принципиален, принципиально что реального контакта там больше нет.
UPDATE contact_channels SET value = anon_email(contact_id, :'salt') WHERE true;

-- =============================================================================
-- 3. Профиль учащегося (ContactLearnerProfile) — синтетические, но ВАЛИДНЫЕ значения:
--    normalize_snils должен принять СНИЛС, иначе загрузка шаблона LMS в dev не пройдёт.
-- =============================================================================

UPDATE contact_learner_profiles SET
    snils = anon_snils(contact_id, :'salt'),
    passport_series = anon_passport_series(contact_id, :'salt'),
    passport_number = anon_passport_number(contact_id, :'salt'),
    passport_issued_by = 'ОТДЕЛОМ УФМС (обезличено для dev)',
    passport_issued_at = '2015-01-15',
    passport_dept_code = anon_dept_code(contact_id, :'salt'),
    birth_date = '1995-06-15',
    reg_region = 'Московская область',
    reg_city = 'Пример',
    reg_street = 'ул. Примерная',
    reg_house = '1',
    reg_apartment = CASE WHEN reg_apartment IS NOT NULL THEN '1' END,
    reg_zip = '100000',
    first_name_dative = anon_first_name(contact_id, :'salt'),
    last_name_dative = anon_last_name(contact_id, :'salt'),
    middle_name_dative = CASE
        WHEN middle_name_dative IS NOT NULL THEN anon_middle_name(contact_id, :'salt')
    END,
    diploma_surname = CASE WHEN diploma_surname IS NOT NULL THEN anon_last_name(contact_id, :'salt') END,
    diploma_number = CASE WHEN diploma_number IS NOT NULL THEN anon_passport_number(contact_id, :'salt') END,
    diploma_series = CASE WHEN diploma_series IS NOT NULL THEN anon_passport_series(contact_id, :'salt') END,
    diploma_reg_number = CASE
        WHEN diploma_reg_number IS NOT NULL THEN anon_passport_number(contact_id, :'salt')
    END
WHERE true;

-- =============================================================================
-- 4. Организации: ИП обезличивается целиком (ПДн физлица, dop.md §11.8), вузы — только
--    контактные данные (главный email/телефон), названия и адреса вузов НЕ трогаем
--    (решение владельца, см. докстринг файла). `registry_snapshot` — кэш сырого ответа
--    ЕГРЮЛ/реестра, может дублировать те же поля — обнуляем всегда, кэш восстанавливается
--    по требованию через обычный фоновый опрос реестра.
-- =============================================================================

UPDATE organizations SET
    name = CASE WHEN org_type = 'individual_entrepreneur' THEN 'ИП ' || anon_full_name(id, :'salt') ELSE name END,
    short_name = CASE WHEN org_type = 'individual_entrepreneur' THEN anon_last_name(id, :'salt') ELSE short_name END,
    inn = CASE WHEN org_type = 'individual_entrepreneur' AND inn IS NOT NULL
        THEN lpad((anon_bits(anon_token(id, :'salt'), 1) % 1000000000000)::text, 12, '0')
        ELSE inn
    END,
    legal_address = CASE WHEN org_type = 'individual_entrepreneur' THEN 'г. Пример, ул. Примерная, д. 1' ELSE legal_address END,
    actual_address = CASE WHEN org_type = 'individual_entrepreneur' THEN 'г. Пример, ул. Примерная, д. 1' ELSE actual_address END,
    main_phone = CASE WHEN main_phone IS NOT NULL THEN anon_phone(id, :'salt') END,
    main_email = CASE WHEN main_email IS NOT NULL THEN anon_email(id, :'salt') END,
    registry_snapshot = NULL
WHERE true;

UPDATE organization_licenses SET
    manager_full_name = CASE WHEN manager_full_name IS NOT NULL THEN anon_full_name(id, :'salt') END
WHERE true;

-- Кэш ответов ЕГРЮЛ по ИНН: для ИП несёт то же самое ФИО, что `organizations.name` выше, в
-- чужом формате — проще и надёжнее обезличить целиком, чем поддерживать второй набор правил.
TRUNCATE egrul_entries;

-- =============================================================================
-- 5. Свободный текст: комментарии, задачи, события, вложения — фиктивное содержимое.
--    FK на автора (author_id/created_by) не трогаем: имя автора подставится из уже
--    обезличенных users/contacts при чтении, отдельно анонимизировать не нужно.
-- =============================================================================

UPDATE deal_comments SET body = '[обезличено для dev] текст комментария' WHERE true;
UPDATE deal_comment_revisions SET body = '[обезличено для dev] прежняя редакция комментария' WHERE true;
UPDATE tasks SET description = CASE WHEN description IS NOT NULL
    THEN '[обезличено для dev] описание задачи' END WHERE true;
UPDATE attachments SET description = CASE WHEN description IS NOT NULL
    THEN '[обезличено для dev] описание вложения' END WHERE true;
UPDATE deal_events SET payload = '{}'::jsonb WHERE true;
UPDATE outbox_events SET payload = '{}'::jsonb WHERE true;
UPDATE inbound_messages SET raw_payload = '{}'::jsonb WHERE true;
UPDATE import_row_results SET row_data = '{}'::jsonb, before_snapshot = '{}'::jsonb WHERE true;
UPDATE learning_progress SET raw = '{}'::jsonb WHERE true;
UPDATE data_erasure_requests SET
    reason = '[обезличено для dev]',
    rejection_reason = CASE WHEN rejection_reason IS NOT NULL THEN '[обезличено для dev]' END
WHERE true;

-- Свободный текст комментария к переходу статуса: роль приложения не может его менять
-- (REVOKE UPDATE в `0004_deals_sprint.py`), но суперпользователь, которым выполняется этот
-- скрипт, REVOKE не касается — к триггерам (как `audit_log`/`signatures` выше) это не
-- относится, здесь просто обычный UPDATE.
UPDATE deal_status_history SET comment = CASE WHEN comment IS NOT NULL
    THEN '[обезличено для dev] комментарий к переходу' END WHERE true;
UPDATE organization_licenses SET comment = CASE WHEN comment IS NOT NULL
    THEN '[обезличено для dev]' END WHERE true;
UPDATE admin_approvals SET
    reason = CASE WHEN reason IS NOT NULL THEN '[обезличено для dev]' END,
    payload = '{}'::jsonb
WHERE true;
UPDATE edm_agreements SET revoke_reason = CASE WHEN revoke_reason IS NOT NULL
    THEN '[обезличено для dev]' END WHERE true;
UPDATE signature_documents SET
    title = '[обезличено для dev] документ на подпись',
    void_reason = CASE WHEN void_reason IS NOT NULL THEN '[обезличено для dev]' END
WHERE true;
UPDATE signature_requests SET reject_reason = CASE WHEN reject_reason IS NOT NULL
    THEN '[обезличено для dev]' END WHERE true;
-- Заголовки сделок/задач/инцидентов — короткие, обычно шаблонные, но пользователь мог
-- вписать туда что угодно (ФИО, название проекта) — безопаснее переписать, чем гадать.
UPDATE deals SET title = '[обезличено для dev] сделка' WHERE true;
UPDATE tasks SET title = '[обезличено для dev] задача' WHERE true;
UPDATE security_incidents SET
    title = '[обезличено для dev] инцидент',
    description = '[обезличено для dev]'
WHERE true;

-- =============================================================================
-- 6. Подписание: подписант — псевдоним СВЯЗАННОГО пользователя/контакта (не самой строки
--    подписи), чтобы совпадать с его именем в users/contacts после переобезличивания выше.
-- =============================================================================

-- `signer_identifier_masked` — "уже замаскированный" идентификатор для экрана ревью
-- (`iv***@...`), но домен в нём читается как есть: обнаружено прямой проверкой (тест на
-- исходное значение всё равно оставался бы в дампе без этой строки).
UPDATE signature_requests r SET
    signer_name_snapshot = CASE WHEN r.signer_name_snapshot IS NOT NULL
        THEN anon_full_name(COALESCE(r.signer_user_id, r.signer_contact_id, r.id), :'salt')
    END,
    signer_identifier_masked = CASE WHEN r.signer_identifier_masked IS NOT NULL
        THEN substr(anon_email(COALESCE(r.signer_user_id, r.signer_contact_id, r.id), :'salt'), 1, 2)
            || '***@example.invalid'
    END
WHERE true;

-- `signatures` несёт триггер неизменяемости `trg_signatures_immutable`
-- (`0007_signing_sprint.py`): после коммита меняться может только `is_disputed`, любая
-- другая правка — "signatures is append-only: only is_disputed may change" (проверено).
-- Отключаем на время этого блока, как `audit_log` выше (не партиционирована — без цикла).
ALTER TABLE signatures DISABLE TRIGGER USER;

UPDATE signatures s SET
    signer_display = anon_full_name(
        COALESCE(req.signer_user_id, req.signer_contact_id, s.id), :'salt'
    ),
    evidence = '{}'::jsonb,
    ip = NULL,
    user_agent = NULL
FROM signature_requests req
WHERE s.request_id = req.id;

-- Подпись без найденного запроса (не должно случаться при целой БД, защитная ветка) —
-- всё равно не оставляет реальное имя/evidence/ip.
UPDATE signatures s SET
    signer_display = anon_full_name(s.id, :'salt'),
    evidence = '{}'::jsonb,
    ip = NULL,
    user_agent = NULL
WHERE NOT EXISTS (SELECT 1 FROM signature_requests req WHERE req.id = s.request_id);

ALTER TABLE signatures ENABLE TRIGGER USER;

UPDATE consents SET
    ip = NULL,
    user_agent = NULL
WHERE true;

-- =============================================================================
-- 7. Журнал аудита — ЦЕЛИКОМ: партиционная append-only таблица с триггером
--    неизменяемости (`0002_identity_sprint.py`), переписать строки по отдельности —
--    не вариант. Отключаем пользовательские триггеры ТОЛЬКО в этой временной базе,
--    чистим и сбрасываем голову цепочки: dev не проверяет цепочку аудита существующих
--    записей, только может писать новые с нуля.
-- =============================================================================

-- `audit_log` партиционирована по месяцам (`audit_log_attach_guards`) — каждая партиция несёт
-- СВОЮ копию обоих триггеров (`0002_identity_sprint.py`), и `ALTER TABLE audit_log DISABLE
-- TRIGGER USER` на родителе их не трогает (проверено: TRUNCATE родителя всё равно падал с
-- "audit_log is append-only"). Отключаем на родителе и на каждой партиции в цикле.
DO $$
DECLARE
    rel regclass;
BEGIN
    EXECUTE 'ALTER TABLE audit_log DISABLE TRIGGER USER';
    FOR rel IN SELECT inhrelid::regclass FROM pg_inherits WHERE inhparent = 'audit_log'::regclass
    LOOP
        EXECUTE format('ALTER TABLE %s DISABLE TRIGGER USER', rel);
    END LOOP;
END $$;

TRUNCATE audit_log;

DO $$
DECLARE
    rel regclass;
BEGIN
    EXECUTE 'ALTER TABLE audit_log ENABLE TRIGGER USER';
    FOR rel IN SELECT inhrelid::regclass FROM pg_inherits WHERE inhparent = 'audit_log'::regclass
    LOOP
        EXECUTE format('ALTER TABLE %s ENABLE TRIGGER USER', rel);
    END LOOP;
END $$;

UPDATE audit_chain_head SET hash = NULL;

-- =============================================================================
-- 8. Секреты и персональные операционные данные, не нужные в dev — TRUNCATE.
--    Файлы: сами объекты в S3 этот дамп не несёт, поэтому помечаем как удалённые —
--    скачивание в dev честно ответит «файла нет», а не отдаст чужой боевой storage_key.
-- =============================================================================

UPDATE system_settings SET value = 'null'::jsonb WHERE is_secret;
-- `original_filename` — как пользователь назвал файл при загрузке, может содержать ФИО
-- (`Паспорт_Иванова.pdf`) независимо от содержимого самого файла; `storage_key` бессмыслен
-- без объекта в S3 (его в дампе нет), переписываем на всякий случай тоже.
UPDATE files SET
    status = 'deleted',
    original_filename = '[обезличено для dev].bin',
    storage_key = 'deleted/' || id::text
WHERE true;

-- notification_deliveries.notification_id -> notifications.id: в одной команде, иначе
-- TRUNCATE notifications один падает на FK (проверено).
TRUNCATE notifications, notification_deliveries;
TRUNCATE user_invites;
TRUNCATE signature_otp_codes;
TRUNCATE idempotency_keys;
TRUNCATE security_events;
TRUNCATE org_lookup_log;

-- =============================================================================
-- 9. Вспомогательные функции — не нужны в самом дампе, только во время обезличивания.
-- =============================================================================

DROP FUNCTION anon_token(uuid, text);
DROP FUNCTION anon_bits(text, int);
DROP FUNCTION anon_is_male(uuid, text);
DROP FUNCTION anon_last_name(uuid, text);
DROP FUNCTION anon_first_name(uuid, text);
DROP FUNCTION anon_middle_name(uuid, text);
DROP FUNCTION anon_full_name(uuid, text);
DROP FUNCTION anon_email(uuid, text);
DROP FUNCTION anon_phone(uuid, text);
DROP FUNCTION anon_snils(uuid, text);
DROP FUNCTION anon_passport_series(uuid, text);
DROP FUNCTION anon_passport_number(uuid, text);
DROP FUNCTION anon_dept_code(uuid, text);

\echo 'anonymize.sql: готово'
