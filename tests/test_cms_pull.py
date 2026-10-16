"""B5-2: забор лидов с сайта по API (`CmsPullService`/`sweep_cms_leads_pull`).

Настоящая Postgres обязательна (`TEST_DATABASE_URL`); сеть подменяется `Network`
(`tests/lms_helpers.py`, тот же приём, что у LMS-забора — `MockTransport` на
`httpx.AsyncClient`). Контракта мока сайта в этом репозитории нет
(`deploy/mocks/cms/openapi.yaml` живёт в другом репозитории) — форма ответа
взята из текста задачи B5-2 документа спринтов.
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import uuid
from typing import Any

import httpx
import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.lms_helpers import Network

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

_CMS_URL = "https://cms.invalid"


async def _prepare(*, active: bool = True, pull_enabled: bool = True) -> None:
    """Служебная учётка, источник `cms` (включён под забор) и опубликованная воронка B2C по
    умолчанию — тот же приём, что `tests/test_cms_webhook.py::_prepare`."""
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.integration.models import IntegrationSource
    from app.modules.integration.seed import seed_integration_account
    from app.modules.workflow.models import Workflow
    from app.modules.workflow.seed import _b2c_spec, seed_workflow

    async with session_scope() as session:
        await seed_integration_account(session)
        source = (
            await session.execute(select(IntegrationSource).where(IntegrationSource.code == "cms"))
        ).scalar_one_or_none()
        if source is None:
            source = IntegrationSource(code="cms", name="Сайт (Laravel CMS)")
            session.add(source)
        source.is_active = active
        source.pull_enabled = pull_enabled
        source.base_url = None  # берём из settings.cms_base_url (monkeypatch в тесте)
        source.last_sync_at = None

        workflow = (
            await session.execute(
                select(Workflow).where(
                    Workflow.deal_type == "b2c",
                    Workflow.is_default.is_(True),
                    Workflow.state == "published",
                )
            )
        ).scalar_one_or_none()
        if workflow is None:
            spec = dataclasses.replace(_b2c_spec(), code=f"b2c_cms_pull_{uuid.uuid4().hex[:8]}")
            await seed_workflow(session, spec)


async def _reset_cursor() -> None:
    from sqlalchemy import delete

    from app.core.db import session_scope
    from app.modules.integration.models import SyncCursor

    async with session_scope() as session:
        await session.execute(delete(SyncCursor).where(SyncCursor.source_code == "cms"))


async def _release_pull_lock() -> None:
    from app.core.redis_client import get_redis, key_lock

    await get_redis().delete(key_lock("integration:cms_pull"))


async def _cursor() -> str | None:
    from app.core.db import session_scope
    from app.modules.integration.lms import get_cursor

    async with session_scope() as session:
        return await get_cursor(session, source_code="cms", resource="leads_updated_since")


async def _inbound(key: str) -> dict[str, Any] | None:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.integration.models import InboundMessage

    async with session_scope() as session:
        message = (
            await session.execute(
                select(InboundMessage).where(
                    InboundMessage.source_code == "cms", InboundMessage.external_id == key
                )
            )
        ).scalar_one_or_none()
        if message is None:
            return None
        return {
            "status": message.status,
            "channel": message.channel,
            "error": message.error,
            "resulting_entity_id": message.resulting_entity_id,
        }


def _lead(lead_id: str, **overrides: Any) -> dict[str, Any]:
    """Телефон/email выводятся из `lead_id` детерминированно: один и тот же `lead_id` всегда
    даёт одного и того же «человека» (нужно для тестов повтора), а РАЗНЫЕ `lead_id` — разных,
    иначе дедупликация по контакту (`_open_duplicate`) слила бы два разных лида в одну сделку,
    как уже слила бы на живом сайте, если бы оба писали с одного телефона."""
    digits = int.from_bytes(hashlib.sha256(lead_id.encode()).digest()[:4], "big") % 10_000_000
    row = {
        "lead_id": lead_id,
        "name": "Мария Кузнецова",
        "phone": f"+7926{digits:07d}",
        "email": f"{lead_id.replace('-', '_')}@example.com",
        "product_interest": "python-basics",
        "source_campaign": "yandex-direct-sept",
        "submitted_at": "2026-09-16T08:12:00Z",
        "updated_at": "2026-09-16T08:12:00Z",
        **overrides,
    }
    return {key: value for key, value in row.items() if value is not None}


class TestCmsPull:
    @pytest.fixture(autouse=True)
    def _setup(self, client, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core.config import get_settings

        self.client = client
        self.network = Network().install(monkeypatch)
        # БД тестов общая и не откатывается между тестами (см. `tests/conftest.py`): свой
        # префикс `lead_id` на тест, иначе `UNIQUE(source_code, external_id)` столкнул бы
        # одноимённые лиды двух разных тестов.
        self.run_id = uuid.uuid4().hex[:8]
        monkeypatch.setattr(get_settings(), "cms_base_url", _CMS_URL)
        monkeypatch.setattr(get_settings(), "cms_auth_ref", None)
        run(client, functools.partial(_prepare))
        run(client, _reset_cursor)

    def _id(self, suffix: str) -> str:
        return f"lead-{self.run_id}-{suffix}"

    def _serve(self, pages: list[dict[str, Any]]) -> None:
        """`pages` — тела ответов по очереди (одно на страницу); лишние запросы повторяют
        последнюю страницу."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            index = min(calls["n"], len(pages) - 1)
            calls["n"] += 1
            return httpx.Response(200, json=pages[index])

        self.network.respond = handler

    def _run(self) -> Any:
        """Лок между прогонами снимает Lua-скрипт (`redis.Lock.release`), а `fakeredis` без
        `lupa` его не исполняет (тот же приём и тот же пробел, что у `crm_helpers.
        _release_transition_lock`): ключ удаляется вручную, иначе второй вызов в этом же
        тесте получил бы `busy` до конца TTL (600 с)."""
        from app.modules.integration.cms_pull import CmsPullService

        result = run(self.client, functools.partial(CmsPullService().run, "cron"))
        run(self.client, _release_pull_lock)
        return result

    def test_two_leads_on_two_pages_create_two_deals(self) -> None:
        lead_a, lead_b = self._id("3001"), self._id("3002")
        self._serve(
            [
                {"items": [_lead(lead_a)], "next_cursor": "page2"},
                {"items": [_lead(lead_b, updated_at="2026-09-16T09:00:00Z")], "next_cursor": None},
            ]
        )

        result = self._run()

        assert result.status == "ok"
        assert result.created == 2
        assert result.duplicates == 0
        assert result.failed == 0
        assert run(self.client, _cursor) == "2026-09-16T09:00:00Z"

        first = run(self.client, functools.partial(_inbound, lead_a))
        assert first is not None
        assert first["status"] == "processed"
        assert first["channel"] == "pull"
        assert first["resulting_entity_id"] is not None

    def test_repeated_run_with_the_same_leads_creates_nothing(self) -> None:
        lead = self._id("3001")
        self._serve([{"items": [_lead(lead)], "next_cursor": None}])

        first = self._run()
        assert first.created == 1

        second = self._run()
        assert second.created == 0
        assert second.duplicates == 1

    def test_lead_without_phone_or_email_fails_but_does_not_hold_the_cursor(self) -> None:
        lead_bad, lead_ok = self._id("bad"), self._id("3001")
        self._serve(
            [
                {
                    "items": [
                        _lead(lead_bad, phone=None, email=None, updated_at="2026-09-16T08:00:00Z"),
                        _lead(lead_ok, updated_at="2026-09-16T09:00:00Z"),
                    ],
                    "next_cursor": None,
                }
            ]
        )

        result = self._run()

        assert result.created == 1
        assert result.failed == 1
        bad = run(self.client, functools.partial(_inbound, lead_bad))
        assert bad is not None
        assert bad["status"] == "failed"
        # Бизнес-ошибка (нет контакта) курсор не держит — он ушёл дальше этой строки.
        assert run(self.client, _cursor) == "2026-09-16T09:00:00Z"

    def test_temporary_failure_holds_the_cursor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import app.modules.integration.cms_pull as cms_pull_module

        async def boom(self: Any, record: dict[str, Any], key: str) -> tuple[str, bool]:
            return "failed", True

        monkeypatch.setattr(cms_pull_module.CmsPullService, "_ingest_one", boom)
        self._serve([{"items": [_lead(self._id("3001"))], "next_cursor": None}])

        result = self._run()

        assert result.failed == 1
        # Временный сбой держит курсор на прежнем значении (`None` — забора ещё не было).
        assert run(self.client, _cursor) is None

    def test_site_returns_500_is_an_error_without_the_url_in_the_message(self) -> None:
        self.network.respond = lambda request: httpx.Response(500, text="boom")

        result = self._run()

        assert result.status == "error"
        assert result.error is not None
        assert "cms.invalid" not in result.error

    def test_disabled_source_makes_no_http_calls(self) -> None:
        run(self.client, functools.partial(_prepare, pull_enabled=False))
        self._serve([{"items": [_lead("lead-3001")], "next_cursor": None}])

        result = self._run()

        assert result.status == "skipped"
        assert self.network.calls == []

    def test_empty_base_url_makes_no_http_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "cms_base_url", None)
        self._serve([{"items": [_lead("lead-3001")], "next_cursor": None}])

        result = self._run()

        assert result.status == "skipped"
        assert self.network.calls == []

    def test_a_lead_already_delivered_by_webhook_is_a_replay_not_a_new_deal(self) -> None:
        """Ключ идемпотентности вебхука — тот же `lead_id`, что и у pull (конвенция сайта, см.
        докстринг `CmsLeadService.ingest_pulled`): строка во входящих с этим `external_id`
        считается обработанной независимо от канала, которым она попала туда первой. Настоящую
        подпись вебхука здесь поднимать не нужно (её покрывает `tests/test_cms_webhook.py`) —
        важен только факт «ключ уже занят и обработан»."""
        lead = self._id("3001")

        async def _insert_prior_webhook_delivery() -> None:
            from app.core.db import session_scope
            from app.modules.integration.models import InboundMessage, InboundStatus

            async with session_scope() as session:
                session.add(
                    InboundMessage(
                        source_code="cms",
                        external_id=lead,
                        message_type="lead",
                        channel="webhook",
                        raw_payload=_lead(lead),
                        signature_valid=True,
                        status=InboundStatus.PROCESSED.value,
                        resulting_entity_type="deal",
                        resulting_entity_id=uuid.uuid4(),
                    )
                )

        run(self.client, _insert_prior_webhook_delivery)

        self._serve([{"items": [_lead(lead)], "next_cursor": None}])
        result = self._run()

        assert result.created == 0
        assert result.duplicates == 1
