"""Ключ идемпотентности: занятие атомарно, упавший запрос ключ не отравляет.

Найдено внешним тестированием: ключ, зарезервированный в Redis, не снимался при ошибке — повтор с
тем же ключом получал 409 «ещё обрабатывается» на сутки, — а проверка не была атомарной: два
одновременных запроса с одним ключом выполнялись оба.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import login

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _contact_body(tag: str) -> dict:
    return {
        "first_name": "Пётр",
        "last_name": f"Идемпотентов-{tag}",
        "email": f"idem.{tag}@example.ru",
    }


class TestFailedRequestDoesNotPoisonTheKey:
    def test_retry_after_a_failed_request_is_executed_again(self, client) -> None:
        login(client, "ADMIN")
        tag = uuid.uuid4().hex[:8]
        key = f"idem-{uuid.uuid4().hex}"
        body = _contact_body(tag)

        # Первый контакт с этим email заводится штатно.
        first = client.post("/api/contacts", json=body, headers={"Idempotency-Key": f"a-{key}"})
        assert first.status_code == 201, first.text

        # Второй запрос с другим ключом падает на дубле (409 CRM-1301) и должен освободить ключ.
        failed = client.post("/api/contacts", json=body, headers={"Idempotency-Key": key})
        assert failed.status_code == 409 and failed.json()["code"] == "CRM-1301", failed.text

        # Тот же ключ и тело: раньше — 409 CRM-1003 «ещё обрабатывается» на сутки. Теперь запрос
        # выполняется снова и получает настоящий ответ.
        again = client.post("/api/contacts", json=body, headers={"Idempotency-Key": key})
        assert again.status_code == 409 and again.json()["code"] == "CRM-1301", again.text

    def test_success_is_replayed_from_the_stored_response(self, client) -> None:
        login(client, "ADMIN")
        key = f"idem-{uuid.uuid4().hex}"
        body = _contact_body(uuid.uuid4().hex[:8])

        first = client.post("/api/contacts", json=body, headers={"Idempotency-Key": key})
        assert first.status_code == 201, first.text
        replay = client.post("/api/contacts", json=body, headers={"Idempotency-Key": key})
        assert replay.status_code == 201, replay.text
        assert replay.json()["id"] == first.json()["id"]  # тот же ответ, а не второй контакт

    def test_same_key_with_another_body_is_a_conflict(self, client) -> None:
        login(client, "ADMIN")
        key = f"idem-{uuid.uuid4().hex}"
        first = client.post(
            "/api/contacts",
            json=_contact_body(uuid.uuid4().hex[:8]),
            headers={"Idempotency-Key": key},
        )
        assert first.status_code == 201, first.text
        other = client.post(
            "/api/contacts",
            json=_contact_body(uuid.uuid4().hex[:8]),
            headers={"Idempotency-Key": key},
        )
        assert other.status_code == 409 and other.json()["code"] == "CRM-1003", other.text


async def _race(actor_id: uuid.UUID, key: str) -> list[str]:
    """Два запроса с одним ключом доходят до занятия одновременно (оба прошли «поиск»)."""
    from app.core.db import session_scope
    from app.core.errors import AppError
    from app.core.idempotency import IdempotencyGuard

    async def attempt(name: str) -> str:
        async with session_scope() as session:
            guard = IdempotencyGuard(session, actor_id=actor_id)
            body = b'{"same": "body"}'
            assert await guard.lookup(key=key, method="POST", path="/x", body=body) is None
            await asyncio.sleep(0.05)  # оба уже прошли проверку
            try:
                await guard.reserve(key=key, method="POST", path="/x", body=body)
            except AppError as exc:
                return f"{name}:{exc.code}"
            await asyncio.sleep(0.2)  # «операция» идёт, транзакция открыта
            await guard.store(key=key, status=201, body={"ok": True})
            return f"{name}:ok"

    return list(await asyncio.gather(attempt("a"), attempt("b")))


def test_two_parallel_requests_with_one_key_do_not_both_run(client) -> None:
    from tests.conftest import _make_user

    user = run(client, _make_user, "ADMIN")
    outcomes = run(client, _race, user.id, f"race-{uuid.uuid4().hex}")
    winners = [o for o in outcomes if o.endswith(":ok")]
    losers = [o for o in outcomes if o.endswith("CRM-1003")]
    assert len(winners) == 1 and len(losers) == 1, outcomes
