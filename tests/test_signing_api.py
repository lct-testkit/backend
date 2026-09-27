"""Сквозные тесты ПЭП поверх БД (`signing/service.py`, `signing/router.py`,
`signing/public_router.py`) на настоящей PostgreSQL (`TEST_DATABASE_URL`) —
см. докстринг `tests/conftest.py`.

Документы, файлы и подписи заводятся в БД напрямую: S3 и NTP в тестах
недоступны, поэтому сам `seal()` здесь не запускается — проверяется то, что
вокруг него: чтение, проверка файла, попытки кода, ссылка внешнему подписанту.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, _make_user, run
from tests.signing_helpers import _build, _login, _request_state, _sha

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


class TestVerifyByFile:
    """`POST /signatures/verify`: подписанная копия со штампом (`signed-{id}.pdf`)
    узнаётся так же, как исходный PDF."""

    def _verify(self, client, data: bytes):
        return client.post(
            "/api/signatures/verify", files={"file": ("check.pdf", data, "application/pdf")}
        )

    def test_original_pdf_is_recognised(self, client) -> None:
        _login(client, run(client, _make_user, "KAM"))
        built = _build(
            client,
            status="signed",
            requests=[{"user": None, "status": "signed"}],
            with_results=True,
        )

        body = self._verify(client, built.original).json()

        assert body["status"] == "valid"
        assert body["signature_id"] == str(built.signature_ids[0])

    def test_stamped_copy_is_recognised(self, client) -> None:
        _login(client, run(client, _make_user, "KAM"))
        built = _build(
            client,
            status="signed",
            requests=[{"user": None, "status": "signed"}],
            with_results=True,
        )

        body = self._verify(client, built.stamped).json()

        assert body["status"] == "valid"
        assert body["signature_id"] == str(built.signature_ids[0])
        assert body["document_hash"] == _sha(built.original)

    def test_latest_signature_of_the_document_is_reported(self, client) -> None:
        _login(client, run(client, _make_user, "KAM"))
        built = _build(
            client,
            status="signed",
            requests=[{"user": None, "status": "signed"}, {"user": None, "status": "signed"}],
            with_results=True,
        )

        body = self._verify(client, built.stamped).json()

        assert body["signature_id"] == str(built.signature_ids[-1])

    def test_unknown_file_is_a_mismatch(self, client) -> None:
        _login(client, run(client, _make_user, "KAM"))
        _build(
            client,
            status="signed",
            requests=[{"user": None, "status": "signed"}],
            with_results=True,
        )

        assert self._verify(client, b"%PDF-1.4 something else").json()["status"] == "hash_mismatch"


class TestOtpAttemptsLeft:
    """Остаток попыток приходит с сервера — счётчик на странице не расходится
    с реальным после перезагрузки."""

    def _internal(self, client):
        signer = run(client, _make_user, "KAM")
        _login(client, signer)
        built = _build(client, requests=[{"user": signer, "status": "sent"}])
        return f"/api/signature-requests/{built.request_ids[0]}", built.request_ids[0]

    @staticmethod
    def _wrong(code: str | None) -> str:
        return "999999" if code == "000000" else "000000"

    def test_challenge_reports_the_attempt_limit(self, client) -> None:
        base, _ = self._internal(client)

        response = client.post(f"{base}/challenge")

        assert response.status_code == 200, response.text
        assert response.json()["max_attempts"] == 3

    def test_wrong_code_reports_how_many_attempts_are_left(self, client) -> None:
        base, _ = self._internal(client)
        code = client.post(f"{base}/challenge").json()["debug_code"]

        left = []
        for _ in range(2):
            response = client.post(f"{base}/sign", json={"otp": self._wrong(code)})
            assert response.status_code == 422, response.text
            assert response.json()["code"] == "CRM-1503"
            left.append(response.json()["attempts_left"])

        assert left == [2, 1]

    def test_last_wrong_code_locks_the_request(self, client) -> None:
        base, request_id = self._internal(client)
        code = client.post(f"{base}/challenge").json()["debug_code"]

        for _ in range(2):
            client.post(f"{base}/sign", json={"otp": self._wrong(code)})
        last = client.post(f"{base}/sign", json={"otp": self._wrong(code)})

        assert last.status_code == 422, last.text
        assert last.json()["attempts_left"] == 0
        assert run(client, _request_state, request_id)["status"] == "locked"

    def test_public_link_reports_the_same(self, client) -> None:
        token = f"tok-{uuid.uuid4().hex}"
        _build(client, requests=[{"user": None, "status": "sent", "token": token}])
        base = f"/public/sign/{token}"

        challenge = client.post(f"{base}/challenge")
        assert challenge.status_code == 200, challenge.text
        assert challenge.json()["max_attempts"] == 3
        wrong = client.post(
            f"{base}/sign", json={"otp": self._wrong(challenge.json()["debug_code"])}
        )

        assert wrong.status_code == 422, wrong.text
        assert wrong.json()["attempts_left"] == 2


class TestDocumentCard:
    """Карточка документа отдаёт то, что нужно после подписания: файлы результата
    и подписи, из которых строится ссылка на проверку `/verify/{id}`."""

    def test_signed_document_exposes_result_files_and_signatures(self, client) -> None:
        signer = run(client, _make_user, "KAM")
        _login(client, signer)
        built = _build(
            client,
            status="signed",
            requests=[{"user": signer, "status": "signed"}, {"user": None, "status": "signed"}],
            with_results=True,
        )

        response = client.get(f"/api/signature-documents/{built.document_id}")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["signed_file_id"] == str(built.signed_file_id)
        assert body["protocol_file_id"] == str(built.protocol_file_id)
        assert [(item["id"], item["signer_display"]) for item in body["signatures"]] == [
            (str(built.signature_ids[0]), "Подписант 1"),
            (str(built.signature_ids[1]), "Подписант 2"),
        ]
        assert all(item["signed_at"] for item in body["signatures"])

    def test_document_in_progress_has_no_results_yet(self, client) -> None:
        signer = run(client, _make_user, "KAM")
        _login(client, signer)
        built = _build(client, requests=[{"user": signer, "status": "sent"}])

        body = client.get(f"/api/signature-documents/{built.document_id}").json()

        assert body["signed_file_id"] is None
        assert body["protocol_file_id"] is None
        assert body["signatures"] == []

    def test_entity_history_carries_the_same_fields(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        built = _build(
            client,
            status="signed",
            requests=[{"user": None, "status": "signed"}],
            with_results=True,
        )

        response = client.get(
            "/api/signature-documents",
            params={"entity_type": "erasure_request", "entity_id": str(built.entity_id)},
        )

        assert response.status_code == 200, response.text
        (item,) = response.json()["items"]
        assert item["signed_file_id"] == str(built.signed_file_id)
        assert [sig["id"] for sig in item["signatures"]] == [str(built.signature_ids[0])]


class TestDocumentsBatch:
    """`GET /signature-documents/batch` (C-5): вкладка «Документы» знает только id
    (свои созданные, свои задачи на подпись, для аудит-ролей — журнал) и раньше
    добывала карточки N запросами `GET /signature-documents/{id}` — в проде 300
    документов слали 300 GET. Права на каждый id — ровно как у одиночной карточки
    (`TestDocumentCard` выше): недоступный или не найденный документ тихо
    пропускается, запрос в целом не отказывает."""

    def _batch(self, client, ids):
        return client.get(
            "/api/signature-documents/batch",
            params={"ids": ",".join(str(i) for i in ids)},
        )

    def test_returns_only_accessible_documents(self, client) -> None:
        # KAM видит свою (подписант), не видит чужую (не подписант, не ADMIN —
        # entity_type="erasure_request" пускает только ADMIN, dop.md §10 п.
        # `_check_entity_access`) и не видит несуществующую — все три молча
        # опускаются кроме доступной, запрос не отказывает целиком.
        signer = run(client, _make_user, "KAM")
        _login(client, signer)
        mine = _build(client, requests=[{"user": signer, "status": "sent"}])
        someone_elses = _build(client, requests=[{"user": None, "status": "sent"}])
        missing = uuid.uuid4()

        response = self._batch(client, [mine.document_id, someone_elses.document_id, missing])

        assert response.status_code == 200, response.text
        ids = {item["id"] for item in response.json()["items"]}
        assert ids == {str(mine.document_id)}

    def test_admin_sees_everything_in_the_batch(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        a = _build(client, requests=[{"user": None, "status": "sent"}])
        b = _build(
            client,
            status="signed",
            requests=[{"user": None, "status": "signed"}],
            with_results=True,
        )

        response = self._batch(client, [a.document_id, b.document_id])

        assert response.status_code == 200, response.text
        ids = {item["id"] for item in response.json()["items"]}
        assert ids == {str(a.document_id), str(b.document_id)}

    def test_duplicate_ids_are_deduplicated(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        built = _build(client, requests=[{"user": None, "status": "sent"}])

        response = self._batch(client, [built.document_id, built.document_id, built.document_id])

        assert response.status_code == 200, response.text
        assert [item["id"] for item in response.json()["items"]] == [str(built.document_id)]

    def test_limit_is_enforced(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        too_many = [uuid.uuid4() for _ in range(101)]

        response = self._batch(client, too_many)

        assert response.status_code == 422, response.text
        assert response.json()["code"] == "CRM-1001"

    def test_exactly_at_the_limit_is_accepted(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))
        at_limit = [uuid.uuid4() for _ in range(100)]

        response = self._batch(client, at_limit)

        assert response.status_code == 200, response.text
        assert response.json()["items"] == []

    def test_malformed_id_is_rejected_outright(self, client) -> None:
        # Кривой UUID — опечатка вызывающей стороны, а не вопрос доступа: 422 на
        # весь запрос, а не молчаливый пропуск (тем же приёмом, что
        # `identity/router_directory.py`).
        _login(client, run(client, _make_user, "ADMIN"))

        response = client.get("/api/signature-documents/batch", params={"ids": "not-a-uuid"})

        assert response.status_code == 422, response.text
        assert response.json()["code"] == "CRM-1001"

    def test_empty_ids_returns_an_empty_list_not_an_error(self, client) -> None:
        _login(client, run(client, _make_user, "ADMIN"))

        response = client.get("/api/signature-documents/batch", params={"ids": ""})

        assert response.status_code == 200, response.text
        assert response.json()["items"] == []

    def test_permissions_match_the_single_card_endpoint(self, client) -> None:
        # AUDITOR не имеет signature:create (TestPermissionMatrix, test_signing.py) —
        # как и на одиночной карточке, весь запрос отказывает 403, а не «пустой список».
        built = _build(client, requests=[{"user": None, "status": "sent"}])
        _login(client, run(client, _make_user, "AUDITOR"))

        response = self._batch(client, [built.document_id])

        assert response.status_code == 403, response.text

    def test_batch_card_matches_the_single_card(self, client) -> None:
        signer = run(client, _make_user, "KAM")
        _login(client, signer)
        built = _build(
            client,
            status="signed",
            requests=[{"user": signer, "status": "signed"}, {"user": None, "status": "signed"}],
            with_results=True,
        )

        single = client.get(f"/api/signature-documents/{built.document_id}").json()
        (batch_item,) = self._batch(client, [built.document_id]).json()["items"]

        assert batch_item == single


class TestReissueLink:
    """`POST /signature-requests/{id}/reissue-link`: токен внешнего подписанта
    не вернуть (в БД только sha256), а когда очередь доходит до него не из
    `send()`, ссылку не получил никто."""

    def _scene(self, client, *, request_status: str = "sent", external: bool = True):
        initiator = run(client, _make_user, "KAM")
        old = f"old-{uuid.uuid4().hex}"
        signer = None if external else run(client, _make_user, "KAM")
        built = _build(
            client,
            created_by=initiator.id,
            status="partially_signed",
            requests=[
                {"user": run(client, _make_user, "KAM"), "status": "signed"},
                {"user": signer, "status": request_status, "token": old if external else None},
            ],
        )
        return initiator, built, old

    def _reissue(self, client, built):
        return client.post(f"/api/signature-requests/{built.request_ids[1]}/reissue-link")

    def test_initiator_gets_a_fresh_link_and_the_old_one_stops_working(self, client) -> None:
        initiator, built, old = self._scene(client)
        _login(client, initiator)

        response = self._reissue(client, built)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["id"] == str(built.request_ids[1])
        prefix = "http://localhost:8080/sign/"
        assert body["sign_url"].startswith(prefix)
        new = body["sign_url"].removeprefix(prefix)
        assert new and new != old
        assert client.get(f"/public/sign/{old}").json()["code"] == "CRM-1504"
        page = client.get(f"/public/sign/{new}")
        assert page.status_code == 200, page.text
        assert page.json()["my_request_id"] == str(built.request_ids[1])

    def test_reissue_is_written_to_the_audit_log(self, client) -> None:
        from sqlalchemy import select

        from app.core.db import session_scope
        from app.modules.audit.models import AuditLog

        initiator, built, _ = self._scene(client)
        _login(client, initiator)
        assert self._reissue(client, built).status_code == 200

        async def _actions() -> list[str]:
            async with session_scope() as session:
                rows = await session.execute(
                    select(AuditLog.action).where(AuditLog.entity_id == built.request_ids[1])
                )
                return list(rows.scalars())

        assert "SIGNATURE_LINK_REISSUED" in run(client, _actions)

    def test_admin_may_reissue(self, client) -> None:
        _, built, _ = self._scene(client)
        _login(client, run(client, _make_user, "ADMIN"))

        assert self._reissue(client, built).status_code == 200

    def test_a_token_nobody_received_is_replaced(self, client) -> None:
        # Очередь дошла до внешнего подписанта из `_seal()`: токен сгенерирован
        # и никому не отдан — хэш в БД есть, ссылки у инициатора нет.
        initiator, built, old = self._scene(client)
        _login(client, initiator)

        response = self._reissue(client, built)

        assert response.status_code == 200, response.text
        state = run(client, _request_state, built.request_ids[1])
        assert state["token_hash"] != _sha(old.encode())

    def test_someone_else_is_refused(self, client) -> None:
        _, built, old = self._scene(client)
        _login(client, run(client, _make_user, "KAM"))

        response = self._reissue(client, built)

        assert response.status_code == 403, response.text
        state = run(client, _request_state, built.request_ids[1])
        assert state["token_hash"] == _sha(old.encode())

    def test_request_that_has_not_reached_its_turn_is_refused(self, client) -> None:
        initiator, built, _ = self._scene(client, request_status="pending")
        _login(client, initiator)

        response = self._reissue(client, built)

        assert response.status_code == 409, response.text
        assert response.json()["code"] == "CRM-1505"

    @pytest.mark.parametrize("status", ["signed", "rejected", "locked", "void", "expired"])
    def test_finished_request_is_refused(self, client, status: str) -> None:
        initiator, built, _ = self._scene(client, request_status=status)
        _login(client, initiator)

        assert self._reissue(client, built).status_code == 409

    def test_internal_signer_needs_no_link(self, client) -> None:
        initiator, built, _ = self._scene(client, external=False)
        _login(client, initiator)

        response = self._reissue(client, built)

        assert response.status_code == 422, response.text


class TestOtpChannel:
    """Телефон внутреннего подписанта (раньше задать было нечем) переключает
    код подтверждения с email на SMS."""

    PHONE = "+7 (999) 123-45-67"

    def _sms_challenge(self, client, monkeypatch, *, set_phone):
        sent: list[str] = []

        async def fake_send_sms(*, to: str, message: str, base_url: str | None = None) -> str:
            sent.append(to)
            return "msg-1"

        monkeypatch.setattr("app.modules.signing.service.send_sms", fake_send_sms)
        signer = run(client, _make_user, "KAM")
        set_phone(client, signer)
        _login(client, signer)
        built = _build(client, requests=[{"user": signer, "status": "sent"}])
        response = client.post(f"/api/signature-requests/{built.request_ids[0]}/challenge")
        return response, sent

    def test_phone_set_by_an_admin(self, client, monkeypatch) -> None:
        def by_admin(client, signer) -> None:
            _login(client, run(client, _make_user, "ADMIN"))
            response = client.patch(
                f"/api/admin/users/{signer.id}",
                json={"phone": self.PHONE},
                headers={"If-Match": str(signer.version)},
            )
            assert response.status_code == 200, response.text

        response, sent = self._sms_challenge(client, monkeypatch, set_phone=by_admin)

        assert response.status_code == 200, response.text
        assert response.json()["channel"] == "sms"
        assert response.json()["sent_to_masked"].endswith("67")
        assert sent == [self.PHONE]

    def test_phone_set_by_the_signer_himself(self, client, monkeypatch) -> None:
        def by_himself(client, signer) -> None:
            _login(client, signer)
            assert client.patch("/api/me", json={"phone": self.PHONE}).status_code == 200

        response, sent = self._sms_challenge(client, monkeypatch, set_phone=by_himself)

        assert response.json()["channel"] == "sms"
        assert sent == [self.PHONE]

    def test_without_a_phone_the_code_goes_to_email(self, client, monkeypatch) -> None:
        response, sent = self._sms_challenge(client, monkeypatch, set_phone=lambda *_: None)

        assert response.json()["channel"] == "email"
        assert sent == []


class TestEdmAgreementExpiry:
    """Статус `expired` соглашения об ЭДО выставляет задача сверки сроков — по
    `valid_to`, как раньше это считал только интерфейс."""

    @staticmethod
    async def _agreement(valid_to: dt.date | None, status: str = "active") -> uuid.UUID:
        from app.core.db import session_scope
        from app.modules.signing.models import EdmAgreement

        async with session_scope() as session:
            agreement = EdmAgreement(
                party_type="organization",
                party_id=uuid.uuid4(),
                conclusion_method="paper",
                valid_to=valid_to,
                status=status,
                revoked_at=dt.datetime.now(dt.UTC) if status == "revoked" else None,
            )
            session.add(agreement)
            await session.flush()
            return agreement.id

    @staticmethod
    async def _status(agreement_id: uuid.UUID) -> str:
        from app.core.db import session_scope
        from app.modules.signing.models import EdmAgreement

        async with session_scope() as session:
            agreement = await session.get(EdmAgreement, agreement_id)
            return agreement.status

    @staticmethod
    async def _sweep() -> dict[str, int]:
        from app.modules.signing.tasks import sweep_signature_deadlines

        return await sweep_signature_deadlines({})

    def test_agreement_past_its_valid_to_is_expired(self, client) -> None:
        today = dt.date.today()
        ended = run(client, self._agreement, today - dt.timedelta(days=1))

        run(client, self._sweep)

        assert run(client, self._status, ended) == "expired"

    def test_valid_to_is_inclusive(self, client) -> None:
        last_day = run(client, self._agreement, dt.date.today())

        run(client, self._sweep)

        assert run(client, self._status, last_day) == "active"

    def test_open_ended_agreement_stays_active(self, client) -> None:
        open_ended = run(client, self._agreement, None)

        run(client, self._sweep)

        assert run(client, self._status, open_ended) == "active"

    def test_revoked_agreement_stays_revoked(self, client) -> None:
        revoked = run(client, self._agreement, dt.date.today() - dt.timedelta(days=30), "revoked")

        run(client, self._sweep)

        assert run(client, self._status, revoked) == "revoked"

    def test_expiry_is_written_to_the_audit_log_once(self, client) -> None:
        from sqlalchemy import func, select

        from app.core.db import session_scope
        from app.modules.audit.models import AuditLog

        ended = run(client, self._agreement, dt.date.today() - dt.timedelta(days=2))

        async def _entries() -> int:
            async with session_scope() as session:
                return int(
                    (
                        await session.execute(
                            select(func.count(AuditLog.id)).where(
                                AuditLog.action == "EDM_AGREEMENT_EXPIRED",
                                AuditLog.entity_id == ended,
                            )
                        )
                    ).scalar_one()
                )

        run(client, self._sweep)
        run(client, self._sweep)

        assert run(client, _entries) == 1

    def test_expired_agreement_no_longer_opens_external_signing(self, client) -> None:
        from app.core.db import session_scope
        from app.modules.catalog.models import Contact
        from app.modules.signing.service import EdmAgreementService

        async def _lookup() -> bool:
            async with session_scope() as session:
                contact = Contact(first_name="Пётр", last_name="Сидоров")
                session.add(contact)
                await session.flush()
                from app.modules.signing.models import EdmAgreement

                session.add(
                    EdmAgreement(
                        party_type="contact",
                        party_id=contact.id,
                        conclusion_method="paper",
                        valid_to=dt.date.today() - dt.timedelta(days=1),
                    )
                )
                await session.flush()
                found = await EdmAgreementService(session).find_active_for_contact(contact)
                return found is None

        assert run(client, _lookup)


class TestPublicSignRateLimit:
    """Лимит публичных ручек подписи считается по токену: подписанты за одним NAT
    не делят одну минутную квоту, а перебор чужих токенов упирается в мягкий
    лимит по IP."""

    @staticmethod
    def _limit() -> int:
        from app.core.config import get_settings

        return get_settings().public_sign_rate_limit_per_min

    def test_one_signer_is_limited_by_his_own_token(self, client) -> None:
        token = f"tok-{uuid.uuid4().hex}"

        statuses = [client.get(f"/public/sign/{token}").status_code for _ in range(self._limit())]
        blocked = client.get(f"/public/sign/{token}")

        assert set(statuses) == {404}  # токена нет, но до лимита запросы доходят до ручки
        assert blocked.status_code == 429
        assert blocked.json()["code"] == "CRM-8429"
        assert int(blocked.headers["Retry-After"]) >= 1

    def test_a_colleague_behind_the_same_ip_is_not_affected(self, client) -> None:
        first, second = (f"tok-{uuid.uuid4().hex}" for _ in range(2))
        for _ in range(self._limit() + 1):
            client.get(f"/public/sign/{first}")

        assert client.get(f"/public/sign/{first}").status_code == 429
        assert client.get(f"/public/sign/{second}").status_code == 404

    def test_all_four_token_routes_share_the_token_budget(self, client) -> None:
        token = f"tok-{uuid.uuid4().hex}"
        calls = [
            lambda: client.get(f"/public/sign/{token}"),
            lambda: client.post(f"/public/sign/{token}/challenge"),
            lambda: client.post(f"/public/sign/{token}/sign", json={"otp": "123456"}),
            lambda: client.post(f"/public/sign/{token}/reject", json={"reason": "нет"}),
        ]
        for index in range(self._limit()):
            calls[index % 4]()

        assert calls[0]().status_code == 429

    def test_the_ip_wide_limit_is_softer_but_it_exists(self, client) -> None:
        soft_limit = self._limit() * 10
        statuses = {
            client.get(f"/public/sign/tok-{uuid.uuid4().hex}").status_code
            for _ in range(soft_limit)
        }

        assert statuses == {404}
        assert client.get(f"/public/sign/tok-{uuid.uuid4().hex}").status_code == 429

    def test_public_verify_keeps_its_own_ip_bucket(self, client) -> None:
        for _ in range(self._limit()):
            client.get(f"/public/sign/tok-{uuid.uuid4().hex}")

        assert client.get(f"/public/verify/{uuid.uuid4()}").status_code == 200
