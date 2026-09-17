"""Структурное логирование.

Логи приложения и логи сторонних библиотек (uvicorn, SQLAlchemy, arq) идут
через один конвейер stdlib + structlog. Благодаря этому `request_id`
попадает в каждую запись, независимо от того, какой слой её создал —
требование раздела 2 о прослеживаемости запроса через все слои.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from app.core.context import get_actor, get_request_id
from app.core.masking import REDACTED, SECRET_KEYS


def _add_request_context(
    _logger: Any, _name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    request_id = get_request_id()
    if request_id:
        event_dict["request_id"] = request_id
    actor = get_actor()
    if actor:
        if actor.user_id:
            event_dict["actor_id"] = str(actor.user_id)
        if actor.role:
            event_dict["actor_role"] = actor.role
    return event_dict


def _redact_secrets(
    _logger: Any, _name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Последний барьер: пароли и токены не должны попасть в лог даже случайно."""
    for key in list(event_dict):
        if key.lower() in SECRET_KEYS:
            event_dict[key] = REDACTED
    return event_dict


def _drop_color_message(
    _logger: Any, _name: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    """Убирает дубль сообщения от uvicorn с ANSI-кодами внутри JSON."""
    event_dict.pop("color_message", None)
    return event_dict


def _shared_processors() -> list[structlog.types.Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        _add_request_context,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        # ExtraAdder переносит поля из stdlib-вызовов вида
        # logger.info(..., extra={...}). Он обязан идти раньше уборщиков:
        # иначе секрет, переданный через extra, не будет замаскирован.
        structlog.stdlib.ExtraAdder(),
        _drop_color_message,
        _redact_secrets,
    ]


def configure_logging(*, level: str = "INFO", json_output: bool = True) -> None:
    log_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
    shared = _shared_processors()

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer(ensure_ascii=False)
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            *shared,
            # Передаёт событие в stdlib-обработчик, где его отрендерит formatter.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        # foreign_pre_chain применяется к записям от uvicorn и SQLAlchemy,
        # чтобы они получили тот же формат и тот же request_id.
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            renderer,
        ],
    )

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(log_level)

    # Свой access-лог пишет RequestContextMiddleware, дублировать не нужно.
    #
    # Важно погасить и родительский логгер `uvicorn`: именно на нём висит
    # обработчик из LOGGING_CONFIG uvicorn. Если очистить только
    # `uvicorn.error`, записи всплывут на родителе в текстовом формате.
    for name, noisy_level in (
        ("uvicorn", logging.INFO),
        ("uvicorn.access", logging.WARNING),
        ("uvicorn.error", logging.INFO),
        ("sqlalchemy.engine", logging.WARNING),
        ("arq", logging.INFO),
    ):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.setLevel(noisy_level)
        logger.propagate = True
