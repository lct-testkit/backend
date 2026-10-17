"""Общий HTTP-приём исходящих вызовов интеграций (B5-2).

`_http_errors_without_url` жила только в `integration.lms` — B5-2 заводит второго клиента
(`CmsClient`, `integration.cms_client`), которому нужен тот же приём: наружу уходит только
операция и статус/тип сбоя, без адреса источника (он может быть задан с логином или ключом в
строке — `integration/security.py:_PROD_FORBIDDEN_HOSTS` и соседи рассчитывают, что адрес
внешней системы никогда не попадает в лог/ответ API). `httpx.HTTPStatusError.raise_for_status()`
вшивает `response.url` в текст сообщения, и без этой обёртки адрес уходил бы в
`outbox_events.last_error`/`integration_sources.last_run` и оттуда в ответ админской ручки.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

#: Таймаут исходящего вызова интеграции — те же 10/5 с, что уже были у `LmsClient`.
TIMEOUT = httpx.Timeout(10.0, connect=5.0)


@asynccontextmanager
async def http_errors_without_url(source: str, operation: str) -> AsyncIterator[None]:
    """`source` — код источника (`lms`/`cms`) для текста ошибки и метрик."""
    try:
        yield
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(
            f"{source} {operation} http error: "
            f"{exc.response.status_code} {exc.response.reason_phrase}"
        ) from None
    except httpx.HTTPError as exc:
        raise RuntimeError(f"{source} {operation} request failed: {type(exc).__name__}") from None
