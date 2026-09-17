"""Курсорная пагинация.

Раздел 2: клиент передаёт `limit` и `cursor`, максимум 100. Ответ — объект
с `items` и `next_cursor`, где `next_cursor` равен `null`, если данных
больше нет. `OFFSET` не используется: он деградирует на больших таблицах.
Курсор непрозрачен для клиента и кодирует пару (sort_value, id).
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import uuid
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field
from sqlalchemy import DateTime, literal
from sqlalchemy import tuple_ as sa_tuple

from app.core.errors import ErrorCode, FieldError, ValidationError

T = TypeVar("T")

DEFAULT_LIMIT = 50
MAX_LIMIT = 100


class Cursor(BaseModel):
    """Позиция в отсортированной выборке."""

    # Значение поля сортировки: обычно created_at, но может быть любым.
    value: Any
    id: uuid.UUID

    def encode(self) -> str:
        payload = {"v": self.value, "i": str(self.id)}
        if isinstance(self.value, dt.datetime):
            payload["v"] = self.value.isoformat()
        raw = json.dumps(payload, separators=(",", ":"), default=str).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    @classmethod
    def decode(cls, encoded: str) -> Cursor:
        try:
            padding = "=" * (-len(encoded) % 4)
            raw = base64.urlsafe_b64decode(encoded + padding)
            payload = json.loads(raw)
            return cls(value=payload["v"], id=uuid.UUID(payload["i"]))
        except (binascii.Error, ValueError, KeyError, TypeError) as exc:
            raise ValidationError(
                "Курсор повреждён или устарел",
                [FieldError(field="cursor", reason="Некорректное значение курсора")],
            ) from exc

    def as_datetime(self) -> dt.datetime:
        """Курсор по created_at: значение приходит строкой ISO 8601."""
        if isinstance(self.value, dt.datetime):
            return self.value
        return dt.datetime.fromisoformat(str(self.value))


class PageParams(BaseModel):
    """Параметры запроса списка. Используется как FastAPI-зависимость."""

    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    cursor: str | None = None

    @property
    def decoded_cursor(self) -> Cursor | None:
        return Cursor.decode(self.cursor) if self.cursor else None

    @property
    def fetch_limit(self) -> int:
        """Запрашиваем на один больше, чтобы понять, есть ли следующая страница."""
        return self.limit + 1


class Page(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None

    @classmethod
    def build(
        cls,
        rows: list[Any],
        *,
        limit: int,
        cursor_value: Any = None,
        serializer: Any = None,
    ) -> Page[T]:
        """Обрезает лишнюю строку и строит курсор по последнему элементу страницы.

        `cursor_value` — функция, достающая значение поля сортировки из строки.
        По умолчанию берётся `created_at`.
        """
        has_more = len(rows) > limit
        visible = rows[:limit]

        next_cursor: str | None = None
        if has_more and visible:
            last = visible[-1]
            value = (
                cursor_value(last) if callable(cursor_value) else last.created_at
            )
            next_cursor = Cursor(value=value, id=last.id).encode()

        items = [serializer(row) for row in visible] if callable(serializer) else visible
        return cls(items=items, next_cursor=next_cursor)


def keyset_before(sort_column: Any, id_column: Any, cursor: Cursor) -> Any:
    """Условие «строго раньше курсора» для сортировки по убыванию.

    Питоновское `(a, b) < (x, y)` для колонок SQLAlchemy **не** даёт
    row-comparison: кортежи сравниваются поэлементно и в SQL уходит только
    первое сравнение, а `id` как tie-breaker молча теряется. Строки с
    одинаковым значением сортировки тогда дублируются или пропадают между
    страницами. Поэтому здесь явный `tuple_`, который транслируется в
    `(sort, id) < (:sort, :id)`.
    """
    return sa_tuple(sort_column, id_column) < sa_tuple(
        _bind(cursor, sort_column), literal(cursor.id, id_column.type)
    )


def keyset_after(sort_column: Any, id_column: Any, cursor: Cursor) -> Any:
    """То же для сортировки по возрастанию."""
    return sa_tuple(sort_column, id_column) > sa_tuple(
        _bind(cursor, sort_column), literal(cursor.id, id_column.type)
    )


def _bind(cursor: Cursor, sort_column: Any) -> Any:
    """Приводит значение курсора к типу колонки сортировки.

    Курсор едет в base64-JSON, поэтому `created_at` возвращается строкой —
    без явного приведения PostgreSQL сравнивал бы timestamptz с text.
    """
    column_type = getattr(sort_column, "type", None)
    value = cursor.value
    if isinstance(column_type, DateTime) and not isinstance(value, dt.datetime):
        value = cursor.as_datetime()
    return literal(value, column_type)


def enforce_limit(limit: int) -> int:
    if limit > MAX_LIMIT:
        raise ValidationError(
            f"Максимальный размер страницы — {MAX_LIMIT}",
            [FieldError(field="limit", reason=f"не больше {MAX_LIMIT}", code=ErrorCode.VALIDATION)],
        )
    return limit
