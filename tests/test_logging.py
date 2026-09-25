"""Логирование: секреты не должны попадать в логи через сторонние библиотеки."""

from __future__ import annotations

import io
import logging

import httpx

from app.core.logging import configure_logging

_SECRET_URL = "https://portal.example/rest/1/s3cr3tc0de/crm.item.add.json"


def test_httpx_request_lines_do_not_reach_the_log() -> None:
    """Секрет Bitrix24 — весь URL вебхука. httpx на INFO пишет URL каждого
    запроса целиком, поэтому его логгер обязан быть не ниже WARNING."""
    configure_logging(level="INFO", json_output=True)
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))
        with httpx.Client(transport=transport) as client:
            client.post(_SECRET_URL, json={})
    finally:
        root.removeHandler(handler)

    assert "s3cr3tc0de" not in stream.getvalue()
