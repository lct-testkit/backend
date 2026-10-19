"""G-3 (QA 20.10): включённому источнику нужны адрес и способ входа; способ входа — из списка."""

from __future__ import annotations

import functools

import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import login

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


async def _reset(code: str, **values) -> None:
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
        source.is_active = False
        source.pull_enabled = False
        source.base_url = None
        source.auth_type = None
        source.credentials_ref = None
        for key, value in values.items():
            setattr(source, key, value)


def _patch(client, code: str, **body):
    return client.patch(f"/api/admin/integrations/sources/{code}", json=body)


def _fields(response) -> set[str]:
    return {item["field"] for item in response.json()["errors"]}


@pytest.fixture
def admin(client, monkeypatch):
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "lms_base_url", None)
    monkeypatch.setattr(settings, "cms_base_url", None)
    monkeypatch.setattr(settings, "bitrix_webhook_url_ref", None)
    login(client, "ADMIN")
    return client


def test_enabling_without_address_and_auth_is_refused(admin) -> None:
    run(admin, functools.partial(_reset, "lms"))
    response = _patch(admin, "lms", is_active=True)
    assert response.status_code == 422, response.text
    assert _fields(response) == {"base_url", "auth_type"}


def test_pull_alone_also_needs_the_details(admin) -> None:
    run(admin, functools.partial(_reset, "lms"))
    assert _fields(_patch(admin, "lms", pull_enabled=True)) == {"base_url", "auth_type"}


def test_enabling_with_both_is_saved(admin) -> None:
    run(admin, functools.partial(_reset, "lms"))
    response = _patch(
        admin, "lms", is_active=True, base_url="https://lms.example.org", auth_type="bearer"
    )
    assert response.status_code == 200, response.text
    assert response.json()["is_active"] is True


def test_not_a_url_is_refused(admin) -> None:
    run(admin, functools.partial(_reset, "lms"))
    response = _patch(admin, "lms", base_url="сайт", auth_type="bearer", is_active=True)
    assert response.status_code == 422, response.text
    assert "base_url" in _fields(response)


def test_unknown_auth_type_is_refused(admin) -> None:
    run(admin, functools.partial(_reset, "lms"))
    response = _patch(admin, "lms", auth_type="magic")
    assert response.status_code == 422, response.text
    assert _fields(response) == {"auth_type"}


def test_disabled_source_may_stay_incomplete(admin) -> None:
    run(admin, functools.partial(_reset, "lms"))
    assert _patch(admin, "lms", name="Только название").status_code == 200
    assert _patch(admin, "lms", is_active=False, base_url=None).status_code == 200


def test_renaming_an_already_enabled_source_is_not_blocked(admin) -> None:
    run(
        admin,
        functools.partial(_reset, "lms", is_active=True, base_url="https://lms.example.org"),
    )
    # Настроен до этой проверки (без способа входа): правка названия проходит...
    assert _patch(admin, "lms", name="LMS школы").status_code == 200
    # ...а попытка тронуть сам вход без способа входа — нет.
    assert _fields(_patch(admin, "lms", base_url="https://lms2.example.org")) == {"auth_type"}


def test_bitrix_needs_the_secret_reference_not_an_address(admin) -> None:
    run(admin, functools.partial(_reset, "bitrix24"))
    response = _patch(admin, "bitrix24", is_active=True, auth_type="none")
    assert response.status_code == 422, response.text
    assert _fields(response) == {"credentials_ref"}
    ok = _patch(admin, "bitrix24", is_active=True, auth_type="none", credentials_ref="BITRIX_HOOK")
    assert ok.status_code == 200, ok.text
    run(admin, functools.partial(_reset, "bitrix24"))
