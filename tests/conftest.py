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

    from fastapi.testclient import TestClient

    from app.core import redis_client
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    monkeypatch.setattr(
        redis_client, "_client", fakeredis.aioredis.FakeRedis(decode_responses=True)
    )

    with TestClient(create_app()) as test_client:
        yield test_client


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
