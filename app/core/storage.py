"""Проверка доступности объектного хранилища.

Хранилище — SeaweedFS с S3-совместимым API. MinIO спецификацией запрещён.
Полный клиент (presigned PUT/GET, карантин, дедупликация по sha256)
появляется в спринте файлов; здесь достаточно health-check для /health/ready.
"""

from __future__ import annotations

import time

import httpx

from app.core.config import get_settings
from app.core.errors import DependencyStatus


async def check_storage() -> DependencyStatus:
    settings = get_settings()
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            # Анонимный GET на корень возвращает 200/403/404 — любой ответ
            # подтверждает, что S3-шлюз поднят и отвечает по HTTP.
            response = await client.get(settings.s3_endpoint_url)
        reachable = response.status_code < 500
        return DependencyStatus(
            name="seaweedfs",
            ok=reachable,
            latency_ms=round((time.perf_counter() - started) * 1000, 2),
            error=None if reachable else f"HTTP {response.status_code}",
        )
    except Exception as exc:
        return DependencyStatus(name="seaweedfs", ok=False, error=type(exc).__name__)
