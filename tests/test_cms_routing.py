"""B5-4 (BUG-15): маршрут на B2B — целевая воронка и тип сделки по значению поля заявки
(`POST /api/v1/integrations/cms/leads`) и администрирование маршрутизации
(`GET/PUT/DELETE /api/admin/integrations/sources/cms/routing`). Настоящая Postgres
(`TEST_DATABASE_URL`, см. докстринг `tests/conftest.py`).

До этой задачи `CmsLeadService._process_lead` жёстко заводила `deal_type="b2c"` — заявка
вуза с сайта попасть в B2B-воронку не могла (BUG-15). Правила маршрутизации смотрят на
промапленное поле (здесь — `lead_kind`) и переключают: тип сделки, воронку/начальный статус
(по коду — раздел «Коды статусов и воронок нигде не зашиты литералом» в `routing.py`) и
политику ответственного (`fixed`/`organization_owner`/`least_loaded`).

Заводимая B2B-заявка — форма вуза: организация находится по ИНН/названию или заводится,
представитель — контакт, привязанный к ней; дедупликация — по ОРГАНИЗАЦИИ (не по
представителю: другой сотрудник того же вуза не должен открыть вторую сделку).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import functools
import hashlib
import json
import random
import uuid
from typing import Any

import pytest

from app.modules.catalog.validators import validate_inn
from app.modules.integration.security import compute_signature
from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import create_user, login

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


async def _reset_cms_source_config() -> None:
    """`cms` — одна строка на всю (общую, не откатываемую между тестами) БД: маппинг/
    маршрутизация этого файла не должны переживать его тесты и ломать `test_cms_webhook.py`
    (без своего маппинга, т.е. ожидающий `field_mapping IS NULL`) — тот же приём, что
    `_prepare_source` других файлов (всегда выставляет `field_mapping` явно, а не ждёт,
    что прошлый тест его сбросил)."""
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.integration.models import IntegrationSource

    async with session_scope() as session:
        source = (
            await session.execute(select(IntegrationSource).where(IntegrationSource.code == "cms"))
        ).scalar_one_or_none()
        if source is not None:
            source.field_mapping = None
            source.routing = None


@pytest.fixture(autouse=True)
def _cleanup_cms_source_config(client):
    yield
    run(client, _reset_cms_source_config)

_URL = "/api/v1/integrations/cms/leads"
_ROUTING_URL = "/api/admin/integrations/sources/cms/routing"
_SECRET_ENV = "CMS_ROUTING_TEST_SECRET"
_SECRET = "cms-routing-test-secret"

# B5-4 регистрирует эти цели в `field_mapping.TARGETS["cms"]` (см. докстринг модуля) —
# без маппинга сайт обязан был бы слать их буквально этими именами ключей; с маппингом
# источник может называть их как угодно. Здесь настраиваем явный маппинг один раз.
_MAPPING_BODY = {
    "mode": "strict",
    "rules": [
        {"target": "phone", "source": "phone"},
        {"target": "email", "source": "email"},
        {"target": "first_name", "source": "first_name"},
        {"target": "last_name", "source": "last_name"},
        {"target": "comment", "source": "comment"},
        {"target": "lead_kind", "source": "lead_kind"},
        {"target": "organization_name", "source": "organization_name"},
        {"target": "organization_inn", "source": "organization_inn"},
        {"target": "students_planned", "source": "students_planned"},
    ],
}


def _marker() -> str:
    return uuid.uuid4().hex[:10]


def _valid_inn() -> str:
    """Случайный ИНН юрлица с верной контрольной суммой (каждый десятый подходит)."""
    while True:
        candidate = "77" + "".join(str(random.randint(0, 9)) for _ in range(8))
        if validate_inn(candidate).ok:
            return candidate


def _phone(seed: str) -> str:
    """E.164-номер, выведенный из `seed`: разные `seed` не совпадают, одинаковый — совпадает
    (как и в `test_cms_pull.py`) — заявки разных тестов не путают дедупликацию контактов."""
    digest = hashlib.sha256(seed.encode()).hexdigest()
    return "+79" + "".join(str(int(ch, 16) % 10) for ch in digest[:9])


def _email(seed: str) -> str:
    return f"{hashlib.sha256(seed.encode()).hexdigest()[:16]}@example.com"


# --- БД: подготовка ------------------------------------------------------------------------


async def _prepare_source(*, credentials_ref: str | None, routing: dict[str, Any] | None) -> None:
    """Служебная учётка, источник `cms` (маппинг B5-4 всегда включён), и B2C-воронка по
    умолчанию — на случай, если маршрут откатится на дефолт (`routing=None` либо правило не
    совпало и `default.deal_type == "b2c"`)."""
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
        source.field_mapping = _MAPPING_BODY
        source.routing = routing

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
            spec = dataclasses.replace(_b2c_spec(), code=f"b2c_route_{_marker()}")
            await seed_workflow(session, spec)


async def _seed_b2b_workflow(code: str) -> None:
    """Свежая (не дефолтная — чтобы не задеть частичный уникальный индекс «один дефолт на
    deal_type») опубликованная B2B-воронка с заданным кодом: маршрут ссылается на неё по коду,
    а не на дефолтную, — так тест ловит и ошибку резолва по коду, а не только фолбэк."""
    from app.core.db import session_scope
    from app.modules.workflow.seed import _b2b_spec, seed_workflow

    async with session_scope() as session:
        spec = dataclasses.replace(_b2b_spec(), code=code)
        await seed_workflow(session, spec, is_default=False)


async def _insert_organization(
    *, name: str | None, inn: str | None, owner_id: uuid.UUID | None = None, deleted: bool = False
) -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.catalog.models import Organization

    async with session_scope() as session:
        organization = Organization(
            name=name or f"Вуз {_marker()}",
            org_type="university",
            inn=inn,
            owner_id=owner_id,
            source="test",
        )
        if deleted:
            organization.deleted_at = dt.datetime.now(dt.UTC)
        session.add(organization)
        await session.flush()
        return organization.id


async def _organization_by_inn(inn: str) -> dict[str, Any] | None:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.catalog.models import Organization

    async with session_scope() as session:
        row = (
            await session.execute(
                select(
                    Organization.id,
                    Organization.name,
                    Organization.owner_id,
                    Organization.org_type,
                ).where(Organization.inn == inn, Organization.deleted_at.is_(None))
            )
        ).first()
        return None if row is None else dict(row._mapping)


async def _count_organizations_by_inn(inn: str) -> int:
    from sqlalchemy import func, select

    from app.core.db import session_scope
    from app.modules.catalog.models import Organization

    async with session_scope() as session:
        return int(
            await session.scalar(
                select(func.count()).select_from(Organization).where(Organization.inn == inn)
            )
            or 0
        )


async def _deal_by_id(deal_id: uuid.UUID) -> dict[str, Any] | None:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.crm.models import Deal
    from app.modules.workflow.models import WorkflowStatus

    async with session_scope() as session:
        row = (
            await session.execute(
                select(
                    Deal.deal_type,
                    Deal.organization_id,
                    Deal.owner_id,
                    Deal.students_planned,
                    Deal.workflow_id,
                    WorkflowStatus.code.label("status_code"),
                )
                .join(WorkflowStatus, WorkflowStatus.id == Deal.status_id)
                .where(Deal.id == deal_id)
            )
        ).first()
        return None if row is None else dict(row._mapping)


async def _workflow_id_by_code(code: str) -> uuid.UUID:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.workflow.models import Workflow

    async with session_scope() as session:
        return (
            await session.execute(select(Workflow.id).where(Workflow.code == code))
        ).scalar_one()


async def _inbound_error(key: str) -> str | None:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.integration.models import InboundMessage

    async with session_scope() as session:
        return (
            await session.execute(
                select(InboundMessage.error)
                .where(InboundMessage.source_code == "cms", InboundMessage.external_id == key)
                .order_by(InboundMessage.received_at.desc())
            )
        ).scalars().first()


async def _deal_comment_bodies(deal_id: uuid.UUID) -> list[str]:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.crm.models import DealComment

    async with session_scope() as session:
        rows = (
            await session.execute(select(DealComment.body).where(DealComment.deal_id == deal_id))
        ).scalars()
        return list(rows)


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


def _university_lead(
    seed: str,
    *,
    org_name: str | None = None,
    org_inn: str | None = None,
    students_planned: int | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "lead_kind": "university",
        "first_name": "Пётр",
        "last_name": "Сидоров",
        "phone": _phone(seed),
        "email": _email(seed),
        "comment": f"Заявка с формы вуза, seed={seed}",
    }
    if org_name is not None:
        body["organization_name"] = org_name
    if org_inn is not None:
        body["organization_inn"] = org_inn
    if students_planned is not None:
        body["students_planned"] = students_planned
    return body


def _routing_body(
    *, workflow_code: str, owner_policy: str, fixed_owner_id: str | None = None
) -> dict[str, Any]:
    route: dict[str, Any] = {
        "deal_type": "b2b",
        "workflow_code": workflow_code,
        "initial_status_code": "first_contact",
        "owner_policy": owner_policy,
    }
    if fixed_owner_id is not None:
        route["fixed_owner_id"] = fixed_owner_id
    return {
        "default": {"deal_type": "b2c"},
        "rules": [{"when": {"target": "lead_kind", "equals": "university"}, "route": route}],
    }


# =============================================================================
# B5-4: обработка B2B-заявки
# =============================================================================


class TestB2BLead:
    def test_new_organization_is_created_with_fixed_owner_and_students_planned(
        self, client, monkeypatch
    ) -> None:
        monkeypatch.setenv(_SECRET_ENV, _SECRET)
        code = f"b2b_route_{_marker()}"
        run(client, _seed_b2b_workflow, code)
        kam = create_user(client, "KAM")
        routing = _routing_body(
            workflow_code=code, owner_policy="fixed", fixed_owner_id=str(kam.id)
        )
        run(
            client,
            functools.partial(_prepare_source, credentials_ref=_SECRET_ENV, routing=routing),
        )

        inn = _valid_inn()
        org_name = f"РГУ им. Тестова {_marker()}"
        seed = f"new-org-{_marker()}"
        body = _university_lead(seed, org_name=org_name, org_inn=inn, students_planned=120)
        response = _send_webhook(client, body, key=seed)
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "processed"
        deal_id = uuid.UUID(response.json()["deal_id"])

        organization = run(client, _organization_by_inn, inn)
        assert organization is not None
        assert organization["name"] == org_name
        assert organization["org_type"] == "university"
        assert organization["owner_id"] == kam.id

        deal = run(client, _deal_by_id, deal_id)
        assert deal is not None
        assert deal["deal_type"] == "b2b"
        assert deal["organization_id"] == organization["id"]
        assert deal["owner_id"] == kam.id
        assert deal["students_planned"] == 120
        assert deal["workflow_id"] == run(client, _workflow_id_by_code, code)
        assert deal["status_code"] == "first_contact"

    def test_second_lead_for_the_same_organization_is_a_duplicate_not_a_new_deal(
        self, client, monkeypatch
    ) -> None:
        monkeypatch.setenv(_SECRET_ENV, _SECRET)
        code = f"b2b_route_{_marker()}"
        run(client, _seed_b2b_workflow, code)
        kam = create_user(client, "KAM")
        routing = _routing_body(
            workflow_code=code, owner_policy="fixed", fixed_owner_id=str(kam.id)
        )
        run(
            client,
            functools.partial(_prepare_source, credentials_ref=_SECRET_ENV, routing=routing),
        )

        inn = _valid_inn()
        org_name = f"МГУ им. Тестова {_marker()}"
        first_seed = f"first-{_marker()}"
        first = _send_webhook(
            client,
            _university_lead(first_seed, org_name=org_name, org_inn=inn),
            key=first_seed,
        )
        assert first.status_code == 200, first.text
        first_deal_id = uuid.UUID(first.json()["deal_id"])

        # Другой представитель того же вуза (другой телефон/email) — но дедупликация B2B
        # смотрит на организацию, а не на контакт.
        second_seed = f"second-{_marker()}"
        second = _send_webhook(
            client,
            _university_lead(second_seed, org_name=org_name, org_inn=inn),
            key=second_seed,
        )
        assert second.status_code == 200, second.text
        assert second.json()["status"] == "duplicate"
        assert uuid.UUID(second.json()["deal_id"]) == first_deal_id

        assert run(client, _count_organizations_by_inn, inn) == 1
        comments = run(client, _deal_comment_bodies, first_deal_id)
        assert any("Повторная заявка" in body for body in comments)

    def test_ambiguous_organization_name_without_inn_is_rejected(
        self, client, monkeypatch
    ) -> None:
        monkeypatch.setenv(_SECRET_ENV, _SECRET)
        code = f"b2b_route_{_marker()}"
        run(client, _seed_b2b_workflow, code)
        routing = _routing_body(workflow_code=code, owner_policy="least_loaded")
        run(
            client,
            functools.partial(_prepare_source, credentials_ref=_SECRET_ENV, routing=routing),
        )

        name = f"Политех {_marker()}"
        run(client, functools.partial(_insert_organization, name=name, inn=None))
        run(client, functools.partial(_insert_organization, name=name, inn=None))

        seed = f"ambiguous-{_marker()}"
        response = _send_webhook(
            client, _university_lead(seed, org_name=name), key=seed
        )
        assert response.status_code == 422, response.text
        error = run(client, _inbound_error, seed)
        assert error is not None and "неоднозначно" in error

    def test_organization_with_a_soft_deleted_inn_is_rejected(
        self, client, monkeypatch
    ) -> None:
        monkeypatch.setenv(_SECRET_ENV, _SECRET)
        code = f"b2b_route_{_marker()}"
        run(client, _seed_b2b_workflow, code)
        routing = _routing_body(workflow_code=code, owner_policy="least_loaded")
        run(
            client,
            functools.partial(_prepare_source, credentials_ref=_SECRET_ENV, routing=routing),
        )

        inn = _valid_inn()
        run(
            client,
            functools.partial(
                _insert_organization, name=f"Удалённый вуз {_marker()}", inn=inn, deleted=True
            ),
        )

        seed = f"deleted-org-{_marker()}"
        response = _send_webhook(
            client, _university_lead(seed, org_name="Новое имя", org_inn=inn), key=seed
        )
        assert response.status_code == 422, response.text
        error = run(client, _inbound_error, seed)
        assert error is not None and "удалена" in error

    def test_organization_owner_policy_uses_the_existing_owner(
        self, client, monkeypatch
    ) -> None:
        monkeypatch.setenv(_SECRET_ENV, _SECRET)
        code = f"b2b_route_{_marker()}"
        run(client, _seed_b2b_workflow, code)
        routing = _routing_body(workflow_code=code, owner_policy="organization_owner")
        run(
            client,
            functools.partial(_prepare_source, credentials_ref=_SECRET_ENV, routing=routing),
        )

        head = create_user(client, "HEAD")
        inn = _valid_inn()
        run(
            client,
            functools.partial(
                _insert_organization,
                name=f"Вуз с ответственным {_marker()}",
                inn=inn,
                owner_id=head.id,
            ),
        )

        seed = f"org-owner-{_marker()}"
        response = _send_webhook(
            client, _university_lead(seed, org_name="Другое название", org_inn=inn), key=seed
        )
        assert response.status_code == 200, response.text
        deal = run(client, _deal_by_id, uuid.UUID(response.json()["deal_id"]))
        assert deal is not None
        assert deal["owner_id"] == head.id


# =============================================================================
# B5-4: администрирование маршрутизации
# =============================================================================


class TestRoutingAdmin:
    def test_kam_cannot_read_or_write_routing(self, client) -> None:
        login(client, "KAM")
        assert client.get(_ROUTING_URL).status_code == 403
        body = _routing_body(workflow_code="whatever", owner_policy="least_loaded")
        assert client.put(_ROUTING_URL, json=body).status_code == 403

    def test_unknown_workflow_code_is_rejected(self, client) -> None:
        run(client, functools.partial(_prepare_source, credentials_ref=None, routing=None))
        login(client, "ADMIN")
        body = _routing_body(workflow_code=f"no-such-{_marker()}", owner_policy="least_loaded")
        response = client.put(_ROUTING_URL, json=body)
        assert response.status_code == 422, response.text

    def test_unknown_initial_status_code_is_rejected(self, client) -> None:
        code = f"b2b_route_{_marker()}"
        run(client, _seed_b2b_workflow, code)
        run(client, functools.partial(_prepare_source, credentials_ref=None, routing=None))
        login(client, "ADMIN")
        body = _routing_body(workflow_code=code, owner_policy="least_loaded")
        body["rules"][0]["route"]["initial_status_code"] = f"no-such-status-{_marker()}"
        response = client.put(_ROUTING_URL, json=body)
        assert response.status_code == 422, response.text

    def test_fixed_policy_without_fixed_owner_id_is_rejected(self, client) -> None:
        code = f"b2b_route_{_marker()}"
        run(client, _seed_b2b_workflow, code)
        run(client, functools.partial(_prepare_source, credentials_ref=None, routing=None))
        login(client, "ADMIN")
        body = _routing_body(workflow_code=code, owner_policy="fixed")
        response = client.put(_ROUTING_URL, json=body)
        assert response.status_code == 422, response.text

    def test_saved_routing_round_trips_and_delete_restores_the_previous_behaviour(
        self, client, monkeypatch
    ) -> None:
        monkeypatch.setenv(_SECRET_ENV, _SECRET)
        code = f"b2b_route_{_marker()}"
        run(client, _seed_b2b_workflow, code)
        kam = create_user(client, "KAM")
        run(client, functools.partial(_prepare_source, credentials_ref=_SECRET_ENV, routing=None))

        login(client, "ADMIN")
        body = _routing_body(workflow_code=code, owner_policy="fixed", fixed_owner_id=str(kam.id))
        saved = client.put(_ROUTING_URL, json=body)
        assert saved.status_code == 200, saved.text

        got = client.get(_ROUTING_URL)
        assert got.status_code == 200, got.text
        assert got.json()["routing"]["rules"][0]["route"]["workflow_code"] == code

        deleted = client.delete(_ROUTING_URL)
        assert deleted.status_code == 204, deleted.text
        assert client.get(_ROUTING_URL).json()["routing"] is None

        # Без маршрутизации заявка вуза снова идёт в B2C-воронку по умолчанию, как до B5-4:
        # организация не заводится вовсе.
        inn = _valid_inn()
        seed = f"reverted-{_marker()}"
        response = _send_webhook(
            client,
            _university_lead(seed, org_name=f"Вуз после отключения {_marker()}", org_inn=inn),
            key=seed,
        )
        assert response.status_code == 200, response.text
        assert run(client, _organization_by_inn, inn) is None
        deal = run(client, _deal_by_id, uuid.UUID(response.json()["deal_id"]))
        assert deal is not None
        assert deal["deal_type"] == "b2c"
