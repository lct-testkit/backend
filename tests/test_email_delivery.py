"""Почта по SMTP: транспорт, канал уведомлений `email`, ссылка и код подписания.

`smtplib` подменён записывающей заглушкой: сеть не нужна. Сквозные части идут на настоящей
PostgreSQL (`TEST_DATABASE_URL`).
"""

from __future__ import annotations

import datetime as dt
import functools
import smtplib
import uuid
from email.message import EmailMessage
from typing import ClassVar

import pytest
from pydantic import SecretStr

from app.modules.notification import email_transport
from app.modules.notification.email_transport import EmailSendError, _classify
from tests.conftest import TEST_DATABASE_URL, _make_user, run
from tests.signing_helpers import _build, _login
from tests.test_notification_hardening import (
    _add_template,
    _close_other_pending,
    _deliveries,
    _dispatch,
    _notify,
)

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


class FakeSMTP:
    """Записывает сеанс: что вызвали и какие письма ушли. `fail_*` — что бросить на шаге."""

    instances: ClassVar[list[FakeSMTP]] = []
    messages: ClassVar[list[EmailMessage]] = []
    fail_connect: ClassVar[Exception | None] = None
    fail_login: ClassVar[Exception | None] = None
    fail_send: ClassVar[Exception | None] = None

    def __init__(self, host: str, port: int = 0, timeout: float | None = None, **kwargs) -> None:
        if FakeSMTP.fail_connect is not None:
            raise FakeSMTP.fail_connect
        self.host, self.port, self.timeout, self.kwargs = host, port, timeout, kwargs
        self.calls: list[object] = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc_info) -> bool:
        return False

    def ehlo(self) -> None:
        self.calls.append("ehlo")

    def starttls(self, context=None) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append(("login", user, password))
        if FakeSMTP.fail_login is not None:
            raise FakeSMTP.fail_login

    def send_message(self, message: EmailMessage) -> None:
        if FakeSMTP.fail_send is not None:
            raise FakeSMTP.fail_send
        FakeSMTP.messages.append(message)


class FakeSMTPSSL(FakeSMTP):
    pass


def _configure(monkeypatch, **overrides) -> None:
    from app.core.config import get_settings

    settings = get_settings()
    values = {
        "smtp_host": "smtp.test",
        "smtp_port": 587,
        "smtp_user": "robot",
        "smtp_password": SecretStr("s3cret-pass"),
        "smtp_from": "crm@rt-it-school.ru",
        "smtp_starttls": True,
    }
    values.update(overrides)
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value)


@pytest.fixture
def smtp(client, monkeypatch):
    """SMTP настроен, `smtplib` заменён. Зависит от `client`: настройки правятся после старта."""
    FakeSMTP.instances, FakeSMTP.messages = [], []
    FakeSMTP.fail_connect = FakeSMTP.fail_login = FakeSMTP.fail_send = None
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTPSSL)
    _configure(monkeypatch)
    yield FakeSMTP
    FakeSMTP.fail_connect = FakeSMTP.fail_login = FakeSMTP.fail_send = None


@pytest.fixture
def no_smtp(client, monkeypatch):
    _configure(monkeypatch, smtp_host="")
    FakeSMTP.instances, FakeSMTP.messages = [], []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    return FakeSMTP


def _body(message: EmailMessage) -> str:
    return message.get_content()


async def _send(**kwargs) -> None:
    await email_transport.send_email(**kwargs)


async def _send_error(**kwargs) -> EmailSendError:
    try:
        await email_transport.send_email(**kwargs)
    except EmailSendError as exc:
        return exc
    raise AssertionError("ожидалась EmailSendError")


class TestTransport:
    def test_a_letter_goes_out_in_russian_utf8(self, client, smtp) -> None:
        run(
            client,
            functools.partial(_send, to="ivan@example.ru", subject="Тема письма", body="Текст"),
        )

        (message,) = smtp.messages
        assert message["From"] == "crm@rt-it-school.ru"
        assert message["To"] == "ivan@example.ru"
        assert message["Subject"] == "Тема письма"
        assert _body(message).strip() == "Текст"

    def test_starttls_and_login_are_used_when_configured(self, client, smtp) -> None:
        run(client, functools.partial(_send, to="a@example.ru", subject="s", body="b"))

        (session,) = smtp.instances
        assert session.calls == ["ehlo", "starttls", "ehlo", ("login", "robot", "s3cret-pass")]
        assert session.host == "smtp.test" and session.port == 587
        assert session.timeout and session.timeout <= 30

    def test_no_login_without_a_user_and_no_starttls_when_disabled(
        self, client, smtp, monkeypatch
    ) -> None:
        _configure(monkeypatch, smtp_user="", smtp_password=None, smtp_starttls=False)

        run(client, functools.partial(_send, to="a@example.ru", subject="s", body="b"))

        (session,) = smtp.instances
        assert session.calls == ["ehlo"]

    def test_port_465_means_implicit_tls(self, client, smtp, monkeypatch) -> None:
        _configure(monkeypatch, smtp_port=465)

        run(client, functools.partial(_send, to="a@example.ru", subject="s", body="b"))

        (session,) = smtp.instances
        assert isinstance(session, FakeSMTPSSL)
        assert "starttls" not in session.calls

    def test_the_sender_falls_back_to_the_smtp_user(self, client, smtp, monkeypatch) -> None:
        _configure(monkeypatch, smtp_from="", smtp_user="robot@rt-it-school.ru")

        run(client, functools.partial(_send, to="a@example.ru", subject="s", body="b"))

        assert smtp.messages[0]["From"] == "robot@rt-it-school.ru"

    @pytest.mark.parametrize(
        "overrides",
        [{"smtp_host": ""}, {"smtp_host": "  "}, {"smtp_from": "", "smtp_user": ""}],
    )
    def test_without_a_server_or_a_sender_nothing_is_configured(
        self, client, monkeypatch, overrides
    ) -> None:
        _configure(monkeypatch, **overrides)

        assert email_transport.smtp_configured() is False
        error = run(
            client, functools.partial(_send_error, to="a@example.ru", subject="s", body="b")
        )
        assert error.retryable is False

    def test_a_recipient_with_a_line_break_is_refused_before_any_connection(
        self, client, smtp
    ) -> None:
        error = run(
            client,
            functools.partial(_send_error, to="a@example.ru\nBcc: x@y.z", subject="s", body="b"),
        )

        assert error.retryable is False
        assert smtp.instances == []

    def test_a_line_break_in_the_subject_cannot_inject_a_header(self, client, smtp) -> None:
        run(
            client,
            functools.partial(_send, to="a@example.ru", subject="Тема\nBcc: x@y.z", body="b"),
        )

        (message,) = smtp.messages
        assert message["Bcc"] is None

    @pytest.mark.parametrize(
        ("failure", "retryable"),
        [
            (OSError("connection refused"), True),
            (TimeoutError("timed out"), True),
            (smtplib.SMTPServerDisconnected("gone"), True),
            (smtplib.SMTPResponseException(451, b"try later"), True),
            (smtplib.SMTPAuthenticationError(535, b"bad credentials"), True),
            (smtplib.SMTPRecipientsRefused({"ivan@example.ru": (550, b"no such user")}), False),
        ],
    )
    def test_failures_are_classified_and_never_leak_the_server_text(
        self, failure, retryable
    ) -> None:
        error = _classify(failure)

        assert error.retryable is retryable
        assert "ivan@example.ru" not in str(error)
        assert "no such user" not in str(error)

    def test_a_failed_connection_surfaces_as_a_retryable_error(self, client, smtp) -> None:
        smtp.fail_connect = OSError("network unreachable")

        error = run(
            client, functools.partial(_send_error, to="a@example.ru", subject="s", body="b")
        )

        assert error.retryable is True

    def test_a_4xx_reply_carries_its_code(self, client, smtp) -> None:
        smtp.fail_send = smtplib.SMTPResponseException(452, b"mailbox full")

        error = run(
            client, functools.partial(_send_error, to="a@example.ru", subject="s", body="b")
        )

        assert error.retryable is True
        assert "452" in str(error)


class TestNotificationChannel:
    @staticmethod
    def _notify_by_email(client, body: str = "Здравствуйте, {{ name }}"):
        user = run(client, _make_user, "KAM")
        code = f"MAIL_{uuid.uuid4().hex[:10].upper()}"
        run(client, _close_other_pending)
        run(
            client,
            functools.partial(_add_template, code, "email", subject="Тема {{ name }}", body=body),
        )
        run(client, functools.partial(_notify, user.id, code, payload={"name": "Пётр"}))
        return user

    def test_an_email_delivery_is_sent_through_smtp(self, client, smtp) -> None:
        user = self._notify_by_email(client)

        run(client, _dispatch)

        (message,) = smtp.messages
        assert message["To"] == user.email
        assert message["Subject"] == "Тема Пётр"
        assert _body(message).strip() == "Здравствуйте, Пётр"
        (delivery,) = run(client, _deliveries, user.id)
        assert delivery["status"] == "sent"

    def test_a_temporary_failure_keeps_the_delivery_pending(self, client, smtp) -> None:
        smtp.fail_connect = OSError("network unreachable")
        user = self._notify_by_email(client)

        result = run(client, _dispatch)  # цикл не падает

        assert result["sent"] == 0
        (delivery,) = run(client, _deliveries, user.id)
        assert (delivery["status"], delivery["attempt"]) == ("pending", 1)
        assert "OSError" in delivery["error"]

    def test_after_the_last_attempt_the_delivery_is_failed(self, client, smtp) -> None:
        from sqlalchemy import update

        from app.core.config import get_settings
        from app.core.db import session_scope
        from app.modules.notification.models import NotificationDelivery

        smtp.fail_connect = OSError("network unreachable")
        user = self._notify_by_email(client)
        (delivery,) = run(client, _deliveries, user.id)

        async def _last_attempt() -> None:
            async with session_scope() as session:
                await session.execute(
                    update(NotificationDelivery)
                    .where(NotificationDelivery.id == delivery["id"])
                    .values(
                        attempt=get_settings().notification_max_delivery_attempts - 1,
                        created_at=dt.datetime.now(dt.UTC) - dt.timedelta(hours=1),
                    )
                )

        run(client, _last_attempt)
        result = run(client, _dispatch)

        assert result["failed"] == 1
        (delivery,) = run(client, _deliveries, user.id)
        assert delivery["status"] == "failed"

    def test_a_refused_recipient_is_not_retried(self, client, smtp) -> None:
        smtp.fail_send = smtplib.SMTPRecipientsRefused({"x@y.z": (550, b"no such user")})
        user = self._notify_by_email(client)

        run(client, _dispatch)

        (delivery,) = run(client, _deliveries, user.id)
        assert delivery["status"] == "skipped"
        assert "отверг" in delivery["error"]

    def test_without_smtp_the_channel_stays_a_stub(self, client, no_smtp) -> None:
        user = self._notify_by_email(client)

        run(client, _dispatch)

        (delivery,) = run(client, _deliveries, user.id)
        assert delivery["status"] == "skipped"
        assert "не настроен" in delivery["error"]
        assert no_smtp.instances == []


# --- подписание ----------------------------------------------------------------------------


def _reissue_scene(client):
    """Внешний подписант ждёт подписи (`sent`): у контакта из построителя email `p@example.ru`."""
    initiator = run(client, _make_user, "KAM")
    built = _build(
        client,
        created_by=initiator.id,
        status="partially_signed",
        requests=[
            {"user": run(client, _make_user, "KAM"), "status": "signed"},
            {"user": None, "status": "sent", "token": f"old-{uuid.uuid4().hex}"},
        ],
    )
    return initiator, built


class TestSigningMail:
    def test_a_reissued_link_is_also_mailed_to_the_signer(self, client, smtp) -> None:
        initiator, built = _reissue_scene(client)
        _login(client, initiator)

        response = client.post(f"/api/signature-requests/{built.request_ids[1]}/reissue-link")

        assert response.status_code == 200, response.text
        sign_url = response.json()["sign_url"]
        (message,) = smtp.messages
        assert message["To"] == "p@example.ru"
        assert message["Subject"] == "Вам направлен документ на подписание"
        body = _body(message)
        assert sign_url in body
        assert "Договор на подпись" in body
        # Ни имени подписанта, ни его реквизитов в письме нет.
        assert "Пётр" not in body and "Сидоров" not in body and "Подписант" not in body

    def test_the_link_is_the_public_signing_page_of_the_app(self, client, smtp) -> None:
        from app.core.config import get_settings

        initiator, built = _reissue_scene(client)
        _login(client, initiator)

        client.post(f"/api/signature-requests/{built.request_ids[1]}/reissue-link")

        (message,) = smtp.messages
        assert f"{get_settings().base_url.rstrip('/')}/sign/" in _body(message)

    def test_a_mail_failure_does_not_break_reissue(self, client, smtp) -> None:
        smtp.fail_connect = OSError("network unreachable")
        initiator, built = _reissue_scene(client)
        _login(client, initiator)

        response = client.post(f"/api/signature-requests/{built.request_ids[1]}/reissue-link")

        assert response.status_code == 200, response.text
        assert response.json()["sign_url"]  # ссылка инициатору выдана в любом случае

    def test_without_smtp_nothing_is_mailed_and_the_link_is_given_as_before(
        self, client, no_smtp
    ) -> None:
        initiator, built = _reissue_scene(client)
        _login(client, initiator)

        response = client.post(f"/api/signature-requests/{built.request_ids[1]}/reissue-link")

        assert response.status_code == 200, response.text
        assert response.json()["sign_url"]
        assert no_smtp.messages == []

    def test_the_letter_leaves_only_after_the_commit(self, client, smtp) -> None:
        """Письмо уходит после коммита: откат не должен оставить получателю мёртвую ссылку."""
        from app.core.db import session_scope
        from app.modules.signing.models import SignatureRequest
        from app.modules.signing.service import SignatureDocumentService

        _, built = _reissue_scene(client)

        async def queue(*, roll_back: bool) -> None:
            try:
                async with session_scope() as session:
                    service = SignatureDocumentService(session)
                    request = await session.get(SignatureRequest, built.request_ids[1])
                    document = await service.get_or_404(built.document_id)
                    await service._queue_link_email(document, request, "tok")  # noqa: SLF001
                    if roll_back:
                        raise RuntimeError("откат запроса")
            except RuntimeError:
                pass

        run(client, functools.partial(queue, roll_back=True))
        assert smtp.messages == []

        run(client, functools.partial(queue, roll_back=False))
        assert len(smtp.messages) == 1

    def test_sending_a_document_mails_the_first_external_signer(self, client, smtp) -> None:
        from app.core.db import session_scope
        from app.modules.signing.service import SignatureDocumentService

        built = _build(client, status="draft", requests=[{"user": None, "status": "pending"}])

        async def scenario() -> dict:
            async with session_scope() as session:
                service = SignatureDocumentService(session)
                document = await service.get_or_404(built.document_id)
                _, revealed = await service.send(document)
                return revealed

        revealed = run(client, scenario)

        (token,) = revealed.values()
        (message,) = smtp.messages
        assert message["To"] == "p@example.ru"
        assert f"/sign/{token}" in _body(message)


async def _dispatch_otp(channel: str, destination: str, code: str) -> bool:
    from app.core.db import session_scope
    from app.modules.signing.service import SignatureRequestService

    async with session_scope() as session:
        return await SignatureRequestService(session)._dispatch_otp(  # noqa: SLF001
            channel, destination, code
        )


class TestOtpByEmail:
    def test_the_code_is_mailed(self, client, smtp) -> None:
        delivered = run(client, _dispatch_otp, "email", "signer@example.ru", "654321")

        assert delivered is True
        (message,) = smtp.messages
        assert message["To"] == "signer@example.ru"
        assert "654321" in _body(message)

    def test_a_failed_send_is_reported_as_not_delivered(self, client, smtp) -> None:
        smtp.fail_connect = OSError("network unreachable")

        assert run(client, _dispatch_otp, "email", "signer@example.ru", "654321") is False

    def test_without_smtp_email_stays_undeliverable(self, client, no_smtp) -> None:
        assert run(client, _dispatch_otp, "email", "signer@example.ru", "654321") is False
        assert no_smtp.messages == []

    def test_telegram_still_has_no_transport(self, client, smtp) -> None:
        assert run(client, _dispatch_otp, "telegram", "@signer", "654321") is False
        assert smtp.messages == []
