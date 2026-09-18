"""Проверка входящих вебхуков: HMAC-подпись, резолвинг `credentials_ref`.

`credentials_ref` в `integration_sources`/`cms_webhook_secret_ref`/
`lms_auth_ref` в `config.py` — это **имя переменной окружения**, не сам
секрет (раздел 7.8: «ссылка на секрет, НЕ сам секрет»). Тот же принцип, что
`signature_server_secret` применяет к единственному статическому секрету
подписания, только здесь секретов несколько (один на источник) и они
адресуются по имени, а не захардкожены в `Settings`.
"""

from __future__ import annotations

import hashlib
import hmac
import os

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.models import SecurityEvent, SecurityEventType, Severity

_SIGNATURE_PREFIX = "sha256="


def resolve_secret(credentials_ref: str | None) -> str | None:
    if not credentials_ref:
        return None
    return os.environ.get(credentials_ref)


def compute_signature(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str | None, body: bytes, provided: str | None) -> bool:
    """Раздел 4.14: «HMAC-SHA256 подпись в заголовке». Формат заголовка —
    `sha256=<hex>` (соглашение GitHub/Stripe-style вебхуков; ни один из
    спецификационных документов не фиксирует точный формат — решение
    зафиксировано здесь), голый hex без префикса тоже принимается.

    Секрет не настроен (админ не заполнил `credentials_ref`, dev-стенд без
    реального CMS) → подпись считается недействительной, а не пропущенной:
    молчаливое отключение проверки было бы дырой, которую легко не заметить
    в проде.
    """
    if not secret or not provided:
        return False
    candidate = provided.removeprefix(_SIGNATURE_PREFIX).strip().lower()
    expected = compute_signature(secret, body)
    return hmac.compare_digest(expected, candidate)


async def record_signature_failure(
    session: AsyncSession,
    *,
    source_code: str,
    external_id: str,
    ip: str | None,
    user_agent: str | None,
) -> None:
    """Тот же класс события, что `SIGNATURE_OTP_FAILED` уже пишет для
    неверного OTP при подписании (dop.md §10.4 п.12) — неверная HMAC-подпись
    входящего вебхука точно так же попытка обхода, только со стороны
    внешней системы, а не интерактивного пользователя."""
    session.add(
        SecurityEvent(
            user_id=None,
            event_type=SecurityEventType.INTEGRATION_SIGNATURE_INVALID.value,
            severity=Severity.WARNING.value,
            ip=ip,
            user_agent=user_agent,
            details={"source_code": source_code, "external_id": external_id},
        )
    )
    await session.flush()
