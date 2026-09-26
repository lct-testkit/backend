"""Отправка писем по SMTP (уведомления канала `email`, ссылки и коды подписания).

`smtplib` синхронный, поэтому вызов уходит в поток (`asyncio.to_thread`): цикл событий API и
воркера не ждёт сетевой обмен. Пока `SMTP_HOST` пуст (или нет адреса отправителя), транспорт не
настроен: `smtp_configured()` возвращает `False`, вызывающий код ведёт себя как до появления
почты (заглушка канала, ссылка подписанта выдаётся инициатору вручную).

Сбой отправки не роняет вызывающий цикл: наружу выходит только `EmailSendError` с признаком
`retryable`. Временные ошибки (обрыв, таймаут, ответ 4xx, отказ входа) допускают повтор — задача
доставки уведомлений повторит их по своему расписанию и после последней попытки пометит
доставку `failed`. Отказ сервера принять адрес получателя повтором не лечится (`retryable=False`).

В письме — только то, что нужно получателю; текст сообщения серверу отдаётся, но в журнал не
попадает: там маскированный адрес и тип ошибки. Тексты ошибок сервера тоже не пишутся (в них бывает
адрес получателя целиком).
"""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import structlog

from app.core.config import get_settings
from app.core.masking import mask_email

logger = structlog.get_logger(__name__)

# Предел на каждую сетевую операцию сокета (соединение, приветствие, отправка). Сообщение из
# нескольких операций укладывается в десятки секунд — воркер не зависает на мёртвом сервере.
_SMTP_TIMEOUT_SECONDS = 15
# Общий предел ожидания результата из потока: страховка поверх пооперационных таймаутов.
_TOTAL_TIMEOUT_SECONDS = 60
# Порт «неявного TLS» (SMTPS): соединение шифруется сразу, без STARTTLS.
_SMTPS_PORT = 465


class EmailSendError(Exception):
    """Письмо не ушло. `retryable` — есть ли смысл повторить позже."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


def _sender() -> str:
    settings = get_settings()
    return (settings.smtp_from or "").strip() or (settings.smtp_user or "").strip()


def smtp_configured() -> bool:
    """Транспорт готов к работе: задан сервер и есть адрес, от имени которого пишем."""
    return bool((get_settings().smtp_host or "").strip() and _sender())


def _build_message(*, sender: str, to: str, subject: str, body: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain=sender.rpartition("@")[2] or None)
    # Автоответы и «отпуск» на служебные письма не нужны.
    message["Auto-Submitted"] = "auto-generated"
    # base64: тело по-русски, а не каждый сервер принимает 8bit.
    message.set_content(body, charset="utf-8", cte="base64")
    return message


def _send_blocking(message: EmailMessage) -> None:
    """Один сеанс SMTP. Вызывается только из потока."""
    settings = get_settings()
    host = settings.smtp_host.strip()
    port = settings.smtp_port
    password = settings.smtp_password.get_secret_value() if settings.smtp_password else ""
    context = ssl.create_default_context()

    smtp: smtplib.SMTP
    if port == _SMTPS_PORT:
        smtp = smtplib.SMTP_SSL(host, port, timeout=_SMTP_TIMEOUT_SECONDS, context=context)
    else:
        smtp = smtplib.SMTP(host, port, timeout=_SMTP_TIMEOUT_SECONDS)
    with smtp:
        smtp.ehlo()
        if settings.smtp_starttls and port != _SMTPS_PORT:
            smtp.starttls(context=context)
            smtp.ehlo()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, password)
        smtp.send_message(message)


def _classify(exc: BaseException) -> EmailSendError:
    """Ошибка `smtplib`/сети → `EmailSendError` без текста сервера (в нём бывает адрес)."""
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return EmailSendError("сервер отверг адрес получателя", retryable=False)
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return EmailSendError("сервер не принял учётные данные SMTP", retryable=True)
    code = getattr(exc, "smtp_code", None)
    if code is not None:
        return EmailSendError(f"SMTP: {type(exc).__name__} (код {code})", retryable=True)
    return EmailSendError(f"SMTP: {type(exc).__name__}", retryable=True)


async def send_email(*, to: str, subject: str, body: str) -> None:
    """Отправляет письмо. Бросает `EmailSendError`, ничего другого наружу не выпускает.

    Вызывающий обязан сам проверить `smtp_configured()`: без настроек отправлять некуда."""
    sender = _sender()
    if not (get_settings().smtp_host or "").strip() or not sender:
        raise EmailSendError("SMTP не настроен (нужны SMTP_HOST и SMTP_FROM)", retryable=False)
    if not to or "@" not in to or any(ch in to for ch in "\r\n"):
        # Перевод строки в адресе — попытка вписать заголовок; такого получателя не бывает.
        raise EmailSendError("у получателя нет корректного адреса", retryable=False)

    message = _build_message(sender=sender, to=to, subject=subject.replace("\n", " "), body=body)
    try:
        await asyncio.wait_for(asyncio.to_thread(_send_blocking, message), _TOTAL_TIMEOUT_SECONDS)
    except EmailSendError:
        raise
    except Exception as exc:  # noqa: BLE001 — сеть и SMTP отдают десятки разных исключений
        failure = _classify(exc)
        logger.warning(
            "email_send_failed",
            to=mask_email(to),
            error=str(failure),
            retryable=failure.retryable,
        )
        raise failure from None
    logger.info("email_sent", to=mask_email(to))
