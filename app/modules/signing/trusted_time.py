"""Доверенное время для ПЭП (dop.md §10.4 п.14, §10.8, §13).

До этого спринта источник времени в evidence был жёстко `system_clock`/
`drift_ms=None` — контейнера `ntp` не существовало, реального NTP-запроса
было не с чем сверять (см. историю в `git blame` `signing/service.py`).
Теперь `ntp` — часть стека (docker-compose.yml), и здесь — настоящий
NTPv3-клиент (`ntplib`, UDP/123), а не HTTP-обёртка: источник в evidence
честно звучит как `ntp://...`, потому что им и является.

dop.md §10.8, строка «Отсутствует доверенное время»: рассинхрон > 5 с
блокирует подписание. Недоступность самого NTP-сервера в эту строку
буквально не входит (речь об ИЗМЕРЕННОМ рассинхроне) — трактуем как
деградацию до прежнего честного `system_clock`, а не повод положить весь
поток подписания из-за разовой сетевой заминки мок-контейнера. Тот же
принцип отказоустойчивости, что `integration.bitrix` уже применяет к
недоступности внешнего портала.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import ntplib
import structlog

from app.core.config import get_settings
from app.core.errors import AppError, ErrorCode

logger = structlog.get_logger(__name__)

FALLBACK_SOURCE = "system_clock"


def _query_offset_seconds(host: str, port: int, timeout: float) -> float:
    """Синхронный вызов `ntplib` (нет asyncio-варианта) — `get_trusted_time`
    оборачивает его в `asyncio.to_thread`, чтобы не блокировать event loop
    на время сетевого запроса."""
    response = ntplib.NTPClient().request(host, port=port, version=3, timeout=timeout)
    return float(response.offset)


async def get_trusted_time() -> tuple[dt.datetime, str, int | None]:
    """Возвращает `(текущее время, source, drift_ms)`.

    Не бросает исключение при недоступности NTP — деградирует до
    `system_clock`/`drift_ms=None` (см. докстринг модуля) и пишет
    предупреждение в лог. Бросает `AppError(SIGNATURE_TIME_UNTRUSTED)`
    только когда NTP реально ответил и измеренный рассинхрон превышает
    `settings.signature_max_clock_drift_ms` — единственный сценарий,
    который dop.md §10.8 требует блокировать.
    """
    settings = get_settings()
    now = dt.datetime.now(dt.UTC)
    try:
        offset_seconds = await asyncio.wait_for(
            asyncio.to_thread(
                _query_offset_seconds,
                settings.ntp_host,
                settings.ntp_port,
                settings.ntp_timeout_seconds,
            ),
            timeout=settings.ntp_timeout_seconds + 1,
        )
    except Exception as exc:  # noqa: BLE001 — любая сетевая/протокольная ошибка деградирует одинаково
        logger.warning("ntp_query_failed", host=settings.ntp_host, error=str(exc))
        return now, FALLBACK_SOURCE, None

    drift_ms = round(offset_seconds * 1000)
    if abs(drift_ms) > settings.signature_max_clock_drift_ms:
        logger.warning(
            "ntp_drift_exceeded",
            host=settings.ntp_host,
            drift_ms=drift_ms,
            max_drift_ms=settings.signature_max_clock_drift_ms,
        )
        raise AppError(
            ErrorCode.SIGNATURE_TIME_UNTRUSTED,
            "Рассинхрон часов сервера с доверенным временем превышает допустимый порог",
            extra={"drift_ms": drift_ms, "max_drift_ms": settings.signature_max_clock_drift_ms},
        )
    return now, f"ntp://{settings.ntp_host}", drift_ms
