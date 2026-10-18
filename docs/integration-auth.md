# Доступ внешних систем (B5-10)

Два способа получить доступ к API от имени роли `INTEGRATION` — сервисной учётки без
браузерной сессии. Выбор между ними — вопрос владельцу/организаторам (см. README, раздел
«Открытые вопросы»): OAuth2 подходит системе, умеющей `client_credentials`; API-ключ — любой
системе, умеющей только добавить заголовок.

## Вариант A: OAuth2 client_credentials (Keycloak)

Требует клиента `crm-integration` в realm (заводится в спринте D1, confidential,
`serviceAccountsEnabled: true`, роль `INTEGRATION` у сервисной учётки, audience-маппер на
`crm-bff` — без него токен не пройдёт проверку аудитории, см. `core/security.py`). Без этого
клиента в вашем realm раздел ниже не сработает — доступны только API-ключи (вариант B).

```bash
# 1. Получить токен по client_credentials. $KEYCLOAK — публичный адрес Keycloak
#    (settings.keycloak_url), $SECRET — секрет клиента crm-integration из вашего хранилища.
TOKEN=$(curl -s -X POST "$KEYCLOAK/realms/crm/protocol/openid-connect/token" \
  -d grant_type=client_credentials \
  -d client_id=crm-integration \
  -d client_secret="$SECRET" \
  | jq -r .access_token)

# 2. Вызвать API с этим токеном. В профиле prod Bearer без сессии разрешён только роли
#    INTEGRATION (settings.bearer_auth_mode == "integration_only") — обычный пользователь
#    получит 401 даже с действительным токеном своей роли.
curl -H "Authorization: Bearer $TOKEN" "$CRM/api/deals?limit=10"
```

Что видит эта роль: `GET /api/deals` — только сделки, у которых `created_by` равен самой
сервисной учётке (`DealScope.SOURCE`, `app/core/permissions.py::deal_scope_for`); контакты в
ответе остаются маскированными (`contact:reveal` роли не выдан). Согласие на обработку ПДн и
обязательная смена пароля для неё не проверяются (`core/deps.py::get_consented_principal`) — это
не субъект ПДн и не вводит пароль.

## Вариант B: API-ключ (`X-API-Key`)

Не требует Keycloak вовсе. Ключ выпускает администратор через административный API; значение
показывается РОВНО ОДИН РАЗ, в БД хранится только его SHA-256-хэш.

```bash
# 1. ADMIN создаёт служебную учётку роли INTEGRATION (если она ещё не создана сидом —
#    integration.seed.seed_integration_account), затем выпускает ей ключ. $ADMIN_SESSION —
#    cookie+CSRF вошедшего администратора (см. README, раздел «Аутентификация»).
curl -s -X POST "$CRM/api/admin/integrations/api-keys" \
  -H "Content-Type: application/json" \
  -H "X-CSRF-Token: $ADMIN_CSRF" \
  -b "$ADMIN_SESSION" \
  -d '{"name": "1С: обмен сделками", "user_id": "<id учётки роли INTEGRATION>", "expires_in_days": 90}'
# → {"id": "...", "key": "crm_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX", "key_prefix": "crm_XXXXXXXX", ...}
# Сохраните поле "key" сразу: повторный GET его не вернёт.

# 2. Вызвать API с этим ключом — заголовок проверяется раньше cookie и Bearer.
curl -H "X-API-Key: crm_XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX" \
  "$CRM/api/deals?limit=10"
```

Срок жизни ключа — `expires_in_days` (по умолчанию `API_KEY_DEFAULT_TTL_DAYS=90`), не продлевается
автоматически: выпустите новый и отзовите старый (`DELETE /api/admin/integrations/api-keys/{id}`).
Блокировка самой учётки (`users.status != 'active'`) или смена её роли с `INTEGRATION` на другую
аннулирует ключ немедленно — проверка идёт при каждом запросе, не только при выпуске. Видимость
данных та же, что у варианта A (скоуп `DealScope.SOURCE`, контакты маскированы).

Откат без деплоя: `API_KEYS_ENABLED=false` — заголовок `X-API-Key` не проверяется вовсе, как
будто этой возможности нет; существующие ключи остаются в БД и снова заработают при возврате
флага в `true`.

## Проверка на стенде

- `APP_PROFILE=prod`: обычный пользователь с Bearer-токеном своей сессии получает 401 (сервисная
  аутентификация не подменяет BFF-сессию ни для кого, кроме `INTEGRATION`).
- `X-API-Key` неизвестного или отозванного ключа — 401, без уточнения причины.
- Ключ пользователя НЕ роли `INTEGRATION` не создаётся — 422 при `POST .../api-keys`.
