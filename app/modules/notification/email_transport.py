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
from collections.abc import Mapping
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid, parseaddr

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
    """Адрес отправителя (без имени): им же подписан конверт и строится домен `Message-ID`."""
    settings = get_settings()
    configured = (settings.smtp_from or "").strip() or (settings.smtp_user or "").strip()
    return parseaddr(configured)[1] or configured


def _from_header() -> str:
    """`From` с именем: «РТК Школа CRM <crm@…>». Если в `SMTP_FROM` имя уже есть, оно главнее
    настройки `SMTP_FROM_NAME`; пустое имя даёт голый адрес. Кириллица кодируется по RFC 2047."""
    settings = get_settings()
    configured = (settings.smtp_from or "").strip()
    name, address = parseaddr(configured)
    address = address or _sender()
    name = name or (settings.smtp_from_name or "").strip()
    return formataddr((name, address)) if name else address


def smtp_configured() -> bool:
    """Транспорт готов к работе: задан сервер и есть адрес, от имени которого пишем."""
    return bool((get_settings().smtp_host or "").strip() and _sender())


# Тип вложения `cid`: (MIME-тип, содержимое). Встроенные картинки едут внутри HTML-части
# (`multipart/related`), а не отдельным вложением, поэтому клиент показывает их в самом письме.
InlineImages = Mapping[str, tuple[str, bytes]]


def _build_message(
    *,
    sender: str,
    to: str,
    subject: str,
    text: str,
    html: str | None = None,
    inline: InlineImages | None = None,
    headers: Mapping[str, str] | None = None,
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain=_sender().rpartition("@")[2] or None)
    # Автоответы и «отпуск» на служебные письма не нужны.
    message["Auto-Submitted"] = "auto-generated"
    for name, value in (headers or {}).items():
        # Значение с переводом строки — попытка вписать заголовок.
        message[name] = value.replace("\r", " ").replace("\n", " ")
    # base64: тело по-русски, а не каждый сервер принимает 8bit.
    message.set_content(text, charset="utf-8", cte="base64")
    # После `set_content`: он стирает все заголовки `Content-*`.
    message["Content-Language"] = "ru"
    if html is not None:
        # multipart/alternative: клиент без HTML (и поисковик по почте) читает текстовую часть.
        message.add_alternative(html, subtype="html", charset="utf-8", cte="base64")
        if inline:
            html_part = list(message.iter_parts())[-1]
            for cid, (content_type, data) in inline.items():
                maintype, _, subtype = content_type.partition("/")
                html_part.add_related(
                    data,
                    maintype=maintype,
                    subtype=subtype,
                    cid=f"<{cid}>",
                    filename=f"{cid}.{subtype}",
                    disposition="inline",
                )
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


async def send_email(
    *,
    to: str,
    subject: str,
    text: str | None = None,
    html: str | None = None,
    inline: InlineImages | None = None,
    headers: Mapping[str, str] | None = None,
    body: str | None = None,
) -> None:
    """Отправляет письмо. Бросает `EmailSendError`, ничего другого наружу не выпускает.

    `text` — текстовая часть (обязательна: её читают клиенты без HTML); `body` — прежнее имя
    того же аргумента. `html` добавляет вторую часть `multipart/alternative`, `inline` —
    картинки `cid:` для неё (`{"logo": ("image/png", байты)}`), `headers` — дополнительные
    заголовки (например, `List-Unsubscribe`).

    Вызывающий обязан сам проверить `smtp_configured()`: без настроек отправлять некуда."""
    sender = _sender()
    if not (get_settings().smtp_host or "").strip() or not sender:
        raise EmailSendError("SMTP не настроен (нужны SMTP_HOST и SMTP_FROM)", retryable=False)
    if not to or "@" not in to or any(ch in to for ch in "\r\n"):
        # Перевод строки в адресе — попытка вписать заголовок; такого получателя не бывает.
        raise EmailSendError("у получателя нет корректного адреса", retryable=False)
    text_part = text if text is not None else body
    if text_part is None:
        raise EmailSendError("у письма нет текста", retryable=False)

    message = _build_message(
        sender=_from_header(),
        to=to,
        subject=subject.replace("\n", " "),
        text=text_part,
        html=html,
        inline=inline,
        headers=headers,
    )
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
