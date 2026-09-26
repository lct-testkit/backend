"""Заготовки для тестов обмена с LMS: сделка с контактом (или организацией) и продуктами, источник
`lms`, подмена сети. Требуют `TEST_DATABASE_URL`, как и остальные сквозные тесты
(`tests/conftest.py`).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

LMS_URL = "https://lms.invalid/api"


async def make_scene(
    *,
    deal_type: str = "b2c",
    lines: list[dict[str, Any]] | None = None,
    contact: dict[str, Any] | None = None,
    order_number: str | None = None,
) -> dict[str, Any]:
    """Сделка `deal_type` с продуктами `lines` (`[{"name": …, "stream_number": …}]`, по умолчанию
    один продукт с потоком 3). Всё пишется прямо в БД: воронка — черновик, Keycloak не нужен."""
    from app.core.db import session_scope
    from app.modules.catalog.models import Contact, Organization, Product
    from app.modules.crm.models import Deal, DealProduct
    from app.modules.identity.models import User
    from app.modules.workflow.models import Workflow, WorkflowStatus

    token = uuid.uuid4().hex
    lines = [{"name": f"Курс {token[:6]}", "stream_number": 3}] if lines is None else lines
    async with session_scope() as session:
        workflow = Workflow(code=f"wf-{token[:10]}", name="Воронка LMS", deal_type=deal_type)
        owner = User(
            keycloak_id=str(uuid.uuid4()),
            email=f"{token[:12]}@rt-it-school.ru",
            full_name="Петров П.П.",
            role="KAM",
            status="active",
            consent_version="1.0",
        )
        session.add_all([workflow, owner])
        await session.flush()
        status = WorkflowStatus(workflow_id=workflow.id, code="new", name="Новая")
        session.add(status)

        contact_row = organization_row = None
        contact_data: dict[str, Any] = {}
        if deal_type == "b2c":
            contact_data = {
                "first_name": "Дарья",
                "last_name": "Осипенко",
                "middle_name": "Игоревна",
                "email": f"{token[:12]}@example.ru",
                "phone": "+79990234365",
                **(contact or {}),
            }
            contact_row = Contact(**contact_data)
            session.add(contact_row)
        else:
            organization_row = Organization(
                name=f"Вуз {token[:6]}",
                org_type="university",
                inn=str(uuid.uuid4().int % 10**10).zfill(10),
            )
            session.add(organization_row)
        await session.flush()

        deal = Deal(
            number=f"D-{token[:12]}",
            title="Зачисление в LMS",
            deal_type=deal_type,
            workflow_id=workflow.id,
            status_id=status.id,
            contact_id=contact_row.id if contact_row else None,
            organization_id=organization_row.id if organization_row else None,
            owner_id=owner.id,
            order_number=order_number,
        )
        session.add(deal)
        await session.flush()

        products = []
        for line in lines:
            product = Product(code=f"c-{uuid.uuid4().hex[:10]}", name=line["name"])
            session.add(product)
            await session.flush()
            session.add(
                DealProduct(
                    deal_id=deal.id,
                    product_id=product.id,
                    quantity=1,
                    stream_number=line.get("stream_number"),
                )
            )
            await session.flush()
            products.append({"id": product.id, "code": product.code, "name": product.name})

        return {
            "deal_id": deal.id,
            "deal_number": deal.number,
            "order_number": order_number,
            "contact_id": contact_row.id if contact_row else None,
            "contact": contact_data,
            "organization_id": organization_row.id if organization_row else None,
            "organization": (
                {"name": organization_row.name, "inn": organization_row.inn}
                if organization_row
                else None
            ),
            "products": products,
        }


async def activate_lms_source(*, base_url: str | None = LMS_URL) -> None:
    """Источник `lms` включён, а чужие ожидающие события закрыты: цикл доставки не должен
    подхватывать их вместе с нашими."""
    from sqlalchemy import select, update

    from app.core.db import session_scope
    from app.modules.integration.models import IntegrationSource, OutboxEvent

    async with session_scope() as session:
        await session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.status.in_(["pending", "failed"]))
            .values(status="sent")
        )
        source = (
            await session.execute(select(IntegrationSource).where(IntegrationSource.code == "lms"))
        ).scalar_one_or_none()
        if source is None:
            source = IntegrationSource(code="lms", name="LMS")
            session.add(source)
        source.is_active = True
        source.base_url = base_url


class Network:
    """Подмена сети: запросы `httpx.AsyncClient` перехватывает `MockTransport`, ответ задаёт
    `respond`; `calls` — всё, что ушло (`method`, `url`, `params`, `body`)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.respond = lambda request: httpx.Response(200, json={})

    def install(self, monkeypatch: pytest.MonkeyPatch) -> Network:
        real_client = httpx.AsyncClient

        def handler(request: httpx.Request) -> httpx.Response:
            self.calls.append(
                {
                    "method": request.method,
                    "url": str(request.url).split("?")[0],
                    "params": dict(request.url.params),
                    "body": json.loads(request.content) if request.content else None,
                }
            )
            return self.respond(request)

        def client_with_mock_network(*args: object, **kwargs: object) -> httpx.AsyncClient:
            return real_client(*args, transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", client_with_mock_network)
        return self
