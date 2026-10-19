"""G-1 (QA 20.10): `POST /api/admin/integrations/sources/{code}/check` — настоящая проверка
соединения с источником вместо перечитывания списка. Сеть подменяется транспортом httpx."""

from __future__ import annotations

import functools
from typing import Any

import httpx
import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import create_user, login, sign_in

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

_URL = "/api/admin/integrations/sources/{code}/check"
_HOST = "https://lms-check.invalid"


async def _configure(code: str, *, active: bool, base_url: str | None = _HOST) -> None:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.integration.models import IntegrationSource

    async with session_scope() as session:
        source = await session.scalar(
            select(IntegrationSource).where(IntegrationSource.code == code)
        )
        if source is None:
            source = IntegrationSource(code=code, name=code)
            session.add(source)
        source.is_active = active
        source.base_url = base_url
        source.last_error = None
        source.last_sync_at = None


async def _source_row(code: str) -> tuple[Any, Any]:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.integration.models import IntegrationSource

    async with session_scope() as session:
        row = (
            await session.execute(
                select(IntegrationSource.last_sync_at, IntegrationSource.last_error).where(
                    IntegrationSource.code == code
                )
            )
        ).one()
        return row[0], row[1]


def _network(monkeypatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        "app.modules.integration.check.httpx.AsyncClient",
        functools.partial(real, transport=httpx.MockTransport(_handler)),
    )
    return seen


def test_working_source_is_ok_and_nothing_is_written(client, monkeypatch) -> None:
    login(client, "ADMIN")
    run(client, functools.partial(_configure, "lms", active=True))
    seen = _network(monkeypatch, lambda request: httpx.Response(200, json={"items": []}))

    response = client.post(_URL.format(code="lms"))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["code"] == "lms"
    assert body["message"] == "Отвечает"
    assert body["http_status"] == 200
    assert isinstance(body["duration_ms"], int) and body["duration_ms"] >= 0
    assert body["checked_at"]
    assert str(seen[0].url).startswith(f"{_HOST}/students/progress")
    assert seen[0].url.params["limit"] == "1"
    # Проверка связи — не синхронизация.
    assert run(client, _source_row, "lms") == (None, None)


@pytest.mark.parametrize(
    ("status", "needle"),
    [(401, "отказала в доступе"), (404, "нужного адреса нет"), (503, "сбой на стороне источника")],
)
def test_error_statuses_are_explained_in_russian(client, monkeypatch, status, needle) -> None:
    login(client, "ADMIN")
    run(client, functools.partial(_configure, "lms", active=True))
    _network(monkeypatch, lambda request: httpx.Response(status))

    body = client.post(_URL.format(code="lms")).json()

    assert body["ok"] is False
    assert body["http_status"] == status
    assert needle in body["message"]
    assert _HOST not in body["message"]


def test_connection_failure_does_not_leak_the_address(client, monkeypatch) -> None:
    login(client, "ADMIN")
    run(client, functools.partial(_configure, "lms", active=True))

    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    _network(monkeypatch, _boom)

    body = client.post(_URL.format(code="lms")).json()

    assert body["ok"] is False
    assert body["http_status"] is None
    assert "Не удалось подключиться" in body["message"]
    assert "lms-check" not in body["message"]


def test_timeout_is_reported(client, monkeypatch) -> None:
    login(client, "ADMIN")
    run(client, functools.partial(_configure, "lms", active=True))

    def _slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    _network(monkeypatch, _slow)

    body = client.post(_URL.format(code="lms")).json()

    assert body["ok"] is False
    assert "таймаут" in body["message"]


def test_inactive_source_is_refused_with_a_clear_message(client, monkeypatch) -> None:
    login(client, "ADMIN")
    run(client, functools.partial(_configure, "lms", active=False))
    seen = _network(monkeypatch, lambda request: httpx.Response(200))

    response = client.post(_URL.format(code="lms"))

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "CRM-1703"
    assert "включите" in response.json()["detail"]
    assert seen == []


def test_unknown_source_is_404(client) -> None:
    login(client, "ADMIN")
    response = client.post(_URL.format(code="nope"))
    assert response.status_code == 404, response.text


def test_source_without_an_address_reports_it(client, monkeypatch) -> None:
    from app.core.config import get_settings

    login(client, "ADMIN")
    run(client, functools.partial(_configure, "lms", active=True, base_url=None))
    monkeypatch.setattr(get_settings(), "lms_base_url", None)

    body = client.post(_URL.format(code="lms")).json()

    assert body["ok"] is False
    assert "Адрес источника не задан" in body["message"]


def test_bitrix_probe_reads_server_time_and_flags_api_errors(client, monkeypatch) -> None:
    login(client, "ADMIN")
    run(client, functools.partial(_configure, "bitrix24", active=True, base_url=None))
    monkeypatch.setenv("QA_BITRIX_HOOK", "https://portal.invalid/rest/1/secret-code")
    from sqlalchemy import update

    async def _ref() -> None:
        from app.core.db import session_scope
        from app.modules.integration.models import IntegrationSource

        async with session_scope() as session:
            await session.execute(
                update(IntegrationSource)
                .where(IntegrationSource.code == "bitrix24")
                .values(credentials_ref="QA_BITRIX_HOOK")
            )

    run(client, _ref)
    seen = _network(monkeypatch, lambda request: httpx.Response(200, json={"result": {}}))
    ok = client.post(_URL.format(code="bitrix24")).json()
    assert ok["ok"] is True
    assert str(seen[0].url).endswith("/rest/1/secret-code/server.time.json")
    assert "secret-code" not in str(ok)

    _network(
        monkeypatch,
        lambda request: httpx.Response(200, json={"error": "INVALID_CREDENTIALS"}),
    )
    bad = client.post(_URL.format(code="bitrix24")).json()
    assert bad["ok"] is False
    assert "secret-code" not in str(bad)


def test_only_an_admin_may_check(client, monkeypatch) -> None:
    login(client, "ADMIN")
    run(client, functools.partial(_configure, "lms", active=True))
    sign_in(client, create_user(client, "KAM"))
    assert client.post(_URL.format(code="lms")).status_code == 403
