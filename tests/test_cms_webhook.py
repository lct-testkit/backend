"""Вебхук сайта `POST /api/v1/integrations/cms/leads` — сквозные тесты на настоящей PostgreSQL
(`TEST_DATABASE_URL`, см. докстринг `tests/conftest.py`).

Здесь собраны дефекты, найденные внешним тестировщиком, и правила приёма заявок:

* тело не JSON-объект → 422, а не 500; длинный `Idempotency-Key` → 422; не-ASCII в подписи → 401;
* подпись проверяется до повтора по ключу: чужой запрос с угаданным ключом ключ не занимает;
* отказ обработки оставляет тело во входящих со статусом `failed` и обезличенной причиной;
* `{}` не создаёт «Без имени —»: нужен email или телефон;
* ключи английские и русские (выгрузка «Данные оплат»), номер потока и сумма разбираются;
* один человек — один контакт при любом написании телефона и регистре email;
* заявка с номером заказа идёт через `OrderIngestService` (идемпотентно по номеру).

Секрет вебхука лежит в переменной окружения теста, источник `cms` включается прямо в БД.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import functools
import json
import uuid
from typing import Any

import pytest

from app.modules.integration.security import compute_signature
from tests.conftest import TEST_DATABASE_URL, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

_URL = "/api/v1/integrations/cms/leads"
_SECRET_ENV = "CMS_WEBHOOK_TEST_SECRET"
_SECRET = "cms-webhook-test-secret"
_AUTO: Any = object()


# --- БД: подготовка и чтение ----------------------------------------------------------------


async def _prepare(*, credentials_ref: str | None, active: bool = True) -> dict[str, Any]:
    """Служебная учётка, источник `cms` и воронка B2C по умолчанию (общая БД тестов может их не
    иметь). Воронку создаёт сид `b2c_individual_v1`, если своей по умолчанию ещё нет."""
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
        source.credentials_ref = credentials_ref

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
            spec = dataclasses.replace(_b2c_spec(), code=f"b2c_cms_{uuid.uuid4().hex[:8]}")
            workflow = await seed_workflow(session, spec)
        assert workflow is not None
        statuses = {s["code"]: s for s in workflow.published_graph["statuses"]}
        initial = next(s for s in statuses.values() if s["type"] == "initial")
        paid = statuses.get("payment_contract", initial)
        return {"initial_status_id": initial["id"], "paid_status_id": paid["id"]}


async def _inbound(key: str) -> dict[str, Any] | None:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.integration.models import InboundMessage

    async with session_scope() as session:
        message = (
            await session.execute(
                select(InboundMessage).where(
                    InboundMessage.source_code == "cms", InboundMessage.external_id == key
                )
            )
        ).scalar_one_or_none()
        if message is None:
            return None
        return {
            "id": str(message.id),
            "status": message.status,
            "error": message.error,
            "raw_payload": message.raw_payload,
            "signature_valid": message.signature_valid,
            "message_type": message.message_type,
            "entity_type": message.resulting_entity_type,
            "entity_id": str(message.resulting_entity_id) if message.resulting_entity_id else None,
        }


async def _inbound_count(key: str) -> int:
    from sqlalchemy import func, select

    from app.core.db import session_scope
    from app.modules.integration.models import InboundMessage

    async with session_scope() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(InboundMessage)
                .where(InboundMessage.source_code == "cms", InboundMessage.external_id == key)
            )
            or 0
        )


async def _signature_events(key: str) -> list[dict[str, Any]]:
    """События безопасности о неверной подписи с этим ключом и связанные с ними записи-улики."""
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.identity.models import SecurityEvent
    from app.modules.integration.models import InboundMessage

    async with session_scope() as session:
        events = (
            (
                await session.execute(
                    select(SecurityEvent).where(
                        SecurityEvent.event_type == "INTEGRATION_SIGNATURE_INVALID",
                        SecurityEvent.details["external_id"].astext == key,
                    )
                )
            )
            .scalars()
            .all()
        )
        result = []
        for event in events:
            evidence = await session.get(
                InboundMessage, uuid.UUID(event.details["inbound_message_id"])
            )
            assert evidence is not None
            result.append(
                {
                    "ip": event.ip,
                    "external_id": evidence.external_id,
                    "status": evidence.status,
                    "error": evidence.error,
                    "signature_valid": evidence.signature_valid,
                    "raw_payload": evidence.raw_payload,
                }
            )
        return result


async def _malformed_evidence(after: dt.datetime) -> list[dict[str, Any]]:
    """Записи-улики `malformed:*` (битое тело с верной подписью), появившиеся позже `after`."""
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.integration.models import InboundMessage

    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(InboundMessage).where(
                        InboundMessage.source_code == "cms",
                        InboundMessage.external_id.like("malformed:%"),
                        InboundMessage.received_at >= after,
                    )
                )
            )
            .scalars()
            .all()
        )
        return [{"status": m.status, "error": m.error, "raw_payload": m.raw_payload} for m in rows]


async def _now() -> dt.datetime:
    from sqlalchemy import func, select

    from app.core.db import session_scope

    async with session_scope() as session:
        return (await session.execute(select(func.clock_timestamp()))).scalar_one()


async def _deal(deal_id: str) -> dict[str, Any]:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.catalog.models import Product
    from app.modules.crm.models import Deal, DealComment, DealProduct

    async with session_scope() as session:
        deal = await session.get(Deal, uuid.UUID(deal_id))
        assert deal is not None
        lines = (
            (
                await session.execute(
                    select(DealProduct)
                    .where(DealProduct.deal_id == deal.id)
                    .order_by(DealProduct.created_at)
                )
            )
            .scalars()
            .all()
        )
        products = {}
        for line in lines:
            product = await session.get(Product, line.product_id)
            assert product is not None
            products[str(line.product_id)] = product.name
        comments = (
            (await session.execute(select(DealComment).where(DealComment.deal_id == deal.id)))
            .scalars()
            .all()
        )
        return {
            "id": str(deal.id),
            "title": deal.title,
            "deal_type": deal.deal_type,
            "status_id": str(deal.status_id),
            "source": deal.source,
            "external_ids": deal.external_ids,
            "order_number": deal.order_number,
            "custom_fields": deal.custom_fields,
            "amount": deal.amount,
            "contact_id": str(deal.contact_id) if deal.contact_id else None,
            "closed_at": deal.closed_at,
            "lines": [
                {
                    "product_id": str(line.product_id),
                    "product_name": products[str(line.product_id)],
                    "stream_number": line.stream_number,
                    "price": line.price,
                }
                for line in lines
            ],
            "comments": [(c.body, c.is_system, c.author_id) for c in comments],
        }


async def _contact(contact_id: str) -> dict[str, Any]:
    from app.core.db import session_scope
    from app.modules.catalog.models import Contact

    async with session_scope() as session:
        contact = await session.get(Contact, uuid.UUID(contact_id))
        assert contact is not None
        return {
            "id": str(contact.id),
            "first_name": contact.first_name,
            "last_name": contact.last_name,
            "middle_name": contact.middle_name,
            "email": contact.email,
            "phone": contact.phone,
            "source": contact.source,
            "external_ids": contact.external_ids,
            "organization_id": contact.organization_id,
        }


async def _counts() -> dict[str, int]:
    from sqlalchemy import func, select

    from app.core.db import session_scope
    from app.modules.catalog.models import Contact, Product
    from app.modules.crm.models import Deal

    async with session_scope() as session:
        return {
            "contacts": int(await session.scalar(select(func.count()).select_from(Contact)) or 0),
            "deals": int(await session.scalar(select(func.count()).select_from(Deal)) or 0),
            "products": int(await session.scalar(select(func.count()).select_from(Product)) or 0),
        }


async def _make_product(name: str, *, price: str | None = None) -> str:
    from decimal import Decimal

    from app.core.db import session_scope
    from app.modules.catalog.models import Product

    async with session_scope() as session:
        product = Product(
            code=f"t-{uuid.uuid4().hex[:10]}",
            name=name,
            base_price=Decimal(price) if price else None,
            is_active=True,
        )
        session.add(product)
        await session.flush()
        return str(product.id)


async def _contacts_with_phone(phone: str) -> int:
    from sqlalchemy import func, select

    from app.core.db import session_scope
    from app.modules.catalog.models import Contact

    async with session_scope() as session:
        return int(
            await session.scalar(
                select(func.count()).select_from(Contact).where(Contact.phone == phone)
            )
            or 0
        )


async def _contacts_with_email(email: str) -> int:
    from sqlalchemy import func, select

    from app.core.db import session_scope
    from app.modules.catalog.models import Contact

    async with session_scope() as session:
        return int(
            await session.scalar(
                select(func.count()).select_from(Contact).where(Contact.email == email)
            )
            or 0
        )


# --- Клиент вебхука -------------------------------------------------------------------------


def _email() -> str:
    return f"lead-{uuid.uuid4().hex[:12]}@example.ru"


def _national() -> str:
    """10 цифр российского номера без кода страны: у каждого теста свой человек."""
    return f"9{uuid.uuid4().int % 10**9:09d}"


def _unique_course() -> str:
    return f"Курс {uuid.uuid4().hex[:10]}"


class Webhook:
    def __init__(self, client, info: dict[str, Any]) -> None:
        self.client = client
        self.info = info

    def send(
        self,
        body: Any = None,
        *,
        raw: bytes | None = None,
        key: Any = _AUTO,
        signature: Any = _AUTO,
        secret: str = _SECRET,
        headers: dict[str, Any] | None = None,
    ):
        payload = raw if raw is not None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers: dict[str, Any] = {"Content-Type": "application/json"}
        if key is _AUTO:
            key = f"lead-{uuid.uuid4().hex}"
        if key is not None:
            request_headers["Idempotency-Key"] = key
        if signature is _AUTO:
            signature = f"sha256={compute_signature(secret, payload)}"
        if signature is not None:
            request_headers["X-Signature"] = signature
        request_headers.update(headers or {})
        self.last_key = key
        return self.client.post(_URL, content=payload, headers=request_headers)

    def lead(self, **fields: Any) -> dict[str, Any]:
        """Минимально достаточная заявка: уникальный email плюс переданные поля."""
        return {"email": _email(), **fields}


@pytest.fixture
def hook(client, monkeypatch: pytest.MonkeyPatch) -> Webhook:
    monkeypatch.setenv(_SECRET_ENV, _SECRET)
    info = run(client, functools.partial(_prepare, credentials_ref=_SECRET_ENV))
    return Webhook(client, info)


def _accepted(response, *, status: str = "processed") -> dict[str, Any]:
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == status, body
    return body


def _problem(response, status: int) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["request_id"]
    return body


# =============================================================================================
# a) Тело запроса — JSON-объект
# =============================================================================================


class TestBodyShape:
    @pytest.mark.parametrize(
        "raw",
        [b"null", b"[]", b'[{"email": "a@b.ru"}]', b'"text"', b"42", b"true", b"3.14"],
        ids=["null", "empty-array", "array-of-objects", "string", "number", "bool", "float"],
    )
    def test_anything_but_an_object_is_a_validation_error(self, hook, raw: bytes) -> None:
        problem = _problem(hook.send(raw=raw), 422)

        assert problem["code"] == "CRM-1001"
        assert "JSON-объект" in problem["detail"]

    @pytest.mark.parametrize(
        "raw",
        [
            b"",
            b"{",
            b'{"email": ',
            b"not json",
            b'{"email": "a@b.ru",}',
            b"\xff\xfe\x00{",
            b'{"amount": NaN}',
            b'{"amount": Infinity}',
            b'{"amount": -Infinity}',
            b'{"amount": 1e999}',
            b"[" * 20000,
            b"[" * 40 + b"]" * 40,
        ],
        ids=[
            "empty",
            "unclosed",
            "truncated",
            "plain-text",
            "trailing-comma",
            "not-utf8",
            "nan",
            "infinity",
            "minus-infinity",
            "float-overflow",
            "recursion",
            "deep-nesting",
        ],
    )
    def test_broken_json_is_a_validation_error(self, hook, raw: bytes) -> None:
        problem = _problem(hook.send(raw=raw), 422)

        assert problem["code"] == "CRM-1001"

    def test_deeply_nested_object_is_refused(self, hook) -> None:
        deep: Any = {}
        for _ in range(30):
            deep = {"a": deep}
        body = {"email": _email(), "extra": deep}

        problem = _problem(hook.send(body), 422)

        assert "вложенность" in problem["detail"]

    def test_broken_body_leaves_evidence_but_does_not_occupy_the_key(self, client, hook) -> None:
        started = run(client, _now)
        key = f"key-{uuid.uuid4().hex}"

        assert hook.send(raw=b'{"email": ', key=key).status_code == 422

        # Тело осталось во входящих, но под собственным ключом, не под ключом отправителя.
        assert run(client, _inbound, key) is None
        evidence = run(client, _malformed_evidence, started)
        assert any(
            e["status"] == "failed" and e["raw_payload"]["_raw"] == '{"email": ' for e in evidence
        )
        # И исправленная доставка с тем же ключом принимается.
        _accepted(hook.send(hook.lead(), key=key))

    def test_nul_and_lone_surrogates_are_not_a_500(self, client, hook) -> None:
        # Postgres не принимает `\u0000` и одиночные суррогаты в JSONB.
        raw = (
            f'{{"email": "{_email()}", "first_name": "Ив\\u0000ан", "comment": "\\ud800 привет"}}'
        ).encode()
        key = f"key-{uuid.uuid4().hex}"

        body_ok = _accepted(hook.send(raw=raw, key=key))

        stored = run(client, _inbound, key)
        assert stored is not None and stored["status"] == "processed"
        assert "\x00" not in json.dumps(stored["raw_payload"], ensure_ascii=False)
        assert body_ok["deal_id"]

    def test_oversized_body_is_refused(self, hook) -> None:
        raw = json.dumps({"email": _email(), "comment": "x" * (300 * 1024)}).encode()

        problem = _problem(hook.send(raw=raw), 413)

        assert problem["code"] == "CRM-1001"


# =============================================================================================
# b) Idempotency-Key
# =============================================================================================


class TestIdempotencyKey:
    def test_missing_key_is_a_validation_error(self, hook) -> None:
        problem = _problem(hook.send(hook.lead(), key=None), 422)

        assert problem["code"] == "CRM-1001"
        assert problem["errors"][0]["field"] == "Idempotency-Key"

    @pytest.mark.parametrize("key", ["", "   ", "\t"], ids=["empty", "spaces", "tab"])
    def test_blank_key_is_a_validation_error(self, hook, key: str) -> None:
        problem = _problem(hook.send(hook.lead(), key=key), 422)

        assert problem["errors"][0]["field"] == "Idempotency-Key"

    def test_key_longer_than_255_characters_is_a_validation_error(self, hook) -> None:
        problem = _problem(hook.send(hook.lead(), key="k" * 256), 422)

        assert problem["code"] == "CRM-1001"
        assert problem["errors"][0]["field"] == "Idempotency-Key"

    def test_key_of_exactly_255_characters_is_accepted(self, client, hook) -> None:
        key = uuid.uuid4().hex + "k" * (255 - 32)

        _accepted(hook.send(hook.lead(), key=key))

        assert run(client, _inbound, key) is not None

    def test_surrounding_spaces_do_not_make_a_different_key(self, client, hook) -> None:
        key = f"key-{uuid.uuid4().hex}"
        first = _accepted(hook.send(hook.lead(), key=key))

        again = _accepted(hook.send(hook.lead(), key=f"  {key}  "))

        assert again["inbound_message_id"] == first["inbound_message_id"]

    def test_control_characters_in_the_key_are_refused(self, hook) -> None:
        problem = _problem(hook.send(hook.lead(), key="key\x01x"), 422)

        assert problem["errors"][0]["field"] == "Idempotency-Key"

    def test_key_is_checked_before_the_signature(self, client, hook) -> None:
        # Неверная подпись + плохой ключ: отвечаем про ключ (422), ничего не записывая.
        problem = _problem(hook.send(hook.lead(), key="k" * 300, signature="sha256=00"), 422)

        assert problem["errors"][0]["field"] == "Idempotency-Key"


# =============================================================================================
# c, d) Подпись
# =============================================================================================


class TestSignature:
    def test_valid_signature_with_and_without_prefix(self, hook) -> None:
        body = hook.lead()
        raw = json.dumps(body).encode()

        with_prefix = hook.send(raw=raw, signature=f"sha256={compute_signature(_SECRET, raw)}")
        bare = hook.send(raw=raw, signature=compute_signature(_SECRET, raw).upper())

        _accepted(with_prefix)
        assert bare.status_code == 200, bare.text  # тот же человек: второй раз — дубль
        assert bare.json()["status"] == "duplicate"

    def test_non_ascii_signature_is_401_not_500(self, hook) -> None:
        response = hook.send(hook.lead(), signature="подпись-не-ASCII".encode())

        assert _problem(response, 401)["code"] == "CRM-1701"

    @pytest.mark.parametrize(
        "signature",
        [b"sha256=\xff\xfe\xfd", b"sha256=\xe9", b"\x80" * 200],
        ids=["utf8-garbage", "latin1", "long-high-bytes"],
    )
    def test_high_bytes_in_the_signature_are_401(self, hook, signature: bytes) -> None:
        assert _problem(hook.send(hook.lead(), signature=signature), 401)["code"] == "CRM-1701"

    @pytest.mark.parametrize("signature", [None, "", "sha256=", "sha256=deadbeef", "wrong"])
    def test_missing_or_wrong_signature_is_401(self, hook, signature: str | None) -> None:
        assert _problem(hook.send(hook.lead(), signature=signature), 401)["code"] == "CRM-1701"

    def test_signature_of_another_secret_is_401(self, hook) -> None:
        response = hook.send(hook.lead(), secret="другой-секрет")

        assert _problem(response, 401)["code"] == "CRM-1701"

    def test_body_changed_after_signing_is_401(self, hook) -> None:
        signed = json.dumps(hook.lead()).encode()
        signature = f"sha256={compute_signature(_SECRET, signed)}"

        response = hook.send(raw=signed.replace(b"lead-", b"evil-"), signature=signature)

        assert _problem(response, 401)["code"] == "CRM-1701"

    def test_wrong_signature_leaves_evidence_and_a_security_event(self, client, hook) -> None:
        key = f"key-{uuid.uuid4().hex}"
        body = hook.lead(first_name="Подделка")

        assert hook.send(body, key=key, signature="sha256=00").status_code == 401

        (event,) = run(client, _signature_events, key)
        assert event["external_id"].startswith("invalid:")
        assert event["status"] == "failed"
        assert event["error"] == "invalid_signature"
        assert event["signature_valid"] is False
        assert body["email"] in event["raw_payload"]["_raw"]
        # Под ключом отправителя — ничего.
        assert run(client, _inbound, key) is None

    def test_wrong_signature_does_not_occupy_the_key(self, client, hook) -> None:
        """Регрессия: запрос с чужой подписью и угаданным ключом «занимал» его, и настоящая
        доставка получала `200 failed`."""
        key = f"key-{uuid.uuid4().hex}"
        body = hook.lead(first_name="Иван", last_name="Иванов")

        assert hook.send(body, key=key, signature="sha256=00").status_code == 401
        real = _accepted(hook.send(body, key=key))

        stored = run(client, _inbound, key)
        assert stored is not None
        assert (stored["status"], stored["signature_valid"]) == ("processed", True)
        assert stored["id"] == real["inbound_message_id"]
        assert real["deal_id"]

    def test_repeated_forgeries_with_one_key_all_answer_401(self, client, hook) -> None:
        key = f"key-{uuid.uuid4().hex}"

        for _ in range(3):
            assert hook.send(hook.lead(), key=key, signature="sha256=00").status_code == 401

        assert len(run(client, _signature_events, key)) == 3

    def test_forged_request_cannot_read_the_result_of_a_real_delivery(self, hook) -> None:
        key = f"key-{uuid.uuid4().hex}"
        _accepted(hook.send(hook.lead(), key=key))

        # Раньше повтор по ключу отдавался до проверки подписи.
        assert hook.send(hook.lead(), key=key, signature="sha256=00").status_code == 401

    def test_inactive_source_answers_503_before_the_signature(self, client, hook) -> None:
        run(client, functools.partial(_prepare, credentials_ref=_SECRET_ENV, active=False))
        key = f"key-{uuid.uuid4().hex}"

        problem = _problem(hook.send(hook.lead(), key=key, signature="sha256=00"), 503)

        assert problem["code"] == "CRM-1703"
        assert run(client, _signature_events, key) == []

    def test_secret_comes_from_settings_when_the_source_has_no_reference(
        self, client, hook, monkeypatch
    ) -> None:
        from app.core.config import get_settings

        run(client, functools.partial(_prepare, credentials_ref=None))
        monkeypatch.setattr(get_settings(), "cms_webhook_secret_ref", _SECRET_ENV)

        _accepted(hook.send(hook.lead()))

    def test_source_reference_wins_over_settings(self, client, hook, monkeypatch) -> None:
        from app.core.config import get_settings

        monkeypatch.setenv("CMS_WEBHOOK_TEST_OTHER", "другой-секрет")
        monkeypatch.setattr(get_settings(), "cms_webhook_secret_ref", "CMS_WEBHOOK_TEST_OTHER")

        _accepted(hook.send(hook.lead()))  # подпись от секрета источника, а не настроек
        assert hook.send(hook.lead(), secret="другой-секрет").status_code == 401

    def test_without_any_secret_nothing_is_accepted(self, client, hook, monkeypatch) -> None:
        from app.core.config import get_settings

        run(client, functools.partial(_prepare, credentials_ref=None))
        monkeypatch.setattr(get_settings(), "cms_webhook_secret_ref", None)

        assert _problem(hook.send(hook.lead()), 401)["code"] == "CRM-1701"


# =============================================================================================
# e) Отказ обработки не теряет тело
# =============================================================================================


class TestFailuresAreRecorded:
    def test_validation_failure_keeps_the_body_with_a_clean_reason(self, client, hook) -> None:
        key = f"key-{uuid.uuid4().hex}"
        body = {"first_name": "Иван", "comment": "перезвоните"}

        problem = _problem(hook.send(body, key=key), 422)

        assert {e["field"] for e in problem["errors"]} == {"email", "phone"}
        stored = run(client, _inbound, key)
        assert stored is not None
        assert stored["status"] == "failed"
        assert stored["raw_payload"] == body
        assert stored["signature_valid"] is True
        assert "email" in stored["error"] and "phone" in stored["error"]
        assert stored["entity_id"] is None

    def test_business_error_of_the_order_flow_is_recorded_too(self, client, hook) -> None:
        key = f"key-{uuid.uuid4().hex}"
        body = hook.lead(**{"Номер заявки": f"ORD-{uuid.uuid4().hex}", "Имя": "И", "Фамилия": "Ф"})

        problem = _problem(hook.send(body, key=key), 422)

        assert problem["detail"] == "Для заказа укажите курс"
        stored = run(client, _inbound, key)
        assert stored is not None and stored["status"] == "failed"
        assert "Для заказа укажите курс" in stored["error"]

    def test_unexpected_error_is_a_generic_500_and_the_body_survives(
        self, client, hook, monkeypatch
    ) -> None:
        from app.modules.crm.service import DealService

        async def broken_create(self, principal, payload, **kwargs):
            raise RuntimeError("INSERT INTO deals (title) VALUES ('секрет') — стек и SQL наружу")

        monkeypatch.setattr(DealService, "create", broken_create)
        before = run(client, _counts)
        key = f"key-{uuid.uuid4().hex}"
        body = hook.lead(first_name="Иван", last_name="Иванов")

        response = hook.send(body, key=key)

        problem = _problem(response, 500)
        assert problem["code"] == "CRM-9000"
        assert "INSERT" not in response.text and "секрет" not in response.text
        stored = run(client, _inbound, key)
        assert stored is not None
        assert stored["status"] == "failed"
        assert stored["raw_payload"] == body
        assert "INSERT" not in stored["error"] and "секрет" not in stored["error"]
        assert "Traceback" not in stored["error"]
        assert "request_id" in stored["error"]
        # Контакт, заведённый до сбоя, откатился вместе с обработкой: остаётся только запись.
        assert run(client, _counts) == before
        assert run(client, _contacts_with_email, body["email"]) == 0

    def test_catalog_error_of_a_server_kind_keeps_its_status(
        self, client, hook, monkeypatch
    ) -> None:
        """Ошибка каталога (не неожиданное исключение) отдаётся как есть — здесь 503 «нет служебной
        учётки»: текст написан нами, без SQL; тело при этом всё равно остаётся во входящих."""
        from app.core.errors import AppError, ErrorCode
        from app.modules.integration import cms as cms_module

        async def no_account(session):
            raise AppError(
                ErrorCode.DEPENDENCY_UNAVAILABLE, "Служебная учётка не найдена", status=503
            )

        monkeypatch.setattr(cms_module, "get_integration_principal", no_account)
        key = f"key-{uuid.uuid4().hex}"

        problem = _problem(hook.send(hook.lead(), key=key), 503)

        assert problem["code"] == "CRM-9503"
        stored = run(client, _inbound, key)
        assert stored is not None and stored["status"] == "failed"
        assert "Служебная учётка не найдена" in stored["error"]

    def test_database_error_does_not_poison_the_evidence(self, client, hook, monkeypatch) -> None:
        """Ошибка БД внутри обработки переводит транзакцию в «сломанное» состояние; запись с
        телом всё равно должна сохраниться (обработка идёт в SAVEPOINT)."""
        from sqlalchemy import text

        from app.modules.crm.service import DealService

        async def db_failure(self, principal, payload, **kwargs):
            await self._session.execute(text("SELECT * FROM table_that_does_not_exist"))

        monkeypatch.setattr(DealService, "create", db_failure)
        key = f"key-{uuid.uuid4().hex}"

        response = hook.send(hook.lead(), key=key)

        problem = _problem(response, 500)
        assert "table_that_does_not_exist" not in response.text
        assert problem["code"] == "CRM-9000"
        stored = run(client, _inbound, key)
        assert stored is not None and stored["status"] == "failed"
        assert "table_that_does_not_exist" not in stored["error"]

    def test_delivery_repeated_after_a_failure_is_processed_again(self, client, hook) -> None:
        key = f"key-{uuid.uuid4().hex}"
        assert hook.send({"first_name": "Иван"}, key=key).status_code == 422
        failed = run(client, _inbound, key)
        assert failed is not None and failed["status"] == "failed"

        accepted = _accepted(hook.send(hook.lead(first_name="Иван"), key=key))

        stored = run(client, _inbound, key)
        assert stored is not None
        assert (stored["status"], stored["error"]) == ("processed", None)
        assert stored["id"] == failed["id"] == accepted["inbound_message_id"]
        assert run(client, _inbound_count, key) == 1

    def test_failure_can_repeat_and_stays_a_single_record(self, client, hook) -> None:
        key = f"key-{uuid.uuid4().hex}"

        for _ in range(3):
            assert hook.send({}, key=key).status_code == 422

        assert run(client, _inbound_count, key) == 1


# =============================================================================================
# f) Контактные данные обязательны
# =============================================================================================


class TestContactData:
    def test_empty_object_is_not_a_nameless_contact(self, client, hook) -> None:
        before = run(client, _counts)

        problem = _problem(hook.send({}), 422)

        assert problem["code"] == "CRM-1001"
        assert problem["detail"] == "Укажите email или телефон"
        assert {e["field"] for e in problem["errors"]} == {"email", "phone"}
        assert run(client, _counts) == before

    @pytest.mark.parametrize(
        "body",
        [
            {"first_name": "Иван", "last_name": "Иванов"},
            {"comment": "перезвоните", "product_name": "Курс"},
            {"email": "", "phone": ""},
            {"email": "   ", "phone": None},
        ],
        ids=["only-name", "only-comment", "empty-strings", "blank-and-null"],
    )
    def test_body_without_email_and_phone_is_refused(self, client, hook, body) -> None:
        before = run(client, _counts)

        problem = _problem(hook.send(body), 422)

        assert {e["field"] for e in problem["errors"]} == {"email", "phone"}
        assert run(client, _counts) == before

    def test_invalid_email_is_refused(self, hook) -> None:
        problem = _problem(hook.send({"email": "not-an-email", "phone": "+79001112233"}), 422)

        assert [e["field"] for e in problem["errors"]] == ["email"]

    def test_invalid_phone_is_refused(self, hook) -> None:
        problem = _problem(hook.send({"email": _email(), "phone": "12345"}), 422)

        assert [e["field"] for e in problem["errors"]] == ["phone"]

    def test_all_problems_are_reported_at_once(self, hook) -> None:
        body = {"email": "bad", "phone": "1", "stream_number": 0, "amount": "abc"}

        problem = _problem(hook.send(body), 422)

        assert {e["field"] for e in problem["errors"]} == {
            "email",
            "phone",
            "stream_number",
            "amount",
        }

    def test_contact_without_a_name_gets_placeholders_and_a_mark(self, client, hook) -> None:
        national = _national()

        body = _accepted(hook.send({"phone": f"8 ({national[:3]}) {national[3:]}"}))

        contact = run(client, _contact, body["contact_id"])
        assert (contact["first_name"], contact["last_name"]) == ("Без имени", "—")
        assert contact["external_ids"]["needs_normalization"] is True
        assert contact["phone"] == f"+7{national}"
        assert contact["source"] == "cms"

    def test_partial_name_is_completed_with_a_placeholder(self, client, hook) -> None:
        body = _accepted(hook.send(hook.lead(last_name="Петров")))

        contact = run(client, _contact, body["contact_id"])
        assert (contact["first_name"], contact["last_name"]) == ("Без имени", "Петров")
        assert contact["external_ids"]["needs_normalization"] is True

    def test_named_contact_is_not_marked(self, client, hook) -> None:
        body = _accepted(hook.send(hook.lead(first_name="Иван", last_name="Иванов")))

        contact = run(client, _contact, body["contact_id"])
        assert "needs_normalization" not in contact["external_ids"]
        assert contact["external_ids"]["cms_lead_id"]

    def test_email_and_phone_are_normalised(self, client, hook) -> None:
        national = _national()
        email = _email()

        body = _accepted(
            hook.send(
                {
                    "first_name": "Иван",
                    "last_name": "Иванов",
                    "email": f"  {email.upper()}  ",
                    "phone": f"+7 ({national[:3]}) {national[3:6]}-{national[6:8]}-{national[8:]}",
                }
            )
        )

        contact = run(client, _contact, body["contact_id"])
        assert contact["email"] == email
        assert contact["phone"] == f"+7{national}"

    def test_phone_given_as_a_number_is_accepted(self, client, hook) -> None:
        national = int(_national())
        phone = 70_000_000_000 + national  # 7 и десять цифр, числом

        body = _accepted(hook.send({"first_name": "Иван", "last_name": "Ив", "phone": phone}))

        assert run(client, _contact, body["contact_id"])["phone"] == f"+7{national}"

    @pytest.mark.parametrize("value", [True, ["a"], {"a": 1}], ids=["bool", "list", "object"])
    def test_wrongly_typed_fields_are_refused(self, hook, value) -> None:
        problem = _problem(hook.send({"email": _email(), "first_name": value}), 422)

        assert [e["field"] for e in problem["errors"]] == ["first_name"]

    def test_over_long_name_is_refused_not_a_500(self, hook) -> None:
        problem = _problem(hook.send(hook.lead(last_name="Я" * 200)), 422)

        assert [e["field"] for e in problem["errors"]] == ["last_name"]


# =============================================================================================
# g) Контракт: английские и русские ключи
# =============================================================================================


class TestContract:
    def test_russian_keys_of_the_payments_file(self, client, hook) -> None:
        order = f"ORD-{uuid.uuid4().hex[:14].upper()}"
        course = _unique_course()
        email = _email()
        national = _national()
        row = {
            "Номер заявки": order,
            "Курс": course,
            "Фамилия": "Осипенко",
            "Имя": "Дарья",
            "Отчество": "Игоревна",
            "Телефон": f"7 ({national[:3]}) {national[3:6]}-{national[6:8]}-{national[8:]}",
            "Email": email.upper(),
            "Номер потока": 3,
        }

        body = _accepted(hook.send(row))

        deal = run(client, _deal, body["deal_id"])
        assert deal["order_number"] == order
        assert deal["deal_type"] == "b2c" and deal["source"] == "cms"
        assert [(line["product_name"], line["stream_number"]) for line in deal["lines"]] == [
            (course, 3)
        ]
        contact = run(client, _contact, body["contact_id"])
        assert (contact["last_name"], contact["first_name"], contact["middle_name"]) == (
            "Осипенко",
            "Дарья",
            "Игоревна",
        )
        assert contact["phone"] == f"+7{national}"
        assert contact["email"] == email
        assert run(client, _inbound, hook.last_key)["message_type"] == "order"

    def test_keys_are_case_and_whitespace_insensitive(self, client, hook) -> None:
        email = _email()

        body = _accepted(
            hook.send(
                {
                    " First_Name ": "Иван",
                    "LAST NAME": "Иванов",
                    "  e-mail": email,
                    "Phone": f"+7{_national()}",
                    "Product-Name": "Что-то",
                }
            )
        )

        contact = run(client, _contact, body["contact_id"])
        assert (contact["first_name"], contact["last_name"], contact["email"]) == (
            "Иван",
            "Иванов",
            email,
        )

    def test_russian_keys_in_any_case(self, client, hook) -> None:
        body = _accepted(
            hook.send(
                {
                    "фамилия": "Сидоров",
                    "ИМЯ": "Пётр",
                    " отчество ": "Петрович",
                    "EMAIL": _email(),
                }
            )
        )

        contact = run(client, _contact, body["contact_id"])
        assert (contact["last_name"], contact["first_name"], contact["middle_name"]) == (
            "Сидоров",
            "Пётр",
            "Петрович",
        )

    @pytest.mark.parametrize("key", ["name", "full_name", "ФИО", "фио"])
    def test_full_name_string_is_split(self, client, hook, key: str) -> None:
        body = _accepted(hook.send(hook.lead(**{key: "Иванов Иван Иванович"})))

        contact = run(client, _contact, body["contact_id"])
        assert (contact["last_name"], contact["first_name"], contact["middle_name"]) == (
            "Иванов",
            "Иван",
            "Иванович",
        )
        assert "needs_normalization" not in contact["external_ids"]

    def test_explicit_name_parts_win_over_the_full_name(self, client, hook) -> None:
        body = _accepted(hook.send(hook.lead(first_name="Пётр", name="Иванов Иван Иванович")))

        contact = run(client, _contact, body["contact_id"])
        assert (contact["last_name"], contact["first_name"], contact["middle_name"]) == (
            "Иванов",
            "Пётр",
            "Иванович",
        )

    @pytest.mark.parametrize("value", [2, "2", " 2 ", 2.0, "2.0"])
    def test_stream_number_forms_are_accepted(self, client, hook, value) -> None:
        course = _unique_course()
        row = hook.lead(**{"Курс": course, "Номер заявки": f"O-{uuid.uuid4().hex}"})
        row.update({"Имя": "И", "Фамилия": "Ф", "stream_number": value})

        body = _accepted(hook.send(row))

        assert run(client, _deal, body["deal_id"])["lines"][0]["stream_number"] == 2

    @pytest.mark.parametrize(
        "value", [0, -1, "0", "abc", 2.5, True, [1], {"n": 1}, 10**12, "inf", "nan"]
    )
    def test_invalid_stream_number_is_refused(self, hook, value) -> None:
        problem = _problem(hook.send(hook.lead(stream_number=value)), 422)

        assert [e["field"] for e in problem["errors"]] == ["stream_number"]

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (1500, "1500.00"),
            (1500.5, "1500.50"),
            ("1500,50", "1500.50"),
            ("15 000,5", "15000.50"),
            ("15 000,00", "15000.00"),
            ("1,500.25", "1500.25"),
            ("1.500,25", "1500.25"),
            ("2500", "2500.00"),
            (0.01, "0.01"),
        ],
    )
    def test_amount_forms_are_accepted(self, client, hook, value, expected: str) -> None:
        body = _accepted(hook.send(hook.lead(amount=value)))

        assert str(run(client, _deal, body["deal_id"])["amount"]) == expected

    @pytest.mark.parametrize(
        "value",
        [0, -5, "0", "-1,5", "abc", "1e999999", "NaN", "inf", 10**13, True, [1], 0.001, "0,001"],
    )
    def test_invalid_amount_is_refused(self, hook, value) -> None:
        problem = _problem(hook.send(hook.lead(amount=value)), 422)

        assert [e["field"] for e in problem["errors"]] == ["amount"]

    def test_blank_optional_values_are_just_absent(self, hook) -> None:
        _accepted(hook.send(hook.lead(amount="", stream_number="", comment="  ", product_name="")))

    @pytest.mark.parametrize(
        "key", ["product_name", "product_code", "program_code", "course", "Курс"]
    )
    def test_every_course_alias_finds_the_catalog_product(self, client, hook, key: str) -> None:
        name = _unique_course()
        run(client, _make_product, name)

        body = _accepted(hook.send(hook.lead(**{key: name})))

        deal = run(client, _deal, body["deal_id"])
        assert [line["product_name"] for line in deal["lines"]] == [name]

    @pytest.mark.parametrize("key", ["order_number", "order_id", "Номер заявки", "номер заявки "])
    def test_every_order_number_alias_makes_an_order(self, client, hook, key: str) -> None:
        order = f"ORD-{uuid.uuid4().hex}"
        row = hook.lead(**{key: order, "Курс": _unique_course(), "Имя": "И", "Фамилия": "Ф"})

        body = _accepted(hook.send(row))

        assert run(client, _deal, body["deal_id"])["order_number"] == order

    def test_order_number_may_be_a_number(self, client, hook) -> None:
        order = 10_000_000_000 + uuid.uuid4().int % 10**9
        row = hook.lead(order_id=order, course=_unique_course(), first_name="И", last_name="Ф")

        body = _accepted(hook.send(row))

        assert run(client, _deal, body["deal_id"])["order_number"] == str(order)

    def test_unknown_keys_are_ignored(self, hook) -> None:
        _accepted(hook.send(hook.lead(utm_source="ads", **{"Что-то": [1, 2]})))

    def test_created_at_of_any_format_is_tolerated(self, hook) -> None:
        for value in ("2026-09-25T12:30:00Z", "вчера", 12345, None):
            _accepted(hook.send(hook.lead(created_at=value)))


# =============================================================================================
# h) Дубликаты контактов
# =============================================================================================


class TestContactDeduplication:
    def test_same_phone_in_another_format_is_the_same_contact(self, client, hook) -> None:
        national = _national()
        first = _accepted(
            hook.send(
                {
                    "phone": f"+7 ({national[:3]}) {national[3:6]}-{national[6:8]}-{national[8:]}",
                    "last_name": "Смирнов",
                    "first_name": "Пётр",
                }
            )
        )

        second = hook.send({"phone": f"8{national}", "last_name": "Смирнов", "first_name": "Пётр"})

        assert second.status_code == 200, second.text
        assert second.json()["contact_id"] == first["contact_id"]
        assert run(client, _contacts_with_phone, f"+7{national}") == 1

    def test_email_case_does_not_make_a_second_contact(self, client, hook) -> None:
        email = _email()
        first = _accepted(
            hook.send({"email": email.upper(), "last_name": "Орлов", "first_name": "О"})
        )

        second = hook.send({"email": email, "last_name": "Орлов", "first_name": "О"})

        assert second.json()["contact_id"] == first["contact_id"]
        assert run(client, _contacts_with_email, email) == 1

    def test_email_wins_even_when_the_surname_differs(self, client, hook) -> None:
        email = _email()
        first = _accepted(hook.send({"email": email, "last_name": "Орлов", "first_name": "О"}))

        second = hook.send({"email": email, "last_name": "Соколов", "first_name": "С"})

        assert second.json()["contact_id"] == first["contact_id"]

    def test_phone_alone_with_another_surname_is_a_different_person(self, client, hook) -> None:
        # Общий номер семьи или кафедры: одного телефона недостаточно.
        national = _national()
        first = _accepted(
            hook.send({"phone": f"+7{national}", "last_name": "Ким", "first_name": "А"})
        )

        second = _accepted(
            hook.send({"phone": f"+7{national}", "last_name": "Пак", "first_name": "Б"})
        )

        assert second["contact_id"] != first["contact_id"]
        assert run(client, _contacts_with_phone, f"+7{national}") == 2

    def test_lead_without_a_surname_is_matched_by_phone(self, client, hook) -> None:
        national = _national()
        first = _accepted(
            hook.send({"phone": f"+7{national}", "last_name": "Ким", "first_name": "А"})
        )

        second = _accepted(hook.send({"phone": f"8 {national}"}), status="duplicate")

        assert second["contact_id"] == first["contact_id"]
        assert run(client, _contacts_with_phone, f"+7{national}") == 1

    def test_existing_contact_is_completed_not_overwritten(self, client, hook) -> None:
        email = _email()
        national = _national()
        first = _accepted(hook.send({"email": email, "last_name": "Орлов", "first_name": "Олег"}))

        _accepted(
            hook.send(
                {
                    "email": email,
                    "phone": f"+7{national}",
                    "last_name": "Другой",
                    "first_name": "Другой",
                    "middle_name": "Иванович",
                }
            ),
            status="duplicate",
        )

        contact = run(client, _contact, first["contact_id"])
        assert (contact["last_name"], contact["first_name"]) == ("Орлов", "Олег")
        assert contact["phone"] == f"+7{national}"  # дозаполнено: раньше телефона не было
        assert contact["middle_name"] == "Иванович"


# =============================================================================================
# i) Лид и заказ
# =============================================================================================


class TestLeadFlow:
    def test_lead_creates_a_deal_in_the_initial_status(self, client, hook) -> None:
        key = f"key-{uuid.uuid4().hex}"
        course = _unique_course()

        body = _accepted(
            hook.send(
                hook.lead(first_name="Иван", last_name="Иванов", product_name=course), key=key
            )
        )

        assert body["deal_id"] and body["contact_id"] and body["inbound_message_id"]
        deal = run(client, _deal, body["deal_id"])
        assert deal["status_id"] == hook.info["initial_status_id"]
        assert deal["deal_type"] == "b2c" and deal["source"] == "cms"
        assert deal["external_ids"] == {"cms_lead_id": key}
        assert deal["title"] == course
        assert deal["order_number"] is None
        assert deal["contact_id"] == body["contact_id"]
        stored = run(client, _inbound, key)
        assert stored is not None
        assert (stored["status"], stored["message_type"]) == ("processed", "lead")
        assert (stored["entity_type"], stored["entity_id"]) == ("deal", body["deal_id"])

    def test_lead_without_a_course_gets_a_generic_title(self, client, hook) -> None:
        key = f"key-{uuid.uuid4().hex}"

        body = _accepted(hook.send(hook.lead(), key=key))

        assert run(client, _deal, body["deal_id"])["title"] == f"Заявка с сайта {key}"

    def test_lead_attaches_the_catalog_product_without_creating_one(self, client, hook) -> None:
        name = _unique_course()
        product_id = run(client, functools.partial(_make_product, name, price="1234.50"))
        before = run(client, _counts)

        body = _accepted(hook.send(hook.lead(course=name.upper(), stream_number=2)))

        deal = run(client, _deal, body["deal_id"])
        assert [(line["product_id"], line["stream_number"]) for line in deal["lines"]] == [
            (product_id, 2)
        ]
        assert str(deal["lines"][0]["price"]) == "1234.50"
        assert run(client, _counts)["products"] == before["products"]

    def test_unknown_course_is_only_the_title_and_no_product_is_invented(
        self, client, hook
    ) -> None:
        course = _unique_course()
        before = run(client, _counts)

        body = _accepted(hook.send(hook.lead(course=course, stream_number=4)))

        deal = run(client, _deal, body["deal_id"])
        assert deal["title"] == course and deal["lines"] == []
        assert run(client, _counts)["products"] == before["products"]
        # Номеру потока некуда лечь — он остаётся в комментарии сделки.
        assert any("Номер потока: 4" in text for text, _s, _a in deal["comments"])

    def test_deal_amount_from_the_body(self, client, hook) -> None:
        body = _accepted(hook.send(hook.lead(amount="990,50")))

        assert str(run(client, _deal, body["deal_id"])["amount"]) == "990.50"

    def test_comment_and_page_become_a_system_comment(self, client, hook) -> None:
        body = _accepted(
            hook.send(
                hook.lead(
                    comment="Позвоните после 18:00",
                    source_url="https://site.example/course/python",
                    created_at="2026-09-25T09:30:00Z",
                    external_id="site-lead-77",
                )
            )
        )

        deal = run(client, _deal, body["deal_id"])
        ((text, is_system, author),) = deal["comments"]
        assert is_system is True and author is None
        assert "Позвоните после 18:00" in text
        assert "https://site.example/course/python" in text
        assert "25.09.2026 09:30" in text
        assert deal["external_ids"]["cms_external_id"] == "site-lead-77"

    def test_no_comment_when_there_is_nothing_to_say(self, client, hook) -> None:
        body = _accepted(hook.send(hook.lead()))

        assert run(client, _deal, body["deal_id"])["comments"] == []

    def test_replayed_delivery_returns_the_stored_result(self, client, hook) -> None:
        key = f"key-{uuid.uuid4().hex}"
        body = hook.lead(first_name="Иван", last_name="Иванов")
        first = _accepted(hook.send(body, key=key))
        before = run(client, _counts)

        again = _accepted(hook.send(hook.lead(first_name="Пётр"), key=key))

        assert again == first
        assert run(client, _counts) == before
        assert run(client, _inbound_count, key) == 1

    def test_replay_returns_the_first_status_even_for_a_duplicate(self, client, hook) -> None:
        email = _email()
        _accepted(hook.send({"email": email, "last_name": "Иванов", "first_name": "И"}))
        key = f"key-{uuid.uuid4().hex}"
        first = _accepted(
            hook.send({"email": email, "last_name": "Иванов", "first_name": "И"}, key=key),
            status="duplicate",
        )

        again = _accepted(hook.send({"email": email}, key=key), status="duplicate")

        assert again == first

    def test_concurrent_delivery_of_one_key_gets_the_winners_result(
        self, client, hook, monkeypatch
    ) -> None:
        """Параллельный запрос успел записать тот же ключ между нашей проверкой и вставкой:
        нарушение уникальности не 500 и не 409, а итог победителя."""
        from app.modules.integration.cms import CmsLeadService

        key = f"key-{uuid.uuid4().hex}"
        winner = _accepted(hook.send(hook.lead(), key=key))
        real_find = CmsLeadService._find_message
        calls = 0

        async def blind_first_time(self, found_key: str):
            nonlocal calls
            calls += 1
            return None if calls == 1 else await real_find(self, found_key)

        monkeypatch.setattr(CmsLeadService, "_find_message", blind_first_time)
        before = run(client, _counts)

        loser = _accepted(hook.send(hook.lead(), key=key))

        assert calls == 2
        assert loser == winner
        assert run(client, _counts) == before

    def test_same_person_and_product_is_a_duplicate(self, client, hook) -> None:
        email = _email()
        name = _unique_course()
        run(client, _make_product, name)
        first = _accepted(
            hook.send({"email": email, "course": name, "first_name": "Иван", "last_name": "Иванов"})
        )

        again = _accepted(
            hook.send(
                {
                    "email": email.upper(),
                    "course": name,
                    "first_name": "Иван",
                    "last_name": "Иванов",
                }
            ),
            status="duplicate",
        )

        assert again["deal_id"] == first["deal_id"]
        assert again["contact_id"] == first["contact_id"]

    def test_the_same_person_asking_for_another_product_gets_a_new_deal(self, client, hook) -> None:
        """Регрессия: любая открытая B2C-сделка человека считалась дублем, и заявка на другой
        курс терялась молча."""
        email = _email()
        python_course, testing_course = _unique_course(), _unique_course()
        run(client, _make_product, python_course)
        run(client, _make_product, testing_course)
        first = _accepted(
            hook.send(
                {"email": email, "course": python_course, "first_name": "И", "last_name": "Ф"}
            )
        )

        second = _accepted(
            hook.send(
                {"email": email, "course": testing_course, "first_name": "И", "last_name": "Ф"}
            )
        )

        assert second["deal_id"] != first["deal_id"]
        assert second["contact_id"] == first["contact_id"]
        deal = run(client, _deal, second["deal_id"])
        assert [line["product_name"] for line in deal["lines"]] == [testing_course]

    def test_lead_without_a_product_repeats_any_open_deal(self, client, hook) -> None:
        email = _email()
        name = _unique_course()
        run(client, _make_product, name)
        first = _accepted(
            hook.send({"email": email, "course": name, "first_name": "И", "last_name": "Ф"})
        )

        again = _accepted(hook.send({"email": email}), status="duplicate")

        assert again["deal_id"] == first["deal_id"]

    def test_unknown_products_are_compared_by_title(self, client, hook) -> None:
        email = _email()
        course = _unique_course()
        first = _accepted(
            hook.send({"email": email, "course": course, "first_name": "И", "last_name": "Ф"})
        )

        same = _accepted(
            hook.send(
                {
                    "email": email,
                    "course": f"  «{course.lower()}»  ",
                    "first_name": "И",
                    "last_name": "Ф",
                }
            ),
            status="duplicate",
        )
        other = _accepted(
            hook.send(
                {"email": email, "course": _unique_course(), "first_name": "И", "last_name": "Ф"}
            )
        )

        assert same["deal_id"] == first["deal_id"]
        assert other["deal_id"] != first["deal_id"]

    def test_repeated_request_adds_a_note_to_the_existing_deal(self, client, hook) -> None:
        email = _email()
        first = _accepted(hook.send({"email": email, "first_name": "И", "last_name": "Ф"}))

        again = _accepted(hook.send({"email": email, "comment": "Жду звонка"}), status="duplicate")

        assert again["deal_id"] == first["deal_id"]
        deal = run(client, _deal, first["deal_id"])
        ((text, _system, _author),) = deal["comments"]
        assert text.startswith("Повторная заявка с сайта") and "Жду звонка" in text

    def test_closed_deal_does_not_swallow_a_new_lead(self, client, hook) -> None:
        email = _email()
        first = _accepted(hook.send({"email": email, "first_name": "И", "last_name": "Ф"}))

        async def close() -> None:
            from app.core.db import session_scope
            from app.modules.crm.models import Deal

            async with session_scope() as session:
                deal = await session.get(Deal, uuid.UUID(first["deal_id"]))
                assert deal is not None
                deal.closed_at = dt.datetime.now(dt.UTC)

        run(client, close)

        again = _accepted(hook.send({"email": email}))

        assert again["deal_id"] != first["deal_id"]
        assert again["contact_id"] == first["contact_id"]


class TestOrderFlow:
    @staticmethod
    def order(**overrides: Any) -> dict[str, Any]:
        national = _national()
        row: dict[str, Any] = {
            "Номер заявки": f"ORD-{uuid.uuid4().hex[:16].upper()}",
            "Курс": _unique_course(),
            "Фамилия": "Осипенко",
            "Имя": "Дарья",
            "Отчество": "Игоревна",
            "Телефон": f"7 ({national[:3]}) {national[3:6]}-{national[6:8]}-{national[8:]}",
            "Email": _email(),
            "Номер потока": 2,
        }
        row.update(overrides)
        return row

    def test_order_creates_a_paid_deal_and_the_missing_product(self, client, hook) -> None:
        row = self.order()
        before = run(client, _counts)

        body = _accepted(hook.send(row))

        deal = run(client, _deal, body["deal_id"])
        assert deal["order_number"] == row["Номер заявки"]
        assert deal["status_id"] == hook.info["paid_status_id"]
        assert deal["custom_fields"]["payment_confirmed"] is True
        assert deal["external_ids"]["order_number"] == row["Номер заявки"]
        assert [(line["product_name"], line["stream_number"]) for line in deal["lines"]] == [
            (row["Курс"], 2)
        ]
        after = run(client, _counts)
        assert after["deals"] == before["deals"] + 1 and after["products"] == before["products"] + 1
        assert after["contacts"] == before["contacts"] + 1

    def test_order_reuses_the_catalog_product_and_its_price(self, client, hook) -> None:
        name = _unique_course()
        product_id = run(client, functools.partial(_make_product, name, price="45000"))
        before = run(client, _counts)

        body = _accepted(hook.send(self.order(**{"Курс": name})))

        deal = run(client, _deal, body["deal_id"])
        assert deal["lines"][0]["product_id"] == product_id
        assert str(deal["amount"]) == "45000.00"
        assert run(client, _counts)["products"] == before["products"]

    def test_amount_from_the_body_wins_over_the_price_list(self, client, hook) -> None:
        name = _unique_course()
        run(client, functools.partial(_make_product, name, price="45000"))

        body = _accepted(hook.send(self.order(**{"Курс": name, "Сумма": "39 900,00"})))

        assert str(run(client, _deal, body["deal_id"])["amount"]) == "39900.00"

    def test_order_is_idempotent_by_its_number(self, client, hook) -> None:
        row = self.order()
        first = _accepted(hook.send(row))
        before = run(client, _counts)

        # Сайт повторил заказ под новым `Idempotency-Key`: сделка та же, статус — дубль.
        second = _accepted(hook.send(row), status="duplicate")

        assert second["deal_id"] == first["deal_id"]
        assert second["contact_id"] == first["contact_id"]
        assert run(client, _counts) == before

    def test_repeated_order_fills_in_what_the_first_one_lacked(self, client, hook) -> None:
        name = _unique_course()
        row = self.order(**{"Курс": name})
        del row["Номер потока"]
        first = _accepted(hook.send(row))
        assert run(client, _deal, first["deal_id"])["lines"][0]["stream_number"] is None

        again = _accepted(hook.send({**row, "Номер потока": 3, "Сумма": 12000}), status="duplicate")

        deal = run(client, _deal, first["deal_id"])
        assert again["deal_id"] == first["deal_id"]
        assert deal["lines"][0]["stream_number"] == 3
        assert str(deal["amount"]) == "12000.00"

    def test_order_of_a_known_person_joins_the_existing_contact(self, client, hook) -> None:
        email = _email()
        lead = _accepted(
            hook.send({"email": email, "first_name": "Дарья", "last_name": "Осипенко"})
        )

        order = _accepted(hook.send(self.order(Email=email.upper())))

        assert order["contact_id"] == lead["contact_id"]
        assert order["deal_id"] != lead["deal_id"]

    def test_order_without_a_course_is_refused(self, hook) -> None:
        row = self.order()
        del row["Курс"]

        problem = _problem(hook.send(row), 422)

        assert problem["detail"] == "Для заказа укажите курс"
        assert [(e["field"], e["reason"]) for e in problem["errors"]] == [
            ("course", "Для заказа укажите курс")
        ]

    def test_order_without_names_is_refused(self, hook) -> None:
        row = self.order()
        del row["Фамилия"], row["Имя"]

        problem = _problem(hook.send(row), 422)

        assert {e["field"] for e in problem["errors"]} == {"last_name", "first_name"}

    def test_order_number_over_64_characters_is_refused(self, hook) -> None:
        problem = _problem(hook.send(self.order(**{"Номер заявки": "O" * 65})), 422)

        assert [e["field"] for e in problem["errors"]] == ["order_number"]

    def test_order_without_contact_data_is_refused(self, hook) -> None:
        row = self.order()
        del row["Телефон"], row["Email"]

        problem = _problem(hook.send(row), 422)

        assert {e["field"] for e in problem["errors"]} == {"email", "phone"}


class TestRateLimit:
    def test_limit_applies_before_anything_else(self, hook, monkeypatch) -> None:
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "integration_webhook_rate_limit_per_min", 2)

        statuses = [hook.send(hook.lead()).status_code for _ in range(3)]

        assert statuses == [200, 200, 429]
