"""Справочник сотрудников для интерфейса.

Зачем: `GET /api/admin/users` доступен только ADMIN, а имена авторов комментариев,
ответственных, исполнителей задач и участников сделок нужны всем ролям, иначе
интерфейс показывает голые идентификаторы, а руководитель не может выбрать
преемника при переназначении.

Отдаём минимум — идентификатор, ФИО, роль, команду и статус. Ни email, ни телефона,
ни должности. По списку `ids` возвращаем и заблокированных/уволенных/обезличенных
(для обезличенных `full_name` — стабильный псевдоним), чтобы история не теряла имён.
Поиск по `q` показывает только действующих сотрудников.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_, select

from app.core.deps import ConsentedUser, DbSession
from app.core.errors import FieldError, ValidationError
from app.modules.identity.models import User, UserStatus

router = APIRouter(prefix="/users", tags=["users"])

_MAX_IDS = 200
_ROLES = {"KAM", "HEAD", "ADMIN", "AUDITOR"}


class DirectoryEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    full_name: str
    display_name: str | None = None
    role: str
    team_id: uuid.UUID | None = None
    status: str


class DirectoryResponse(BaseModel):
    items: list[DirectoryEntry]


@router.get(
    "/directory",
    summary="Справочник сотрудников",
    description=(
        "Имена сотрудников для подписей, пикеров и ленты активности. `ids` — "
        "идентификаторы через запятую (до 200, любой статус); `q` — поиск по ФИО среди "
        "действующих; `roles` — фильтр по ролям через запятую. Без параметров возвращает "
        "первых `limit` действующих сотрудников. Роль: любой аутентифицированный "
        "пользователь с принятым согласием."
    ),
    response_model=DirectoryResponse,
)
async def directory(
    _: ConsentedUser,
    session: DbSession,
    q: Annotated[str | None, Query(max_length=100, description="Поиск по ФИО")] = None,
    ids: Annotated[str | None, Query(description="UUID через запятую")] = None,
    roles: Annotated[str | None, Query(description="Роли через запятую")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> DirectoryResponse:
    stmt = select(User).where(User.role != "INTEGRATION")

    if ids:
        parsed: list[uuid.UUID] = []
        for raw in ids.split(","):
            raw = raw.strip()
            if not raw:
                continue
            try:
                parsed.append(uuid.UUID(raw))
            except ValueError:
                raise ValidationError(
                    "Некорректный идентификатор в `ids`",
                    [FieldError(field="ids", reason=f"не UUID: {raw[:40]}")],
                ) from None
        if len(parsed) > _MAX_IDS:
            raise ValidationError(
                f"Слишком много идентификаторов (максимум {_MAX_IDS})",
                [FieldError(field="ids", reason="превышен лимит")],
            )
        if not parsed:
            return DirectoryResponse(items=[])
        stmt = stmt.where(User.id.in_(parsed))
    else:
        stmt = stmt.where(
            User.deleted_at.is_(None),
            User.status.in_([UserStatus.ACTIVE.value, UserStatus.INVITED.value]),
        )

    if roles:
        wanted = {r.strip().upper() for r in roles.split(",") if r.strip()}
        unknown = wanted - _ROLES
        if unknown:
            raise ValidationError(
                "Неизвестная роль в `roles`",
                [FieldError(field="roles", reason=f"допустимо: {', '.join(sorted(_ROLES))}")],
            )
        stmt = stmt.where(User.role.in_(sorted(wanted)))

    if q and q.strip():
        pattern = f"%{q.strip()}%"
        stmt = stmt.where(or_(User.full_name.ilike(pattern), User.display_name.ilike(pattern)))

    stmt = stmt.order_by(User.full_name).limit(limit if not ids else _MAX_IDS)
    rows = (await session.execute(stmt)).scalars().all()
    return DirectoryResponse(items=[DirectoryEntry.model_validate(row) for row in rows])
