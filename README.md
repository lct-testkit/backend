<div align="center">

# RTK School CRM — бэкенд

**Модульный монолит на FastAPI для CRM ИТ Школы Ростелекома: закрытый контур, вход через Keycloak, неизменяемый аудит и простая электронная подпись**

<sub>Команда **«Тесткит»** — [github.com/lct-testkit](https://github.com/lct-testkit)</sub>

<!--STATS-->
**181** операция API &nbsp;·&nbsp; **13** модулей &nbsp;·&nbsp; **65** таблиц &nbsp;·&nbsp; **15** миграций &nbsp;·&nbsp; **458** тестов &nbsp;·&nbsp; **11** сервисов Compose
<!--/STATS-->

[Быстрый старт](#быстрый-старт) · [Примеры](#примеры-использования) · [Как устроено](#как-устроено) · [Модули](#что-внутри) · [Настройка](#настройка) · [Разработка](#разработка) · [Безопасность](#безопасность) · [Ограничения](#ограничения-и-известные-проблемы)

| <img src="docs/img/swagger.png" width="410" alt="Swagger UI на /api/docs: контракт OpenAPI, схема авторизации Bearer"> | <img src="docs/img/keycloak-login.png" width="410" alt="Страница входа Keycloak, realm crm"> |
|:-:|:-:|
| *Swagger UI — `/api/docs`, OpenAPI 3.0.3* | *вход Keycloak — `/auth`, realm `crm`* |

<sub>Обе страницы отдаёт один Caddy на порту 8080. Swagger UI подключён из локального пакета, без CDN: контур закрыт и в рантайме не ходит в интернет.</sub>

</div>

## Почему это интересно

* **Закрытый контур, запуск одной командой.** `docker compose up -d --build` поднимает 11 сервисов; интернет нужен только на сборке образов. Swagger UI отдаётся из локального пакета, шрифт с кириллицей для PDF вложен в образ — CDN не нужны.
* **BFF: токены Keycloak не покидают Redis.** Браузер получает только httpOnly-cookie сессии (`Secure` вне профиля `dev`, `SameSite=Lax`) и CSRF-cookie. Вход — OIDC с `state`, `nonce` и PKCE S256, `id_token` проверяется полностью, обновление токена идёт под локом.
* **Идемпотентность и оптимистичные блокировки.** `Idempotency-Key` (8–255 символов, ключ живёт внутри актора) на создании сделок, организаций и контактов: тот же ключ с другим телом — `409 CRM-1003`. Изменения требуют `If-Match` с `version`, конфликт — `409 CRM-1002` с актуальными значениями.
* **RFC 7807 везде.** Любая ошибка — `application/problem+json` с кодом из каталога `CRM-XXYY` (45 кодов) и `request_id`; стектрейсы наружу не уходят.
* **Неизменяемый аудит с цепочкой хэшей.** `audit_log` партиционирован по месяцам, триггеры на каждой партиции запрещают `UPDATE`, `DELETE` и `TRUNCATE`, каждая запись хранит `prev_hash` и SHA-256 `hash`; `GET /api/admin/audit/verify-chain` пересчитывает хвост цепочки. Запись идёт в той же транзакции, что и бизнес-изменение; отказы в доступе тоже попадают в журнал.
* **ПЭП с проверкой.** Одноразовый код (6 цифр, в базе только bcrypt-хэш, 3 попытки, 5 минут), HMAC-SHA256 метка целостности, цепочка хэшей подписей, неизменяемая таблица `signatures`, доверенное время из контейнера `ntp` (рассинхрон больше 5 с блокирует подписание), публичная проверка `/public/verify/{id}` и проверка файла по хэшу.
* **152-ФЗ и обезличивание.** Согласие на обработку ПДн хранится с версией политики и SHA-256 её текста (до принятия бизнес-ручки отвечают `403 CRM-1105`); маскирование телефонов и email; запросы на удаление или обезличивание с блокерами, отсрочкой 30 дней, подтверждением второго администратора и актом об уничтожении.
* **Воронки — граф с публикацией и мастером сопоставления.** Статусы и переходы с DSL условий (белый список полей), валидация графа, публикация со снимком и `graph_hash`: сделки живут по опубликованному снимку. Архивирование статуса — мастер сопоставления, который переносит сделки батчами в фоне.
* **RBAC, иерархия команд и «четыре глаза».** Пять ролей, 40 прав и три уровня проверки: маршрут, объект, SQL-скоуп списка (свои сделки, команда рекурсивно по дереву `teams`, все, только созданные источником). Необратимые админ-операции подтверждает второй администратор (`CRM-1902`).

## Как это выглядит

> Картинки этого файла — `docs/img/swagger.png` и `docs/img/keycloak-login.png`: оба экрана открываются на http://localhost:8080 после запуска стека.

<img src="docs/img/swagger.png" width="720" alt="Swagger UI: 29 групп ручек, кнопка Authorize для Bearer-токена">

*Swagger UI на `/api/docs`: 181 операция в 30 группах, схема `BearerAuth` (вставьте access-токен Keycloak в Authorize). В `prod` интерактивный UI закрыт, схема `/api/openapi.json` остаётся.*

<img src="docs/img/keycloak-login.png" width="410" alt="Форма входа Keycloak, realm crm">

*Вход: `GET /api/auth/login` отправляет браузер в Keycloak, после ввода пароля бэкенд создаёт серверную сессию.*

```mermaid
flowchart LR
  A[Браузер] -->|GET /api/auth/login| B[api: state + nonce + PKCE в Redis, 10 мин]
  B -->|307| C[Keycloak: форма входа, realm crm]
  C -->|code + state| D[api: /api/auth/callback]
  D -->|обмен кода, проверка id_token| C
  D -->|токены| E[(Redis: session:sid)]
  D -->|Set-Cookie: crm_sid httpOnly + crm_csrf| A
  A -->|cookie + X-CSRF-Token| F[api: бизнес-ручки]
  F -->|токен из сессии, права из кэша| E
```

## Быстрый старт

Нужны Docker с Compose v2. Интернет требуется только на сборке образов, в рантайме контур закрыт.

```bash
cp .env.example .env
```

```bash
docker compose up -d --build
```

Поднимутся 11 сервисов: `postgres`, `redis`, `seaweedfs`, `keycloak`, `ntp`, `sms-gateway-mock`, разовые `migrate` (миграции) и `seed` (две демо-воронки: `b2b_university_v1` на 14 шагов, `b2c_individual_v1` на 6), затем `api`, `worker` и `caddy`. `migrate` и `seed` после завершения видны только в `docker compose ps -a` (`Exited (0)`); `api` и `worker` стартуют после успешного `seed`, `api` дополнительно ждёт готовности Keycloak. На чистой машине это около минуты, дольше всего поднимается Keycloak.

```bash
curl http://localhost:8080/health/ready
```

Ожидается `"status":"ok"` и `"ok":true` у `postgres`, `redis`, `keycloak_jwks`, `seaweedfs` и `queue`. Веб-клиент подключается профилем `web` (см. ниже); без него на `/` Caddy отвечает ошибкой, API и Keycloak работают.

| Адрес | Что это |
|---|---|
| http://localhost:8080/api/docs | Swagger UI (в профиле `prod` закрыт) |
| http://localhost:8080/api/openapi.json | OpenAPI 3.0.3, 181 операция |
| http://localhost:8080/health/live | liveness |
| http://localhost:8080/health/ready | готовность: БД, Redis, JWKS Keycloak, SeaweedFS, очередь |
| http://localhost:8080/auth | Keycloak, realm `crm`; консоль — `/auth/admin` |
| http://localhost:8080/ | веб-клиент (профиль `web` или Vite через `WEB_UPSTREAM`) |
| http://localhost:8333 | SeaweedFS S3 через Caddy: presigned-ссылки, браузер ходит сюда напрямую, минуя API |
| localhost:5433 | PostgreSQL для pgAdmin (`POSTGRES_PORT`): база `crm`, пользователь `crm` |
| https://localhost:8443 | то же по TLS (`tls internal`, самоподписанный сертификат — предупреждение браузера ожидаемо) |

Метрики Prometheus доступны только внутри сети (`api:8000/metrics`): через Caddy `/metrics` отвечает `404`.

Демо-учётки создаются при импорте realm `crm` (`deploy/keycloak/realm-crm.json`). **Только для демо**, в настоящей эксплуатации заменить:

| Логин | Пароль | Роль |
|---|---|---|
| `kam.ivanov` | `Kam123456789!` | KAM: свои сделки, организации, подписи |
| `head.petrov` | `Head12345678!` | HEAD: сделки команды, импорт, переназначение |
| `admin.crm` | `Admin12345678!` | ADMIN: все права |
| `admin.volkov` | `Volkov12345678!` | ADMIN (второй, для «четырёх глаз») |
| `auditor.smirnov` | `Audit12345678!` | AUDITOR: журнал аудита |
| `admin` (консоль Keycloak) | `admin` | `/auth/admin` (`KEYCLOAK_ADMIN`, `KEYCLOAK_ADMIN_PASSWORD`) |

Три вещи, о которые спотыкаются:

* **Профиль.** В `.env.example` стоит `APP_PROFILE=dev`, а дефолт в compose — `demo`. `dev`: cookie без `Secure`; `dev` и `demo`: Swagger UI, вход по Bearer для всех ролей, CORS для `localhost:5173` и `localhost:3000`, заглушка автоподстановки ИНН, код ПЭП в поле `debug_code`. `prod`: Swagger UI закрыт (схема остаётся), Bearer только у роли `INTEGRATION`, всё перечисленное выше выключено.
* **Переменные.** В контейнеры `api`, `worker`, `migrate` и `seed` попадают только переменные из списка `x-api-env` в `docker-compose.yml` (`env_file` не используется): значения из `.env` для остальных настроек (лимиты, таймауты) не действуют, пока их не добавят в этот список. Подробнее — «Настройка».
* **Код не монтируется.** Образы `api`, `worker`, `migrate`, `seed` и `sms-gateway-mock` собираются из `Dockerfile` (`COPY app`, `COPY migrations`), bind-mount нет. Правка кода требует пересборки: `docker compose up -d --build api worker`.

Веб-клиент собирается из соседнего репозитория `../frontend` (нужен пакет дизайн-системы, см. [`../README.md`](../README.md)):

```bash
docker compose --profile web up -d --build
```

`api` горизонтально масштабируется репликами: у сервиса нет `container_name` и портов на хосте, Caddy находит реплики через DNS (`dynamic a`, обновление раз в 10 секунд) и балансирует по `least_conn`. Один uvicorn-воркер на контейнер: реестр `prometheus_client` живёт в памяти процесса, и при нескольких воркерах scrape видел бы лишь часть трафика.

```bash
docker compose up -d --scale api=3
```

## Примеры использования

Все команды и ответы ниже проверены на живом стенде (тот же `docker compose up -d --build`, демо-учётки из «Быстрого старта»). Ответы урезаны — длинные значения и повторяющиеся поля отмечены `…`.

### Вход и профиль (password grant)

Password grant — прямой обмен пароля на токен Keycloak, минуя браузерный OIDC-поток; удобен для проверки контура и для сервисных вызовов. `client_secret` — из вашего `.env` (`KEYCLOAK_CLIENT_SECRET`, по умолчанию `crm-bff-secret`).

<details>
<summary>Код и ответ (18 строк)</summary>

```bash
SECRET=$(grep '^KEYCLOAK_CLIENT_SECRET=' .env | cut -d= -f2-)

TOKEN=$(curl -s http://localhost:8080/auth/realms/crm/protocol/openid-connect/token \
  -d grant_type=password -d client_id=crm-bff -d client_secret="$SECRET" \
  -d scope=openid -d username=kam.ivanov --data-urlencode 'password=Kam123456789!' \
  | sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p')

curl -s http://localhost:8080/api/me -H "Authorization: Bearer $TOKEN"
```

```json
{"id":"01a0bf55-486b-7000-9454-4c74200b2209","full_name":"Иван Тесткитович","email":"ivanov@rt-it-school.ru",
 "role":"KAM","team_id":"01a0c00e-…","status":"active","locale":"ru","timezone":"Europe/Moscow",
 "consent_required":false,"password_change_required":false,
 "scopes":["catalog:read","contact:read","contact:reveal","contact:write","deal:create","deal:read", …],
 "teams":["01a0c00e-…"],"perm_epoch":1,"last_login_at":"2026-09-20T18:23:56.579722Z","version":2}
```

</details>

Без токена бизнес-ручки отвечают RFC 7807:

```bash
curl -s http://localhost:8080/api/deals
```

```json
{"type":"https://crm.rt-it-school.ru/problems/crm-1101","title":"Пользователь не аутентифицирован",
 "status":401,"detail":"Сессия не найдена: выполните вход","instance":"/api/deals",
 "request_id":"cfe79a19-…","code":"CRM-1101"}
```

### Список сделок: курсорная пагинация

```bash
curl -s "http://localhost:8080/api/deals?limit=3" -H "Authorization: Bearer $TOKEN"
```

Ответ — `{"items": [...], "next_cursor": "eyJ2Ijoi…"}`; три сделки на странице, каждая с `number`, `title`, `amount` строкой (`"150000.00"`) и отдельным `currency`, `version` для `If-Match`. Тот же `next_cursor`, переданный в `&cursor=`, возвращает следующие три без пересечений — проверено (id страниц 1 и 2 не совпали). `limit` больше 100 отклоняется `422 CRM-1001`.

### Проверка цепочки аудита

```bash
curl -s "http://localhost:8080/api/admin/audit/verify-chain?limit=1000" -H "Authorization: Bearer $ADMIN_TOKEN"
```

```json
{"checked":1000,"ok":false,"problems":["01a0c4a1-…: разрыв цепочки с предыдущей записью", …]}
```

На этом стенде хвост цепочки честно показал разрывы (интерактивная проверка эндпоинтов из этого README писала записи из нескольких параллельных запросов почти одновременно, а голова цепочки берётся под advisory-локом только на запись — гонка обнаруживается самой проверкой, что и есть цель ручки). Короткие окна (`limit=10..20`) в тот же момент были целыми (`"ok":true`) — разрывы сосредоточены в конкретных секундах параллельной нагрузки, не во всей цепочке.

### Скачать контракт OpenAPI

```bash
curl -s http://localhost:8080/api/openapi.json -o openapi.json
```

`openapi.json` — 3.0.3, 146 путей, 181 операция, 213 схем, единственная схема авторизации `BearerAuth`. Тот же файл — источник для генератора клиента фронтенда (`frontend/tools/gen-api.mjs`).

### Автоподстановка и проверка ИНН

```bash
curl -s -X POST http://localhost:8080/api/org-lookup/validate \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"kind":"inn","value":"7707083893"}'
```

```json
{"ok":true,"reason":null}
```

```bash
curl -s "http://localhost:8080/api/org-lookup/suggest?q=7707&limit=2" -H "Authorization: Bearer $TOKEN"
```

```json
{"items":[{"inn":"7707049388","name":"ПАО «Ростелеком»","region":null,"status":"active","is_liquidated":false,"provider":"mock"}]}
```

`provider":"mock"` — цепочка «локальный реестр ЕГРЮЛ → подтверждённые организации нашей БД → мок-провайдер»; мок подключается только вне `prod` (`EXTERNAL_ORG_LOOKUP_ENABLED`, по умолчанию `false`, реальных внешних провайдеров в коде нет).

### Redirect-safe вход (проверка исправления открытого редиректа)

```bash
curl -si "http://localhost:8080/api/auth/login?next=/deals" | grep -i location
curl -si "http://localhost:8080/api/auth/login?next=https://evil.example" | grep -i location
```

Оба ответа — `307` на Keycloak с одинаковой формой `redirect_uri=…/api/auth/callback`; посторонний адрес в `next` не влияет на редирект — он либо сохраняется в Redis как безопасный путь (`/deals`), либо отбрасывается в `null` (`app/modules/identity/redirects.py::safe_next_path`, проверено `tests/test_redirects.py`).

## Как устроено

```mermaid
flowchart LR
  Browser[Браузер] --> Caddy[Caddy :8080 / :8443]
  Caddy -->|"/api/* /public/* /health/*"| Api[api: FastAPI]
  Caddy -->|"/auth/*"| Keycloak[Keycloak: realm crm]
  Caddy -->|остальное| Web["web: SPA (профиль web)"]
  Caddy -->|":8333, минуя api"| Seaweed[(SeaweedFS: S3)]
  Api --> Postgres[(PostgreSQL 16)]
  Api --> Redis[(Redis 7)]
  Api -.->|presigned PUT/GET| Seaweed
  Api --> Keycloak
  Worker[worker: arq] --> RedisQ[(Redis: очередь arq)]
  Worker --> Postgres
  Worker --> Seaweed
  Api -.->|события отправляет| RedisQ
```

| Слой | Что делает |
|---|---|
| **Caddy** | единая точка входа, TLS-терминация (`:8443`), CSP/HSTS/`X-Request-Id`, реверс-прокси на `api` по DNS (`dynamic a` + `least_conn` — так работает `--scale api=N`), на `keycloak` под `/auth` без среза префикса, на `web` или `WEB_UPSTREAM` — всё остальное; отдельный сайт `:8333` — прямой проход к SeaweedFS для presigned-ссылок |
| **api** | FastAPI, модульный монолит; один uvicorn-воркер на контейнер, HTTP на 8000 внутри сети |
| **worker** | тот же образ, команда `arq app.worker.main.WorkerSettings`; периодические задачи по cron и очередь событий |
| **postgres** | основное хранилище приложения и отдельная база `keycloak` в том же кластере |
| **redis** | сессии BFF, кэш прав (`cache:perm:{user_id}`, TTL 5 мин), локи, идемпотентность, очередь arq, rate-limit |
| **seaweedfs** | S3-совместимое хранилище файлов, вложений, отчётов, реестра, подписанных документов; MinIO запрещён спецификацией |
| **keycloak** | OIDC-провайдер, realm `crm`, роли, политика паролей, брутфорс-защита |
| **ntp** | доверенное время для штампа подписания (`ntplib`, UDP/123); при недоступности — деградация до системных часов, а не отказ подписания |
| **sms-gateway-mock** | мок внешнего SMS-провайдера для доставки OTP ПЭП внутри закрытого контура (тот же образ `api`, команда `sms-gateway-mock`) |
| **web** *(профиль `web`)* | статический SPA-образ фронтенда за тем же Caddy |

Приложение — модульный монолит: один деплоймент-артефакт (`app/main.py`, 39 вызовов `include_router`), внутри — 13 независимых пакетов в `app/modules/`. Модуль не импортирует репозитории другого модуля напрямую; кросс-модульные вызовы идут через сервисные Protocol-интерфейсы (`OwnershipService`, `DealStatusService`, `NotificationService`, `OutboxService`, `SigningService`, `AntivirusScanner`, `OrgLookupProvider`) с регистрацией реализации на старте `api`/`worker` — так любой модуль можно вынести отдельным сервисом без переписывания вызывающего кода.

## Что внутри

Число операций на модуль — по тегам `GET /api/openapi.json`; 13 модулей в сумме дают 179, плюс 2 health-пробы вне модульной системы (`GET /health/live`, `/health/ready` — объявлены в `app/main.py`, ни один `app/modules/*` пакет их не владеет) — 181 всего.

| Модуль | Что делает | Ключевые эндпоинты · таблицы |
|---|---|---|
| `admin` | системные настройки, флаги, чтение/экспорт журнала аудита и проверка его цепочки | `GET/PATCH /api/admin/feature-flags`, `GET/PUT /api/admin/system-settings`, `GET /api/admin/audit`, `/export`, `/verify-chain` — 7 операций · `feature_flags`, `system_settings`, `admin_approvals`, `idempotency_keys` |
| `audit` | сервис записи в неизменяемый журнал; своих эндпоинтов нет — вызывается всеми модулями в той же транзакции, что бизнес-изменение | 0 операций · `audit_log` (партиции по месяцам, триггеры неизменяемости на каждой) |
| `catalog` | организации, контакты, продукты, справочники (направления, причины отказа, календарь, пользовательские поля, регионы), лицензии/договоры вуз-вендор-ПО | `GET/POST /api/organizations`, `/contacts`, `/products` + 5 справочников + `GET /api/organization-licenses` (раздел 4, Треб.1), `DELETE` у направлений и причин отказа — 32 операции · 11 таблиц (`organizations`, `contacts`, `products`, `directions`, `regions`, `organization_licenses`, …) |
| `crm` | сделки, переходы по воронке, комментарии, задачи, участники | `GET/POST /api/deals`, `/{id}/transition`, `/reassign`, `/comments`, `/tasks` — 20 операций · 8 таблиц (`deals`, `deal_status_history`, `deal_comments`, `tasks`, …) |
| `files` | загрузка через presigned-URL, magic-bytes и антивирус-заглушка, вложения к сущностям | `POST /api/files/upload-intent`, `/{id}/commit`, `GET /api/attachments` — 7 операций · `files`, `attachments` |
| `identity` | аутентификация BFF/OIDC, администрирование пользователей и команд, приглашения, согласие, обезличивание | `GET/POST /api/auth/*`, `GET /api/me`, `GET/POST /api/admin/users`, `/teams`, `/approvals` — 37 операций · 7 таблиц (`users`, `teams`, `consents`, `data_erasure_requests`, …) |
| `imports` | импорт каталогов из xlsx/xls/csv: профилирование, автоподбор маппинга, dry-run, применение, откат | `POST /api/imports`, `/{id}/dry-run`, `/apply`, `/rollback`, `PUT /{id}/mapping` — 9 операций · `import_jobs`, `import_row_results`, `import_presets` |
| `integration` | вебхуки CMS/LMS/Bitrix24 с HMAC-подписью, исходящий outbox с backoff, административный контур источников | `POST /api/v1/integrations/{cms,lms,bitrix}/*`, `GET/PATCH /api/admin/integrations/sources` — 8 операций · 6 таблиц (`integration_sources`, `inbound_messages`, `outbox_events`, …) |
| `notification` | уведомления (in-app, заглушки email/telegram), шаблоны Jinja2, настройки получателя | `GET /api/notifications`, `/read`, `GET/PUT /api/me/notification-prefs`, `GET/POST/DELETE /api/admin/notification-templates` — 8 операций · 4 таблицы |
| `registry` | автоподстановка и проверка ИНН/ОГРН, локальный реестр ЕГРЮЛ, сверка реквизитов (drift) | `GET /api/org-lookup/suggest`, `POST /validate`, `POST /api/admin/registry/import`, `DELETE /api/admin/registry/versions/{id}` — 6 операций · 4 таблицы (`registry_versions`, `egrul_entries`, …) |
| `reporting` | 8 видов отчётов (xlsx/pdf/png через matplotlib и xhtml2pdf), дашборды с виджетами | `GET/POST /api/reports`, `/report-templates`, `GET /{id}/data` (датасет в JSON без файла), `GET/POST /api/dashboards`, `/widgets` — 15 операций · `report_templates`, `report_jobs`, `dashboards`, `dashboard_widgets` |
| `signing` | ПЭП: документы и запросы на подпись, OTP-код, публичная страница подписания и проверки, соглашения об ЭДО | `POST /api/signature-documents`, `/send`, `POST /api/signature-requests/{id}/{challenge,sign}`, `GET /public/sign/{token}`, `POST /api/signatures/verify` — 21 операция · 6 таблиц |
| `workflow` | конструктор воронок: статусы, переходы, DSL условий, валидация, публикация со снимком, архивирование статуса, удаление черновика | `GET/POST /api/workflows`, `PUT /{id}/graph`, `POST /{id}/publish`, `POST /{id}/statuses/{sid}/archive`, `DELETE /api/workflows/{id}` — 9 операций · `workflows`, `workflow_statuses`, `workflow_transitions`, `sla_rules`, `status_mapping_jobs` |

```
app/
├── main.py              точка входа, сборка FastAPI-приложения
├── api/                 health.py (/health/*, /metrics), docs.py (локальный Swagger UI)
├── core/                кросс-модульные соглашения: config, errors, problem (RFC 7807),
│                        security (JWT), permissions, deps, csrf, rate_limit, idempotency,
│                        pagination, masking, redis_client, db, metrics, ids (UUIDv7)
├── db/                  базовый класс ORM (base.py) и реестр моделей для Alembic (models.py)
├── middleware/          request_context.py: request_id, RED-метрики
├── mocks/               sms_gateway.py — мок SMS-провайдера (свой процесс в compose)
├── modules/             admin, audit, catalog, crm, files, identity, imports,
│                        integration, notification, registry, reporting, signing, workflow
├── assets/fonts/        DejaVu Sans — кириллица в PDF (xhtml2pdf) и графиках (matplotlib)
└── worker/main.py       arq: воркер и планировщик периодических задач
migrations/versions/     14 миграций Alembic: 0001_baseline … 0014_organization_erasure
tests/                   15 файлов, 417 тестов (офлайн + сквозные с TEST_DATABASE_URL)
loadtest/                Locust: provision.py + 2 locustfile, README со своими результатами
deploy/                  Caddyfile, entrypoint.sh, keycloak/realm-crm.json, postgres/, seaweedfs/
```

## Настройка

Полный список переменных — `.env.example`; код читает их в `app/core/config.py` (`Settings`, pydantic-settings) и в `docker-compose.yml`. Приложение не стартует без `APP_PROFILE` и строк подключения — падение на старте лучше, чем работа с половиной конфига.

**Важная оговорка про Docker.** В контейнеры `api`, `worker`, `migrate` и `seed` попадают только переменные из явного списка `x-api-env` в `docker-compose.yml` (34 имени) — `env_file` не используется совсем. Значения `.env`, которых нет в этом списке (лимиты файлов, TTL сессии/CSRF/идемпотентности, параметры ПЭП, импорта, приглашений, 152-ФЗ и другие), в контейнерах не действуют: работает дефолт из `Settings`, даже если в `.env` записано другое.

| Переменная | Значение по умолчанию | Зачем |
|---|---|---|
| `APP_PROFILE` | `dev` (`.env.example`) / `demo` (compose) | режим: cookie, Bearer-доступ, Swagger UI, CORS, `debug_code` — см. «Быстрый старт» |
| `BASE_URL` | `http://localhost:8080` | публичный адрес: `redirect_uri` OIDC, ссылки приглашений и подписи |
| `DATABASE_URL` | `crm_app:crm_app@postgres:5432/crm` | подключение под ограниченной ролью (только SELECT/INSERT на `audit_log`); `migrate` получает отдельную суперпользовательскую строку |
| `KC_DATABASE_URL` | `crm:crm@localhost:5432/keycloak` (обязателен) | подключение Keycloak к своей базе |
| `REDIS_URL` | `redis://redis:6379/0` | сессии, кэш прав, локи, идемпотентность, rate-limit, очередь arq |
| `KEYCLOAK_URL` / `KEYCLOAK_INTERNAL_URL` | публичный `…/auth` / внутренний `http://keycloak:8080/auth` | issuer токена и ссылка входа / серверные вызовы JWKS, token, Admin API |
| `KEYCLOAK_CLIENT_SECRET`, `KEYCLOAK_ADMIN_CLIENT_SECRET` | `crm-bff-secret`, `crm-admin-secret` | секреты клиентов BFF и Admin API — сменить в реальной эксплуатации |
| `KEYCLOAK_VERIFY_AUDIENCE` | `true` | проверка `aud` токена; выключать нельзя — иначе подойдёт токен любого клиента realm'а |
| `S3_ENDPOINT_URL` / `S3_PUBLIC_ENDPOINT_URL` | `http://seaweedfs:8333` / закомментирован | серверные вызовы S3 / presigned-ссылки браузеру (без второго действует первый) |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` | `crm_access` / `crm_secret_key` | ключи identity `crm-api` из `deploy/seaweedfs/s3.json` |
| `SIGNATURE_SERVER_SECRET` | `change-me-in-prod` | HMAC-метка целостности подписи — обязательно сменить в реальной эксплуатации |
| `NTP_HOST` / `SMS_GATEWAY_URL` | `ntp` / `http://sms-gateway-mock:8090` | сетевые имена контейнеров стека, не опциональная интеграция |
| `CMS_WEBHOOK_SECRET_REF` / `BITRIX_WEBHOOK_URL_REF` | `CMS_WEBHOOK_SECRET` / `BITRIX_WEBHOOK_URL` | имя переменной, где лежит секрет (раздел 7.8: «ссылка на секрет, не сам секрет»), не значение |
| `INTEGRATION_WEBHOOK_RATE_LIMIT_PER_MIN` | 60 | лимит запросов на вебхуки CMS/LMS/Bitrix24 |
| `LOG_LEVEL` / `LOG_JSON` | `INFO` / `true` | уровень и формат логов (structlog + JSON) |
| `UVICORN_WORKERS` | 1 | воркеров uvicorn на контейнер `api`; масштабирование — только репликами |
| `HTTP_PORT` / `HTTPS_PORT` / `S3_PROXY_PORT` / `POSTGRES_PORT` | 8080 / 8443 / 8333 / 5433 | порты на хосте (`caddy`, `postgres`) |
| `WEB_UPSTREAM` | `web:3000` | куда Caddy отдаёт всё, что не `/api`, `/public`, `/health`, `/static`, `/auth` |
| `CSP_SCRIPT_SRC` | `'self'` | `script-src` в CSP; ослабляется для Vite в разработке |
| `APP_MODE` | `demo` | режим клиента `web`: выбор демо-роли или одна кнопка входа; читает только образ `web`, не `Settings` |
| `CRM_TLS_HOST` | `localhost` | имя TLS-сайта в Caddyfile (SNI сертификата `tls internal`); смените, если стенд открывается не по `localhost` |
| `SESSION_TTL`, `SESSION_IDLE_TIMEOUT`, `CSRF_*`, `ALLOW_BEARER_AUTH`, лимиты файлов, `PASSWORD_CHANGE_*`, `ERASURE_*`, `INVITE_*` и другие | см. `.env.example` | объявлены в `Settings` и документированы в `.env.example`, но **не входят в `x-api-env`** — в контейнерах всегда действует дефолт `config.py`, значение из `.env` игнорируется |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_ECHO`, `SESSION_COOKIE_NAME`, `PAGINATION_MAX_LIMIT`, `APP_NAME`, `APP_VERSION`, `API_PREFIX`, `PUBLIC_PREFIX`, `LLM_*` | см. `config.py` | есть в `Settings`, но ❌ отсутствуют в `.env.example`; `LLM_*` дополнительно не используется ни в одном модуле кода |

Секреты демо-стенда (`POSTGRES_PASSWORD`, `CRM_APP_PASSWORD`, `KEYCLOAK_CLIENT_SECRET`, `KEYCLOAK_ADMIN_CLIENT_SECRET`, `SIGNATURE_SERVER_SECRET`, `S3_SECRET_KEY`) — только для демо, в `.env.example` подставлены заглушками (`crm`, `change-me-in-prod`); для настоящей эксплуатации заменить все сразу, они не связаны друг с другом.

## Разработка

Образы `api`, `worker`, `migrate`, `seed` и `sms-gateway-mock` собираются из `Dockerfile` (`COPY app`, `COPY migrations`) — bind-mount кода нет. Правка файла в `app/` не подхватывается на лету: нужна пересборка `docker compose up -d --build api worker` (остальные три — только при следующем перезапуске стека).

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -e ".[dev]"
```

**Тесты.** `pytest.ini_options` в `pyproject.toml` — `testpaths = ["tests"]`, `asyncio_mode = "auto"`. Офлайновая часть (15 файлов, 417 тестов) не поднимает ни БД, ни Redis, ни Keycloak, но требует существующий `.env` — `Settings()` дёргается уже при импорте (CSRF/permissions-хелперы), поэтому `.env.example` копируется даже для чисто офлайнового прогона:

```bash
cp .env.example .env
pytest -q
```

Сквозные тесты (`tests/test_api_smoke.py`, 13 из 417) поднимают приложение целиком и включаются только при заданном `TEST_DATABASE_URL` — без него модуль пропускается целиком (`pytest.mark.skipif`), Redis подменяется `fakeredis` (`pytest.importorskip`), Keycloak — подставным декодером токена:

```bash
createdb crm_test
DATABASE_URL=postgresql+asyncpg://crm@127.0.0.1:5432/crm_test alembic upgrade head
TEST_DATABASE_URL=postgresql+asyncpg://crm@127.0.0.1:5432/crm_test pytest -q
```

**Линтер.** `ruff` (`[tool.ruff]` в `pyproject.toml`): `line-length = 100`, `target-version = "py312"`, правила `E, F, I, UP, B, ASYNC`; для `migrations/versions/*.py` отдельно отключён `E501` (DDL и SQL-литералы читаются хуже с разрывом строк). `ruff check app tests` в CI (`.github/workflows/ci.yml`, джоб `lint`) обязателен для прохождения PR.

```bash
ruff check app tests
```

**Миграции.** Alembic, `migrations/env.py` берёт URL из `Settings().database_url` (или `MIGRATIONS_DATABASE_URL` при локальном запуске вне Docker — та же ограниченная роль `crm_app` не имеет DDL-прав, см. «Аудит»). 14 линейных ревизий, от `0001_baseline` до `0014_organization_erasure`.

```bash
alembic upgrade head
alembic revision --autogenerate -m "описание"
```

Новая модель добавляется в `app/modules/<module>/models.py` и обязательно импортируется в `app/db/models.py` — иначе Alembic не увидит её при автогенерации.

**Сидинг.** Единая точка входа контейнера — `deploy/entrypoint.sh` (`api`\|`worker`\|`migrate`\|`seed`\|`sms-gateway-mock`\|`shell`). Режим `seed` запускает только `python -m app.modules.workflow.seed` (две демо-воронки); сиды отчётов, уведомлений и интеграций в контейнер не входят и запускаются вручную (все идемпотентны):

```bash
docker compose exec api python -m app.modules.reporting.seed       # 8 шаблонов отчётов
docker compose exec api python -m app.modules.notification.seed    # 27 шаблонов уведомлений
docker compose exec api python -m app.modules.integration.seed     # источники интеграций (заведены is_active=false)
```

На этом стенде все три уже применены (проверено `GET /api/report-templates` → 8 записей, `GET /api/admin/notification-templates` → 27, `GET /api/admin/integrations/sources` → 3 записи `is_active: false`).

**Нагрузочные тесты.** `loadtest/` — Locust: `provision.py` создаёт в БД напрямую (минуя API, через опубликованный порт `5433`) организацию-фикстуру и пул сделок; `locustfile_transition.py` и `locustfile_comment.py` гоняют `POST /api/deals/{id}/transition` и `POST /api/deals/{id}/comments` под прямым password-grant токеном Keycloak. Цель — `new_spec §0`: p95 ≤ 300 мс при 50 RPS; `loadtest/README.md` содержит зафиксированный прогон (p95 79 мс на переходах, 49 мс на комментариях, 3 реплики `api`).

```bash
python loadtest/provision.py --count 4000 --comment-pool-size 100
locust -f loadtest/locustfile_transition.py --headless -u 50 -r 25 -t 60s --host=http://localhost:8080
```

**Очередь arq.** `app/worker/main.py`: 16 функций, из них 15 — периодические задачи по cron (партиции аудита раз в сутки, чистка идемпотентных ключей раз в час, эскалация SLA и просроченные подписи раз в 15 минут, доставка outbox и уведомлений раз в минуту, обновление материализованных представлений отчётов раз в 5 минут, и другие — расписание закомментировано построчно в коде). Healthcheck воркера — не HTTP, а `arq app.worker.main.WorkerSettings --check` (heartbeat в Redis).

**Логи.** `structlog` + stdlib в одном конвейере (`app/core/logging.py`), JSON по умолчанию (`LOG_JSON=true`); `request_id` попадает в каждую запись через contextvars.

**Метрики.** Prometheus на `GET /metrics` — только внутри docker-сети (`api:8000/metrics`), через Caddy отвечает `404` (проверено). RED-метрики (`crm_http_requests_total`, `crm_http_request_duration_seconds`, `crm_http_errors_total`), плюс `crm_queue_depth`, `crm_sla_violations_total`, `crm_sla_breaching_deals`, `crm_import_duration_seconds`, `crm_cache_requests_total`, `crm_audit_records_total`, `crm_dependency_up` — полный список в `app/core/metrics.py`.

## Безопасность

Только подтверждённое в коде и на живом стенде:

* **CSRF.** Double-submit токен на мутирующих запросах с сессионной cookie: cookie `crm_csrf` (читаемая JS — вторая половина приёма) плюс заголовок `X-CSRF-Token`, сверка `hmac.compare_digest` (`app/core/csrf.py`, `CRM-1107` при расхождении). Запросы с `Authorization: Bearer` не проверяются — у них нет cookie, значит нет и вектора CSRF.
* **CSP, HSTS и остальные заголовки — на каждом ответе Caddy** (проверено `curl -i`): `Content-Security-Policy` со списком `connect-src`/`img-src`/`frame-src`, ограниченным собственным origin и портом SeaweedFS; `Strict-Transport-Security`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy`, заголовок `Server` скрыт (`deploy/Caddyfile`).
* **Redirect-safe `next`.** Адрес возврата после входа принимается только как путь внутри сайта (`app/modules/identity/redirects.py::safe_next_path`; 17 тестов в `tests/test_redirects.py`, проверено и живым запросом с `next=https://evil.example`) — раньше `GET /api/auth/login?next=…` работал как открытый редирект, исправлено.
* **RBAC на трёх уровнях.** 5 ролей (`KAM`, `HEAD`, `ADMIN`, `AUDITOR`, `INTEGRATION`), 40 прав (`app/core/permissions.py`); маршрут (`require_permission`), объект (проверка в сервисах) и SQL-скоуп списка (`deal_scope_clause`: свои сделки / команда рекурсивно по `teams.parent_id` через `WITH RECURSIVE` / всё / только источник / пусто для AUDITOR).
* **«Четыре глаза».** Создание администратора и исполнение запроса на обезличивание требуют подтверждения вторым администратором (`ApprovalService.require`, отказ — `CRM-1902`); подтверждающий не может быть тем же администратором, что инициировал операцию, подтверждение привязано к хэшу конкретных параметров операции.
* **Лимиты и проверка загрузок.** По умолчанию 50 МБ на обычный файл, 500 МБ на вложение сделки (проверено: запрос на 100 МБ PDF отклонён `413 CRM-1402`); SVG запрещён жёстко, независимо от списка расширений (XSS-вектор, проверено `415 CRM-1401`); реальное содержимое сверяется по magic bytes с заявленным расширением — расхождение переводит файл в статус `infected` и блокирует скачивание. Отдельного карантинного потока нет: статус `quarantined` объявлен в модели, но код его не присваивает — антивирус — заглушка `NullAntivirusScanner`, всегда возвращающая «чисто».
* **Блокировка после неудачных попыток.** Keycloak: `bruteForceProtected`, 5 попыток, ожидание до 900 с (`deploy/keycloak/realm-crm.json`). Собственная защита — rate-limit на смену пароля (5 попыток → блок 15 минут), на попытки OTP ПЭП (3 на запрос, плюс лимит частоты отправки кода), на публичные страницы подписания (10 запросов/мин на IP) и на проверку токена приглашения.
* **Политика паролей — не короче 12 символов.** На стороне Keycloak realm: `length(12) and upperCase(1) and lowerCase(1) and digits(1) and notUsername and notEmail and passwordHistory(5)`; на стороне API — та же граница в схеме смены пароля (`Field(min_length=12)`).
* **Эпоха прав.** `perm_epoch` — атрибут пользователя, попадает в access-токен протокол-мэппером Keycloak; понижение роли или блокировка увеличивают эпоху, и токен со старой эпохой отвергается (`CRM-1103`) до истечения собственного TTL (5 минут) — без этого блокировка ждала бы, пока истечёт уже выданный токен.
* **Секреты не логируются.** `app/core/masking.py`: фиксированный список ключей (`password`, `token`, `client_secret`, `authorization`, `cookie`, …) вырезается из логов и аудита целиком; ПДн (телефон, email, ФИО при обезличивании) маскируются по формату, а не вырезаются.
* **Идемпотентность в границах актора.** `Idempotency-Key` скоупится по `actor_id` (или по источнику интеграции для анонимных вызовов) — угаданный ключ другого пользователя не возвращает чужой сохранённый ответ.

## Ограничения и известные проблемы

Список найденных при интеграции проблем ведёт фронтенд-репозиторий — [`../frontend/docs/backend-issues.md`](../frontend/docs/backend-issues.md), 96 пунктов трёх областей (B — настройка и справочники, 34; A — CRM, 34; C — доступы/подпись, 28), каждый привязан к файлу и строке. Ниже — самое существенное для эксплуатации, кратким изложением, не копией.

| Проблема | Как обходится / статус |
|---|---|
| `GET /api/admin/audit/verify-chain` на широких выборках (`limit≥100`) нашёл разрывы цепочки на этом стенде | узкие недавние окна (`limit≤20`) в тот же момент были целыми — похоже на след параллельной активности нескольких сессий на общем стенде за время его жизни; полной трассировки причины не делалось, стоит проверить на выделенном стенде перед сдачей |
| `PUT /workflows/{id}/graph` раньше удалял и создавал заново все статусы и переходы → `500`, как только по переходу прошла хотя бы одна сделка (внешний ключ истории) | исправлено в рабочей копии (не закоммичено): переходы сопоставляются по паре статусов и обновляются на месте, занятый статус/переход даёт `422` вместо `500` |
| `GET /api/auth/login?next=…` был открытым редиректом | исправлено: `safe_next_path` принимает только путь внутри сайта, проверено тестами и живым запросом (см. «Безопасность») |
| Сиды `reporting`/`notification`/`integration` не входят в `entrypoint.sh seed` (только воронки) | запускаются вручную, идемпотентны (см. «Разработка»); на этом стенде уже применены |
| Автоподстановка по ИНН не имеет реального внешнего провайдера | цепочка «локальный реестр → подтверждённые организации → мок» — мок работает только вне `prod`; в `prod` доступен только локальный реестр и уже подтверждённые организации |
| Мастер передачи дел (offboard) принимает только одного преемника, «по-сделочно» нет | точечное распределение — через отдельную массовую ручку `POST /deals/bulk/reassign` |

Остальные пункты — несогласованные коды ошибок для лимита отчётов, отсутствующая `DELETE`-ручка у производственного календаря (остальные справочники и воронки её уже получили, см. выше), N+1 запросы карточек без денормализованных имён, отсутствие пагинации у части списков — в [`../frontend/docs/backend-issues.md`](../frontend/docs/backend-issues.md), с привязкой к файлу и строке. Данные отчёта в JSON, не только файлом, — `GET /api/reports/{report_id}/data`.

| Документ | Что внутри |
|---|---|
| [`_spec/spec.txt`](_spec/spec.txt) | исходная спецификация — источник приоритета при расхождениях |
| [`../new_spec.md`](../new_spec.md) | паспорт проекта и ТЗ — второй по приоритету источник |
| [`../dop.md`](../dop.md) | дополнения: ПЭП, автоподстановка по ИНН и ЕГРЮЛ, дизайн-система |
| [`../README.md`](../README.md) | обзор всего проекта: состав репозиториев, быстрый старт демо- и prod-версии клиента, адреса и порты всего стека |
| [`../frontend/README.md`](../frontend/README.md) | веб-клиент: стек, сборка, режимы demo/prod |
| [`../frontend/docs/api-endpoints.md`](../frontend/docs/api-endpoints.md) | те же 181 операция API, сгенерированный список по тегам |
| [`../frontend/docs/backend-issues.md`](../frontend/docs/backend-issues.md) | 96 несоответствий бэкенда, найденных на живом стенде, с привязкой к файлу и строке |
| [`loadtest/README.md`](loadtest/README.md) | нагрузочное тестирование: методика, зафиксированный прогон, интерпретация результатов |
| [`../deploy/README.md`](../deploy/README.md) | инфраструктурный репозиторий: манифест образов, CI/CD, состояние по фазам |

