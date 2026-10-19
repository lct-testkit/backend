"""Почта по SMTP: транспорт, канал уведомлений `email`, ссылка и код подписания.

`smtplib` подменён записывающей заглушкой: сеть не нужна. Сквозные части идут на настоящей
PostgreSQL (`TEST_DATABASE_URL`).
"""

from __future__ import annotations

import datetime as dt
import functools
import re
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
    """Текстовая часть письма (у письма с HTML она одна из двух частей `alternative`)."""
    part = message.get_body(preferencelist=("plain",))
    assert part is not None
    return part.get_content()


def _html(message: EmailMessage) -> str:
    part = message.get_body(preferencelist=("html",))
    assert part is not None
    return part.get_content()


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
        # Имя отправителя: «РТК Школа CRM <crm@…>», кириллица кодируется по RFC 2047.
        assert message["From"] == "РТК Школа CRM <crm@rt-it-school.ru>"
        assert "=?utf-8?" in message.as_string().split("From:", 1)[1].split("\n", 1)[0].lower()
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

        assert smtp.messages[0]["From"] == "РТК Школа CRM <robot@rt-it-school.ru>"

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
        assert "Здравствуйте, Пётр" in _body(message)
        assert _html(message).count("Здравствуйте, Пётр") >= 1
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
        assert message["Subject"] == "Документ на подпись: Договор на подпись"
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


# --- структура письма: multipart, cid, заголовки -----------------------------------------------

_PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


class TestMessageStructure:
    def test_html_letter_is_alternative_with_the_logo_related_to_the_html(
        self, client, smtp
    ) -> None:
        run(
            client,
            functools.partial(
                _send,
                to="a@example.ru",
                subject="Тема",
                text="Текст письма",
                html='<p>Привет</p><img src="cid:logo">',
                inline={"logo": ("image/png", _PNG)},
            ),
        )

        (message,) = smtp.messages
        assert message.get_content_type() == "multipart/alternative"
        plain, related = list(message.iter_parts())
        assert plain.get_content_type() == "text/plain"
        assert plain.get_content().strip() == "Текст письма"
        assert related.get_content_type() == "multipart/related"
        html, image = list(related.iter_parts())
        assert html.get_content_type() == "text/html"
        assert "cid:logo" in html.get_content()
        assert image["Content-ID"] == "<logo>"
        assert image.get_content_type() == "image/png"
        assert image.get_content_disposition() == "inline"
        assert image.get_payload(decode=True) == _PNG

    def test_without_html_the_letter_stays_a_single_text_part(self, client, smtp) -> None:
        run(client, functools.partial(_send, to="a@example.ru", subject="s", text="только текст"))

        (message,) = smtp.messages
        assert not message.is_multipart()
        assert message.get_content_type() == "text/plain"

    def test_the_old_body_argument_is_an_alias_of_text(self, client, smtp) -> None:
        run(client, functools.partial(_send, to="a@example.ru", subject="s", body="по-старому"))

        assert _body(smtp.messages[0]).strip() == "по-старому"

    def test_a_letter_without_any_text_is_refused(self, client, smtp) -> None:
        error = run(client, functools.partial(_send_error, to="a@example.ru", subject="s"))

        assert error.retryable is False and smtp.instances == []

    def test_service_headers(self, client, smtp) -> None:
        run(client, functools.partial(_send, to="a@example.ru", subject="s", text="t"))

        (message,) = smtp.messages
        assert message["Content-Language"] == "ru"
        assert message["Auto-Submitted"] == "auto-generated"
        assert message["Message-ID"].endswith("@rt-it-school.ru>")
        assert message["List-Unsubscribe"] is None

    def test_extra_headers_cannot_inject_new_ones(self, client, smtp) -> None:
        run(
            client,
            functools.partial(
                _send,
                to="a@example.ru",
                subject="s",
                text="t",
                headers={"X-Test": "a\r\nBcc: x@y.z"},
            ),
        )

        (message,) = smtp.messages
        assert message["Bcc"] is None
        assert message["X-Test"].startswith("a")

    def test_the_sender_name_can_be_changed_or_dropped(self, client, smtp, monkeypatch) -> None:
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "smtp_from_name", "Школа")
        run(client, functools.partial(_send, to="a@example.ru", subject="s", text="t"))
        monkeypatch.setattr(get_settings(), "smtp_from_name", "")
        run(client, functools.partial(_send, to="a@example.ru", subject="s", text="t"))

        named, bare = smtp.messages
        assert named["From"] == "Школа <crm@rt-it-school.ru>"
        assert bare["From"] == "crm@rt-it-school.ru"

    def test_a_name_inside_smtp_from_wins_over_the_setting(self, client, smtp, monkeypatch) -> None:
        _configure(monkeypatch, smtp_from="Рассылка <crm@rt-it-school.ru>")

        run(client, functools.partial(_send, to="a@example.ru", subject="s", text="t"))

        assert smtp.messages[0]["From"] == "Рассылка <crm@rt-it-school.ru>"
        assert email_transport.smtp_configured() is True


class TestListUnsubscribe:
    def test_a_notification_letter_has_it_and_is_html(self, client, smtp) -> None:
        from app.core.config import get_settings

        user = TestNotificationChannel._notify_by_email(client)  # noqa: SLF001

        run(client, _dispatch)

        (message,) = smtp.messages
        assert message["To"] == user.email
        assert message["List-Unsubscribe"] == (
            f"<{get_settings().base_url.rstrip('/')}/notifications/settings>"
        )
        assert message.get_content_type() == "multipart/alternative"
        assert "Здравствуйте, Пётр" in _html(message)

    def test_service_letters_do_not_have_it(self, client, smtp) -> None:
        initiator, built = _reissue_scene(client)
        _login(client, initiator)
        client.post(f"/api/signature-requests/{built.request_ids[1]}/reissue-link")
        run(client, _dispatch_otp, "email", "signer@example.ru", "654321")

        link, otp = smtp.messages
        assert link["List-Unsubscribe"] is None and otp["List-Unsubscribe"] is None
        # Но и у них есть HTML-копия с логотипом.
        for message in (link, otp):
            assert 'src="cid:logo"' in _html(message)
            assert message.get_content_type() == "multipart/alternative"


# --- письма-уведомления: кнопка, детали, данные ----------------------------------------------


async def _seed_templates() -> None:
    from app.core.db import session_scope
    from app.modules.notification.seed import seed_notification_templates

    async with session_scope() as session:
        await seed_notification_templates(session)


async def _manager_with_blocked_employee():
    """Руководитель и его заблокированный подчинённый."""
    from app.core.db import session_scope
    from app.modules.identity.models import User

    async with session_scope() as session:
        manager = User(
            keycloak_id=str(uuid.uuid4()),
            email=f"{uuid.uuid4().hex[:12]}@rt-it-school.ru",
            full_name="Руководитель Роман",
            role="HEAD",
            status="active",
            consent_version="1.0",
        )
        session.add(manager)
        await session.flush()
        employee = User(
            keycloak_id=str(uuid.uuid4()),
            email=f"{uuid.uuid4().hex[:12]}@rt-it-school.ru",
            full_name="Петров Пётр Петрович",
            role="KAM",
            status="blocked",
            manager_id=manager.id,
            consent_version="1.0",
        )
        session.add(employee)
        await session.flush()
        session.expunge_all()
        return manager, employee


class TestNoticeLetters:
    def test_a_document_notification_gets_a_button_and_a_table(self, client, smtp) -> None:
        from app.core.config import get_settings

        initiator = run(client, _make_user, "KAM")
        built = _build(client, created_by=initiator.id, status="partially_signed", requests=[])
        code = f"MAIL_{uuid.uuid4().hex[:10].upper()}"
        run(client, _close_other_pending)
        run(
            client,
            functools.partial(
                _add_template, code, "email", subject="Тема", body="Документ ждёт внимания."
            ),
        )
        run(
            client,
            functools.partial(
                _notify,
                initiator.id,
                code,
                entity_type="signature_document",
                entity_id=built.document_id,
            ),
        )

        run(client, _dispatch)

        (message,) = smtp.messages
        html = _html(message)
        url = f"{get_settings().base_url.rstrip('/')}/signing/{built.document_id}"
        assert f'href="{url}"' in html and url in _body(message)
        assert html.count('class="btn-a"') == 1
        assert "Договор на подпись" in html  # строка «Документ» из самой сущности

    def test_a_template_without_an_entity_is_a_plain_paragraph(self, client, smtp) -> None:
        user = TestNotificationChannel._notify_by_email(client)  # noqa: SLF001

        run(client, _dispatch)

        (message,) = smtp.messages
        assert message["To"] == user.email
        assert 'class="btn-a"' not in _html(message)
        assert "None" not in _html(message)

    def test_a_broken_detail_lookup_still_sends_the_letter(self, client, smtp, monkeypatch) -> None:
        from app.modules.notification import notice

        async def boom(*_args, **_kwargs):
            raise RuntimeError("нет сущности")

        monkeypatch.setattr(notice, "_entity_rows", boom)
        user = TestNotificationChannel._notify_by_email(client)  # noqa: SLF001

        run(client, _dispatch)

        (message,) = smtp.messages
        assert message["To"] == user.email and _html(message)

    def test_a_blocked_employee_notice_goes_to_the_manager_with_the_name(
        self, client, smtp
    ) -> None:
        from app.modules.notification.models import NotificationPriority

        run(client, _seed_templates)
        run(client, _close_other_pending)
        manager, employee = run(client, _manager_with_blocked_employee)

        run(
            client,
            functools.partial(
                _notify,
                employee.id,
                "USER_ACCOUNT_BLOCKED",
                priority=NotificationPriority.HIGH,
                payload={"reason": "Нарушение политики"},
            ),
        )
        run(client, _dispatch)

        (message,) = smtp.messages
        assert message["To"] == manager.email
        text, html = _body(message), _html(message)
        assert message["Subject"] == "Сотрудник заблокирован"
        assert "Сотрудник Петров Пётр Петрович заблокирован" in text
        assert "Ваша учётная запись" not in text and "Ваша учётная запись" not in html
        assert "Нарушение политики" in text
        assert "По вопросам обращайтесь к руководителю" not in text

    def test_a_missing_reason_leaves_no_dangling_reason_in_the_defaults(self) -> None:
        """Раньше в тексте оставалось «Причина: .», когда причины не было."""
        from app.modules.notification.seed import _DEFAULT_TEMPLATES
        from app.modules.notification.service import render_template

        checked = 0
        for code, _channel, subject, body in _DEFAULT_TEMPLATES:
            if code not in {"USER_PASSWORD_RESET", "USER_ACCOUNT_BLOCKED"}:
                continue
            for template in (subject, body):
                if template:
                    checked += 1
                    rendered = render_template(template, {})
                    assert "Причина" not in rendered and "None" not in rendered, rendered
        assert checked == 6


class TestPasswordChangedNotice:
    def test_the_time_is_moscow_and_an_internal_ip_is_not_shown(
        self, client, smtp, monkeypatch
    ) -> None:
        from app.modules.identity import router_me
        from tests.crm_helpers import create_user, sign_in

        async def ok(*_args, **_kwargs) -> None:
            return None

        for name in ("set_password", "update_required_actions", "logout_all_sessions"):
            monkeypatch.setattr(router_me.keycloak_client, name, ok)
        monkeypatch.setattr(router_me.keycloak_client, "password_grant", ok)
        run(client, _seed_templates)
        run(client, _close_other_pending)
        user = create_user(client, "KAM")
        sign_in(client, user)

        response = client.post(
            "/api/me/password",
            json={
                "current_password": "Old-Password-12345",
                "new_password": "New-Password-12345",
                "new_password_repeat": "New-Password-12345",
            },
            headers={"X-Real-Ip": "172.18.0.5"},
        )
        assert response.status_code == 200, response.text
        run(client, _dispatch)

        (message,) = [m for m in smtp.messages if m["To"] == user.email]
        text, html = _body(message), _html(message)
        assert "172.18.0.5" not in text and "172.18.0.5" not in html
        assert "IP" not in text
        # «21.10.2026 12:09», а не ISO с поясом.
        assert re.search(r"\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}", text)
        assert not re.search(r"\d{4}-\d{2}-\d{2}T", text + html)
        assert "+00:00" not in text and "+00:00" not in html

    def test_a_public_ip_is_shown(self, client, smtp, monkeypatch) -> None:
        from app.modules.identity import router_me
        from tests.crm_helpers import create_user, sign_in

        async def ok(*_args, **_kwargs) -> None:
            return None

        for name in ("set_password", "update_required_actions", "logout_all_sessions"):
            monkeypatch.setattr(router_me.keycloak_client, name, ok)
        monkeypatch.setattr(router_me.keycloak_client, "password_grant", ok)
        run(client, _seed_templates)
        run(client, _close_other_pending)
        user = create_user(client, "KAM")
        sign_in(client, user)

        client.post(
            "/api/me/password",
            json={
                "current_password": "Old-Password-12345",
                "new_password": "New-Password-12345",
                "new_password_repeat": "New-Password-12345",
            },
            headers={"X-Real-Ip": "93.184.216.34"},
        )
        run(client, _dispatch)

        (message,) = [m for m in smtp.messages if m["To"] == user.email]
        assert "93.184.216.34" in _body(message)


# --- приглашение и сброс пароля шлёт сам backend -------------------------------------------


def _mock_keycloak(monkeypatch) -> None:
    from app.modules.identity.keycloak import keycloak_client

    async def create_user(**_kwargs: object) -> str:
        return str(uuid.uuid4())

    async def noop(*_args: object, **_kwargs: object) -> None:
        return None

    async def forbidden(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("письма Keycloak больше не используются")

    monkeypatch.setattr(keycloak_client, "create_user", create_user)
    for name in (
        "set_realm_role",
        "update_required_actions",
        "logout_all_sessions",
        "delete_user",
        "set_enabled",
    ):
        monkeypatch.setattr(keycloak_client, name, noop)
    monkeypatch.setattr(keycloak_client, "execute_actions_email", forbidden)


def _invite_body(**overrides) -> dict:
    body = {
        "full_name": "Анна Аудитова",
        "email": f"{uuid.uuid4().hex[:10]}@rt-it-school.ru",
        "role": "KAM",
    }
    body.update(overrides)
    return body


def _link_token(message: EmailMessage, base_url: str) -> str:
    match = re.search(rf"{re.escape(base_url.rstrip('/'))}/invite/([\w\-]+)", _body(message))
    assert match, _body(message)
    return match.group(1)


class TestInviteMail:
    def test_the_invite_is_mailed_by_the_backend_with_a_working_link(
        self, client, smtp, monkeypatch
    ) -> None:
        from app.core.config import get_settings
        from tests.crm_helpers import login

        _mock_keycloak(monkeypatch)
        login(client, "ADMIN")
        body = _invite_body()

        response = client.post("/api/admin/users", json=body)

        assert response.status_code == 201, response.text
        data = response.json()
        assert data["invite_email_sent"] is True and data["invite_url"] is None
        (message,) = smtp.messages
        assert message["To"] == body["email"]
        assert message["Subject"] == "Вас пригласили в RTK School CRM"
        assert message["List-Unsubscribe"] is None
        base = get_settings().base_url
        token = _link_token(message, base)
        html = _html(message)
        assert f'href="{base.rstrip("/")}/invite/{token}"' in html
        assert "keycloak" not in message.as_string().lower().replace("rt-it-school", "")
        assert "3 дня" in html and "Менеджер по вузам" in html
        # Ссылка настоящая: страница приглашения её узнаёт.
        checked = client.get(f"/api/auth/invite/{token}")
        assert checked.status_code == 200 and checked.json()["valid"] is True

    def test_without_smtp_the_link_is_returned_to_the_admin(
        self, client, no_smtp, monkeypatch
    ) -> None:
        from tests.crm_helpers import login

        _mock_keycloak(monkeypatch)
        login(client, "ADMIN")

        response = client.post("/api/admin/users", json=_invite_body())

        assert response.status_code == 201, response.text
        data = response.json()
        assert data["invite_email_sent"] is False
        assert data["invite_url"] and "/invite/" in data["invite_url"]
        assert no_smtp.messages == []

    def test_a_failed_send_does_not_break_creation_and_returns_the_link(
        self, client, smtp, monkeypatch
    ) -> None:
        from tests.crm_helpers import login

        _mock_keycloak(monkeypatch)
        smtp.fail_connect = OSError("network unreachable")
        login(client, "ADMIN")

        response = client.post("/api/admin/users", json=_invite_body())

        assert response.status_code == 201, response.text
        data = response.json()
        assert data["invite_email_sent"] is False and data["invite_url"]

    def test_a_resent_invite_is_mailed_too(self, client, smtp, monkeypatch) -> None:
        from tests.crm_helpers import login

        _mock_keycloak(monkeypatch)
        login(client, "ADMIN")
        invited = run(client, _make_user, "KAM", "invited")

        response = client.post(f"/api/admin/users/{invited.id}/invite")

        assert response.status_code == 200, response.text
        data = response.json()
        assert data["invite_email_sent"] is True and data["invite_url"] is None
        (message,) = smtp.messages
        assert message["To"] == invited.email
        assert "/invite/" in _body(message)


class TestResetMail:
    def _reset(self, client, reason: str = "Подозрение на компрометацию"):
        from tests.crm_helpers import login

        login(client, "ADMIN")
        target = run(client, _make_user, "KAM")
        response = client.post(
            f"/api/admin/users/{target.id}/reset-password", json={"reason": reason}
        )
        assert response.status_code == 200, response.text
        return target, response.json()

    def test_one_letter_with_the_link_and_the_reason(self, client, smtp, monkeypatch) -> None:
        from app.core.config import get_settings

        _mock_keycloak(monkeypatch)
        run(client, _close_other_pending)
        target, body = self._reset(client)

        assert body["email_sent"] is True and body["reset_url"] is None
        run(client, _dispatch)  # уведомление не должно породить второе письмо

        (message,) = smtp.messages
        assert message["To"] == target.email
        assert message["Subject"] == "Пароль вашей учётной записи сброшен"
        text, html = _body(message), _html(message)
        token = _link_token(message, get_settings().base_url)
        assert token and "Подозрение на компрометацию" in text and "Подозрение" in html
        assert "1 час" in text
        assert message["List-Unsubscribe"] is None

    def test_the_in_app_notice_stays_and_carries_the_reason(
        self, client, smtp, monkeypatch
    ) -> None:
        _mock_keycloak(monkeypatch)
        run(client, _close_other_pending)
        run(client, _seed_templates)
        target, _ = self._reset(client)

        deliveries = run(client, _deliveries, target.id)

        channels = {d["channel"] for d in deliveries}
        assert channels == {"in_app"}

    def test_without_smtp_the_link_is_returned_and_the_email_channel_stays(
        self, client, no_smtp, monkeypatch
    ) -> None:
        _mock_keycloak(monkeypatch)
        run(client, _seed_templates)
        target, body = self._reset(client)

        assert body["email_sent"] is False
        assert body["reset_url"] and "/invite/" in body["reset_url"]
        assert body["reset_expires_at"]
        assert {d["channel"] for d in run(client, _deliveries, target.id)} == {"in_app", "email"}

    def test_the_reset_link_is_a_working_reset_token(self, client, smtp, monkeypatch) -> None:
        from app.core.config import get_settings

        _mock_keycloak(monkeypatch)
        _, _ = self._reset(client)

        (message,) = smtp.messages
        token = _link_token(message, get_settings().base_url)
        checked = client.get(f"/api/auth/invite/{token}")
        assert checked.status_code == 200 and checked.json()["valid"] is True
