"""Мок SMS-шлюза (dop.md §13, контур B ПЭП: доставка OTP в закрытом контуре).

Отдельный процесс, имитирующий внешнего SMS-провайдера — не знает о
`signature_otp_codes`/`bcrypt`-хэшах и вообще о бизнес-логике ПЭП, только
принимает `{to, message}` и возвращает id, как принял бы настоящий
агрегатор. Логирует получателя на своей стороне — это не наш
audit/notification контур (у настоящего провайдера тоже были бы свои логи
доставки), но не логирует тело сообщения: тот же принцип, что
`SigningService.challenge` уже соблюдает для собственных логов
(dop.md §10.11: «OTP не попадает ни в логи, ни в аудит»).

Запускается тем же образу api/worker, отдельной командой (см.
`deploy/entrypoint.sh` режим `sms-gateway-mock`) — не отдельный build
context, тот же принцип, что `migrate`/`seed` в docker-compose.yml.
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import FastAPI
from pydantic import BaseModel

logger = structlog.get_logger(__name__)

app = FastAPI(title="sms-gateway-mock", docs_url=None, redoc_url=None, openapi_url=None)


class SendRequest(BaseModel):
    to: str
    message: str


class SendResponse(BaseModel):
    id: str
    status: str = "queued"


@app.post("/send", response_model=SendResponse)
async def send(payload: SendRequest) -> SendResponse:
    message_id = str(uuid.uuid4())
    logger.info("sms_gateway_mock_send", message_id=message_id, to=payload.to)
    return SendResponse(id=message_id)


@app.get("/health/live")
async def health_live() -> dict[str, str]:
    return {"status": "ok"}
