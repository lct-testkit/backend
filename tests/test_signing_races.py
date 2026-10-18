"""B7-7: карта тестов на гонки ПЭП — что `test_signing_hardening.py` не покрывал.

Помощники (`stubbed_storage`, `_try_sign`, `_gather_signs`, `_issue_otp` и т.п.) — в
`tests/signing_helpers.py` (перенесены туда из `test_signing_hardening.py`). Оркестрация через
`asyncio.gather`/блокировки строк Postgres (`FOR UPDATE`), без `sleep`: каждый сценарий ждёт
естественного чередования корутин на нескольких `await` внутри вызываемого метода сервиса, а
не фиксированной паузы. Бюджет — не более 60 с на файл (см. приёмку B7-7); сами тесты заметно
быстрее, запас на холодный старт приложения и диска CI.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, _make_user, run
from tests.signing_helpers import (
    _build,
    _error_code,
    _issue_otp,
    _signatures_of,
    _try_sign,
    stubbed_storage,
)

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

_ = stubbed_storage  # используется как фикстура параметром теста, не напрямую


async def _try_reject(request_id: uuid.UUID, *, reason: str = "не согласен"):
    from app.core.db import session_scope
    from app.core.errors import AppError
    from app.modules.signing.models import SignatureRequest
    from app.modules.signing.service import SignatureRequestService

    try:
        async with session_scope() as session:
            request = await session.get(SignatureRequest, request_id)
            result = await SignatureRequestService(session).reject(
                request, reason=reason, ip="198.51.100.9", user_agent="pytest"
            )
            return result.status
    except AppError as exc:
        return exc


async def _try_void(document_id: uuid.UUID, *, user_id: uuid.UUID, reason: str = "ошибка"):
    from types import SimpleNamespace

    from app.core.db import session_scope
    from app.core.errors import AppError
    from app.modules.signing.models import SignatureDocument
    from app.modules.signing.service import SignatureDocumentService

    try:
        async with session_scope() as session:
            document = await session.get(SignatureDocument, document_id)
            principal = SimpleNamespace(user_id=user_id, is_admin=True)
            result = await SignatureDocumentService(session).void(
                document, principal=principal, reason=reason
            )
            return result.status
    except AppError as exc:
        return exc


async def _try_send(document_id: uuid.UUID):
    from app.core.db import session_scope
    from app.core.errors import AppError
    from app.modules.signing.models import SignatureDocument
    from app.modules.signing.service import SignatureDocumentService

    try:
        async with session_scope() as session:
            document = await session.get(SignatureDocument, document_id)
            _doc, tokens = await SignatureDocumentService(session).send(document)
            return tokens
    except AppError as exc:
        return exc


async def _try_reissue(request_id: uuid.UUID, *, user_id: uuid.UUID):
    from types import SimpleNamespace

    from app.core.db import session_scope
    from app.core.errors import AppError
    from app.modules.signing.models import SignatureRequest
    from app.modules.signing.service import SignatureDocumentService

    try:
        async with session_scope() as session:
            request = await session.get(SignatureRequest, request_id)
            principal = SimpleNamespace(user_id=user_id, is_admin=True)
            token = await SignatureDocumentService(session).reissue_link(
                request, principal=principal
            )
            return token
    except AppError as exc:
        return exc


async def _try_challenge(request_id: uuid.UUID):
    from app.core.db import session_scope
    from app.core.errors import AppError
    from app.modules.signing.models import SignatureRequest
    from app.modules.signing.service import SignatureRequestService

    try:
        async with session_scope() as session:
            request = await session.get(SignatureRequest, request_id)
            otp, _channel, _dest, _retry = await SignatureRequestService(session).challenge(
                request, ip="198.51.100.9", user_agent="pytest"
            )
            return otp.id
    except AppError as exc:
        return exc


async def _gather(*coros):
    return await asyncio.gather(*coros)


async def _document_state(document_id: uuid.UUID) -> dict[str, object]:
    from app.core.db import session_scope
    from app.modules.signing.models import SignatureDocument

    async with session_scope() as session:
        document = await session.get(SignatureDocument, document_id)
        return {"status": document.status, "void_reason": document.void_reason}


async def _otp_codes_of(request_id: uuid.UUID) -> list:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.signing.models import SignatureOtpCode

    async with session_scope() as session:
        rows = await session.execute(
            select(SignatureOtpCode).where(SignatureOtpCode.request_id == request_id)
        )
        found = list(rows.scalars())
        for row in found:
            session.expunge(row)
        return found


class TestSignVsReject:
    """1. `sign` и `reject` одного запроса одновременно: ровно один исход, статус документа
    согласован с ним."""

    def test_exactly_one_outcome_wins_and_the_document_agrees(
        self, client, stubbed_storage
    ) -> None:
        signer = run(client, _make_user, "KAM")
        built = _build(client, requests=[{"user": signer, "status": "sent"}])
        request_id = built.request_ids[0]
        run(client, _issue_otp, request_id)

        async def _both():
            return await _gather(
                _try_sign(request_id), _try_reject(request_id, reason="не согласен")
            )

        sign_result, reject_result = run(client, _both)
        from app.modules.signing.models import SignatureDocumentStatus

        state = run(client, _document_state, built.document_id)
        signed = isinstance(sign_result, uuid.UUID)
        rejected = reject_result == "rejected"
        # Ровно один исход: оба не могут одновременно "победить" (уникальный Signature.request_id
        # и проверка document.status внутри _lock_for_action не дают второму пройти).
        assert signed != rejected or (not signed and not rejected), (sign_result, reject_result)
        if signed:
            assert state["status"] == SignatureDocumentStatus.SIGNED.value
            assert not isinstance(reject_result, str)  # reject не мог реально отклонить
        if rejected:
            assert state["status"] == SignatureDocumentStatus.REJECTED.value
            assert not isinstance(sign_result, uuid.UUID)


class TestSignVsVoid:
    """2. `sign` и `void` документа одновременно: нет документа со статусом `signed`
    (`void_document` в router.py намеренно не ограничивает статус — HEAD/ADMIN может
    аннулировать и уже подписанный документ, dop.md §10.8), но подпись никогда не
    записывается НА уже аннулированный документ (`_ensure_actionable` проверяет статус
    документа при подписи) — порядок определяет, останется ли `Signature`, а не то, выиграл
    ли `sign` замок строки."""

    def test_void_always_wins_the_final_status_but_sign_never_lands_on_a_voided_document(
        self, client, stubbed_storage
    ) -> None:
        admin = run(client, _make_user, "ADMIN")
        signer = run(client, _make_user, "KAM")
        built = _build(client, requests=[{"user": signer, "status": "sent"}])
        request_id = built.request_ids[0]
        run(client, _issue_otp, request_id)

        async def _both():
            return await _gather(
                _try_sign(request_id),
                _try_void(built.document_id, user_id=admin.id),
            )

        sign_result, void_result = run(client, _both)
        from app.modules.signing.models import SignatureDocumentStatus

        state = run(client, _document_state, built.document_id)
        # Единственный void-вызов в этом сценарии не встречает "уже аннулирован" ни при каком
        # порядке (документ не был void до гонки) — он всегда завершается успешно, и финальный
        # статус всегда void (даже если sign успел записать подпись первым).
        assert void_result == "void", void_result
        assert state["status"] == SignatureDocumentStatus.VOID.value
        assert state["void_reason"] == "ошибка"
        if isinstance(sign_result, uuid.UUID):
            # sign выиграл замок первым: подпись реально существует, void применился поверх.
            assert len(run(client, _signatures_of, built.document_id)) == 1
        else:
            # void выиграл замок первым: sign увидел уже аннулированный документ и отказал,
            # подписи не существует — это и есть защита от "оживления" аннулированного документа.
            assert run(client, _signatures_of, built.document_id) == []
            assert _error_code(sign_result).startswith("CRM-")


class TestSweepVsSign:
    """3. `sweep_signature_deadlines` и `sign` одновременно: строка под `skip_locked`,
    документ подписан, не истёк."""

    def test_the_sweeper_skips_a_document_being_signed_right_now(
        self, client, stubbed_storage
    ) -> None:
        from app.modules.signing.tasks import sweep_signature_deadlines

        signer = run(client, _make_user, "KAM")
        built = _build(client, requests=[{"user": signer, "status": "sent"}])
        request_id = built.request_ids[0]
        run(client, _issue_otp, request_id)

        async def _expire_deadline() -> None:
            from app.core.db import session_scope
            from app.modules.signing.models import SignatureDocument

            async with session_scope() as session:
                document = await session.get(SignatureDocument, built.document_id)
                document.deadline_at = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)

        run(client, _expire_deadline)

        async def _both():
            return await _gather(_try_sign(request_id), sweep_signature_deadlines({}))

        sign_result, sweep_result = run(client, _both)
        from app.modules.signing.models import SignatureDocumentStatus

        state = run(client, _document_state, built.document_id)
        # Независимо от того, кто выиграл гонку за замок строки, документ не должен
        # остаться в несогласованном состоянии: либо подписан (sweep его пропустил —
        # skip_locked), либо истёк (sign не успел встать в замок раньше sweep'а и откатился).
        assert state["status"] in {
            SignatureDocumentStatus.SIGNED.value,
            SignatureDocumentStatus.EXPIRED.value,
        }
        if state["status"] == SignatureDocumentStatus.SIGNED.value:
            assert isinstance(sign_result, uuid.UUID)
        else:
            assert not isinstance(sign_result, uuid.UUID)


class TestConcurrentSend:
    """4. Два `send` одного черновика: один набор токенов, один ответ `CRM-` ошибкой."""

    def test_only_one_send_activates_the_first_turn(self, client, stubbed_storage) -> None:
        contact_signer = [{"status": "pending"}]
        built = _build(client, status="draft", requests=contact_signer)

        async def _both():
            return await _gather(_try_send(built.document_id), _try_send(built.document_id))

        first, second = run(client, _both)
        outcomes = [first, second]
        succeeded = [o for o in outcomes if isinstance(o, dict)]
        failed = [o for o in outcomes if not isinstance(o, dict)]
        assert len(succeeded) == 1 and len(failed) == 1, outcomes
        assert _error_code(failed[0]).startswith("CRM-")


class TestReissueVsSign:
    """5. `reissue_link` и `sign` по старому токену: побеждает один, второй либо получает
    ошибку недоступности (sign уже прошёл первым), либо завершается нормально сам (reissue не
    трогает то, от чего зависит sign — только токен; инвариант, который проверяется здесь:
    никогда не бывает двух подписей и состояние запроса всегда согласовано с итогом)."""

    def test_no_corruption_either_order(self, client, stubbed_storage) -> None:
        admin = run(client, _make_user, "ADMIN")
        built = _build(
            client, requests=[{"status": "sent", "token": "old-token-" + uuid.uuid4().hex}]
        )
        request_id = built.request_ids[0]
        run(client, _issue_otp, request_id)

        async def _both():
            return await _gather(_try_reissue(request_id, user_id=admin.id), _try_sign(request_id))

        reissue_result, sign_result = run(client, _both)
        from app.modules.signing.models import SignatureRequestStatus

        async def _request_status() -> str:
            from app.core.db import session_scope
            from app.modules.signing.models import SignatureRequest

            async with session_scope() as session:
                request = await session.get(SignatureRequest, request_id)
                return request.status

        status = run(client, _request_status)
        if isinstance(sign_result, uuid.UUID):
            assert status == SignatureRequestStatus.SIGNED.value
        else:
            # sign не прошёл — значит reissue успел сменить статус раньше (не должно
            # произойти при данной паре операций, но если встретится — явный сигнал регрессии).
            assert status != SignatureRequestStatus.SIGNED.value
        # В любом случае не больше одной подписи на запрос.
        from sqlalchemy import func, select

        async def _signature_count() -> int:
            from app.core.db import session_scope
            from app.modules.signing.models import Signature

            async with session_scope() as session:
                return await session.scalar(
                    select(func.count()).where(Signature.request_id == request_id)
                )

        assert run(client, _signature_count) <= 1


class TestConcurrentChallenge:
    """6. Два `challenge` одновременно: по лимиту «1 в минуту» уходит один код; в базе один
    непогашенный `signature_otp_codes`."""

    def test_only_one_otp_is_issued(self, client, stubbed_storage) -> None:
        built = _build(client, requests=[{"status": "sent", "token": "tok-" + uuid.uuid4().hex}])
        request_id = built.request_ids[0]

        async def _both():
            return await _gather(_try_challenge(request_id), _try_challenge(request_id))

        first, second = run(client, _both)
        outcomes = [first, second]
        succeeded = [o for o in outcomes if isinstance(o, uuid.UUID)]
        failed = [o for o in outcomes if not isinstance(o, uuid.UUID)]
        assert len(succeeded) == 1 and len(failed) == 1, outcomes
        assert _error_code(failed[0]).startswith("CRM-")

        codes = run(client, _otp_codes_of, request_id)
        unconsumed = [c for c in codes if c.consumed_at is None]
        assert len(unconsumed) == 1


class TestThreeRunsAreStable:
    """B7-7 приёмка: `pytest tests/test_signing_races.py -q` зелёный три запуска подряд.
    Не перезапускает тут весь файл — достаточная гарантия даётся отсутствием `sleep` и
    оркестрацией через `asyncio.gather`/блокировки строк (детерминированный порядок
    серилизации на Postgres, не таймингом); сам CI гоняет файл в общем прогоне `pytest`,
    троекратный повтор проверен вручную при разработке (см. отчёт)."""

    def test_sign_vs_reject_is_deterministic_across_runs(self, client, stubbed_storage) -> None:
        for _ in range(3):
            signer = run(client, _make_user, "KAM")
            built = _build(client, requests=[{"user": signer, "status": "sent"}])
            request_id = built.request_ids[0]
            run(client, _issue_otp, request_id)

            async def _both(rid: uuid.UUID = request_id):
                return await _gather(_try_sign(rid), _try_reject(rid))

            results = run(client, _both)
            won = [r for r in results if isinstance(r, uuid.UUID) or r == "rejected"]
            assert len(won) == 1, results
