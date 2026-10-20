"""Тексты ошибок, которые видит человек: по-русски, с заглавной буквы, без кодов полей и
служебных префиксов pydantic («Value error, …»), с согласованным родом («Организация не найдена»).

Чистые проверки — без БД; сквозные (ответ ручки) — в конце, с `TEST_DATABASE_URL`."""

from __future__ import annotations

import re
import uuid
from decimal import Decimal
from typing import Annotated, Literal

import pytest
from pydantic import BaseModel, Field, ValidationError, field_validator
from pydantic_core import PydanticCustomError

from app.core.errors import ErrorCode, NotFoundError
from app.core.problem import _http_detail, reason_text
from tests.conftest import TEST_DATABASE_URL
from tests.crm_helpers import login


class _Sample(BaseModel):
    name: Annotated[str, Field(min_length=3, max_length=5)]
    count: int = Field(ge=0, le=10)
    price: Decimal | None = Field(default=None, gt=0)
    kind: Literal["a", "b", "c"] = "a"
    ident: uuid.UUID | None = None
    tags: list[str] = Field(default_factory=list, max_length=2)

    @field_validator("tags")
    @classmethod
    def _custom(cls, value: list[str]) -> list[str]:
        if "bad" in value:
            raise ValueError("недопустимый тег")
        if "worse" in value:
            raise PydanticCustomError("worse_tag", "очень плохой тег")
        return value


def _reasons(**data) -> dict[str, str]:
    base = {"name": "abcd", "count": 1}
    try:
        _Sample(**{**base, **data})
    except ValidationError as exc:
        return {".".join(map(str, e["loc"])): reason_text(e) for e in exc.errors()}
    return {}


class TestReasonText:
    def test_missing(self) -> None:
        with pytest.raises(ValidationError) as caught:
            _Sample(name="abcd")  # type: ignore[call-arg]
        assert reason_text(caught.value.errors()[0]) == "Обязательное поле"

    def test_length_and_range(self) -> None:
        assert _reasons(name="ab")["name"] == "Слишком коротко: не меньше 3 символов"
        assert _reasons(name="abcdef")["name"] == "Слишком длинно: не больше 5 символов"
        assert _reasons(count=-1)["count"] == "Значение должно быть не меньше 0"
        assert _reasons(count=11)["count"] == "Значение должно быть не больше 10"
        assert _reasons(price="0")["price"] == "Значение должно быть больше 0"
        assert _reasons(tags=["a", "b", "c"])["tags"] == "Слишком много элементов: не больше 2"

    def test_types_and_enums(self) -> None:
        assert _reasons(count="x")["count"] == "Нужно целое число"
        assert _reasons(ident="not-a-uuid")["ident"] == "Некорректный идентификатор"
        assert _reasons(kind="z")["kind"] == "Допустимые значения: a, b, c"

    def test_custom_messages_lose_the_pydantic_prefix_and_get_a_capital(self) -> None:
        assert _reasons(tags=["bad"])["tags"] == "Недопустимый тег"
        assert _reasons(tags=["worse"])["tags"] == "Очень плохой тег"

    def test_no_english_left_in_any_reason(self) -> None:
        samples = [
            _reasons(name="ab"),
            _reasons(count="x"),
            _reasons(kind="z"),
            _reasons(ident="no"),
            _reasons(price="0"),
        ]
        for reasons in samples:
            for text in reasons.values():
                assert not re.search(r"[A-Za-z]{4,}", text.replace("a, b, c", "")), text
                assert text[:1].isupper(), text


class TestNotFoundGender:
    @pytest.mark.parametrize(
        ("entity", "expected"),
        [
            ("Организация", "Организация не найдена"),
            ("Сделка", "Сделка не найдена"),
            ("Контакт", "Контакт не найден"),
            ("Файл документа", "Файл документа не найден"),
            ("Пользователь", "Пользователь не найден"),
            ("Соглашение об ЭДО", "Соглашение об ЭДО не найдено"),
            ("Подтверждение", "Подтверждение не найдено"),
            ("Родительская команда", "Родительская команда не найдена"),
            ("Пользовательское поле", "Пользовательское поле не найдено"),
            ("Статус воронки", "Статус воронки не найден"),
            ("Сторона соглашения", "Сторона соглашения не найдена"),
            ("ПО", "ПО не найдено"),
        ],
    )
    def test_agrees_with_the_noun(self, entity: str, expected: str) -> None:
        assert NotFoundError(entity).detail == expected

    def test_id_is_appended(self) -> None:
        ident = uuid.uuid4()
        assert NotFoundError("Сделка", ident).detail == f"Сделка не найдена: {ident}"
        assert NotFoundError("Сделка", ident).extra == {"entity_type": "Сделка"}


class TestHttpDetail:
    def test_english_starlette_phrases_become_russian(self) -> None:
        assert _http_detail(404, "Not Found", ErrorCode.NOT_FOUND) == "Ресурс не найден"
        assert "не поддерживается" in _http_detail(405, "Method Not Allowed", ErrorCode.INTERNAL)
        assert _http_detail(403, "Forbidden", ErrorCode.FORBIDDEN) == "Недостаточно прав"

    def test_russian_detail_is_kept(self) -> None:
        assert (
            _http_detail(404, "Страница не найдена", ErrorCode.NOT_FOUND) == "Страница не найдена"
        )


@pytest.mark.skipif(not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL")
class TestResponses:
    def test_validation_error_response_is_russian(self, client) -> None:
        login(client, "ADMIN")
        response = client.get("/api/organizations", params={"region_id": "not-a-uuid"})
        assert response.status_code == 422, response.text
        reasons = [e["reason"] for e in response.json()["errors"]]
        assert reasons == ["Некорректный идентификатор"]

    def test_unknown_route_has_a_russian_detail(self, client) -> None:
        login(client, "ADMIN")
        response = client.get("/api/definitely-not-a-route")
        assert response.status_code == 404
        assert response.json()["detail"] == "Ресурс не найден"

    def test_not_found_entity_agrees_in_gender(self, client) -> None:
        login(client, "ADMIN")
        response = client.get(f"/api/organizations/{uuid.uuid4()}")
        assert response.status_code == 404
        assert response.json()["detail"].startswith("Организация не найдена")
