"""Проверка входящих вебхуков: HMAC-подпись, резолвинг `credentials_ref`.

`credentials_ref` в `integration_sources`/`cms_webhook_secret_ref`/
`lms_auth_ref` в `config.py` — это **имя переменной окружения**, не сам
секрет (раздел 7.8: «ссылка на секрет, НЕ сам секрет»). Тот же принцип, что
`signature_server_secret` применяет к единственному статическому секрету
подписания, только здесь секретов несколько (один на источник) и они
адресуются по имени, а не захардкожены в `Settings`.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import os
import socket
import time
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.models import SecurityEvent, SecurityEventType, Severity

_SIGNATURE_PREFIX = "sha256="


def resolve_secret(credentials_ref: str | None) -> str | None:
    if not credentials_ref:
        return None
    return os.environ.get(credentials_ref)


def signing_secret_ref(source: Any, fallback: str | None = None) -> str | None:
    """Имя переменной с секретом подписи ВХОДЯЩИХ вебхуков источника.

    Раньше единственный `credentials_ref` был и ключом подписи вебхука, и (у Bitrix24) адресом
    исходящего вызова с секретом в пути: чтобы задать одно, приходилось отдавать другое. Теперь
    секрет подписи можно вынести в `config.signing_secret_ref`; без него действует прежний
    `credentials_ref`, а затем `fallback` (окружение)."""
    config = getattr(source, "config", None) or {}
    ref = config.get("signing_secret_ref") if isinstance(config, dict) else None
    if isinstance(ref, str) and ref.strip():
        return ref.strip()
    return getattr(source, "credentials_ref", None) or fallback


# --- Адреса внешних систем (SSRF) ----------------------------------------------------------

_URL_MAX = 512  # длина `integration_sources.base_url`
_INTERNAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")


def _forbidden_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address, *, strict: bool
) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    # Link-local (169.254.0.0/16 — там адрес метаданных облака, fe80::/10), неопределённый и
    # многоадресный — никогда: у внешней системы такого адреса не бывает ни в каком окружении.
    if address.is_link_local or address.is_unspecified or address.is_multicast:
        return True
    return strict and not address.is_global


def check_outbound_url(url: str, *, strict: bool) -> str:
    """Адрес внешней системы, к которому воркер будет ходить сам (LMS: с токеном в заголовке).

    Админский `PATCH base_url` раньше принимал что угодно: `file://`, адрес с логином и паролем,
    `http://169.254.169.254/` или внутренний сервис — воркер ходил бы туда, а токен LMS уезжал
    бы вместе с запросом. Разрешены только `http(s)`, без учётных данных в адресе, без адресов
    link-local; при `strict` (профиль prod) — ещё без loopback, частных сетей и внутренних имён.
    Возвращает адрес без пробелов по краям, иначе `ValueError` с причиной для администратора."""
    value = (url or "").strip()
    if not value:
        raise ValueError("Адрес не задан")
    if len(value) > _URL_MAX:
        raise ValueError(f"Адрес длиннее {_URL_MAX} символов")
    if any(ord(char) < 33 or ord(char) == 127 for char in value):
        raise ValueError("Адрес не должен содержать пробелы и управляющие символы")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise ValueError("Адрес не разбирается как URL") from exc
    if parts.scheme not in ("http", "https"):
        raise ValueError("Допустимы только адреса http:// и https://")
    host = parts.hostname
    if not host:
        raise ValueError("В адресе нет хоста")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ValueError("Адрес не должен содержать логин и пароль")
    if port == 0:
        raise ValueError("Порт 0 недопустим")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _forbidden_address(literal, strict=strict):
            raise ValueError("Адрес указывает на внутреннюю или служебную сеть")
    elif strict:
        lowered = host.lower().rstrip(".")
        if lowered == "localhost" or lowered.endswith(_INTERNAL_SUFFIXES):
            raise ValueError("Адрес указывает на внутренний хост")
    return value


async def check_outbound_url_resolved(url: str, *, strict: bool) -> str:
    """`check_outbound_url` плюс проверка того, во что имя хоста разрешается СЕЙЧАС (только при
    `strict`): имя вида `internal.example.com` может вести на `127.0.0.1`. Разрешение имени идёт в
    потоке (`getaddrinfo` блокирует). Проверка повторяется перед каждой отправкой токена: адрес
    у имени мог смениться после сохранения источника."""
    value = check_outbound_url(url, strict=strict)
    if not strict:
        return value
    parts = urlsplit(value)
    host = parts.hostname or ""
    try:
        ipaddress.ip_address(host)
        return value  # адрес-литерал уже проверен выше
    except ValueError:
        pass
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError("Имя хоста не разрешается в адрес") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if _forbidden_address(address, strict=True):
            raise ValueError("Имя хоста разрешается во внутренний адрес")
    return value


def same_host(first: str, second: str) -> bool:
    """Один и тот же хост и порт (порт по умолчанию определяется схемой), регистр имени не важен."""
    a, b = urlsplit(first.strip()), urlsplit(second.strip())
    if not a.hostname or not b.hostname:
        return False
    default = {"http": 80, "https": 443}
    return (a.hostname.lower(), a.port or default.get(a.scheme)) == (
        b.hostname.lower(),
        b.port or default.get(b.scheme),
    )


def compute_signature(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


# Насколько подпись с меткой времени может отличаться от часов сервера (сек.): и «устарела», и
# «из будущего» — отказ. Пять минут — обычное окно для подписанных вебхуков.
TIMESTAMP_TOLERANCE_SECONDS = 300
TIMESTAMP_HEADER = "X-Timestamp"


def verify_signature(
    secret: str | None,
    body: bytes,
    provided: str | None,
    *,
    timestamp: str | None = None,
    require_timestamp: bool = False,
    now: float | None = None,
) -> bool:
    """Раздел 4.14: «HMAC-SHA256 подпись в заголовке». Формат заголовка —
    `sha256=<hex>` (соглашение GitHub/Stripe-style вебхуков; ни один из
    спецификационных документов не фиксирует точный формат — решение
    зафиксировано здесь), голый hex без префикса тоже принимается.

    Секрет не настроен (админ не заполнил `credentials_ref`, dev-стенд без
    реального CMS) → подпись считается недействительной, а не пропущенной:
    молчаливое отключение проверки было бы дырой, которую легко не заметить
    в проде.

    Защита от повтора (необязательная, чтобы не ломать действующих отправителей): если пришёл
    заголовок `X-Timestamp` (unix-секунды), подписывается `<метка>.<тело>`, а метка вне окна в
    пять минут отклоняется. Без заголовка подпись — по одному телу, как раньше; источник с
    `config.require_timestamp = true` такие запросы не принимает. Повтор того же тела с тем же
    ключом идемпотентности и без метки безвреден — вебхуки дедуплицируются по ключу.

    Заголовок приходит от кого угодно и может нести не-ASCII (`X-Signature: подпись`):
    `hmac.compare_digest` на двух `str` такие строки не сравнивает и бросает `TypeError` — то есть
    неверная подпись давала бы 500 вместо 401. Поэтому сравниваются байты.
    """
    if not secret or not provided:
        return False
    signed = body
    if timestamp is not None:
        # Метка времени входит в подписанное сообщение (`<метка>.<тело>`): без этого её мог бы
        # приписать любой, кто перехватил запрос. Устаревшая или «из будущего» метка — отказ, так
        # что перехваченный запрос нельзя проиграть позже окна.
        try:
            sent_at = int(timestamp.strip())
        except ValueError:
            return False
        if abs((time.time() if now is None else now) - sent_at) > TIMESTAMP_TOLERANCE_SECONDS:
            return False
        signed = timestamp.strip().encode("ascii") + b"." + body
    elif require_timestamp:
        # Источник настроен на обязательную метку (`config.require_timestamp`): подпись только по
        # телу, которую можно проиграть, не принимается.
        return False
    candidate = provided.strip().lower().removeprefix(_SIGNATURE_PREFIX).strip()
    expected = compute_signature(secret, signed)
    return hmac.compare_digest(expected.encode("ascii"), candidate.encode("utf-8", "replace"))


async def record_signature_failure(
    session: AsyncSession,
    *,
    source_code: str,
    external_id: str,
    ip: str | None,
    user_agent: str | None,
    details: dict[str, Any] | None = None,
) -> None:
    """Тот же класс события, что `SIGNATURE_OTP_FAILED` уже пишет для
    неверного OTP при подписании (dop.md §10.4 п.12) — неверная HMAC-подпись
    входящего вебхука точно так же попытка обхода, только со стороны
    внешней системы, а не интерактивного пользователя.

    `details` — дополнительные поля события (например, запись во входящих, где
    осталось тело запроса).

    Колонка `ip` — `inet`: значение, которое не разбирается как адрес (имя хоста от прокси,
    `testclient`), ломало бы вставку, то есть неверная подпись давала бы 500 вместо 401."""
    session.add(
        SecurityEvent(
            user_id=None,
            event_type=SecurityEventType.INTEGRATION_SIGNATURE_INVALID.value,
            severity=Severity.WARNING.value,
            ip=_valid_ip(ip),
            user_agent=user_agent,
            details={**(details or {}), "source_code": source_code, "external_id": external_id},
        )
    )
    await session.flush()


def _valid_ip(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None
