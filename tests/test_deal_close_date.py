"""D-3 (QA 20.10): «Плановая дата закрытия» не принимает прошлое — при создании сделки и при
правке самой даты. Исключения: заявки интеграций (источник — внешняя система), сделка, у которой
дата уже в прошлом и PATCH её не меняет, очистка даты.

Чистая проверка не требует БД; сквозные — с `TEST_DATABASE_URL`, см. `tests/conftest.py`."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.core.errors import ValidationError
from app.modules.crm.service import ensure_close_date_not_past
from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import create_deal, create_published_workflow, login

PAST_TEXT = "Плановая дата закрытия уже прошла"
_TODAY = dt.date.today()
_YESTERDAY = (_TODAY - dt.timedelta(days=1)).isoformat()


class TestEnsureCloseDateNotPast:
    def test_past_date_is_rejected_with_a_readable_text(self) -> None:
        with pytest.raises(ValidationError) as caught:
            ensure_close_date_not_past(dt.date(2020, 3, 2), today=dt.date(2026, 10, 20))
        assert caught.value.detail == PAST_TEXT
        assert [e.field for e in caught.value.errors] == ["expected_close_date"]
        assert caught.value.errors[0].reason == PAST_TEXT

    @pytest.mark.parametrize(
        "value",
        [None, dt.date(2026, 10, 20), dt.date(2026, 10, 21), dt.date(2030, 1, 1)],
    )
    def test_today_future_and_empty_are_fine(self, value: dt.date | None) -> None:
        ensure_close_date_not_past(value, today=dt.date(2026, 10, 20))

    def test_the_day_before_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ensure_close_date_not_past(dt.date(2026, 10, 19), today=dt.date(2026, 10, 20))


needs_db = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


async def _force_close_date(deal_id: str, value: dt.date | None) -> None:
    from app.core.db import session_scope
    from app.modules.crm.models import Deal

    async with session_scope() as session:
        deal = await session.get(Deal, uuid.UUID(deal_id))
        assert deal is not None
        deal.expected_close_date = value
        await session.flush()


def _payload(workflow_id: str, org_id: str, **extra):
    return {
        "title": "Сделка для даты",
        "deal_type": "b2b",
        "workflow_id": workflow_id,
        "organization_id": org_id,
        **extra,
    }


@needs_db
class TestCreateDeal:
    def _workflow(self, client) -> str:
        login(client, "ADMIN")
        return create_published_workflow(client)["workflow"]["id"]

    def test_past_date_is_422(self, client) -> None:
        from tests.crm_helpers import create_organization

        workflow_id = self._workflow(client)
        response = client.post(
            "/api/deals",
            json=_payload(workflow_id, create_organization(client), expected_close_date=_YESTERDAY),
        )
        assert response.status_code == 422, response.text
        body = response.json()
        assert body["detail"] == PAST_TEXT
        assert body["errors"][0]["field"] == "expected_close_date"

    def test_today_and_future_are_created(self, client) -> None:
        workflow_id = self._workflow(client)
        today = create_deal(client, workflow_id, expected_close_date=_TODAY.isoformat())
        later = create_deal(
            client,
            workflow_id,
            expected_close_date=(_TODAY + dt.timedelta(days=40)).isoformat(),
        )
        assert today["expected_close_date"] == _TODAY.isoformat()
        assert later["expected_close_date"] == (_TODAY + dt.timedelta(days=40)).isoformat()

    def test_integration_source_may_carry_a_past_date(self, client) -> None:
        from tests.crm_helpers import create_organization

        workflow_id = self._workflow(client)
        org_id = create_organization(client)
        login(client, "INTEGRATION")
        response = client.post(
            "/api/deals",
            json=_payload(
                workflow_id,
                org_id,
                expected_close_date=_YESTERDAY,
                source="cms",
                external_ids={"cms_lead_id": uuid.uuid4().hex},
            ),
        )
        assert response.status_code == 201, response.text
        assert response.json()["expected_close_date"] == _YESTERDAY


@needs_db
class TestUpdateDeal:
    def _deal(self, client) -> dict:
        login(client, "ADMIN")
        workflow_id = create_published_workflow(client)["workflow"]["id"]
        return create_deal(client, workflow_id)

    def _patch(self, client, deal: dict, **fields):
        current = client.get(f"/api/deals/{deal['id']}").json()["deal"]
        return client.patch(
            f"/api/deals/{deal['id']}",
            json=fields,
            headers={"If-Match": str(current["version"])},
        )

    def test_moving_the_date_into_the_past_is_422(self, client) -> None:
        deal = self._deal(client)
        response = self._patch(client, deal, expected_close_date=_YESTERDAY)
        assert response.status_code == 422, response.text
        assert response.json()["detail"] == PAST_TEXT
        # Сделка не изменилась.
        assert client.get(f"/api/deals/{deal['id']}").json()["deal"]["expected_close_date"] is None

    def test_a_future_date_is_saved(self, client) -> None:
        deal = self._deal(client)
        target = (_TODAY + dt.timedelta(days=15)).isoformat()
        response = self._patch(client, deal, expected_close_date=target)
        assert response.status_code == 200, response.text
        assert response.json()["expected_close_date"] == target

    def test_an_already_past_date_that_the_patch_does_not_change_is_kept(self, client) -> None:
        deal = self._deal(client)
        past = _TODAY - dt.timedelta(days=30)
        run(client, _force_close_date, deal["id"], past)

        # Форма шлёт все поля, в том числе неизменившуюся прошедшую дату, — не падаем.
        response = self._patch(
            client, deal, title="Новое название", expected_close_date=past.isoformat()
        )
        assert response.status_code == 200, response.text
        card = response.json()
        assert card["title"] == "Новое название"
        assert card["expected_close_date"] == past.isoformat()

    def test_changing_a_past_date_to_another_past_date_is_422(self, client) -> None:
        deal = self._deal(client)
        run(client, _force_close_date, deal["id"], _TODAY - dt.timedelta(days=30))
        response = self._patch(
            client, deal, expected_close_date=(_TODAY - dt.timedelta(days=5)).isoformat()
        )
        assert response.status_code == 422, response.text

    def test_clearing_a_past_date_is_allowed(self, client) -> None:
        deal = self._deal(client)
        run(client, _force_close_date, deal["id"], _TODAY - dt.timedelta(days=30))
        response = self._patch(client, deal, expected_close_date=None)
        assert response.status_code == 200, response.text
        assert response.json()["expected_close_date"] is None
