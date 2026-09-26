"""Шифрование секретных системных настроек (`system_settings.is_secret`).

Раньше `is_secret` лишь маскировал значение в ответе API: в самой таблице секрет лежал открытым
текстом (JSONB), и любой, кто читает БД (дамп, реплика, SQL-доступ), получал все токены и пароли
интеграций. Теперь значение секретной настройки шифруется Fernet (AES-128-CBC + HMAC) ключом из
окружения и хранится как `{"__enc": "fernet:v1", "ct": "<токен>"}`.

Ключ — переменная окружения `SETTINGS_ENCRYPTION_KEY` (Fernet-ключ: urlsafe base64 от 32 байт;
сгенерировать: `Fernet.generate_key().decode()` из `cryptography.fernet`).
Она читается здесь, а не в `core.config`: общая конфигурация не менялась. **Без ключа всё работает
как раньше** — значение хранится открытым текстом, а в лог пишется предупреждение (на каждую такую
запись), поэтому включение шифрования не ломает уже развёрнутые стенды.

Значения без маркера (записанные до включения ключа) читаются как есть; зашифровать их на месте
можно командой `python -m app.modules.admin.setting_crypto` (идемпотентно). Потеря или смена ключа
делает зашифрованные значения нечитаемыми: их придётся ввести заново — ключ надо хранить рядом с
резервными копиями секретов, но не в БД.
"""

from __future__ import annotations

import asyncio
import json
from functools import lru_cache
from typing import Any

import structlog
from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.errors import AppError, ErrorCode

logger = structlog.get_logger(__name__)

_MARKER_KEY = "__enc"
_MARKER_VALUE = "fernet:v1"


class _SecretsConfig(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    settings_encryption_key: SecretStr | None = None


@lru_cache(maxsize=1)
def _config() -> _SecretsConfig:
    return _SecretsConfig()


def reset_secrets_config() -> None:
    """Сбрасывает кэш ключа: нужен тестам, которые подменяют переменную окружения."""
    _config.cache_clear()


def _fernet() -> Fernet | None:
    key = _config().settings_encryption_key
    if key is None or not key.get_secret_value().strip():
        return None
    try:
        return Fernet(key.get_secret_value().strip().encode())
    except (ValueError, TypeError):
        # Испорченный ключ хуже отсутствующего: молча писать открытым текстом при «включённом»
        # шифровании нельзя, но и ронять запись всех настроек — тоже. Отказ явный.
        raise AppError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "SETTINGS_ENCRYPTION_KEY задан, но не является корректным Fernet-ключом",
        ) from None


def encryption_enabled() -> bool:
    return _fernet() is not None


def is_encrypted(stored: Any) -> bool:
    return isinstance(stored, dict) and stored.get(_MARKER_KEY) == _MARKER_VALUE


def encrypt_value(value: Any) -> Any:
    """Значение для хранения секретной настройки. Без ключа — как есть (с предупреждением)."""
    fernet = _fernet()
    if fernet is None:
        logger.warning(
            "system_setting_secret_stored_plaintext",
            hint="задайте SETTINGS_ENCRYPTION_KEY, чтобы хранить секреты зашифрованными",
        )
        return value
    if is_encrypted(value):
        return value
    token = fernet.encrypt(json.dumps(value, ensure_ascii=False).encode("utf-8")).decode("ascii")
    return {_MARKER_KEY: _MARKER_VALUE, "ct": token}


def decrypt_value(stored: Any) -> Any:
    """Значение настройки из БД в исходном виде; открытые (старые) значения — как есть."""
    if not is_encrypted(stored):
        return stored
    fernet = _fernet()
    if fernet is None:
        raise AppError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "Секретная настройка зашифрована, а SETTINGS_ENCRYPTION_KEY не задан",
        )
    try:
        return json.loads(fernet.decrypt(stored["ct"].encode("ascii")).decode("utf-8"))
    except (InvalidToken, KeyError, ValueError):
        raise AppError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "Секретную настройку не удалось расшифровать: неверный SETTINGS_ENCRYPTION_KEY",
        ) from None


async def encrypt_existing_secrets() -> int:
    """Шифрует на месте все секретные настройки, лежащие открытым текстом. Возвращает их число."""
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.admin.models import SystemSetting

    if not encryption_enabled():
        logger.warning("settings_encryption_key_missing", action="encrypt_existing_secrets")
        return 0
    changed = 0
    async with session_scope() as session:
        rows = await session.scalars(select(SystemSetting).where(SystemSetting.is_secret.is_(True)))
        for setting in rows:
            if not is_encrypted(setting.value):
                setting.value = encrypt_value(setting.value)
                changed += 1
    logger.info("system_setting_secrets_encrypted", count=changed)
    return changed


def main() -> None:
    from app.core.config import get_settings
    from app.core.logging import configure_logging

    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    print(asyncio.run(encrypt_existing_secrets()))


if __name__ == "__main__":
    main()
