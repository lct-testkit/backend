"""Транзакция запроса коммитится до отправки ответа клиенту.

Раньше зависимость сессии выполняла коммит после `yield` уже после того, как ответ ушёл: клиент
получал 201 и мог сразу прийти со следующим запросом раньше коммита (404 на только что созданную
запись), а сбой коммита не превращался в ошибку ответа. Проверка без БД и без сети: приложение
вызывается по ASGI напрямую, порядок событий пишется в общий список.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import FastAPI

from app.core.db import get_db_session
from app.core.deps import DbSession


def _run(app: FastAPI) -> None:
    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(_message: dict) -> None:
        events.append(f"send:{_message['type']}")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": "/create",
        "raw_path": b"/create",
        "query_string": b"",
        "headers": [],
        "server": ("test", 80),
        "client": ("test", 1),
        "scheme": "http",
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))


events: list[str] = []


def test_commit_happens_before_the_response_is_sent() -> None:
    events.clear()
    app = FastAPI()

    async def fake_session() -> AsyncIterator[object]:
        yield object()
        events.append("commit")

    app.dependency_overrides[get_db_session] = fake_session

    @app.post("/create", status_code=201)
    async def create(_session: DbSession) -> dict[str, bool]:
        events.append("handler")
        return {"ok": True}

    _run(app)

    assert events.index("handler") < events.index("commit")
    assert events.index("commit") < events.index("send:http.response.start")
