"""Клиент мок-шлюза SMS (dop.md §13: `sms-gateway-mock` в карте контейнеров
§2.2, для OTP «в закрытом контуре»). Реализация — `app/mocks/sms_gateway.py`,
отдельный процесс/контейнер, имитирующий внешнего SMS-провайдера.

Подключение реального провайдера позже — замена этого модуля на клиента
конкретного API, вызывающая сторона (`SigningService.challenge`) не
меняется: тот же приём, что `SignatureProvider`/`OrgLookupProvider`
(dop.md §10.3/§11.2) — граница уже проведена там, где спецификация её ждёт.
"""

from __future__ import annotations

import httpx
import structlog

from app.core.config import get_settings

logger = structlog.get_logger(__name__)

_TIMEOUT = httpx.Timeout(5.0, connect=3.0)


async def send_sms(*, to: str, message: str, base_url: str | None = None) -> str | None:
    """Возвращает id сообщения у шлюза или `None` при ошибке доставки.

    Не бросает исключение: OTP-запись в БД уже создана и содержит рабочий
    код к моменту вызова (см. `SigningService.challenge`) — недоступность
    мок-шлюза не должна валить весь цикл подписания, тот же принцип
    деградации, что `trusted_time.get_trusted_time` применяет к NTP.

    `base_url` по умолчанию берётся из настроек (реальный вызывающий код в
    `signing.service` его не передаёт) — явный параметр только для тестов,
    тот же приём, что `BitrixClient.__init__(webhook_base_url)` в
    `integration.bitrix` использует для той же цели.
    """
    url = base_url if base_url is not None else get_settings().sms_gateway_url
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(
                f"{url}/send", json={"to": to, "message": message}
            )
            response.raise_for_status()
            return response.json().get("id")
    except httpx.HTTPError as exc:
        logger.warning("sms_gateway_unreachable", error=type(exc).__name__)
        return None
