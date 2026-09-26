"""Внешний источник автоподстановки: публичный поиск ФНС (без сети, на `httpx.MockTransport`)."""

from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

from app.modules.registry import fns
from app.modules.registry.fns import FnsEgrulProvider

_ROW = {
    "c": 'ПАО "РОСТЕЛЕКОМ"',
    "g": "ПРЕЗИДЕНТ: Осеевский Михаил Эдуардович",
    "i": "7707049388",
    "k": "ul",
    "n": 'ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО "РОСТЕЛЕКОМ"',
    "o": "1027700198767",
    "p": "784201001",
    "r": "09.09.2002",
    "t": "секретный-токен-строки",
    "rn": "Г.Санкт-Петербург",
}


@pytest.fixture(autouse=True)
def _fresh_state() -> None:
    fns._reset_state()


def _provider(handler: httpx.MockTransport) -> FnsEgrulProvider:
    return FnsEgrulProvider(base_url="https://fns.test", timeout_seconds=1, transport=handler)


def _ok_transport(rows: list[dict], *, waits: int = 0) -> httpx.MockTransport:
    state = {"gets": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert b"query=7707049388" in request.content or b"query=" in request.content
            return httpx.Response(200, json={"t": "tok", "captchaRequired": False})
        state["gets"] += 1
        if state["gets"] <= waits:
            return httpx.Response(200, json={"status": "wait"})
        return httpx.Response(200, json={"rows": rows})

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_get_by_inn_maps_row_and_drops_token() -> None:
    details = await _provider(_ok_transport([_ROW])).get_by_inn("7707049388")
    assert details is not None
    assert details.provider == "fns_egrul"
    assert details.short_name == 'ПАО "РОСТЕЛЕКОМ"'
    assert details.kpp == "784201001"
    assert details.director_name == "Осеевский Михаил Эдуардович"
    assert details.director_position == "Президент"
    assert details.registration_date == dt.date(2002, 9, 9)
    assert details.status == "active"
    assert "t" not in details.raw


@pytest.mark.asyncio
async def test_get_by_inn_requires_exact_inn() -> None:
    other = {**_ROW, "i": "7700000000"}
    assert await _provider(_ok_transport([other])).get_by_inn("7707049388") is None


@pytest.mark.asyncio
async def test_liquidated_company_is_marked() -> None:
    row = {**_ROW, "e": "01.01.2020"}
    found = await _provider(_ok_transport([row])).suggest("Ростелеком", 5)
    assert [(s.inn, s.status, s.is_liquidated) for s in found] == [
        ("7707049388", "liquidated", True)
    ]


@pytest.mark.asyncio
async def test_waits_for_result() -> None:
    fns_module_pause = fns._RESULT_PAUSE_SECONDS
    fns._RESULT_PAUSE_SECONDS = 0
    try:
        found = await _provider(_ok_transport([_ROW], waits=2)).suggest("7707049388", 5)
    finally:
        fns._RESULT_PAUSE_SECONDS = fns_module_pause
    assert len(found) == 1


@pytest.mark.asyncio
async def test_short_query_does_not_call_the_service() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise AssertionError("сервис не должен вызываться")

    assert await _provider(httpx.MockTransport(boom)).suggest("ро", 5) == []


@pytest.mark.asyncio
async def test_failure_returns_nothing_and_cools_down() -> None:
    calls = {"n": 0}

    def down(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    provider = _provider(httpx.MockTransport(down))
    for _ in range(fns._FAILURES_BEFORE_COOLDOWN):
        assert await provider.get_by_inn("7707049388") is None
    assert calls["n"] == fns._FAILURES_BEFORE_COOLDOWN
    # источник остыл: сервис больше не дёргается, health честно говорит «недоступен»
    assert await provider.get_by_inn("7707049388") is None
    assert calls["n"] == fns._FAILURES_BEFORE_COOLDOWN
    assert await provider.health() is False


@pytest.mark.asyncio
async def test_captcha_or_bad_json_is_not_an_error() -> None:
    def captcha(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps({"captchaRequired": True}).encode())

    assert await _provider(httpx.MockTransport(captcha)).get_by_inn("7707049388") is None

    def garbage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>")

    fns._reset_state()
    assert await _provider(httpx.MockTransport(garbage)).get_by_inn("7707049388") is None


@pytest.mark.skipif(
    not __import__("tests.conftest", fromlist=["TEST_DATABASE_URL"]).TEST_DATABASE_URL,
    reason="нужен TEST_DATABASE_URL с применёнными миграциями",
)
class TestChainFlag:
    """Внешний источник стоит в цепочке только при включённом флаге `external_org_lookup`."""

    @staticmethod
    async def _names(enable: bool | None) -> list[str]:
        from sqlalchemy import update

        from app.core.db import session_scope
        from app.modules.admin.models import FeatureFlag
        from app.modules.registry.providers import resolve_chain

        async with session_scope() as session:
            if enable is not None:
                # миграция 0021 заводит флаг выключенным; тест переключает его и возвращает назад
                await session.execute(
                    update(FeatureFlag)
                    .where(FeatureFlag.code == "external_org_lookup")
                    .values(is_enabled=enable)
                )
                await session.flush()
            return [provider.name for provider in await resolve_chain(session)]

    def test_flag_row_is_seeded_off_and_chain_has_no_external(self, client) -> None:
        from tests.conftest import run

        names = run(client, self._names, False)
        assert "fns_egrul" not in names

    def test_enabled_flag_adds_fns_before_mock(self, client) -> None:
        from tests.conftest import run

        try:
            names = run(client, self._names, True)
            assert names.index("fns_egrul") < names.index("mock")
        finally:
            run(client, self._names, False)
