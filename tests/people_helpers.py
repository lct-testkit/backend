"""Заготовки для тестов о людях: контакты, продукты, записи аудита, уникальные значения.

Тестовая БД переживает прогоны и ничего не чистит, поэтому email, телефон и фамилия каждого
контакта уникальны на вызов: дубль по правилам `ContactService.find_matches` иначе мог бы
«совпасть» с записью прошлого запуска. Требуют `TEST_DATABASE_URL`, как и остальные сквозные тесты
(`tests/conftest.py`).
"""

from __future__ import annotations

import uuid
from typing import Any

from tests.conftest import run


def unique_email(prefix: str = "person") -> str:
    return f"{prefix}.{uuid.uuid4().hex[:10]}@example.ru"


def unique_phone() -> str:
    """Российский номер в E.164 с семью уникальными цифрами: `+7999XXXXXXX`."""
    return f"+7999{int(uuid.uuid4().hex[:8], 16) % 10_000_000:07d}"


def unique_surname() -> str:
    return f"Тестов{uuid.uuid4().hex[:8]}"


def create_contact(client, **fields: Any) -> dict[str, Any]:
    """Контакт через API от имени вошедшего пользователя (маскированный `ContactOut`)."""
    response = client.post(
        "/api/contacts",
        json={
            "first_name": "Иван",
            "last_name": unique_surname(),
            "email": unique_email(),
            "phone": unique_phone(),
            **fields,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_product(client, **fields: Any) -> dict[str, Any]:
    response = client.post(
        "/api/products",
        json={"code": f"prod-{uuid.uuid4().hex[:10]}", "name": "Продукт для теста", **fields},
    )
    assert response.status_code == 201, response.text
    return response.json()


def audit_entries(client, action: str, entity_id: str) -> list[dict[str, Any]]:
    """Записи аудита действия по сущности, от старых к новым."""

    async def _load() -> list[dict[str, Any]]:
        from sqlalchemy import select

        from app.core.db import session_scope
        from app.modules.audit.models import AuditLog

        async with session_scope() as session:
            rows = (
                (
                    await session.execute(
                        select(AuditLog)
                        .where(
                            AuditLog.action == action, AuditLog.entity_id == uuid.UUID(entity_id)
                        )
                        .order_by(AuditLog.created_at, AuditLog.id)
                    )
                )
                .scalars()
                .all()
            )
            return [
                {
                    "entity_type": row.entity_type,
                    "actor_id": str(row.actor_id) if row.actor_id else None,
                    "actor_role": row.actor_role,
                    "result": row.result,
                    "changes": row.changes,
                }
                for row in rows
            ]

    return run(client, _load)
