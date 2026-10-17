"""B7-5: события безопасности при подозрительных попытках подписи и ввода OTP.

Перебор кода подписи и перебор ссылок — главный вектор атаки на публичные ручки ПЭП; до этой
задачи он был виден только в аудите (`SIGNATURE_OTP_FAILED`), а в журнале `security_events`
(B6-2) — никак. Требует `TEST_DATABASE_URL` (см. докстринг `tests/conftest.py`).
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, _make_user, run
from tests.signing_helpers import _build
from tests.test_signing_hardening import CODE, _issue_otp, _try_sign, stubbed_storage  # noqa: F401

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


async def _security_events(event_type: str) -> list[dict]:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.identity.models import SecurityEvent

    async with session_scope() as session:
        rows = await session.execute(
            select(SecurityEvent).where(SecurityEvent.event_type == event_type)
        )
        return [{"details": r.details, "severity": r.severity, "id": r.id} for r in rows.scalars()]


class TestOtpFailedSecurityEvent:
    def test_three_wrong_codes_give_one_event_without_the_code_or_address(
        self,
        client,
        stubbed_storage,  # noqa: F811
    ) -> None:
        signer = run(client, _make_user, "KAM")
        built = _build(client, requests=[{"user": signer, "status": "sent"}])
        request_id = built.request_ids[0]
        run(client, _issue_otp, request_id)

        for _ in range(3):
            run(client, _try_sign, request_id, "000000")

        events = run(client, _security_events, "SIGNATURE_OTP_FAILED")
        matching = [e for e in events if e["details"].get("request_id") == str(request_id)]
        assert len(matching) == 1
        details = matching[0]["details"]
        assert details["attempts"] == 3
        assert details["channel"] == "email"
        assert matching[0]["severity"] == "warning"
        # Ни код, ни адрес получателя не попадают в детали события.
        assert "000000" not in str(details)
        assert "example.ru" not in str(details)

    def test_a_successful_sign_gives_no_event(self, client, stubbed_storage) -> None:  # noqa: F811
        signer = run(client, _make_user, "KAM")
        built = _build(client, requests=[{"user": signer, "status": "sent"}])
        request_id = built.request_ids[0]
        run(client, _issue_otp, request_id)

        result = run(client, _try_sign, request_id, CODE)
        assert isinstance(result, uuid.UUID)

        events = run(client, _security_events, "SIGNATURE_OTP_FAILED")
        assert not [e for e in events if e["details"].get("request_id") == str(request_id)]


class TestTokenProbeSecurityEvent:
    #: `TestClient` без реального сокета шлёт `request.client.host == "testclient"`, не IP —
    #: `_normalize_ip` отбрасывает его. `X-Real-Ip` — тот же заголовок, с которого
    #: `app.middleware.request_context._client_ip` реально читает адрес за прокси.
    _HEADERS = {"X-Real-Ip": "203.0.113.77"}

    def test_five_bad_tokens_give_one_probe_event(self, client) -> None:
        for _ in range(5):
            response = client.get(
                f"/public/sign/tok-{uuid.uuid4().hex}/file", headers=self._HEADERS
            )
            assert response.status_code == 404, response.text

        events = run(client, _security_events, "SIGNATURE_TOKEN_PROBE")
        assert len(events) >= 1
        assert events[-1]["severity"] == "warning"
        assert events[-1]["details"]["ip"] == "203.0.113.77"

    def test_a_sixth_bad_token_in_the_same_window_does_not_duplicate(self, client) -> None:
        for _ in range(5):
            client.get(f"/public/sign/tok-{uuid.uuid4().hex}/file", headers=self._HEADERS)
        before = len(run(client, _security_events, "SIGNATURE_TOKEN_PROBE"))

        client.get(f"/public/sign/tok-{uuid.uuid4().hex}/file", headers=self._HEADERS)

        after = len(run(client, _security_events, "SIGNATURE_TOKEN_PROBE"))
        assert after == before
