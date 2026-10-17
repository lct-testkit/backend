"""B5-5: ручной запуск «Забрать сейчас» (`POST /api/admin/integrations/sources/{code}/pull`)
и статус синхронизации (`GET .../status`). Настоящая Postgres (`TEST_DATABASE_URL`, см.
докстринг `tests/conftest.py`); сеть подменяется `Network` (`tests/lms_helpers.py`), как и
`test_cms_pull.py`.

На демо администратор нажимает кнопку и в течение минуты видит новые заявки, не дожидаясь
расписания. Ручка сама ничего не забирает — пишет запрос в `pull_request`, его первым делом
читает `sweep_cms_leads_pull`/`sweep_lms_progress_pull` на следующем тике.
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
from tests.crm_helpers import create_user, login, sign_in
from tests.lms_helpers import Network

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

_CMS_URL = "https://cms-pull-manual.invalid"
_PULL_URL = "/api/admin/integrations/sources/cms/pull"
_STATUS_URL = "/api/admin/integrations/sources/cms/status"


async def _prepare(*, active: bool = True, pull_enabled: bool = True) -> None:
    """Тот же приём, что `tests/test_cms_pull.py::_prepare`."""
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
        source.pull_request = None

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
            spec = dataclasses.replace(_b2c_spec(), code=f"b2c_pull_manual_{uuid.uuid4().hex[:8]}")
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


async def _pull_request_row() -> dict[str, Any] | None:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.integration.models import IntegrationSource

    async with session_scope() as session:
        return await session.scalar(
            select(IntegrationSource.pull_request).where(IntegrationSource.code == "cms")
        )


async def _inbound_channel(key: str) -> str | None:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.integration.models import InboundMessage

    async with session_scope() as session:
        return await session.scalar(
            select(InboundMessage.channel).where(
                InboundMessage.source_code == "cms", InboundMessage.external_id == key
            )
        )


async def _cursor() -> str | None:
    from app.core.db import session_scope
    from app.modules.integration.lms import get_cursor

    async with session_scope() as session:
        return await get_cursor(session, source_code="cms", resource="leads_updated_since")


def _lead(lead_id: str, **overrides: Any) -> dict[str, Any]:
    """Тот же приём детерминированных телефона/email, что `test_cms_pull.py::_lead`."""
    digits = int.from_bytes(hashlib.sha256(lead_id.encode()).digest()[:4], "big") % 10_000_000
    row = {
        "lead_id": lead_id,
        "name": "Светлана Орлова",
        "phone": f"+7926{digits:07d}",
        "email": f"{lead_id.replace('-', '_')}@example.com",
        "product_interest": "python-basics",
        "submitted_at": "2026-09-16T08:12:00Z",
        "updated_at": "2026-09-16T08:12:00Z",
        **overrides,
    }
    return {key: value for key, value in row.items() if value is not None}


class TestPullManual:
    @pytest.fixture(autouse=True)
    def _setup(self, client, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core.config import get_settings

        self.client = client
        self.network = Network().install(monkeypatch)
        self.run_id = uuid.uuid4().hex[:8]
        monkeypatch.setattr(get_settings(), "cms_base_url", _CMS_URL)
        monkeypatch.setattr(get_settings(), "cms_auth_ref", None)
        run(client, functools.partial(_prepare))
        run(client, _reset_cursor)

    def _id(self, suffix: str) -> str:
        return f"lead-{self.run_id}-{suffix}"

    def _serve(self, pages: list[dict[str, Any]]) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            index = min(calls["n"], len(pages) - 1)
            calls["n"] += 1
            return httpx.Response(200, json=pages[index])

        self.network.respond = handler

    def _sweep(self) -> Any:
        from app.modules.integration.tasks import sweep_cms_leads_pull

        result = run(self.client, functools.partial(sweep_cms_leads_pull, {}))
        run(self.client, _release_pull_lock)
        return result

    def test_request_writes_pull_request_and_returns_202(self) -> None:
        admin = login(self.client, "ADMIN")
        response = self.client.post(_PULL_URL, json={"updated_since": None})
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["status"] == "queued"
        assert "requested_at" in body

        stored = run(self.client, _pull_request_row)
        assert stored is not None
        assert stored["requested_by"] == str(admin.id)
        assert stored["updated_since"] is None

    def test_second_request_while_queued_is_409_busy(self) -> None:
        login(self.client, "ADMIN")
        first = self.client.post(_PULL_URL, json={})
        assert first.status_code == 202, first.text

        second = self.client.post(_PULL_URL, json={})
        assert second.status_code == 409, second.text
        assert second.json()["code"] == "CRM-1705"

    def test_inactive_source_is_409(self) -> None:
        run(self.client, functools.partial(_prepare, active=False))
        login(self.client, "ADMIN")
        response = self.client.post(_PULL_URL, json={})
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "CRM-1703"

    def test_kam_cannot_request_a_pull(self) -> None:
        kam = create_user(self.client, "KAM")
        sign_in(self.client, kam)
        response = self.client.post(_PULL_URL, json={})
        assert response.status_code == 403, response.text

    def test_sweep_runs_immediately_and_clears_the_request_even_when_disabled(self) -> None:
        # Расписание выключено и последний забор только что был — без ручного запроса тик
        # пропустил бы прогон вовсе.
        run(self.client, functools.partial(_prepare, pull_enabled=False))
        login(self.client, "ADMIN")

        lead_id = self._id("manual-1")
        self._serve([{"items": [_lead(lead_id)], "next_cursor": None}])

        request = self.client.post(_PULL_URL, json={})
        assert request.status_code == 202, request.text
        assert run(self.client, _pull_request_row) is not None

        result = self._sweep()
        assert result["status"] == "ok"
        assert result["created"] == 1
        assert run(self.client, _pull_request_row) is None
        # Кнопка «Забрать сейчас» — заявка видна как пришедшая по ручному запуску, не по
        # обычному расписанию (`channel`, не только `last_run.trigger`).
        assert run(self.client, _inbound_channel, lead_id) == "manual"

    def test_updated_since_is_sent_to_the_site_and_cursor_does_not_move_back(self) -> None:
        login(self.client, "ADMIN")
        # Курсор уже продвинут обычным прогоном дальше точки ручного запроса.
        self._serve(
            [{"items": [_lead(self._id("auto"), updated_at="2026-09-20T00:00:00Z")]}]
        )
        first = self._sweep()
        assert first["created"] == 1
        assert run(self.client, _cursor) == "2026-09-20T00:00:00Z"

        manual_lead = self._id("manual-earlier")
        self._serve([{"items": [_lead(manual_lead, updated_at="2026-09-10T00:00:00Z")]}])
        posted = self.client.post(_PULL_URL, json={"updated_since": "2026-09-01T00:00:00Z"})
        assert posted.status_code == 202, posted.text

        result = self._sweep()
        assert result["created"] == 1
        sent_params = [call["params"] for call in self.network.calls if call["method"] == "GET"]
        assert any(p.get("updated_since") == "2026-09-01T00:00:00Z" for p in sent_params)
        # Курсор не откатился назад несмотря на более раннюю `updated_since` ручного запроса.
        assert run(self.client, _cursor) == "2026-09-20T00:00:00Z"

    def test_status_reports_settings_cursor_and_last_run(self) -> None:
        login(self.client, "ADMIN")
        lead_id = self._id("status-1")
        self._serve([{"items": [_lead(lead_id)], "next_cursor": None}])
        self._sweep()

        response = self.client.get(_STATUS_URL)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["code"] == "cms"
        assert body["is_active"] is True
        assert body["last_run"]["created"] == 1
        assert body["last_sync_at"] is not None
        assert body["inbound_last_24h"].get("processed", 0) >= 1
        assert "leads_updated_since" in body["cursors"]
