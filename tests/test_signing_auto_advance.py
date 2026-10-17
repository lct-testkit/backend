"""B7-4: автопереход воронки после подписания всех сторон (`on_signed`).

Хранилище подменено (`stubbed_storage`, импортирован из `test_signing_hardening`) — проверяется
логика автоперехода, а не инфраструктура S3/NTP."""

from __future__ import annotations

import functools
import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, _make_user, run
from tests.signing_helpers import _build
from tests.test_signing_hardening import (  # noqa: F401
    CODE,
    _get,
    _issue_otp,
    _try_sign,
    stubbed_storage,
)

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


async def _deal_with_two_statuses(*, on_signed: str | None) -> dict[str, object]:
    from app.core.db import session_scope
    from app.modules.catalog.models import Organization
    from app.modules.crm.models import Deal
    from app.modules.identity.models import User
    from app.modules.workflow.models import Workflow, WorkflowStatus

    async with session_scope() as session:
        workflow = Workflow(
            code=f"wf-{uuid.uuid4().hex[:10]}", name="Воронка", deal_type="b2b", state="draft"
        )
        session.add(workflow)
        await session.flush()
        current = WorkflowStatus(workflow_id=workflow.id, code="current", name="Текущий")
        next_status = WorkflowStatus(workflow_id=workflow.id, code="next", name="Следующий")
        org = Organization(name=f"Вуз {uuid.uuid4().hex[:6]}", org_type="university")
        owner = User(
            keycloak_id=str(uuid.uuid4()),
            email=f"{uuid.uuid4().hex[:8]}@rt-it-school.ru",
            full_name="Петров П.П.",
            role="KAM",
            status="active",
            consent_version="1.0",
        )
        session.add_all([current, next_status, org, owner])
        await session.flush()
        deal = Deal(
            number=f"D-{uuid.uuid4().hex[:10]}",
            title="Сделка с автопереходом",
            deal_type="b2b",
            workflow_id=workflow.id,
            status_id=current.id,
            organization_id=org.id,
            owner_id=owner.id,
            signature_status="pending",
        )
        session.add(deal)
        await session.flush()
        return {
            "deal_id": deal.id,
            "current_status_id": current.id,
            "next_status_id": next_status.id,
        }


async def _attach_document(
    deal_id: uuid.UUID,
    document_id: uuid.UUID,
    *,
    requested_in_status_id: uuid.UUID,
    on_signed: str | None,
) -> None:
    from app.core.db import session_scope
    from app.modules.crm.models import Deal
    from app.modules.signing.models import SignatureDocument

    async with session_scope() as session:
        document = await session.get(SignatureDocument, document_id)
        document.entity_type = "deal"
        document.entity_id = deal_id
        document.on_signed = on_signed
        document.requested_in_status_id = requested_in_status_id
        deal = await session.get(Deal, deal_id)
        deal.active_signature_document_id = document_id
        deal.status_id = requested_in_status_id
        await session.flush()


def _scene(client, *, on_signed: str | None):
    signer = run(client, _make_user, "KAM")
    built = _build(client, requests=[{"user": signer, "status": "sent"}])
    scene = run(client, functools.partial(_deal_with_two_statuses, on_signed=on_signed))
    run(
        client,
        functools.partial(
            _attach_document,
            scene["deal_id"],
            built.document_id,
            requested_in_status_id=scene["current_status_id"],
            on_signed=on_signed,
        ),
    )
    run(client, _issue_otp, built.request_ids[0])
    return built, scene


class TestAutoAdvance:
    def test_the_last_signature_moves_the_deal_forward(  # noqa: F811
        self,
        client,
        stubbed_storage,  # noqa: F811
    ) -> None:
        from sqlalchemy import select

        from app.modules.crm.models import Deal, DealStatusHistory

        built, scene = _scene(client, on_signed="next")

        assert isinstance(run(client, _try_sign, built.request_ids[0]), uuid.UUID)

        deal = run(client, _get, Deal, scene["deal_id"])
        assert deal.status_id == scene["next_status_id"]

        async def _history():
            from app.core.db import session_scope

            async with session_scope() as session:
                rows = (
                    (
                        await session.execute(
                            select(DealStatusHistory).where(
                                DealStatusHistory.deal_id == scene["deal_id"]
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                return [r.reason for r in rows]

        reasons = run(client, _history)
        assert "auto" in reasons

    def test_no_on_signed_leaves_the_deal_alone(self, client, stubbed_storage) -> None:  # noqa: F811
        from app.modules.crm.models import Deal

        built, scene = _scene(client, on_signed=None)

        assert isinstance(run(client, _try_sign, built.request_ids[0]), uuid.UUID)

        deal = run(client, _get, Deal, scene["deal_id"])
        assert deal.status_id == scene["current_status_id"]

    def test_manual_transition_before_the_last_signature_blocks_auto_advance(  # noqa: F811
        self,
        client,
        stubbed_storage,  # noqa: F811
    ) -> None:
        """Сделка успела уйти на другой статус вручную — `requested_in_status_id` больше не
        совпадает с текущим, автопереход не срабатывает (и не создаёт запись history)."""
        from sqlalchemy import select

        from app.modules.crm.models import Deal, DealStatusHistory

        built, scene = _scene(client, on_signed="next")

        async def _move_manually():
            from app.core.db import session_scope

            async with session_scope() as session:
                deal = await session.get(Deal, scene["deal_id"])
                deal.status_id = scene["next_status_id"]

        run(client, _move_manually)

        assert isinstance(run(client, _try_sign, built.request_ids[0]), uuid.UUID)

        deal = run(client, _get, Deal, scene["deal_id"])
        # Остаётся там, куда его передвинули вручную — автопереход не трогает статус повторно.
        assert deal.status_id == scene["next_status_id"]

        async def _auto_history_count():
            from app.core.db import session_scope

            async with session_scope() as session:
                rows = (
                    (
                        await session.execute(
                            select(DealStatusHistory).where(
                                DealStatusHistory.deal_id == scene["deal_id"],
                                DealStatusHistory.reason == "auto",
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                return len(rows)

        assert run(client, _auto_history_count) == 0

    def test_only_the_active_document_triggers_auto_advance(  # noqa: F811
        self,
        client,
        stubbed_storage,  # noqa: F811
    ) -> None:
        from app.modules.crm.models import Deal

        built, scene = _scene(client, on_signed="next")
        # Другой (настоящий) документ подписи — сделка «переключилась» на него, пока первый
        # ещё ждёт подписи.
        other = _build(client, requests=[{"status": "pending"}])

        async def _deactivate():
            from app.core.db import session_scope

            async with session_scope() as session:
                deal = await session.get(Deal, scene["deal_id"])
                deal.active_signature_document_id = other.document_id

        run(client, _deactivate)

        assert isinstance(run(client, _try_sign, built.request_ids[0]), uuid.UUID)

        deal = run(client, _get, Deal, scene["deal_id"])
        assert deal.status_id == scene["current_status_id"]

    def test_an_unknown_target_status_does_not_break_the_signature(  # noqa: F811
        self,
        client,
        stubbed_storage,  # noqa: F811
    ) -> None:
        """Целевого статуса нет в воронке (например, удалён) — подпись всё равно проходит,
        сделка просто не переходит (SAVEPOINT, не вся транзакция)."""
        built, scene = _scene(client, on_signed="does-not-exist")

        signature_id = run(client, _try_sign, built.request_ids[0])

        assert isinstance(signature_id, uuid.UUID)
