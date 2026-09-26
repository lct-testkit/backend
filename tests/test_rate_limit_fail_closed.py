"""Лимиты частоты при недоступном Redis.

По умолчанию счётчик без Redis пропускает запрос (доступность важнее). Но у лимитов, которые и
есть защита от перебора (страница и код подписания, ссылка-приглашение, смена пароля), это
означало бы «нет защиты»: достаточно дождаться сбоя Redis. Для них — 503, а не пропуск.
"""

from __future__ import annotations

import pytest

from app.core import rate_limit
from app.core.errors import AppError, ErrorCode
from tests.conftest import TEST_DATABASE_URL, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


@pytest.fixture
def redis_down(client, monkeypatch):
    """Redis недоступен только для счётчиков частоты: сессии и остальное трогать незачем.
    Зависит от `client`, чтобы подмена наступала после старта приложения."""

    def broken():
        raise ConnectionError("redis is down")

    monkeypatch.setattr(rate_limit, "get_redis", broken)


async def _hit(fail_closed: bool):
    return await rate_limit.hit(
        "subject", "test:route", limit=5, window_seconds=60, fail_closed=fail_closed
    )


async def _enforce(fail_closed: bool) -> None:
    await rate_limit.enforce(
        "subject", "test:route", limit=5, window_seconds=60, fail_closed=fail_closed
    )


class TestHit:
    def test_an_ordinary_limit_lets_the_request_through(self, client, redis_down) -> None:
        result = run(client, _hit, False)

        assert result.allowed is True

    def test_a_fail_closed_limit_refuses_with_503(self, client, redis_down) -> None:
        with pytest.raises(AppError) as caught:
            run(client, _enforce, True)

        assert caught.value.code is ErrorCode.DEPENDENCY_UNAVAILABLE
        assert caught.value.status == 503
        assert caught.value.headers and "Retry-After" in caught.value.headers

    def test_a_working_redis_still_counts_and_limits(self, client) -> None:
        async def five_then_sixth() -> None:
            for _ in range(5):
                await rate_limit.enforce(
                    "subject-ok", "test:ok", limit=5, window_seconds=60, fail_closed=True
                )
            with pytest.raises(AppError) as caught:
                await rate_limit.enforce(
                    "subject-ok", "test:ok", limit=5, window_seconds=60, fail_closed=True
                )
            assert caught.value.code is ErrorCode.RATE_LIMITED

        run(client, five_then_sixth)


class TestSensitiveRoutes:
    def test_the_public_signing_page_is_closed_when_redis_is_down(self, client, redis_down) -> None:
        response = client.get("/public/sign/" + "t" * 43)

        assert response.status_code == 503, response.text
        assert response.json()["code"] == "CRM-9503"

    def test_the_public_otp_and_sign_routes_are_closed_too(self, client, redis_down) -> None:
        token = "t" * 43

        assert client.post(f"/public/sign/{token}/challenge").status_code == 503
        assert client.post(f"/public/sign/{token}/sign", json={"otp": "123456"}).status_code == 503

    def test_the_invite_link_check_is_closed_when_redis_is_down(self, client, redis_down) -> None:
        response = client.get("/api/auth/invite/" + "i" * 43)

        assert response.status_code == 503, response.text

    def test_public_verify_stays_open_without_redis(self, client, redis_down) -> None:
        # Проверка подписи — чтение, у неё нет секрета для перебора: остаётся как раньше.
        response = client.get("/public/verify/00000000-0000-0000-0000-000000000000")

        assert response.status_code == 200
        assert response.json()["status"] == "not_found"

    def test_the_signing_page_answers_normally_with_a_working_redis(self, client) -> None:
        response = client.get("/public/sign/" + "t" * 43)

        # Токена нет — обычный отказ по ссылке, а не 503.
        assert response.status_code != 503
