"""UUIDv7 по RFC 9562.

Спецификация (раздел 2) требует UUIDv7 как первичный ключ: он сортируется
по времени создания, что даёт локальность в индексах и позволяет строить
курсорную пагинацию по паре (created_at, id).
"""

from __future__ import annotations

import os
import time
import uuid
from threading import Lock

_lock = Lock()
_last_ms = 0
_counter = 0

# 12 бит rand_a используются как монотонный счётчик внутри одной миллисекунды,
# чтобы идентификаторы не теряли порядок при массовой вставке (импорт, сиды).
_MAX_COUNTER = 0x0FFF


def uuid7() -> uuid.UUID:
    global _last_ms, _counter

    with _lock:
        now_ms = time.time_ns() // 1_000_000
        if now_ms == _last_ms:
            _counter += 1
            if _counter > _MAX_COUNTER:
                # Счётчик переполнен — ждём следующую миллисекунду.
                while now_ms <= _last_ms:
                    now_ms = time.time_ns() // 1_000_000
                _last_ms = now_ms
                _counter = 0
        elif now_ms > _last_ms:
            _last_ms = now_ms
            _counter = 0
        else:
            # Часы уехали назад: не уменьшаем таймстамп, продолжаем счётчик.
            now_ms = _last_ms
            _counter += 1
            if _counter > _MAX_COUNTER:
                _counter = 0

        timestamp = now_ms & ((1 << 48) - 1)
        counter = _counter

    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)

    value = timestamp << 80
    value |= 0x7 << 76  # версия 7
    value |= counter << 64
    value |= 0x2 << 62  # вариант RFC 4122
    value |= rand_b

    return uuid.UUID(int=value)


def uuid7_timestamp_ms(value: uuid.UUID) -> int:
    """Извлекает миллисекунды Unix-времени из UUIDv7."""
    return value.int >> 80


def is_uuid7(value: uuid.UUID) -> bool:
    return value.version == 7
