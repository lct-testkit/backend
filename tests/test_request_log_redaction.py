"""Токены подписания и приглашений не попадают в журнал запросов."""

from __future__ import annotations

import pytest

from app.middleware.request_context import _loggable_path


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
