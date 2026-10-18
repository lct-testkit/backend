"""B5-8: движение статуса сделки по прогрессу LMS (`routing.progress_rules`,
`integration.lms.move_deal_by_progress`).

Воронка — настоящая `b2c_individual_v1` (`workflow.seed._b2c_spec`), не черновик
`tests/lms_helpers.make_scene`: правило маршрутизации проверяет переход в ОПУБЛИКОВАННОМ графе
конкретной воронки, а не произвольный одностатусный черновик.

Сквозные тесты на настоящей PostgreSQL (`TEST_DATABASE_URL`).
"""

from __future__ import annotations

import dataclasses
import functools
import uuid
from typing import Any

import pytest

from tests.conftest import TEST_DATABASE_URL, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

_DEFAULT_RULE: dict[str, Any] = {
    "when": {"completed": True},
    "workflow_code": None,  # заполняется в _scene
    "from_status": "lms_enrollment",
    "to_status": "training_completed",
    "comment": "Обучение завершено в LMS ({progress_pct} %)",
}


async def _scene(*, closed: bool = False) -> dict[str, Any]:
    """Опубликованная воронка b2c (своя копия `_b2c_spec()`, чтобы тесты не делили статусы друг с
    другом), сделка в статусе `lms_enrollment`, продукт и контакт."""
    from app.core.db import session_scope
    from app.modules.catalog.models import Contact, Product
    from app.modules.crm.models import Deal, DealProduct
    from app.modules.identity.models import User
    from app.modules.workflow.seed import _b2c_spec, seed_workflow

    token = uuid.uuid4().hex[:10]
    async with session_scope() as session:
        spec = dataclasses.replace(_b2c_spec(), code=f"b2c_move_{token}", name=f"B2C move {token}")
        workflow = await seed_workflow(session, spec, is_default=False)
        assert workflow is not None
        statuses = {s["code"]: s for s in workflow.published_graph["statuses"]}

        owner = User(
            keycloak_id=str(uuid.uuid4()),
            email=f"{token}@rt-it-school.ru",
            full_name="Петров П.П.",
            role="KAM",
            status="active",
            consent_version="1.0",
        )
        contact = Contact(
            first_name="Дарья",
            last_name="Сидорова",
            email=f"{token}@example.ru",
            phone="+79990234365",
        )
        product = Product(code=f"c-{token}", name=f"Курс {token}")
        session.add_all([owner, contact, product])
        await session.flush()

        deal = Deal(
            number=f"D-{token}",
            title="Зачисление в LMS",
            deal_type="b2c",
            workflow_id=workflow.id,
            status_id=statuses["lms_enrollment"]["id"],
            contact_id=contact.id,
            owner_id=owner.id,
        )
        if closed:
            import datetime as dt

            deal.closed_at = dt.datetime.now(dt.UTC)
        session.add(deal)
        await session.flush()
        session.add(DealProduct(deal_id=deal.id, product_id=product.id, quantity=1))
        await session.flush()

        return {
            "deal_id": deal.id,
            "workflow_code": workflow.code,
            "statuses": {code: s["id"] for code, s in statuses.items()},
            "product_code": product.code,
        }


async def _apply(rows: list[dict[str, Any]], rules: list[dict[str, Any]]) -> dict[str, Any]:
    from app.core.db import session_scope
    from app.modules.integration.lms import apply_progress_rows
    from app.modules.integration.routing import ProgressRule

    async with session_scope() as session:
        batch = await apply_progress_rows(
            session, rows, progress_rules=[ProgressRule.model_validate(r) for r in rules]
        )
        return {
            "outcomes": batch.outcomes,
            "applied": batch.applied,
            "moved": batch.moved,
            "move_failed": batch.move_failed,
        }


async def _deal_status(deal_id: uuid.UUID) -> str:
    from app.core.db import session_scope
    from app.modules.crm.models import Deal
    from app.modules.workflow.models import Workflow
    from app.modules.workflow.service import get_cached_published_graph

    async with session_scope() as session:
        deal = await session.get(Deal, deal_id)
        assert deal is not None
        workflow = await session.get(Workflow, deal.workflow_id)
        assert workflow is not None
        graph = await get_cached_published_graph(workflow)
        return next(s["code"] for s in graph["statuses"] if s["id"] == str(deal.status_id))


async def _deal_version(deal_id: uuid.UUID) -> int:
    from app.core.db import session_scope
    from app.modules.crm.models import Deal

    async with session_scope() as session:
        deal = await session.get(Deal, deal_id)
        assert deal is not None
        return deal.version


async def _deal_comments(deal_id: uuid.UUID) -> list[str]:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.crm.models import DealComment

    async with session_scope() as session:
        rows = (
            (await session.execute(select(DealComment.body).where(DealComment.deal_id == deal_id)))
            .scalars()
            .all()
        )
        return list(rows)


async def _audit_count(deal_id: uuid.UUID, action: str) -> int:
    from sqlalchemy import func, select

    from app.core.db import session_scope
    from app.modules.audit.models import AuditLog

    async with session_scope() as session:
        return (
            await session.scalar(
                select(func.count()).where(
                    AuditLog.entity_type == "deal",
                    AuditLog.entity_id == deal_id,
                    AuditLog.action == action,
                )
            )
        ) or 0


def _rule(scene: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    rule = dict(_DEFAULT_RULE)
    rule["workflow_code"] = scene["workflow_code"]
    rule.update(overrides)
    return rule


class TestMoveDealByProgress:
    def test_completed_row_moves_deal_to_training_completed(self, client) -> None:
        scene = run(client, _scene)
        row = {
            "deal_id": str(scene["deal_id"]),
            "course_external_id": scene["product_code"],
            "progress_pct": 100,
            "completed_at": "2026-09-20T12:00:00Z",
        }

        result = run(client, _apply, [row], [_rule(scene)])

        assert result["applied"] == 1
        assert result["moved"] == 1
        assert result["move_failed"] == 0
        assert run(client, _deal_status, scene["deal_id"]) == "training_completed"
        comments = run(client, _deal_comments, scene["deal_id"])
        assert any("100" in c for c in comments)
        assert run(client, _audit_count, scene["deal_id"], "DEAL_STATUS_CHANGED") == 1

    def test_repeat_run_does_not_move_twice(self, client) -> None:
        scene = run(client, _scene)
        row = {
            "deal_id": str(scene["deal_id"]),
            "course_external_id": scene["product_code"],
            "progress_pct": 100,
            "completed_at": "2026-09-20T12:00:00Z",
        }
        rules = [_rule(scene)]
        run(client, _apply, [row], rules)

        result = run(client, _apply, [row], rules)

        assert result["moved"] == 0
        assert result["move_failed"] == 0
        assert run(client, _audit_count, scene["deal_id"], "DEAL_STATUS_CHANGED") == 1

    def test_partial_progress_does_not_move(self, client) -> None:
        scene = run(client, _scene)
        row = {
            "deal_id": str(scene["deal_id"]),
            "course_external_id": scene["product_code"],
            "progress_pct": 47,
        }

        result = run(client, _apply, [row], [_rule(scene)])

        assert result["applied"] == 1
        assert result["moved"] == 0
        assert result["move_failed"] == 0
        assert run(client, _deal_status, scene["deal_id"]) == "lms_enrollment"

    def test_closed_deal_gives_move_failed_but_progress_is_recorded(self, client) -> None:
        scene = run(client, functools.partial(_scene, closed=True))
        row = {
            "deal_id": str(scene["deal_id"]),
            "course_external_id": scene["product_code"],
            "progress_pct": 100,
            "completed_at": "2026-09-20T12:00:00Z",
        }

        result = run(client, _apply, [row], [_rule(scene)])

        assert result["applied"] == 1
        assert result["moved"] == 0
        assert result["move_failed"] == 1

    def test_refused_transition_leaves_no_partial_writes(self, client, monkeypatch) -> None:
        """Отказ перехода приходит как `AppError` уже ПОСЛЕ `claim_version` (тот бьёт UPDATE
        сразу, до проверки условий). Раньше `move_deal_by_progress` ловил его внутри SAVEPOINT и
        возвращал `MOVE_FAILED` обычным значением: SAVEPOINT закрывался успешно, и версия
        сделки росла на каждом опросе LMS, пока правило подходило, а переход не удавался."""
        from app.core.errors import AppError, ErrorCode
        from app.core.optimistic import claim_version
        from app.modules.crm.service import DealService

        async def _claim_then_refuse(self, deal, principal, **kwargs):
            await claim_version(self._session, deal, kwargs["expected_version"])
            raise AppError(ErrorCode.TRANSITION_CONDITIONS, "условия не выполнены")

        monkeypatch.setattr(DealService, "transition", _claim_then_refuse)

        scene = run(client, _scene)
        row = {
            "deal_id": str(scene["deal_id"]),
            "course_external_id": scene["product_code"],
            "progress_pct": 100,
            "completed_at": "2026-09-20T12:00:00Z",
        }
        version_before = run(client, _deal_version, scene["deal_id"])

        result = run(client, _apply, [row], [_rule(scene)])

        assert result["applied"] == 1
        assert result["moved"] == 0
        assert result["move_failed"] == 1
        assert run(client, _deal_version, scene["deal_id"]) == version_before
        assert run(client, _deal_status, scene["deal_id"]) == "lms_enrollment"

    def test_no_rules_does_not_attempt_move(self, client) -> None:
        scene = run(client, _scene)
        row = {
            "deal_id": str(scene["deal_id"]),
            "course_external_id": scene["product_code"],
            "progress_pct": 100,
            "completed_at": "2026-09-20T12:00:00Z",
        }

        result = run(client, _apply, [row], [])

        assert result["moved"] == 0
        assert result["move_failed"] == 0
        assert run(client, _deal_status, scene["deal_id"]) == "lms_enrollment"


class TestProgressRuleValidation:
    def test_rule_with_unknown_status_is_rejected(self, client) -> None:
        from app.core.db import session_scope
        from app.modules.integration.routing import Routing, validate_routing

        scene = run(client, _scene)

        async def _validate() -> list[str]:
            async with session_scope() as session:
                bad_rule = _rule(scene, to_status="no-such-status")
                routing = Routing.model_validate({"progress_rules": [bad_rule]})
                errors = await validate_routing(session, routing)
                return [e.field for e in errors]

        fields = run(client, _validate)
        assert any("to_status" in f for f in fields)

    def test_rule_with_no_transition_between_statuses_is_rejected(self, client) -> None:
        from app.core.db import session_scope
        from app.modules.integration.routing import Routing, validate_routing

        scene = run(client, _scene)

        async def _validate() -> list[str]:
            async with session_scope() as session:
                routing = Routing.model_validate(
                    {"progress_rules": [_rule(scene, from_status="won", to_status="lost")]}
                )
                errors = await validate_routing(session, routing)
                return [e.field for e in errors]

        fields = run(client, _validate)
        assert any("to_status" in f for f in fields)
