-- B6-13 (`anonymized_dump.sh --link-demo-accounts`): переименовать по ОДНОМУ самому раннему
-- пользователю каждой роли в email демо-учётки (`deploy/keycloak/realm-crm.json`) — вход
-- привязывает `keycloak_id` по email при первом логине (`app/modules/identity/service.py`),
-- а `anonymize.sql` уже обнулил `keycloak_id` у всех. Запускать ПОСЛЕ `anonymize.sql`: ФИО
-- этих пользователей остаются псевдонимами, меняется только email, которым входят в Keycloak.
--
-- Сервисный `INTEGRATION` не входит через UI (API-ключ/Bearer, не пароль) — демо-учётки для
-- него в `realm-crm.json` нет, роль в списке ниже не участвует.

\set ON_ERROR_STOP on

UPDATE users u SET email = 'admin@rt-it-school.ru'
FROM (SELECT id FROM users WHERE role = 'ADMIN' AND deleted_at IS NULL ORDER BY created_at LIMIT 1) t
WHERE u.id = t.id;

UPDATE users u SET email = 'petrov@rt-it-school.ru'
FROM (SELECT id FROM users WHERE role = 'HEAD' AND deleted_at IS NULL ORDER BY created_at LIMIT 1) t
WHERE u.id = t.id;

UPDATE users u SET email = 'ivanov@rt-it-school.ru'
FROM (SELECT id FROM users WHERE role = 'KAM' AND deleted_at IS NULL ORDER BY created_at LIMIT 1) t
WHERE u.id = t.id;

UPDATE users u SET email = 'smirnov@rt-it-school.ru'
FROM (SELECT id FROM users WHERE role = 'AUDITOR' AND deleted_at IS NULL ORDER BY created_at LIMIT 1) t
WHERE u.id = t.id;

\echo 'link_demo_accounts.sql: готово'
