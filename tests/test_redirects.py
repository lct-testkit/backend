"""Адрес возврата после входа: только путь внутри сайта (без открытого редиректа)."""

import pytest

from app.modules.identity.redirects import safe_next_path


@pytest.mark.parametrize(
    "value",
    ["/", "/deals", "/deals/01a0c0?tab=files", "/admin/users#top", "/sign/abc-DEF_123"],
)
def test_site_paths_are_kept(value: str) -> None:
    assert safe_next_path(value) == value


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "deals",
        "https://evil.example",
        "http://localhost:8080/deals",
        "//evil.example",
        "///evil.example",
        "/\evil.example",
        "\\evil.example",
        "javascript:alert(1)",
        "/deals\r\nSet-Cookie: x=1",
        "/deals\x00",
    ],
)
def test_everything_else_is_dropped(value: str | None) -> None:
    assert safe_next_path(value) is None
