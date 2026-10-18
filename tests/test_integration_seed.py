"""Сид источников интеграций (`integration/seed.py`) на настоящей PostgreSQL
(`TEST_DATABASE_URL`, см. докстринг `tests/conftest.py`).

Источник `cms` заводится включённым, только если секрет вебхука реально есть в окружении:
`CMS_WEBHOOK_SECRET_REF` (имя переменной) подставляется в `credentials_ref`. Раньше сид всегда
заводил его выключенным и без ссылки на секрет — вебхук отвечал 503, пока администратор не
пройдёт по нему руками, а `CMS_WEBHOOK_SECRET_REF` не читал вообще никто.

Каждая проверка идёт в транзакции, которая откатывается: чужие источники в общей БД тестов не
трогаются.
"""

from __future__ import annotations

import functools
from typing import Any

import pytest

from tests.conftest import TEST_DATABASE_URL, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

_REF = "CMS_SEED_TEST_SECRET"
_SECRET_VALUE = "значение-которое-нельзя-логировать"


async def _seed(existing: dict[str, dict[str, Any]] | None = None, *, twice: bool = False):
    from sqlalchemy import delete, select

    from app.core.db import session_scope
    from app.modules.integration.models import IntegrationSource
    from app.modules.integration.seed import seed_integration_sources

    async with session_scope() as session:
        await session.execute(delete(IntegrationSource))
        for code, values in (existing or {}).items():
            session.add(IntegrationSource(code=code, name="Заведён вручную", **values))
        await session.flush()

        created = await seed_integration_sources(session)
        second = await seed_integration_sources(session) if twice else None
        rows = {
            source.code: {
                "name": source.name,
                "is_active": source.is_active,
                "credentials_ref": source.credentials_ref,
                "base_url": source.base_url,
            }
            for source in (await session.execute(select(IntegrationSource))).scalars()
        }
        await session.rollback()
        return {"created": created, "second": second, "rows": rows}


@pytest.fixture
def seed(client, monkeypatch: pytest.MonkeyPatch):
    from app.core.config import get_settings
    from app.modules.integration import seed as seed_module

    monkeypatch.delenv(_REF, raising=False)
    monkeypatch.setattr(get_settings(), "cms_webhook_secret_ref", None)

    class _Recorder:
        """Всё, что сид отправил в лог: значение секрета там оказываться не должно."""

        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict[str, Any]]] = []

        def info(self, event: str, **fields: Any) -> None:
            self.calls.append(("info", event, fields))

        def warning(self, event: str, **fields: Any) -> None:
            self.calls.append(("warning", event, fields))

    recorder = _Recorder()
    monkeypatch.setattr(seed_module, "logger", recorder)

    def configure(*, ref: str | None, value: str | None = None) -> _Recorder:
        monkeypatch.setattr(get_settings(), "cms_webhook_secret_ref", ref)
        if value is not None:
            monkeypatch.setenv(_REF, value)
        return recorder

    def go(existing: dict[str, dict[str, Any]] | None = None, *, twice: bool = False):
        return run(client, functools.partial(_seed, existing, twice=twice))

    go.configure = configure  # type: ignore[attr-defined]
    return go


class TestSeededSources:
    def test_all_three_sources_are_created(self, seed) -> None:
        result = seed()

        assert result["created"] == 3
        assert set(result["rows"]) == {"cms", "lms", "bitrix24"}

    def test_without_a_configured_secret_everything_stays_off(self, seed) -> None:
        rows = seed()["rows"]

        for row in rows.values():
            assert row["is_active"] is False
            assert row["credentials_ref"] is None

    def test_configured_secret_present_in_the_environment_activates_cms(self, seed) -> None:
        seed.configure(ref=_REF, value=_SECRET_VALUE)

        rows = seed()["rows"]

        assert rows["cms"]["is_active"] is True
        assert rows["cms"]["credentials_ref"] == _REF
        # В БД лежит имя переменной, не сам секрет.
        assert _SECRET_VALUE not in str(rows)

    def test_the_other_sources_never_switch_on_by_themselves(self, seed) -> None:
        seed.configure(ref=_REF, value=_SECRET_VALUE)

        rows = seed()["rows"]

        for code in ("lms", "bitrix24"):
            assert rows[code]["is_active"] is False
            assert rows[code]["credentials_ref"] is None

    def test_reference_without_a_secret_in_the_environment_leaves_cms_off(self, seed) -> None:
        recorder = seed.configure(ref=_REF)

        rows = seed()["rows"]

        assert rows["cms"]["is_active"] is False
        assert rows["cms"]["credentials_ref"] == _REF  # ссылка заведена: админу остаётся включить
        assert any(event == "cms_source_left_inactive" for _l, event, _f in recorder.calls)

    @pytest.mark.parametrize("value", ["", "   "])
    def test_empty_secret_is_not_a_secret(self, seed, value: str) -> None:
        seed.configure(ref=_REF, value=value)

        rows = seed()["rows"]

        assert rows["cms"]["is_active"] is False
        assert rows["cms"]["credentials_ref"] == _REF

    def test_empty_reference_setting_is_the_same_as_none(self, seed) -> None:
        # docker-compose передаёт незаданную настройку пустой строкой.
        seed.configure(ref="")

        rows = seed()["rows"]

        assert (rows["cms"]["is_active"], rows["cms"]["credentials_ref"]) == (False, None)

    def test_the_secret_value_never_reaches_the_log(self, seed) -> None:
        recorder = seed.configure(ref=_REF, value=_SECRET_VALUE)

        seed(twice=True)

        assert recorder.calls
        assert _SECRET_VALUE not in repr(recorder.calls)


async def _configure_demo(
    *, profile: str, cms_base_url: str | None, lms_base_url: str | None, pre: dict[str, Any] | None
) -> dict[str, Any]:
    from sqlalchemy import delete, select

    from app.core.config import get_settings
    from app.core.db import session_scope
    from app.modules.integration.models import IntegrationSource
    from app.modules.integration.seed import (
        _configure_demo_sources,
        seed_integration_sources,
    )

    settings = get_settings()
    old_profile, old_cms, old_lms = (
        settings.app_profile,
        settings.cms_base_url,
        settings.lms_base_url,
    )
    settings.app_profile = profile  # type: ignore[assignment]
    settings.cms_base_url = cms_base_url
    settings.lms_base_url = lms_base_url
    try:
        async with session_scope() as session:
            await session.execute(delete(IntegrationSource))
            await seed_integration_sources(session)
            for code, values in (pre or {}).items():
                source = (
                    await session.execute(
                        select(IntegrationSource).where(IntegrationSource.code == code)
                    )
                ).scalar_one()
                for key, value in values.items():
                    setattr(source, key, value)
            await session.flush()

            configured = await _configure_demo_sources(session)
            second = await _configure_demo_sources(session)
            rows = {
                source.code: {
                    "is_active": source.is_active,
                    "pull_enabled": source.pull_enabled,
                    "pull_interval_seconds": source.pull_interval_seconds,
                    "base_url": source.base_url,
                    "field_mapping": source.field_mapping,
                    "routing": source.routing,
                    "config": source.config,
                }
                for source in (await session.execute(select(IntegrationSource))).scalars()
            }
            await session.rollback()
            return {"configured": configured, "second": second, "rows": rows}
    finally:
        settings.app_profile, settings.cms_base_url, settings.lms_base_url = (
            old_profile,
            old_cms,
            old_lms,
        )


class TestConfigureDemoSources:
    """B5-7: `_configure_demo_sources` — только `APP_PROFILE=demo` с заданными адресами моков."""

    def test_dev_profile_leaves_sources_untouched(self, client) -> None:
        result = run(
            client,
            functools.partial(
                _configure_demo,
                profile="dev",
                cms_base_url="http://cms",
                lms_base_url="http://lms",
                pre=None,
            ),
        )

        assert result["configured"] == 0
        assert result["rows"]["cms"]["is_active"] is False
        assert result["rows"]["lms"]["is_active"] is False

    def test_demo_without_mock_urls_does_nothing(self, client) -> None:
        result = run(
            client,
            functools.partial(
                _configure_demo, profile="demo", cms_base_url=None, lms_base_url=None, pre=None
            ),
        )

        assert result["configured"] == 0

    def test_demo_with_mock_urls_activates_both_sources_with_mapping_and_routing(
        self, client
    ) -> None:
        result = run(
            client,
            functools.partial(
                _configure_demo,
                profile="demo",
                cms_base_url="http://cms.invalid",
                lms_base_url="http://lms.invalid",
                pre=None,
            ),
        )

        assert result["configured"] == 2
        cms = result["rows"]["cms"]
        lms = result["rows"]["lms"]
        assert cms["is_active"] is True
        assert cms["pull_enabled"] is True
        assert cms["pull_interval_seconds"] == 60
        assert cms["field_mapping"]["rules"]
        assert cms["config"]["demo_seeded"] is True
        assert lms["is_active"] is True
        assert lms["pull_enabled"] is True
        assert lms["config"]["course_code_map"] == {
            "crs-python-basics": "py-base",
            "crs-data-analysis": "ds-pro",
        }

    def test_second_run_does_not_reconfigure(self, client) -> None:
        result = run(
            client,
            functools.partial(
                _configure_demo,
                profile="demo",
                cms_base_url="http://cms.invalid",
                lms_base_url="http://lms.invalid",
                pre=None,
            ),
        )

        assert (result["configured"], result["second"]) == (2, 0)

    def test_manually_disabled_source_stays_disabled_after_reseed(self, client) -> None:
        result = run(
            client,
            functools.partial(
                _configure_demo,
                profile="demo",
                cms_base_url="http://cms.invalid",
                lms_base_url="http://lms.invalid",
                pre={"cms": {"config": {"demo_seeded": True}, "is_active": False}},
            ),
        )

        assert result["rows"]["cms"]["is_active"] is False
        # lms не был отмечен — настраивается как обычно.
        assert result["rows"]["lms"]["is_active"] is True


class TestSeedIsIdempotent:
    def test_second_run_creates_nothing_and_changes_nothing(self, seed) -> None:
        seed.configure(ref=_REF, value=_SECRET_VALUE)

        result = seed(twice=True)

        assert (result["created"], result["second"]) == (3, 0)
        assert result["rows"]["cms"]["is_active"] is True

    def test_existing_source_is_left_exactly_as_the_admin_set_it(self, seed) -> None:
        seed.configure(ref=_REF, value=_SECRET_VALUE)
        existing = {"cms": {"is_active": False, "credentials_ref": "MY_OWN_REF", "base_url": "u"}}

        result = seed(existing)

        # Выключенный админом источник сид не включает, чужую ссылку не перезаписывает.
        assert result["created"] == 2
        assert result["rows"]["cms"] == {
            "name": "Заведён вручную",
            "is_active": False,
            "credentials_ref": "MY_OWN_REF",
            "base_url": "u",
        }

    def test_existing_source_without_a_reference_is_not_switched_on(self, seed) -> None:
        seed.configure(ref=_REF, value=_SECRET_VALUE)

        result = seed({"cms": {"is_active": False}})

        assert result["rows"]["cms"]["is_active"] is False
        assert result["rows"]["cms"]["credentials_ref"] is None

    def test_existing_active_source_stays_active(self, seed) -> None:
        result = seed({"lms": {"is_active": True, "credentials_ref": "LMS_REF"}})

        assert result["rows"]["lms"]["is_active"] is True
        assert result["created"] == 2
