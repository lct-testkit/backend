"""G-1 (QA 20.10): проверка соединения с источником интеграции («Проверить сейчас»).

Раньше кнопка на экране «Интеграции» лишь перечитывала список источников (`GET /sources`) и
ничего не проверяла. Теперь `POST /api/admin/integrations/sources/{code}/check` делает один
лёгкий запрос к внешней системе тем же способом, каким её опрашивают рабочие задачи (адрес
из источника или из окружения, секрет — по `credentials_ref`/переменной окружения), и
сообщает: ответила ли система, за сколько миллисекунд и почему нет.

Проверка ничего не пишет у источника (`last_sync_at`/`last_error` — это итоги синхронизации,
а не проверки связи) и не создаёт сущностей во внешней системе: только чтение (`limit=1`,
у Bitrix24 — `server.time`). Адрес и секрет в ответ не попадают (у Bitrix24 секрет — сам URL).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings
from app.modules.integration.models import IntegrationSource
from app.modules.integration.security import (
    check_outbound_url_resolved,
    resolve_secret,
    same_host,
)

#: Короче рабочего таймаута интеграций (10 с): кнопку нажимает человек и ждёт ответа.
_TIMEOUT = httpx.Timeout(6.0, connect=4.0)

#: Куда стучимся у источников с базовым адресом: безопасное чтение одной записи.
_PROBE_PATHS: dict[str, str] = {"cms": "/api/v1/leads", "lms": "/students/progress"}


@dataclass(slots=True)
class SourceCheckResult:
    ok: bool
    message: str
    duration_ms: int
    http_status: int | None = None


def _elapsed_ms(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


def _failure(message: str, started: float, http_status: int | None = None) -> SourceCheckResult:
    return SourceCheckResult(
        ok=False, message=message, duration_ms=_elapsed_ms(started), http_status=http_status
    )


def _status_message(status: int) -> tuple[bool, str]:
    if 200 <= status < 300:
        return True, "Отвечает"
    if status in (401, 403):
        return False, f"Система отвечает, но отказала в доступе ({status}): проверьте ключ доступа"
    if status == 404:
        return False, "Система отвечает, но нужного адреса нет (404): проверьте адрес источника"
    if status == 429:
        return False, "Система просит не торопиться (429): повторите проверку позже"
    if status >= 500:
        return False, f"Система вернула ошибку ({status}): сбой на стороне источника"
    return False, f"Неожиданный ответ системы ({status})"


async def check_source_connection(
    source: IntegrationSource, settings: Settings, *, client: httpx.AsyncClient | None = None
) -> SourceCheckResult:
    """Один проверочный запрос. Не бросает исключений: любой сбой — `ok=False` с причиной.

    `client` — для тестов (подмена транспорта); в работе клиент создаётся здесь."""
    started = time.monotonic()
    code = source.code
    token: str | None = None
    params: dict[str, Any] | None = None
    try:
        if code == "bitrix24":
            webhook_url = resolve_secret(source.credentials_ref) or resolve_secret(
                settings.bitrix_webhook_url_ref
            )
            if not webhook_url:
                return _failure(
                    "Адрес входящего вебхука Bitrix24 не задан: укажите «Ссылку на секрет»", started
                )
            url = f"{webhook_url.rstrip('/')}/server.time.json"
            method = "POST"
        else:
            default = getattr(settings, f"{code}_base_url", None)
            base_url = source.base_url or default
            if not base_url:
                return _failure("Адрес источника не задан", started)
            token = resolve_secret(getattr(settings, f"{code}_auth_ref", None))
            if token and default and not same_host(base_url, default):
                # Тот же приём, что у отправки событий в LMS: секрет окружения уходит только на
                # хост, заданный окружением, а не на любой адрес из карточки источника.
                token = None
            url = f"{base_url.rstrip('/')}{_PROBE_PATHS[code]}"
            method = "GET"
            params = {"limit": 1}
        try:
            await check_outbound_url_resolved(url, strict=settings.is_prod)
        except ValueError as exc:
            return _failure(f"Адрес отклонён проверкой безопасности: {exc}", started)

        headers = {"Authorization": f"Bearer {token}"} if token else {}
        http = client or httpx.AsyncClient(timeout=_TIMEOUT)
        try:
            response = await http.request(
                method, url, params=params, headers=headers, json={} if method == "POST" else None
            )
        finally:
            if client is None:
                await http.aclose()
        ok, message = _status_message(response.status_code)
        if ok and code == "bitrix24":
            try:
                body: Any = response.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and "error" in body:
                return _failure(
                    "Bitrix24 отклонил запрос: проверьте вебхук и его права",
                    started,
                    response.status_code,
                )
        return SourceCheckResult(
            ok=ok,
            message=message,
            duration_ms=_elapsed_ms(started),
            http_status=response.status_code,
        )
    except httpx.TimeoutException:
        return _failure("Система не ответила за 6 секунд (таймаут)", started)
    except httpx.ConnectError:
        return _failure("Не удалось подключиться: адрес недоступен или не разрешается", started)
    except httpx.HTTPError as exc:
        # Тип ошибки без текста: в нём бывает адрес (у Bitrix24 он же секрет).
        return _failure(f"Сбой соединения ({type(exc).__name__})", started)
