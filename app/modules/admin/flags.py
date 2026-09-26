"""Чтение флагов функциональности (`feature_flags`) с учётом процента раскатки.

Раньше флаг читал единственный потребитель (`integration.tasks`), и ни `rollout`, ни общего
хелпера не было: процент раскатки хранился и редактировался, но нигде не применялся. Хелпер даёт
одно место, где решается «включено ли для этого субъекта»:

* флага нет в таблице — `default` (по умолчанию включено: функция, которую ещё не заводили в
  админке, не должна пропадать);
* `is_enabled=false` — выключено для всех;
* `rollout` 100 — включено для всех, 0 — ни для кого;
* между ними субъект попадает в раскатку по стабильной свёртке `код флага + id субъекта`: один и
  тот же пользователь получает один и тот же ответ, а при росте процента остаются включёнными
  все, кто уже был включён (порог сдвигается, корзина не меняется).
"""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.admin.models import FeatureFlag


def rollout_bucket(code: str, subject_id: uuid.UUID | str) -> int:
    """Корзина 0..99 субъекта для флага: постоянна для пары (флаг, субъект)."""
    digest = hashlib.sha256(f"{code}:{subject_id}".encode()).hexdigest()
    return int(digest[:8], 16) % 100


async def is_feature_enabled(
    session: AsyncSession,
    code: str,
    *,
    subject_id: uuid.UUID | str | None = None,
    default: bool = True,
) -> bool:
    """Включён ли флаг `code` для субъекта (обычно — пользователя).

    Без `subject_id` частичная раскатка (0 < rollout < 100) считается выключенной: решить, в
    какую корзину попал неизвестный субъект, нельзя."""
    flag = await session.scalar(select(FeatureFlag).where(FeatureFlag.code == code))
    if flag is None:
        return default
    if not flag.is_enabled or flag.rollout <= 0:
        return False
    if flag.rollout >= 100:
        return True
    if subject_id is None:
        return False
    return rollout_bucket(code, subject_id) < flag.rollout
