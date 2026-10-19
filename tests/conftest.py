"""Общие приспособления для сквозных тестов, которым нужна настоящая
PostgreSQL (`tests/test_api_smoke.py` и новые тесты П1-П5 из отчёта бэкенд-
агента: `test_reporting.py::TestReportDataEndpoint`, `test_imports.py::
TestLicenseImportEndToEnd`, `test_workflow.py`/`test_catalog.py`/
`test_notifications.py`/`test_registry.py` — тесты `DELETE`-ручек).

Раньше это жило только внутри `test_api_smoke.py`; вынесено сюда, когда
понадобилось тем же приёмом (поднятое приложение + сессия в БД + фейковый
Redis + подставной декодер токена) ещё нескольким модулям — дублировать
фикстуру в каждом файле не хотелось.

Без `TEST_DATABASE_URL` тесты, использующие `client`, пропускаются
(`pytestmark` в каждом использующем файле), поэтому обычный офлайн-прогон
(`pytest -q` без переменной) не меняется — то же поведение, что было у
`test_api_smoke.py` до выноса.

Запуск с реальной БД — см. докстринг `test_api_smoke.py`:

    createdb crm_test
    DATABASE_URL=postgresql+asyncpg://user@localhost:5432/crm_test alembic upgrade head
    TEST_DATABASE_URL=postgresql+asyncpg://user@localhost:5432/crm_test pytest tests/
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import pytest

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

# B6-7: фиксированный Fernet-ключ для тестов (`EncryptedText`/`EncryptedDate`,
# `app.core.field_crypto`) — не настоящий секрет, просто валидный ключ, чтобы CI проходил
# зашифрованный путь, а не только "ключа нет".
TEST_FIELD_ENCRYPTION_KEY = "OnkqcWC6Wn4JMjNYnJiR2i9orwEMgF34hfQLXOClMBg="


def run(client, func, *args: Any) -> Any:
    """Выполняет корутину в событийном цикле TestClient."""
    return client.portal.call(func, *args)


@pytest.fixture
def client(monkeypatch):
    """Приложение с подменёнными Redis и проверкой токена, настоящая БД.

    `fakeredis` импортируется здесь, а не на уровне модуля: конфтест
    подключается ко ВСЕМ файлам `tests/` при сборе, и жёсткий `importorskip`
    на верхнем уровне ломал бы сбор целиком (а не только тесты, которым
    нужен `client`) в окружении без dev-экстры."""
    fakeredis = pytest.importorskip("fakeredis", reason="нужен fakeredis для подмены Redis")
    os.environ.setdefault("APP_PROFILE", "dev")
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL or ""
    os.environ["REDIS_URL"] = "redis://127.0.0.1:6379/0"
    # Только loopback и только IP: имя хоста уходит в системный резолвер, и
    # под VPN или в закрытом контуре разрешение имени зависает дольше любого
    # таймаута httpx. Порт 9 (discard) отказывает в соединении мгновенно.
    os.environ["KEYCLOAK_URL"] = "http://127.0.0.1:9/auth"
    os.environ.setdefault("KEYCLOAK_REALM", "crm")
    os.environ.setdefault("KEYCLOAK_CLIENT_ID", "crm-bff")
    os.environ.setdefault("KEYCLOAK_CLIENT_SECRET", "secret")
    os.environ["S3_ENDPOINT_URL"] = "http://127.0.0.1:9"
    os.environ.setdefault("S3_ACCESS_KEY", "a")
    os.environ.setdefault("S3_SECRET_KEY", "b")
    os.environ.setdefault("SIGNATURE_SERVER_SECRET", "test-secret")
    # B6-10: дефолт кода стал `false` (безопасность по умолчанию в dev/prod); тестам, которые
    # сознательно проверяют путь Bearer (`tests/test_sessions_keycloak.py`, нагрузочные
    # сценарии), он всё ещё нужен — тест, которому нужно выключить его явно, задаёт
    # `ALLOW_BEARER_AUTH=false` сам (`os.environ[...] =`, не `setdefault`).
    os.environ.setdefault("ALLOW_BEARER_AUTH", "true")
    # B6-7: фиксированный тестовый Fernet-ключ — тесты не должны требовать настоящего ключа,
    # но должны проходить именно зашифрованный путь (TEST_FIELD_ENCRYPTION_KEY), а не только
    # ветку "ключа нет" в `field_crypto`. Тесты, которым нужно явно отсутствие ключа, сами
    # делают `monkeypatch.delenv(...)` + `get_settings.cache_clear()` +
    # `field_crypto.reset_field_crypto_cache()`.
    os.environ.setdefault("FIELD_ENCRYPTION_KEY", TEST_FIELD_ENCRYPTION_KEY)

    from fastapi.testclient import TestClient

    from app.core import field_crypto, redis_client
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    field_crypto.reset_field_crypto_cache()
    monkeypatch.setattr(
        redis_client, "_client", fakeredis.aioredis.FakeRedis(decode_responses=True)
    )

    app = _cached_app(create_app)
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        # Тест мог подменить зависимости у общего приложения — следующему они не нужны.
        app.dependency_overrides.clear()


# Приложение на ~280 маршрутов собирается заметно дольше самого теста: FastAPI при первом
# запросе строит зависимости и pydantic-схемы каждого маршрута. Новое приложение на каждый
# тест (было) делало прогон в разы медленнее и раздувало память процесса до гигабайтов —
# сборщик мусора обходил всё растущую кучу, и каждый следующий тест шёл дольше предыдущего.
# Теперь приложение переиспользуется, пока не изменилось окружение: настройки читаются из
# переменных при сборке (`create_app`), поэтому ключ кэша — снимок `os.environ`.
_APP_CACHE: dict[tuple[tuple[str, str], ...], Any] = {}


def _cached_app(factory):
    # PYTEST_CURRENT_TEST pytest меняет на каждый тест — в ключ не входит, иначе кэш бесполезен.
    key = tuple(sorted((k, v) for k, v in os.environ.items() if not k.startswith("PYTEST_")))
    app = _APP_CACHE.get(key)
    if app is None:
        if len(_APP_CACHE) >= 8:
            _APP_CACHE.clear()
        app = _APP_CACHE[key] = factory()
    return app


async def _make_user(role: str = "KAM", status: str = "active"):
    """Создаёт пользователя напрямую в БД: Keycloak в тестах не участвует."""
    from app.core.db import session_scope
    from app.modules.identity.models import User

    async with session_scope() as session:
        user = User(
            keycloak_id=str(uuid.uuid4()),
            email=f"{uuid.uuid4().hex[:12]}@rt-it-school.ru",
            full_name="Иванов Иван Иванович",
            role=role,
            status=status,
            consent_version="1.0",
        )
        session.add(user)
        await session.flush()
        session.expunge(user)
        return user


async def _open_session(user) -> str:
    from app.modules.identity.session_store import session_store

    stored = await session_store.create(
        user_id=user.id,
        keycloak_id=user.keycloak_id,
        access_token="access",
        refresh_token=None,
        id_token=None,
        kc_session_state=None,
        ip="127.0.0.1",
        user_agent="pytest",
    )
    return stored.sid


def authenticate(client, user) -> str:
    """Кладёт сессию в Redis и подменяет декодер токена. Возвращает CSRF-токен."""
    from app.core import deps
    from app.core.csrf import new_csrf_token
    from app.core.security import TokenClaims

    async def fake_decode(token: str) -> TokenClaims:
        return TokenClaims(
            subject=user.keycloak_id,
            raw={"sub": user.keycloak_id},
            email=user.email,
            full_name=user.full_name,
            roles=frozenset({user.role}),
        )

    deps.decode_access_token = fake_decode  # type: ignore[assignment]

    sid = run(client, _open_session, user)
    csrf = new_csrf_token()
    client.cookies.set("crm_sid", sid)
    client.cookies.set("crm_csrf", csrf)
    return csrf


def pytest_sessionfinish(session, exitstatus):
    """`REQUIRE_NO_SKIPS=1` (CI): любой пропущенный тест = красный прогон.

    Тесты со сквозной БД пропускаются через `skipif(not TEST_DATABASE_URL)`,
    поэтому в CI без сервиса Postgres они молча превращались бы в «зелёные».
    Переменная в CI гарантирует, что набор реально выполнился целиком.
    """
    if os.getenv("REQUIRE_NO_SKIPS") != "1":
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    skipped = reporter.stats.get("skipped", []) if reporter else []
    if skipped:
        print(f"\nREQUIRE_NO_SKIPS=1, но пропущено тестов: {len(skipped)}")
        session.exitstatus = 1
