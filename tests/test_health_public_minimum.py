"""`/health/live` и `/health/ready` не раскрывают версию, профиль и состав инфраструктуры;
полный список — только на `/health/details` с верным `X-Health-Token` (B6-8)."""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

TOKEN = "Health-Details-Token-Value-2026-xyz"


@pytest.fixture
def health_token(monkeypatch):
    """Должна встать раньше `client` в сигнатуре теста: `client` строит `Settings` и кэширует
    синглтон внутри своей фикстуры, поэтому переменная окружения обязана быть выставлена
    заранее (порядок настройки фикстур одного скоупа — слева направо по списку параметров)."""
    monkeypatch.setenv("HEALTH_DETAILS_TOKEN", TOKEN)
    get_settings.cache_clear()
    yield TOKEN
    get_settings.cache_clear()


class TestPublicMinimum:
    def test_live_has_only_status(self, health_token, client) -> None:
        response = client.get("/health/live")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_ready_has_no_dependency_list(self, health_token, client) -> None:
        response = client.get("/health/ready")
        # Postgres/Redis есть (критичные) — SeaweedFS и Keycloak в тестовом окружении не
        # подняты, поэтому возможен "degraded", а не только "ok"; важно, что тело не раскрывает
        # список зависимостей в любом случае.
        assert response.status_code in (200, 503)
        body = response.json()
        assert body["status"] in ("ok", "degraded")
        assert "dependencies" not in body

    def test_details_without_token_is_404(self, health_token, client) -> None:
        response = client.get("/health/details")
        assert response.status_code == 404

    def test_details_with_wrong_token_is_404(self, health_token, client) -> None:
        response = client.get("/health/details", headers={"X-Health-Token": "wrong"})
        assert response.status_code == 404

    def test_details_with_correct_token_gives_full_list(self, health_token, client) -> None:
        response = client.get("/health/details", headers={"X-Health-Token": health_token})
        assert response.status_code in (200, 503)
        body = response.json()
        assert body["status"] in ("ok", "degraded", "unavailable")
        names = {dep["name"] for dep in body["dependencies"]}
        assert "postgres" in names
        assert "redis" in names

    def test_keycloak_without_token_is_404(self, health_token, client) -> None:
        assert client.get("/health/keycloak").status_code == 404

    def test_keycloak_with_token_is_not_404(self, health_token, client) -> None:
        response = client.get("/health/keycloak", headers={"X-Health-Token": health_token})
        assert response.status_code != 404


class TestNoTokenConfigured:
    def test_details_404_when_token_not_configured(self, client) -> None:
        """Без `HEALTH_DETAILS_TOKEN` в конфиге ручка недостижима даже с каким-то заголовком."""
        response = client.get("/health/details", headers={"X-Health-Token": "anything"})
        assert response.status_code == 404
