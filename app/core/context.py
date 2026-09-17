"""Контекст запроса в contextvars.

`request_id` обязан проходить через все слои: логи, аудит и тело ответа
(разделы 2 и 18). Актор кладётся сюда после аутентификации, чтобы сервисы
аудита не тащили Request через всю цепочку вызовов.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass

_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_actor: ContextVar[ActorContext | None] = ContextVar("actor", default=None)
_client: ContextVar[ClientContext | None] = ContextVar("client", default=None)


@dataclass(frozen=True, slots=True)
class ActorContext:
    """Кто выполняет действие. Используется аудитом и событиями безопасности."""

    user_id: uuid.UUID | None
    role: str | None
    session_id: str | None = None
    impersonated_by: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class ClientContext:
    """Откуда пришёл запрос. Нужен для аудита и security_events."""

    ip: str | None
    user_agent: str | None


def new_request_id() -> str:
    return str(uuid.uuid4())


def get_request_id() -> str | None:
    return _request_id.get()


def set_request_id(value: str) -> None:
    _request_id.set(value)


def get_actor() -> ActorContext | None:
    return _actor.get()


def set_actor(actor: ActorContext | None) -> None:
    _actor.set(actor)


def get_client() -> ClientContext | None:
    return _client.get()


def set_client(client: ClientContext | None) -> None:
    _client.set(client)


def reset_context() -> None:
    _request_id.set(None)
    _actor.set(None)
    _client.set(None)
