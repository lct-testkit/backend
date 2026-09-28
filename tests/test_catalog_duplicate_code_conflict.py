"""B-33: дубль кода в справочниках каталога отвечает 409 CRM-1301, а не 422.

До этой правки `DirectionService.create`/`ProductService.create` (второй, не-`deleted`
случай)/`LossReasonService.create`/`HolidayService.create`/`CustomFieldDefService.create`
поднимали `ValidationError` (`ErrorCode.VALIDATION`, 422) на дубль кода/даты — тот же класс
ошибок, что у семантически некорректного ввода (например, перевёрнутый период продукта).
Организации и контакты уже сигналили дубль правильно, `AppError(ErrorCode.DUPLICATE, ...)`,
409 (см. `OrganizationService.create`/`ContactService.create`); справочники — нет.

409, а не 422, потому что тут не невалидный ввод: код/дата сами по себе корректны, конфликт
только с уже существующей записью — ровно то, что означает `errors[]` в `FieldError`-форме
уже отдавали (клиенту есть на что смотреть), просто под неверным статусом/кодом ошибки.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL
from tests.crm_helpers import login

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _marker() -> str:
    return uuid.uuid4().hex[:8]


def _assert_duplicate_conflict(response, field: str) -> None:
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["code"] == "CRM-1301"
    assert body["errors"][0]["field"] == field


class TestDirectionDuplicateCode:
    def test_duplicate_code_is_a_409_conflict(self, client) -> None:
        login(client, "ADMIN")
        code = f"dir-{_marker()}"
        first = client.post("/api/directions", json={"code": code, "name": "Первое"})
        assert first.status_code == 201, first.text

        second = client.post("/api/directions", json={"code": code, "name": "Второе"})
        _assert_duplicate_conflict(second, "code")


class TestProductDuplicateCode:
    def test_duplicate_code_is_a_409_conflict(self, client) -> None:
        login(client, "ADMIN")
        code = f"prod-{_marker()}"
        first = client.post("/api/products", json={"code": code, "name": "Первый"})
        assert first.status_code == 201, first.text

        second = client.post("/api/products", json={"code": code, "name": "Второй"})
        _assert_duplicate_conflict(second, "code")


class TestLossReasonDuplicateCode:
    def test_duplicate_code_is_a_409_conflict(self, client) -> None:
        login(client, "ADMIN")
        code = f"reason-{_marker()}"
        first = client.post(
            "/api/loss-reasons",
            json={"code": code, "name": "Первая", "category": "other"},
        )
        assert first.status_code == 201, first.text

        second = client.post(
            "/api/loss-reasons",
            json={"code": code, "name": "Вторая", "category": "other"},
        )
        _assert_duplicate_conflict(second, "code")


class TestHolidayDuplicateDate:
    def test_duplicate_date_is_a_409_conflict(self, client) -> None:
        login(client, "ADMIN")
        # Фиксированная дата безопасна здесь (не как в соседнем файле про DELETE): обе записи
        # создаются и конфликтуют в одном прогоне, чужая оставленная запись с тем же числом
        # только ускорила бы обнаружение дубля, не сломала бы тест.
        date = "2099-01-01"
        first = client.post(
            "/api/holidays", json={"date": date, "name": "Первый", "is_working_day": False}
        )
        if first.status_code == 409:
            # Запись с этой датой уже осталась от прошлого прогона (не транзакция с откатом) —
            # для проверки дубля это ничем не хуже: конфликт уже здесь.
            _assert_duplicate_conflict(first, "date")
            return
        assert first.status_code == 201, first.text

        second = client.post(
            "/api/holidays", json={"date": date, "name": "Второй", "is_working_day": False}
        )
        _assert_duplicate_conflict(second, "date")


class TestCustomFieldDefDuplicateCode:
    def test_duplicate_code_for_the_same_entity_type_is_a_409_conflict(self, client) -> None:
        login(client, "ADMIN")
        code = f"field_{_marker()}"
        body = {
            "entity_type": "organization",
            "code": code,
            "label": "Первое",
            "field_type": "string",
        }
        first = client.post("/api/custom-field-defs", json=body)
        assert first.status_code == 201, first.text

        second = client.post("/api/custom-field-defs", json={**body, "label": "Второе"})
        _assert_duplicate_conflict(second, "code")
