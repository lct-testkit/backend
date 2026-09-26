"""Что уходит в LMS при зачислении (`lms.build_enrollment_details`, `tasks._deliver`).

Действие DSL `integration_event` публикует событие с пустой нагрузкой, и зачисление уходило в LMS
как `{"deal_id", "event_type"}` — без учащегося. Теперь при доставке `LEARNING_ENROLLMENT_SENT` и
`LEARNING_TRANSFER_REQUESTED` нагрузка достраивается по сделке: номер сделки и заказа, поток,
продукт, учащийся (B2C, полностью — это намеренная исходящая передача ПДн) или организация (B2B).

Сквозные тесты на настоящей PostgreSQL (`TEST_DATABASE_URL`); сеть подменяет `httpx.MockTransport`.
"""

from __future__ import annotations

import functools
import uuid
from typing import Any

import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.lms_helpers import LMS_URL, Network, activate_lms_source, make_scene

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


async def _details(deal_id: uuid.UUID) -> dict[str, Any]:
    from app.core.db import session_scope
    from app.modules.integration.lms import build_enrollment_details

    async with session_scope() as session:
        return await build_enrollment_details(session, deal_id)


async def _publish(
    deal_id: uuid.UUID, event_type: str, payload: dict[str, Any] | None = None, **kwargs: Any
) -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.integration.models import OutboxEvent

    async with session_scope() as session:
        event = OutboxEvent(
            aggregate_type=kwargs.get("aggregate_type", "deal"),
            aggregate_id=deal_id,
            event_type=event_type,
            payload=payload or {},
            target="lms",
        )
        session.add(event)
        await session.flush()
        return event.id


async def _event_state(event_id: uuid.UUID) -> dict[str, Any]:
    from app.core.db import session_scope
    from app.modules.integration.models import OutboxEvent

    async with session_scope() as session:
        event = await session.get(OutboxEvent, event_id)
        assert event is not None
        return {"status": event.status, "attempts": event.attempts, "last_error": event.last_error}


async def _sweep() -> dict[str, int]:
    from app.modules.integration.tasks import sweep_outbox_events

    return await sweep_outbox_events({})


async def _mutate_contact(contact_id: uuid.UUID, **values: Any) -> None:
    from app.core.db import session_scope
    from app.modules.catalog.models import Contact

    async with session_scope() as session:
        contact = await session.get(Contact, contact_id)
        assert contact is not None
        for key, value in values.items():
            setattr(contact, key, value)


async def _mutate_deal(deal_id: uuid.UUID, **values: Any) -> None:
    from app.core.db import session_scope
    from app.modules.crm.models import Deal

    async with session_scope() as session:
        deal = await session.get(Deal, deal_id)
        assert deal is not None
        for key, value in values.items():
            setattr(deal, key, value)


class TestBuildEnrollmentDetails:
    def test_b2c_deal_carries_the_learner_the_product_and_the_stream(self, client) -> None:
        scene = run(
            client,
            functools.partial(
                make_scene,
                order_number=f"ORD-{uuid.uuid4().hex[:12]}",
                lines=[{"name": "Промпт-инжиниринг", "stream_number": 4}],
            ),
        )

        details = run(client, _details, scene["deal_id"])

        contact = scene["contact"]
        assert details == {
            "deal_number": scene["deal_number"],
            "order_number": scene["order_number"],
            "stream_number": 4,
            "product": {"code": scene["products"][0]["code"], "name": "Промпт-инжиниринг"},
            "learner": {
                "last_name": "Осипенко",
                "first_name": "Дарья",
                "middle_name": "Игоревна",
                "email": contact["email"],
                "phone": "+79990234365",
            },
        }

    def test_the_learner_is_not_masked(self, client) -> None:
        scene = run(client, make_scene)

        learner = run(client, _details, scene["deal_id"])["learner"]

        assert learner["email"] == scene["contact"]["email"]
        assert learner["phone"] == "+79990234365"
        assert "*" not in "".join(map(str, learner.values()))

    def test_b2b_deal_carries_the_organization_instead_of_a_learner(self, client) -> None:
        scene = run(client, functools.partial(make_scene, deal_type="b2b"))

        details = run(client, _details, scene["deal_id"])

        assert "learner" not in details
        assert details["organization"] == scene["organization"]
        assert details["organization"]["inn"] and details["organization"]["name"]
        assert details["deal_number"] == scene["deal_number"]
        assert details["product"]["name"] == scene["products"][0]["name"]
        assert details["stream_number"] == 3

    def test_missing_pieces_are_left_out(self, client) -> None:
        scene = run(
            client,
            functools.partial(
                make_scene, lines=[], contact={"middle_name": None, "email": None, "phone": None}
            ),
        )

        details = run(client, _details, scene["deal_id"])

        assert details == {
            "deal_number": scene["deal_number"],
            "learner": {"last_name": "Осипенко", "first_name": "Дарья"},
        }

    def test_line_without_a_stream_has_no_stream_number(self, client) -> None:
        scene = run(
            client, functools.partial(make_scene, lines=[{"name": "Курс", "stream_number": None}])
        )

        details = run(client, _details, scene["deal_id"])

        assert "stream_number" not in details
        assert details["product"]["name"] == "Курс"

    def test_the_first_product_line_is_used(self, client) -> None:
        scene = run(
            client,
            functools.partial(
                make_scene,
                lines=[
                    {"name": "Первый курс", "stream_number": 2},
                    {"name": "Второй курс", "stream_number": 7},
                ],
            ),
        )

        details = run(client, _details, scene["deal_id"])

        assert details["product"]["name"] == "Первый курс"
        assert details["stream_number"] == 2

    def test_anonymized_contact_is_not_sent(self, client) -> None:
        scene = run(client, make_scene)
        run(
            client,
            functools.partial(
                _mutate_contact,
                scene["contact_id"],
                is_anonymized=True,
                first_name="Контакт #abc12345",
                last_name="",
                email=None,
                phone=None,
            ),
        )

        assert "learner" not in run(client, _details, scene["deal_id"])

    def test_the_data_is_current_not_a_snapshot(self, client) -> None:
        scene = run(client, make_scene)
        assert run(client, _details, scene["deal_id"])["learner"]["last_name"] == "Осипенко"

        run(client, functools.partial(_mutate_contact, scene["contact_id"], last_name="Иванова"))

        assert run(client, _details, scene["deal_id"])["learner"]["last_name"] == "Иванова"

    def test_unknown_or_deleted_deal_gives_nothing(self, client) -> None:
        import datetime as dt

        scene = run(client, make_scene)
        run(
            client,
            functools.partial(_mutate_deal, scene["deal_id"], deleted_at=dt.datetime.now(dt.UTC)),
        )

        assert run(client, _details, scene["deal_id"]) == {}
        assert run(client, _details, uuid.uuid4()) == {}


class TestDelivery:
    @pytest.fixture(autouse=True)
    def _network(self, client, monkeypatch: pytest.MonkeyPatch) -> None:
        self.client = client
        self.network = Network().install(monkeypatch)
        run(client, activate_lms_source)

    def _deliver(self, deal_id: uuid.UUID, event_type: str, payload=None, **kwargs: Any):
        event_id = run(
            self.client, functools.partial(_publish, deal_id, event_type, payload, **kwargs)
        )
        run(self.client, _sweep)
        return event_id

    def _enrollments(self) -> list[dict[str, Any]]:
        return [c for c in self.network.calls if c["url"] == f"{LMS_URL}/enrollments"]

    @pytest.mark.parametrize(
        "event_type", ["LEARNING_ENROLLMENT_SENT", "LEARNING_TRANSFER_REQUESTED"]
    )
    def test_enrollment_body_carries_the_learner(self, event_type: str) -> None:
        scene = run(
            self.client,
            functools.partial(make_scene, order_number=f"ORD-{uuid.uuid4().hex[:12]}"),
        )

        event_id = self._deliver(scene["deal_id"], event_type)

        (call,) = self._enrollments()
        assert call["method"] == "POST"
        body = call["body"]
        assert body["deal_id"] == str(scene["deal_id"])
        assert body["event_type"] == event_type
        assert body["deal_number"] == scene["deal_number"]
        assert body["order_number"] == scene["order_number"]
        assert body["stream_number"] == 3
        assert body["product"] == {
            "code": scene["products"][0]["code"],
            "name": scene["products"][0]["name"],
        }
        assert body["learner"] == {
            "last_name": "Осипенко",
            "first_name": "Дарья",
            "middle_name": "Игоревна",
            "email": scene["contact"]["email"],
            "phone": "+79990234365",
        }
        assert run(self.client, _event_state, event_id)["status"] == "sent"

    def test_b2b_enrollment_carries_the_organization(self) -> None:
        scene = run(self.client, functools.partial(make_scene, deal_type="b2b"))

        self._deliver(scene["deal_id"], "LEARNING_TRANSFER_REQUESTED")

        (call,) = self._enrollments()
        assert call["body"]["organization"] == scene["organization"]
        assert "learner" not in call["body"]

    def test_explicit_event_payload_wins_over_collected_data(self) -> None:
        scene = run(self.client, make_scene)

        self._deliver(
            scene["deal_id"],
            "LEARNING_ENROLLMENT_SENT",
            {"stream_number": 9, "learner": {"email": "override@example.ru"}, "extra": "x"},
        )

        (call,) = self._enrollments()
        body = call["body"]
        assert body["stream_number"] == 9
        assert body["learner"] == {"email": "override@example.ru"}
        assert body["extra"] == "x"
        assert body["deal_number"] == scene["deal_number"]  # остальное собрано как обычно

    def test_other_events_are_sent_as_before(self) -> None:
        scene = run(self.client, make_scene)

        self._deliver(scene["deal_id"], "DEAL_STATUS_CHANGED", {"status_code": "won"})

        (call,) = self._enrollments()
        assert call["body"] == {
            "deal_id": str(scene["deal_id"]),
            "event_type": "DEAL_STATUS_CHANGED",
            "status_code": "won",
        }

    def test_events_of_other_aggregates_are_not_enriched(self) -> None:
        scene = run(self.client, make_scene)

        self._deliver(scene["deal_id"], "LEARNING_ENROLLMENT_SENT", aggregate_type="signature")

        (call,) = self._enrollments()
        assert call["body"] == {
            "deal_id": str(scene["deal_id"]),
            "event_type": "LEARNING_ENROLLMENT_SENT",
        }

    def test_missing_deal_does_not_break_delivery(self) -> None:
        deal_id = uuid.uuid4()

        event_id = self._deliver(deal_id, "LEARNING_ENROLLMENT_SENT")

        (call,) = self._enrollments()
        assert call["body"] == {"deal_id": str(deal_id), "event_type": "LEARNING_ENROLLMENT_SENT"}
        assert run(self.client, _event_state, event_id)["status"] == "sent"

    def test_deal_without_product_and_with_a_bare_contact_is_still_delivered(self) -> None:
        scene = run(
            self.client,
            functools.partial(
                make_scene, lines=[], contact={"middle_name": None, "email": None, "phone": None}
            ),
        )

        event_id = self._deliver(scene["deal_id"], "LEARNING_ENROLLMENT_SENT")

        (call,) = self._enrollments()
        assert call["body"] == {
            "deal_id": str(scene["deal_id"]),
            "event_type": "LEARNING_ENROLLMENT_SENT",
            "deal_number": scene["deal_number"],
            "learner": {"last_name": "Осипенко", "first_name": "Дарья"},
        }
        assert run(self.client, _event_state, event_id)["status"] == "sent"

    def test_the_payload_is_built_at_delivery_time(self) -> None:
        scene = run(self.client, make_scene)
        event_id = run(
            self.client, functools.partial(_publish, scene["deal_id"], "LEARNING_ENROLLMENT_SENT")
        )
        # Между переходом сделки (публикацией) и доставкой контакт поправили.
        run(
            self.client,
            functools.partial(_mutate_contact, scene["contact_id"], phone="+79001112233"),
        )

        run(self.client, _sweep)

        (call,) = self._enrollments()
        assert call["body"]["learner"]["phone"] == "+79001112233"
        assert run(self.client, _event_state, event_id)["status"] == "sent"

    def test_failed_delivery_is_retried_with_the_same_enriched_body(self) -> None:
        import httpx

        scene = run(self.client, make_scene)
        self.network.respond = lambda request: httpx.Response(503, json={"error": "down"})

        event_id = self._deliver(scene["deal_id"], "LEARNING_ENROLLMENT_SENT")

        state = run(self.client, _event_state, event_id)
        assert (state["status"], state["attempts"]) == ("failed", 1)
        (call,) = self._enrollments()
        assert call["body"]["learner"]["last_name"] == "Осипенко"
        # В журнал ошибок попадает текст без ПДн из тела запроса.
        assert "Осипенко" not in (state["last_error"] or "")
