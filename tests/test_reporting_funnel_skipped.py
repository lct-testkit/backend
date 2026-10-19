"""R-11 (QA 20.10): необязательный шаг, который пропустили все сделки, в «Воронке по статусам»
не показывает противоречивых цифр («Всего прошло 0» рядом с конверсией 81,8 %), а следующий шаг
считается от предыдущего пройденного."""

from __future__ import annotations

import uuid

import pytest

from app.modules.reporting.builders import build_deal_funnel
from tests.conftest import TEST_DATABASE_URL, run
from tests.test_reporting_register import _principal

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


async def _make_world() -> tuple[uuid.UUID, uuid.UUID]:
    from app.core.db import session_scope
    from app.modules.catalog.models import Organization
    from app.modules.crm.models import Deal, DealStatusHistory
    from app.modules.identity.models import User
    from app.modules.workflow.models import Workflow, WorkflowStatus

    token = uuid.uuid4().hex[:10]
    async with session_scope() as session:
        workflow = Workflow(code=f"wf-skip-{token}", name="Воронка пропуска", deal_type="b2b")
        session.add(workflow)
        await session.flush()
        statuses = [
            WorkflowStatus(
                workflow_id=workflow.id, code=code, name=name, type=kind, sort_order=order
            )
            for order, (code, name, kind) in enumerate(
                [
                    ("s0", "Выявление", "initial"),
                    ("s1", "Переговоры", "intermediate"),
                    ("s2", "Корректировка документов", "intermediate"),
                    ("s3", "Договор", "intermediate"),
                    ("s4", "Выиграна", "won"),
                ]
            )
        ]
        session.add_all(statuses)
        await session.flush()
        s0, s1, _s2, s3, s4 = (s.id for s in statuses)
        admin = User(
            keycloak_id=str(uuid.uuid4()),
            email=f"admin-skip-{token}@rt-it-school.ru",
            full_name="Админов А. А.",
            role="ADMIN",
            status="active",
            consent_version="1.0",
        )
        session.add(admin)
        await session.flush()
        org = Organization(name=f"ВУЗ пропуска {token}", org_type="university", owner_id=admin.id)
        session.add(org)
        await session.flush()
        # Никто не заходил в «Корректировку документов»: s0 -> s1 -> s3 (-> s4).
        for index, path in enumerate([[s0, s1, s3, s4], [s0, s1, s3, s4], [s0, s1, s3], [s0, s1]]):
            deal = Deal(
                number=f"D-skip-{token}-{index}",
                title=f"Сделка {index}",
                deal_type="b2b",
                workflow_id=workflow.id,
                status_id=path[-1],
                organization_id=org.id,
                owner_id=admin.id,
                amount=1000,
            )
            session.add(deal)
            await session.flush()
            previous = None
            for to_status in path:
                session.add(
                    DealStatusHistory(
                        deal_id=deal.id, from_status_id=previous, to_status_id=to_status
                    )
                )
                previous = to_status
        await session.flush()
        return workflow.id, admin.id


async def _funnel(workflow_id: uuid.UUID, admin_id: uuid.UUID):
    from app.core.db import session_scope

    async with session_scope() as session:
        return await build_deal_funnel(
            session,
            _principal(admin_id, "ADMIN"),
            {"workflow_id": str(workflow_id), "date_from": "2000-01-01"},
        )


def test_a_step_nobody_went_through_has_no_conversion_and_is_explained(client) -> None:
    workflow_id, admin_id = run(client, _make_world)
    dataset = run(client, _funnel, workflow_id, admin_id)
    by_name = {row[0]: row for row in dataset.rows}

    skipped = by_name["Корректировка документов"]
    assert skipped[2] == 0  # Всего прошло
    assert skipped[3] is None  # Конверсия шага — прочерк, а не 75 %
    assert skipped[5] is None  # Дошли до шага
    assert skipped[6] is None  # От старта, %
    assert dataset.note is not None
    assert "«Корректировка документов» пропускали" in dataset.note

    # «Договор» считается от «Переговоров» (4 -> 3), а не от пропущенного шага.
    assert by_name["Переговоры"][5] == 4
    assert by_name["Договор"][5] == 3
    assert by_name["Договор"][3] == 75.0
    assert by_name["Выиграна"][3] == pytest.approx(66.7)


def test_steps_that_were_visited_are_untouched(client) -> None:
    workflow_id, admin_id = run(client, _make_world)
    dataset = run(client, _funnel, workflow_id, admin_id)
    by_name = {row[0]: row for row in dataset.rows}
    assert by_name["Выявление"][3] == 100.0
    assert by_name["Переговоры"][3] == 100.0
    assert by_name["Договор"][2] == 3
