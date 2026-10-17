"""B5-3: `GET/PUT/DELETE /api/admin/integrations/sources/cms/mapping` и
`.../mapping/preview` — настоящая Postgres (`TEST_DATABASE_URL`).
"""

from __future__ import annotations

import functools
import json
import uuid
from typing import Any

import pytest

from app.modules.integration.security import compute_signature
from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

_URL = "/api/v1/integrations/cms/leads"
_SECRET_ENV = "CMS_MAPPING_TEST_SECRET"
_SECRET = "cms-mapping-test-secret"
_MAPPING_URL = "/api/admin/integrations/sources/cms/mapping"


async def _prepare_source(*, credentials_ref: str | None = None) -> None:
    import dataclasses

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
        source.is_active = True
        source.credentials_ref = credentials_ref
        source.field_mapping = None

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
            spec = dataclasses.replace(_b2c_spec(), code=f"b2c_map_{uuid.uuid4().hex[:8]}")
            await seed_workflow(session, spec)


async def _ensure_product(code: str, name: str) -> None:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.catalog.models import Product

    async with session_scope() as session:
        existing = (
            await session.execute(select(Product).where(Product.code == code))
        ).scalar_one_or_none()
        if existing is None:
            session.add(Product(code=code, name=name))


async def _deal_comment_bodies(deal_id: uuid.UUID) -> list[str]:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.crm.models import DealComment

    async with session_scope() as session:
        rows = (
            await session.execute(select(DealComment.body).where(DealComment.deal_id == deal_id))
        ).scalars()
        return list(rows)


def _login_admin(client) -> None:
    admin = run(client, functools.partial(_make_user, "ADMIN"))
    client.headers["X-CSRF-Token"] = authenticate(client, admin)


def _send_webhook(client, body: dict[str, Any], *, key: str) -> Any:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    signature = f"sha256={compute_signature(_SECRET, payload)}"
    return client.post(
        _URL,
        content=payload,
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": key,
            "X-Signature": signature,
        },
    )


_MAPPING_BODY = {
    "mode": "strict",
    "rules": [
        {"target": "external_id", "source": "lead_id", "required": True},
        {"target": "phone", "source": "phone"},
        {"target": "email", "source": "email", "transform": "lower"},
        {
            "target": "course",
            "source": "product_interest",
            "transform": "value_map",
            "value_map": {"python-basics": "py-base"},
        },
        {
            "target": "comment",
            "source": "source_campaign",
            "transform": "template",
            "template": "Кампания: {value}",
        },
    ],
}


class TestMappingPermissions:
    def test_kam_cannot_read_or_write_mapping(self, client) -> None:
        kam = run(client, functools.partial(_make_user, "KAM"))
        client.headers["X-CSRF-Token"] = authenticate(client, kam)

        assert client.get(_MAPPING_URL).status_code == 403
        assert client.put(_MAPPING_URL, json=_MAPPING_BODY).status_code == 403


class TestMappingSaveAndApply:
    def test_saved_mapping_finds_the_product_and_adds_a_note(self, client, monkeypatch) -> None:
        monkeypatch.setenv(_SECRET_ENV, _SECRET)
        run(client, functools.partial(_prepare_source, credentials_ref=_SECRET_ENV))
        run(client, functools.partial(_ensure_product, "py-base", "Python: базовый курс"))
        _login_admin(client)

        saved = client.put(_MAPPING_URL, json=_MAPPING_BODY)
        assert saved.status_code == 200, saved.text
        assert saved.json()["version"] == 1

        got = client.get(_MAPPING_URL)
        assert got.status_code == 200, got.text
        assert got.json()["mapping"]["mode"] == "strict"
        assert any(t["code"] == "course" for t in got.json()["targets"])

        lead_id = f"lead-{uuid.uuid4().hex[:10]}"
        body = {
            "lead_id": lead_id,
            "name": "Мария Кузнецова",
            "phone": "+79261234567",
            "email": f"{lead_id}@example.com",
            "product_interest": "python-basics",
            "source_campaign": "yandex-direct-sept",
        }
        response = _send_webhook(client, body, key=lead_id)
        assert response.status_code == 200, response.text
        deal_id = uuid.UUID(response.json()["deal_id"])

        comments = run(client, functools.partial(_deal_comment_bodies, deal_id))
        assert any("Кампания: yandex-direct-sept" in body for body in comments)

    def test_delete_restores_the_previous_behaviour(self, client, monkeypatch) -> None:
        monkeypatch.setenv(_SECRET_ENV, _SECRET)
        run(client, functools.partial(_prepare_source, credentials_ref=_SECRET_ENV))
        _login_admin(client)

        client.put(_MAPPING_URL, json=_MAPPING_BODY)
        reset = client.delete(_MAPPING_URL)
        assert reset.status_code == 204, reset.text
        assert client.get(_MAPPING_URL).json()["mapping"] is None

        # Без маппинга вебхук с английскими ключами всё ещё работает как раньше.
        lead_id = f"lead-{uuid.uuid4().hex[:10]}"
        body = {"name": "Пётр Иванов", "phone": "+79261234568", "email": f"{lead_id}@example.com"}
        response = _send_webhook(client, body, key=lead_id)
        assert response.status_code == 200, response.text

    def test_unknown_custom_field_is_rejected(self, client) -> None:
        run(client, functools.partial(_prepare_source, credentials_ref=None))
        _login_admin(client)

        bad = {
            "mode": "strict",
            "rules": [
                {"target": "phone", "source": "phone"},
                {"target": "custom_fields.no_such_field", "source": "x"},
            ],
        }
        response = client.put(_MAPPING_URL, json=bad)
        assert response.status_code == 422, response.text

    def test_mapping_without_contact_target_is_rejected(self, client) -> None:
        run(client, functools.partial(_prepare_source, credentials_ref=None))
        _login_admin(client)

        bad = {"mode": "strict", "rules": [{"target": "course", "source": "product_interest"}]}
        response = client.put(_MAPPING_URL, json=bad)
        assert response.status_code == 422, response.text


class TestMappingPreview:
    def test_preview_does_not_write_anything(self, client) -> None:
        from sqlalchemy import func, select

        from app.core.db import session_scope
        from app.modules.integration.models import InboundMessage

        run(client, functools.partial(_prepare_source, credentials_ref=None))
        run(client, functools.partial(_ensure_product, "py-base", "Python: базовый курс"))
        _login_admin(client)

        async def _count() -> int:
            async with session_scope() as session:
                return (
                    await session.scalar(
                        select(func.count(InboundMessage.id)).where(
                            InboundMessage.source_code == "cms"
                        )
                    )
                ) or 0

        before = run(client, _count)

        response = client.post(
            f"{_MAPPING_URL}/preview",
            json={
                "record": {
                    "lead_id": "lead-preview-1",
                    "phone": "+79261234567",
                    "email": "preview@example.com",
                    "product_interest": "python-basics",
                    "source_campaign": "test-campaign",
                },
                "mapping": _MAPPING_BODY,
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["payload"]["course"] == "py-base"
        assert body["product"] == {"code": "py-base", "name": "Python: базовый курс"}
        # Телефон/email в ответе маскированы.
        assert "+7926" not in body["record"]["phone"]

        after = run(client, _count)
        assert after == before
