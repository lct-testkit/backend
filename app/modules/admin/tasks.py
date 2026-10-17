"""B6-6: фоновая задача очистки по сроку хранения (152-ФЗ ст. 5 ч. 7).

Запускается до `ensure_audit_partitions` (03:00) в ночное окно. Категории и их область —
см. докстринг `app.modules.admin.retention`.
"""

from __future__ import annotations

from typing import Any

import structlog

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.metrics import track_task
from app.modules.admin.retention import RetentionService

logger = structlog.get_logger(__name__)


@track_task
async def sweep_retention(ctx: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    if not settings.retention_enabled:
        return {"enabled": False}
    async with session_scope() as session:
        results = await RetentionService(session, settings).run_all(
            dry_run=settings.retention_dry_run
        )
    summary = {r.category: {"deleted": r.deleted, "status": r.status} for r in results}
    logger.info("retention_swept", dry_run=settings.retention_dry_run, **summary)
    return {"enabled": True, "dry_run": settings.retention_dry_run, "categories": summary}
