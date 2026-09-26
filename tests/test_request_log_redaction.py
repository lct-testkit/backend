"""Токены подписания и приглашений не попадают в журнал запросов."""

from __future__ import annotations

import pytest

from app.middleware.request_context import _loggable_path
from tests.conftest import TEST_DATABASE_URL


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/public/sign/abc123", "/public/sign/***"),
        ("/public/sign/abc123/file", "/public/sign/***/file"),
        ("/public/sign/abc123/sign", "/public/sign/***/sign"),
        ("/api/auth/invite/tok-en_9", "/api/auth/invite/***"),
        ("/api/deals/0193", "/api/deals/0193"),
        ("/public/verify/0193", "/public/verify/0193"),
    ],
)
def test_token_segment_is_hidden(path: str, expected: str) -> None:
    assert _loggable_path(path) == expected


class TestEveryLogLine:
    """Не только журнал запросов: обработчики ошибок пишут `path=request.url.path`, а сторонние
    библиотеки — URL в тексте сообщения. Токен закрывает последний барьер конвейера логов."""

    def test_the_processor_hides_the_token_in_any_string_field(self) -> None:
        from app.core.logging import _redact_path_tokens

        event = {
            "event": "GET http://api/public/sign/SECRETTOKEN/file failed",
            "path": "/public/sign/SECRETTOKEN/sign",
            "exception": 'File "x.py"\nValueError: bad url /api/auth/invite/INVITETOKEN?x=1',
            "status": 404,
            "route": "/public/sign/{token}",
        }

        cleaned = _redact_path_tokens(None, "info", dict(event))

        blob = " ".join(str(v) for v in cleaned.values())
        assert "SECRETTOKEN" not in blob
        assert "INVITETOKEN" not in blob
        assert cleaned["path"] == "/public/sign/***/sign"
        assert cleaned["status"] == 404
        # Шаблон маршрута — не токен: он остаётся читаемым.
        assert cleaned["route"] == "/public/sign/{token}"

    def test_other_paths_are_left_alone(self) -> None:
        from app.core.logging import redact_path_tokens

        assert redact_path_tokens("/api/deals/0193/comments") == "/api/deals/0193/comments"
        assert redact_path_tokens("/public/verify/0193") == "/public/verify/0193"

    @pytest.mark.skipif(not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL")
    def test_a_failed_signing_request_does_not_leave_the_token_in_the_log(self, client) -> None:
        import io
        import logging

        from app.core.logging import configure_logging

        configure_logging(level="INFO", json_output=True)
        root = logging.getLogger()
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(root.handlers[0].formatter)
        root.addHandler(handler)
        token = "LEAKCHECK" + "x" * 34
        try:
            response = client.get(f"/public/sign/{token}")
            client.post(f"/public/sign/{token}/reject", json={"reason": "нет"})
        finally:
            root.removeHandler(handler)

        assert response.status_code == 404
        log = stream.getvalue()
        assert "app_error" in log  # обработчик ошибок действительно писал строку с путём
        assert token not in log
