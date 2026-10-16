"""Фабрика мира для тестов реестра сделок (B1-1, BUG-1): вузы, направления, продукты, КАМы,
руководитель и сделки с разным набором продуктов — всё пишется прямо в БД, тем же приёмом, что
`tests/lms_helpers.py:make_scene` (воронка-черновик, Keycloak не нужен). Требует
`TEST_DATABASE_URL`, как и остальные сквозные тесты (`tests/conftest.py`).

Расширяется задачами B1-7, B1-9 и B1-13 (см. документ спринта B1) — сюда добавляются новые поля
результата `RegisterWorld`, а не копии фабрики.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field


@dataclass(slots=True)
class RegisterWorld:
    organization_ids: dict[str, uuid.UUID] = field(default_factory=dict)
    direction_ids: dict[str, uuid.UUID] = field(default_factory=dict)
    product_ids: dict[str, uuid.UUID] = field(default_factory=dict)
    user_ids: dict[str, uuid.UUID] = field(default_factory=dict)
    deal_ids: dict[str, uuid.UUID] = field(default_factory=dict)
    deal_numbers: dict[str, str] = field(default_factory=dict)
    team_id: uuid.UUID | None = None
    workflow_id: uuid.UUID | None = None
    status_id: uuid.UUID | None = None


async def make_register_world() -> RegisterWorld:
    """Мир из задачи B1-1 документа спринта B1:

    * направления «Программирование» -> «Python» (дочернее) и отдельное «Java»;
    * продукты P (Python, направление «Python»), Q (Java, направление «Java»), R (без
      направления);
    * вузы O1, O2; КАМы KAM1 (вуз O1), KAM2 (вуз O2), руководитель HEAD над обоими;
    * сделки: D1 (O1, KAM1, продукты P и Q, 100000), D2 (O1, KAM1, P, 50000), D3 (O2, KAM2, Q,
      70000), D4 (O2, KAM2, без продуктов, 10000), D5 (с P, мягко удалена).
    """
    from app.core.db import session_scope
    from app.modules.catalog.models import Direction, Organization, Product
    from app.modules.crm.models import Deal, DealProduct
    from app.modules.identity.models import Team, User
    from app.modules.workflow.models import Workflow, WorkflowStatus

    token = uuid.uuid4().hex[:10]
    world = RegisterWorld()

    async with session_scope() as session:
        workflow = Workflow(code=f"wf-register-{token}", name="Воронка реестра", deal_type="b2b")
        session.add(workflow)
        await session.flush()
        world.workflow_id = workflow.id

        statuses = [
            WorkflowStatus(
                workflow_id=workflow.id, code=code, name=name, type=kind, sort_order=order
            )
            for order, (code, name, kind) in enumerate(
                [
                    ("identification", "Выявление", "initial"),
                    ("negotiation", "Переговоры", "intermediate"),
                    ("won", "Выиграна", "won"),
                ]
            )
        ]
        session.add_all(statuses)
        await session.flush()
        world.status_id = statuses[0].id

        head = User(
            keycloak_id=str(uuid.uuid4()),
            email=f"head-{token}@rt-it-school.ru",
            full_name="Руководителев Р. Р.",
            role="HEAD",
            status="active",
            consent_version="1.0",
        )
        session.add(head)
        await session.flush()

        team = Team(name=f"Команда реестра {token}", head_id=head.id)
        session.add(team)
        await session.flush()
        world.team_id = team.id

        kam1 = User(
            keycloak_id=str(uuid.uuid4()),
            email=f"kam1-{token}@rt-it-school.ru",
            full_name="Камов К. К.",
            role="KAM",
            status="active",
            consent_version="1.0",
            team_id=team.id,
        )
        kam2 = User(
            keycloak_id=str(uuid.uuid4()),
            email=f"kam2-{token}@rt-it-school.ru",
            full_name="Камова К. К.",
            role="KAM",
            status="active",
            consent_version="1.0",
            team_id=team.id,
        )
        session.add_all([kam1, kam2])
        await session.flush()
        world.user_ids = {"HEAD": head.id, "KAM1": kam1.id, "KAM2": kam2.id}

        programming = Direction(code=f"programming-{token}", name="Программирование")
        session.add(programming)
        await session.flush()
        python_dir = Direction(code=f"python-{token}", name="Python", parent_id=programming.id)
        java_dir = Direction(code=f"java-{token}", name="Java")
        session.add_all([python_dir, java_dir])
        await session.flush()
        world.direction_ids = {
            "Программирование": programming.id,
            "Python": python_dir.id,
            "Java": java_dir.id,
        }

        product_p = Product(
            code=f"p-{token}", name="Python: базовый курс", direction_id=python_dir.id
        )
        product_q = Product(code=f"q-{token}", name="Java: курс", direction_id=java_dir.id)
        product_r = Product(code=f"r-{token}", name="Без направления")
        session.add_all([product_p, product_q, product_r])
        await session.flush()
        world.product_ids = {"P": product_p.id, "Q": product_q.id, "R": product_r.id}

        org1 = Organization(name=f"ВУЗ 1 {token}", org_type="university", owner_id=kam1.id)
        org2 = Organization(name=f"ВУЗ 2 {token}", org_type="university", owner_id=kam2.id)
        session.add_all([org1, org2])
        await session.flush()
        world.organization_ids = {"O1": org1.id, "O2": org2.id}

        d1 = Deal(
            number=f"D-register-{token}-1",
            title="Сделка 1",
            deal_type="b2b",
            workflow_id=workflow.id,
            status_id=statuses[0].id,
            organization_id=org1.id,
            owner_id=kam1.id,
            amount=100000,
        )
        d2 = Deal(
            number=f"D-register-{token}-2",
            title="Сделка 2",
            deal_type="b2b",
            workflow_id=workflow.id,
            status_id=statuses[0].id,
            organization_id=org1.id,
            owner_id=kam1.id,
            amount=50000,
        )
        d3 = Deal(
            number=f"D-register-{token}-3",
            title="Сделка 3",
            deal_type="b2b",
            workflow_id=workflow.id,
            status_id=statuses[0].id,
            organization_id=org2.id,
            owner_id=kam2.id,
            amount=70000,
        )
        d4 = Deal(
            number=f"D-register-{token}-4",
            title="Сделка 4",
            deal_type="b2b",
            workflow_id=workflow.id,
            status_id=statuses[0].id,
            organization_id=org2.id,
            owner_id=kam2.id,
            amount=10000,
        )
        d5 = Deal(
            number=f"D-register-{token}-5",
            title="Сделка 5 (удалена)",
            deal_type="b2b",
            workflow_id=workflow.id,
            status_id=statuses[0].id,
            organization_id=org1.id,
            owner_id=kam1.id,
            amount=99999,
        )
        session.add_all([d1, d2, d3, d4, d5])
        await session.flush()
        world.deal_ids = {"D1": d1.id, "D2": d2.id, "D3": d3.id, "D4": d4.id, "D5": d5.id}
        world.deal_numbers = {
            "D1": d1.number,
            "D2": d2.number,
            "D3": d3.number,
            "D4": d4.number,
            "D5": d5.number,
        }

        session.add_all(
            [
                DealProduct(deal_id=d1.id, product_id=product_p.id, quantity=1),
                DealProduct(deal_id=d1.id, product_id=product_q.id, quantity=1),
                DealProduct(deal_id=d2.id, product_id=product_p.id, quantity=1),
                DealProduct(deal_id=d3.id, product_id=product_q.id, quantity=1),
                DealProduct(deal_id=d5.id, product_id=product_p.id, quantity=1),
            ]
        )
        d5.deleted_at = dt.datetime.now(dt.UTC)
        await session.flush()

    return world
