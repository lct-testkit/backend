"""Шифрование полей ПДн учащегося (СНИЛС, паспорт, адрес) на уровне приложения (B6-7).

Паспорт проекта обещает «шифрование на уровне полей для паспортных данных» — вариант B из
разбора задачи: шифрование в приложении (`TypeDecorator`, Fernet), а не `pgcrypto` в SQL.
Ключ никогда не доходит до PostgreSQL (не попадёт в `pg_stat_statements` и логи СУБД) и не
виден в обычном SQL-дампе, дампе реплики или прямом доступе к БД.

Тот же приём и библиотека, что у `app.modules.admin.setting_crypto` (секреты системных
настроек), но ключ отдельный (`FIELD_ENCRYPTION_KEY` / `FIELD_ENCRYPTION_KEY_PREV`) — утечка
одного ключа не раскрывает другое.

Без ключа (`FIELD_ENCRYPTION_KEY` не задан) запись остаётся открытым текстом с предупреждением
в логе: тот же компромисс, что у `setting_crypto`, чтобы не ломать уже развёрнутые dev/demo
стенды без правки `.env`. В prod ключ обязателен (`Settings._reject_demo_secrets_in_prod`,
B6-8) и проверяется как корректный Fernet-ключ на старте процесса, а не при первом запросе.

Формат токена в БД: Fernet (`gAAAAA...`, urlsafe base64) в колонке `text`. Значение без этого
признака при чтении считается старым открытым текстом — так старые строки остаются читаемыми
до `tools/encrypt_learner_profiles.py encrypt-existing`.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache

import structlog
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from app.core.errors import AppError, ErrorCode

logger = structlog.get_logger(__name__)

# Fernet-токены начинаются с версии (0x80) в base64 — на практике всегда этим префиксом.
_TOKEN_PREFIX = "gAAAA"


@lru_cache(maxsize=1)
def _multi_fernet() -> MultiFernet | None:
    from app.core.config import get_settings

    settings = get_settings()
    key = settings.field_encryption_key
    if key is None:
        return None
    keys = [Fernet(key.get_secret_value().encode("ascii"))]
    prev = settings.field_encryption_key_prev
    if prev is not None:
        keys.append(Fernet(prev.get_secret_value().encode("ascii")))
    return MultiFernet(keys)


def reset_field_crypto_cache() -> None:
    """Сбрасывает кэш ключа. Нужен тестам, которые подменяют `FIELD_ENCRYPTION_KEY` через
    переменную окружения и `get_settings.cache_clear()`."""
    _multi_fernet.cache_clear()


def encryption_enabled() -> bool:
    return _multi_fernet() is not None


def _is_token(value: str) -> bool:
    return value.startswith(_TOKEN_PREFIX)


def encrypt_text(value: str) -> str:
    """Строка для хранения. Без ключа — как есть (с предупреждением в логе)."""
    fernet = _multi_fernet()
    if fernet is None:
        logger.warning(
            "field_encryption_key_missing_plaintext_write",
            hint="задайте FIELD_ENCRYPTION_KEY, чтобы хранить поле зашифрованным",
        )
        return value
    return fernet.encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_text(stored: str) -> str:
    """Строка из БД в исходном виде. Старые (не-токен) значения — как есть."""
    if not _is_token(stored):
        return stored
    fernet = _multi_fernet()
    if fernet is None:
        raise AppError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "Поле зашифровано, а FIELD_ENCRYPTION_KEY не задан",
        )
    try:
        return fernet.decrypt(stored.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        raise AppError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "Поле не удалось расшифровать: неверный FIELD_ENCRYPTION_KEY",
        ) from None


class EncryptedText(TypeDecorator):
    """Текстовая колонка (`text`), шифруемая Fernet при записи и расшифровываемая при чтении."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: object, dialect: object) -> str | None:
        if value is None:
            return None
        return encrypt_text(str(value))

    def process_result_value(self, value: object, dialect: object) -> str | None:
        if value is None:
            return None
        return decrypt_text(str(value))


class EncryptedDate(TypeDecorator):
    """Дата, хранимая как ISO-строка (`YYYY-MM-DD`) внутри Fernet-токена в колонке `text`.

    Нужна отдельно от `EncryptedText`, потому что модель отдаёт и принимает `datetime.date`,
    а не строку — ORM-код чтения профиля (`LearnerProfileService`, экспорт в LMS, импорт
    людей) не меняется."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: object, dialect: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, dt.date):
            raise TypeError(f"EncryptedDate ожидает datetime.date, получено {type(value)!r}")
        return encrypt_text(value.isoformat())

    def process_result_value(self, value: object, dialect: object) -> dt.date | None:
        if value is None:
            return None
        return dt.date.fromisoformat(decrypt_text(str(value)))
