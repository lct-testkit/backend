"""Чистка JSON перед записью в JSONB.

Тело входящего вебхука сохраняется в `inbound_messages.raw_payload` как есть, а Postgres в JSONB не
принимает NUL-символ, одиночные суррогаты юникода и не-числа (`NaN`, `Infinity`): запрос с таким
телом падал бы 500 уже на сохранении «сырого» тела, до всякой бизнес-логики.
"""

from __future__ import annotations

import math
import re
from typing import Any

# Заменитель недопустимого символа — U+FFFD; кодом, а не литералом: невидимые и «ломаные» символы в
# исходнике легко потерять при правке.
REPLACEMENT = chr(0xFFFD)
# NUL и одиночные суррогаты: `json.loads` их пропускает (`"\u0000"`, `"\ud800"`), JSONB — нет.
UNSTORABLE_CHARS = re.compile(f"[{chr(0)}{chr(0xD800)}-{chr(0xDFFF)}]")


def scrub_text(value: str) -> str:
    """Символы, которых Postgres не принимает, заменяются на U+FFFD."""
    return UNSTORABLE_CHARS.sub(REPLACEMENT, value)


def scrub_json(value: Any) -> Any:
    """Копия значения, пригодная для JSONB: недопустимые символы в строках и ключах заменены на
    U+FFFD, не-числа (`NaN`, `Infinity`) — на `null`. Глубина вложенности должна быть ограничена
    вызывающим: обход рекурсивный."""
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, list):
        return [scrub_json(item) for item in value]
    if isinstance(value, dict):
        return {scrub_text(str(key)): scrub_json(item) for key, item in value.items()}
    return value
