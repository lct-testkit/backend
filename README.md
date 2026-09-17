# CRM ИТ Школы Ростелекома — бэкенд

Модульный монолит на FastAPI. Источник требований — `_spec/spec.txt`,
`new_spec.md`, `dop.md` (в этом порядке приоритета при расхождениях).
Этот репозиторий закрывает **спринты 0–2**: каркас, identity с аудитом и
конструктор воронок. Прикладные модули (сделки, каталоги, ЕГРЮЛ, импорт,
отчёты, интеграции, ПЭП) добавляются следующими спринтами.

## Стек

Зафиксирован спецификацией, замены не допускаются:

| Компонент | Версия | Назначение |
|---|---|---|
| Python / FastAPI | 3.12 / 0.115 | API |
| PostgreSQL | 16 | основное хранилище |
| Redis | 7 | сессии, кэш, локи, идемпотентность, очередь |
| SeaweedFS | 3.68 | S3-совместимое хранилище (MinIO запрещён) |
| Keycloak | 25 | OIDC-провайдер |
| arq | 0.26 | фоновые задачи и планировщик |
| Caddy | 2.8 | TLS-терминация и единая точка входа |

## Запуск

Одна команда на чистой машине. Интернет нужен только на этапе сборки образа;
в рантайме контур закрыт.

```bash
cp .env.example .env
docker compose up -d --build
```

Поднимется: `postgres`, `redis`, `seaweedfs`, `keycloak`, `migrate`, `seed`,
`api`, `worker`, `caddy`. Сервис `migrate` применяет миграции и завершается;
`seed` сразу за ним наполняет демо-воронки `b2b_university_v1` (14 шагов) и
`b2c_individual_v1` (6 шагов) и тоже завершается — идемпотентно, поэтому
безопасен при каждом перезапуске; `api` и `worker` стартуют только после
успешного завершения `seed`. На чистой машине это около минуты — дольше
всего поднимается Keycloak.

У каждого сервиса есть healthcheck, поэтому `docker compose ps` показывает
реальную готовность, а `api` ждёт Keycloak и не отдаёт 401 на первых
запросах. После того как команда вернула управление, вход уже работает.

| Адрес | Что это |
|---|---|
| http://localhost:8080/api/docs | Swagger UI |
| http://localhost:8080/api/openapi.json | OpenAPI-контракт для BFF |
| http://localhost:8080/health/live | liveness |
| http://localhost:8080/health/ready | БД, Redis, JWKS, SeaweedFS, очередь |
| http://localhost:8080/auth | Keycloak (realm `crm`) |
| https://localhost:8443 | то же по TLS (самоподписанный сертификат) |

Метрики Prometheus доступны только внутри сети, на `api:8000/metrics`:
Caddy отдаёт наружу 404.

### Масштабирование

У `api` один uvicorn-воркер на контейнер. `prometheus_client` держит реестр в
памяти процесса, поэтому при нескольких воркерах scrape попадал бы в
случайный из них и показывал лишь часть трафика. Масштабирование — репликами,
Caddy разрешает их через DNS и балансирует по `least_conn`:

```bash
docker compose up -d --scale api=3
```

### Демо-пользователи

Создаются при импорте realm. Роли соответствуют разделу 4 спецификации.

| Логин | Пароль | Роль |
|---|---|---|
| `admin.crm` | `Admin12345678!` | ADMIN |
| `head.petrov` | `Head12345678!` | HEAD |
| `kam.ivanov` | `Kam123456789!` | KAM |
| `auditor.smirnov` | `Audit12345678!` | AUDITOR |

Консоль администратора Keycloak: http://localhost:8080/auth/admin — `admin` / `admin`.

### Проверка входа

Браузером: открыть http://localhost:8080/api/auth/login — произойдёт редирект
на Keycloak, после входа установится httpOnly-cookie сессии.

Программно (direct access grant для проверки контура):

```bash
TOKEN=$(curl -s -X POST \
  http://localhost:8080/auth/realms/crm/protocol/openid-connect/token \
  -d 'grant_type=password&client_id=crm-bff&client_secret=crm-bff-secret' \
  -d 'scope=openid&username=kam.ivanov&password=Kam123456789!' \
  | jq -r .access_token)

curl -s http://localhost:8080/api/me -H "Authorization: Bearer $TOKEN"
```

Первый вход выполняет just-in-time provisioning: локальная запись `users`
создаётся из claims токена, роль берётся из Keycloak.

До принятия согласия на обработку ПДн бизнес-ручки отвечают `403 CRM-1105`.
Согласие принимается с версией политики и SHA-256 её текста — хэш нужен,
чтобы доказать, *какой именно* текст видел пользователь:

```bash
curl -s -X POST http://localhost:8080/api/me/consent \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"policy_version":"1.0","policy_text_hash":"<sha256 текста политики>"}'
```

## Структура

```
app/
├── main.py              точка входа, сборка приложения
├── api/health.py        /health/live, /health/ready, /metrics
├── core/                кросс-модульные соглашения
│   ├── config.py        все переменные окружения раздела 17
│   ├── errors.py        каталог кодов CRM-XXYY
│   ├── problem.py       RFC 7807 Problem Details
│   ├── context.py       request_id и актор в contextvars
│   ├── logging.py       structlog + stdlib, JSON
│   ├── metrics.py       RED, очередь, импорт, SLA, кэш
│   ├── security.py      локальная валидация JWT по кэшу JWKS
│   ├── permissions.py   роли, права, скоупы списков
│   ├── deps.py          зависимости FastAPI
│   ├── cache.py         cache:perm:{user_id} и его инвалидация
│   ├── csrf.py          double-submit токен для мутирующих запросов
│   ├── rate_limit.py    счётчики rl:{subject}:{route}
│   ├── pagination.py    курсорная пагинация (row-comparison по (sort, id))
│   ├── idempotency.py   Idempotency-Key со скоупом актора
│   ├── masking.py       маскирование ПДн и секретов
│   ├── ids.py           UUIDv7 (RFC 9562)
│   ├── db.py            async SQLAlchemy, транзакция на запрос
│   └── redis_client.py  схема ключей раздела 16
├── db/base.py           базовый класс ORM и миксины
├── middleware/          request_id и RED-метрики
├── modules/             identity, crm, workflow, catalog, reporting,
│                        integration, notification, audit, signing, admin
└── worker/main.py       arq: воркер и планировщик
```

Модуль не импортирует репозитории другого модуля. Кросс-модульное
взаимодействие — только через сервисные интерфейсы, чтобы любой модуль можно
было вынести отдельно без переписывания.

## Соглашения API

Действуют для всех ручек, включая будущие (разделы 2 и 21 спецификации).

* **Ошибки** — только RFC 7807, `Content-Type: application/problem+json`.
  В теле `type`, `title`, `status`, `detail`, `instance`, `request_id`, `code`,
  при необходимости массив `errors`. Стектрейсы наружу не отдаются.
* **Коды ошибок** — из каталога `app/core/errors.py` (`CRM-XXYY`).
  Придумывать коды на месте нельзя.
* **Идентификаторы** — UUIDv7, в JSON строками. Автоинкременты наружу запрещены.
* **Даты** — ISO 8601 с часовым поясом, в базе `timestamptz`, наружу UTC.
* **Деньги** — строка `"150000.00"` плюс отдельное поле `currency`,
  в базе `numeric(14,2)`.
* **Списки** — курсорная пагинация: `limit` (максимум 100) и непрозрачный
  `cursor`; ответ `{items, next_cursor}`. `OFFSET` не используется.
* **Обновления** — `If-Match` с текущей `version`; при расхождении `409` и
  `CRM-1002` с актуальными значениями конфликтующих полей.
* **Создание и необратимые операции** — заголовок `Idempotency-Key`.
* **Права** — три уровня: маршрут, объект и SQL-фильтр списка. На скрытие
  кнопки во фронтенде полагаться нельзя.
* **Аудит** — в той же транзакции, что и бизнес-изменение.
* **request_id** — приходит от Caddy или генерируется, проходит через логи,
  аудит и тело ошибки, возвращается в заголовке `X-Request-Id`.

## Аутентификация и сессии

Реализован BFF-паттерн из `new_spec` §3.1: браузер получает только httpOnly
cookie с идентификатором сессии, токены остаются в Redis.

* **OIDC-поток.** `state`, `nonce` и PKCE-верификатор живут в Redis 10 минут.
  В `callback` проверяются все три: отсутствие `state` — отказ без исключений,
  `id_token` валидируется по подписи, `iss`, `aud`, `exp` и `nonce`, а его
  `sub` сверяется с access-токеном.
* **Обновление токена.** Access-токен живёт 5 минут, сессия — до 12 часов,
  поэтому при истечении он молча обновляется по refresh под коротким локом
  `lock:session:{sid}:refresh` (ротация refresh-токена ломается при гонке).
* **Idle-таймаут.** `SESSION_IDLE_TIMEOUT` (по умолчанию 30 минут) проверяется
  по `last_seen_at`; запись в Redis не чаще раза в минуту.
* **CSRF.** Мутирующие запросы с сессионной cookie требуют double-submit
  токен: cookie `crm_csrf` плюс заголовок `X-CSRF-Token` (`CRM-1107`).
  Запросы с `Authorization: Bearer` проверку не проходят — у них нет cookie.
* **Bearer.** В `prod` прямой вход по токену разрешён только роли
  `INTEGRATION`, иначе контур BFF обходился бы целиком (`ALLOW_BEARER_AUTH`).
* **Эпоха прав.** `perm_epoch` пишется в атрибут Keycloak и приезжает в токене
  через протокол-мэппер; токен со старой эпохой отвергается как `CRM-1103`.
* **Кэш прав.** `cache:perm:{user_id}` живёт 5 минут и сбрасывается при смене
  роли, блокировке, увольнении, согласии, смене пароля и выходе — поэтому
  блокировка действует немедленно, а не через пять минут.

Самостоятельной регистрации нет. JIT-provisioning создаёт локальную запись
только если администратор уже что-то сделал для пользователя: завёл
приглашение по email или выдал роль CRM в Keycloak. Учётка realm'а без роли
CRM доступа не получает.

## Аудит

`audit_log` партиционирован по месяцам и защищён от изменения на уровне БД:

* триггеры `trg_audit_log_*_immutable` (уровня строки) и `*_truncate`
  висят **на каждой партиции**, а не только на родителе: `DELETE FROM
  audit_log_2026_09` идёт мимо родительской таблицы, и statement-триггер
  на ней такую операцию не видит;
* новые партиции получают защиту автоматически — её вешает
  `create_audit_log_partition`, которую вызывает ежедневная задача;
* роль `crm_app` имеет на журнале только `INSERT` и `SELECT`; чтобы это
  работало, приложение должно подключаться именно под ней, а не под
  владельцем БД;
* каждая запись хранит `prev_hash` и `hash`, поэтому вырезание записи
  обнаруживается: `GET /api/admin/audit/verify-chain` пересчитывает цепочку;
* `actor_id` намеренно без внешнего ключа — запись обязана переживать
  обезличивание пользователя;
* отказ в доступе фиксируется немедленным коммитом в той же транзакции:
  исключение откатило бы её вместе с записью, а вторая транзакция встала бы
  в очередь за advisory-локом цепочки, который держит первая;
* чувствительные значения маскируются до записи (`i***@domain.ru`),
  а IP нормализуется до валидного `inet` или `NULL`.

Партиции на год вперёд создаются миграцией, далее их поддерживает
ежедневная задача `ensure_audit_partitions`.

## Разработка

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -e ".[dev]"

pytest -q                       # тесты соглашений каркаса
ruff check app tests            # линтер
alembic upgrade head            # миграции
alembic revision --autogenerate -m "описание"
uvicorn app.main:app --reload
arq app.worker.main.WorkerSettings
```

Для локального запуска без Docker нужны поднятые PostgreSQL, Redis и Keycloak,
а `KEYCLOAK_INTERNAL_URL` в `.env` следует оставить пустым.

### Новые модели

Модели добавляются в `app/modules/<module>/models.py` и обязательно
импортируются в `app/db/models.py` — иначе Alembic их не увидит при
автогенерации.

### Переменные окружения

Полный список — в `.env.example` (раздел 17 спецификации). Приложение не
стартует без `APP_PROFILE` и строк подключения. Секреты не хранятся в базе в
открытом виде: поля вида `credentials_ref` ссылаются на секрет, а не содержат его.

`KEYCLOAK_URL` и `KEYCLOAK_INTERNAL_URL` различаются намеренно. Первый —
публичный адрес, из него формируется `iss` токена и ссылка входа. Второй —
адрес для серверных вызовов JWKS, token и Admin API внутри docker-сети.
Поэтому Keycloak запускается с фиксированным `KC_HOSTNAME_URL`: иначе `iss`
зависел бы от того, обратились к нему через Caddy или напрямую.

## Что закрыто спринтами 0–2

Спринт 0 — каркас, спринт 1 — identity и аудит (разделы 6.1, 6.2, 6.12),
спринт 2 — конструктор воронок (раздел 6.5, new_spec §4.11).

| Ручка | Назначение |
|---|---|
| `GET/POST /api/auth/login`, `/callback` | OIDC с проверкой `state`, `nonce`, PKCE |
| `POST /api/auth/logout`, `/backchannel-logout` | локальный выход, SLO, обрыв сессий из Keycloak |
| `GET /api/auth/invite/{token}` | проверка одноразового приглашения |
| `GET /api/me`, `/policy`, `/recent`, `/sessions` | профиль, политика ПДн, последние объекты, сессии |
| `DELETE /api/me/sessions/{sid}` | завершение своей сессии с записью в аудит |
| `POST /api/me/consent` | согласие с проверкой версии и хэша текста |
| `POST /api/me/password` | смена пароля со всей цепочкой последствий §4.4 |
| `GET/POST /api/admin/users`, `GET/PATCH /{id}` | список, создание (SAGA с Keycloak), изменение с `If-Match` |
| `POST /api/admin/users/{id}/block`, `/unblock` | блокировка с обрывом сессий, разблокировка |
| `POST /api/admin/users/{id}/reset-password` | сброс с единым ответом и аннулированием подписей |
| `POST /api/admin/users/{id}/invite` | перевыпуск приглашения с лимитами 1/5 мин и 5/сутки |
| `POST /api/admin/users/{id}/offboard` | мастер передачи дел: `preview` и `confirm` |
| `POST /api/admin/users/{id}/erasure-request` | запрос 152-ФЗ с блокерами |
| `GET/POST/PATCH /api/admin/teams` | иерархия команд для рекурсивного скоупа |
| `GET /api/admin/approvals`, `/approve`, `/reject` | принцип «четырёх глаз» (`CRM-1902`) |
| `GET /api/admin/audit`, `/export`, `/verify-chain` | журнал со скоупом, выгрузка NDJSON, проверка цепочки |
| `GET/PATCH /api/admin/feature-flags`, `system-settings` | флаги и настройки |
| `GET/POST /api/workflows`, `GET /{id}` | список и граф воронки: статусы, переходы, SLA-правила |
| `PUT /api/workflows/{id}/graph` | сохранение черновика графа целиком, с `If-Match` |
| `POST /api/workflows/{id}/validate` | проверка графа: initial, терминалы, достижимость, ловушки, DSL |
| `POST /api/workflows/{id}/publish` | публикация со снимком графа и `graph_hash`, инвалидация кэша |
| `GET /api/workflows/{id}/statuses/{sid}/impact` | предпросмотр архивирования статуса |
| `POST /api/workflows/{id}/statuses/{sid}/archive` | мастер сопоставления: перенос сделок батчами, архивирование |

## Известные ограничения

* Модули `crm`, `signing`, `notification` пока представлены только сервисными
  интерфейсами с заглушками. Поэтому мастер передачи дел честно отвечает
  `supported: false` по сделкам, а не делает вид, что передавать нечего;
  аннулирование запросов подписи и уведомления возвращают нули и пишут в лог;
  предпросмотр архивирования статуса воронки всегда показывает ноль активных
  сделок. Реализация появится в спринтах 3, 7 и 8 — контракты
  (`OwnershipService`, `DealStatusService` в `app/modules/crm/service.py`)
  менять не придётся.
* Клиент SeaweedFS реализован только как health-check; presigned-ссылки,
  антивирус и карантин — в спринте файлов.
* Текст политики обработки ПДн хранится вне бэкенда: `system_settings.pdn_policy`
  содержит версию и `text_hash`, по которым сверяется согласие. Сам текст
  отдаёт фронтенд, админка редакций — в спринте 9.
* `IdempotencyGuard` и таблица `idempotency_keys` готовы, ключ уже скоупится
  актором, но первым потребителем станет создание сделки (спринт 3) — там же
  появятся тесты на повтор и на конфликт тела при том же ключе.
* Запись аудита берёт голову цепочки под транзакционным advisory-локом, что
  сериализует вставки. На целевой нагрузке это допустимо, но при росте
  профиля записи потребуется шардирование цепочки.
* Приложение по умолчанию подключается к БД под владельцем. Ограничение
  «только INSERT/SELECT на журнал» вступает в силу, когда `DATABASE_URL`
  указывает на роль `crm_app`, созданную миграцией 0002; триггеры
  неизменяемости действуют в любом случае.
* Сертификат на `:8443` самоподписанный (`tls internal`): внешний ACME в
  закрытом контуре недоступен.

## Тесты

```bash
pytest -q                       # офлайн: соглашения, схемы, права, курсоры
```

Сквозные тесты (`tests/test_api_smoke.py`) поднимают приложение целиком и
требуют настоящую PostgreSQL — без неё нечем проверить транзакционность
аудита, реакцию на блокировку и idle-таймаут. Redis подменяется `fakeredis`,
Keycloak — подставным декодером токена:

```bash
createdb crm_test
DATABASE_URL=postgresql+asyncpg://crm@127.0.0.1:5432/crm_test alembic upgrade head
TEST_DATABASE_URL=postgresql+asyncpg://crm@127.0.0.1:5432/crm_test pytest -q
```

Без `TEST_DATABASE_URL` они пропускаются, поэтому обычный прогон остаётся
полностью офлайновым.
