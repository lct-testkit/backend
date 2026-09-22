"""Куда вернуть человека после входа: только внутрь этого же сайта.

Адрес `next` приходит из строки запроса и лежит в состоянии OIDC-потока до callback.
Если отдать его в редирект как есть, ссылка `/api/auth/login?next=https://чужой-сайт`
превращает вход в открытый редирект (фишинг после настоящего входа). Поэтому принимается
только путь: начинается с одного «/», без схемы, хоста, обратной косой черты
и управляющих символов.
"""

from __future__ import annotations


def safe_next_path(value: str | None) -> str | None:
    """Вернёт `value`, если это путь внутри сайта (`/deals?tab=files`), иначе None.

    При None после входа человека отправляют на BASE_URL.
    """
    if not value or not value.startswith("/") or value.startswith("//"):
        return None
    if "\\" in value or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return None
    return value
