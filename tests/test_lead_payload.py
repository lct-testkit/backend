"""Разбор тела вебхука сайта (`integration/lead_payload.py`): чистые проверки без БД.

Сквозное поведение вебхука (подпись, повторы, сделки) — в `tests/test_cms_webhook.py`; здесь —
соответствие ключей (английские и русские), приведение значений и то, что все проблемы тела
собираются в один ответ.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from typing import Any

import pytest

from app.core.errors import ValidationError
from app.modules.integration.json_safe import scrub_json
from app.modules.integration.lead_payload import (
    NAMELESS_FIRST_NAME,
    NAMELESS_LAST_NAME,
    LeadPayload,
    parse_json_object,
    parse_lead_payload,
    raw_evidence,
)

_FFFD = chr(0xFFFD)  # заменитель символов, которых нет в JSONB


def _errors(payload: dict[str, Any]) -> dict[str, list[str]]:
    with pytest.raises(ValidationError) as caught:
        parse_lead_payload(payload)
    result: dict[str, list[str]] = {}
    for error in caught.value.errors:
        result.setdefault(error.field, []).append(error.reason)
    return result


class TestParseJsonObject:
    def test_object_is_returned(self) -> None:
        assert parse_json_object(b'{"email": "a@b.ru", "n": [1, 2.5]}') == {
            "email": "a@b.ru",
            "n": [1, 2.5],
        }

    def test_utf8_bom_is_tolerated(self) -> None:
        assert parse_json_object(b"\xef\xbb\xbf" + '{"имя": "Иван"}'.encode()) == {"имя": "Иван"}

    @pytest.mark.parametrize("raw", [b"null", b"[]", b'"x"', b"1", b"true", b"[{}]"])
    def test_other_json_values_are_not_an_object(self, raw: bytes) -> None:
        with pytest.raises(ValidationError, match="JSON-объект"):
            parse_json_object(raw)

    @pytest.mark.parametrize(
        "raw",
        [
            b"",
            b" ",
            b"{",
            b"{'a': 1}",
            b"\xff\xfe",
            b'{"a": NaN}',
            b'{"a": Infinity}',
            b'{"a": -Infinity}',
            b'{"a": 1e999}',
            b'{"a": 1}{"b": 2}',
            b"[" * 100_000,
            b'{"a": ' + b"1" * 5000 + b"}",  # длиннее лимита перевода строки в int
        ],
        # Идентификатор теста попадает в переменную окружения (`PYTEST_CURRENT_TEST`), а на Windows
        # она не длиннее 32 767 символов — длинные тела нельзя оставлять в имени теста.
        ids=[
            "empty",
            "space",
            "unclosed",
            "single-quotes",
            "not-utf8",
            "nan",
            "infinity",
            "minus-infinity",
            "float-overflow",
            "two-objects",
            "recursion",
            "huge-integer",
        ],
    )
    def test_broken_json_is_a_validation_error(self, raw: bytes) -> None:
        with pytest.raises(ValidationError, match="невалидный JSON"):
            parse_json_object(raw)

    def test_too_deep_object_is_refused(self) -> None:
        body: Any = {}
        for _ in range(40):
            body = {"a": body}

        with pytest.raises(ValidationError, match="вложенность"):
            parse_json_object(json.dumps(body).encode())

    def test_reasonable_nesting_is_allowed(self) -> None:
        assert parse_json_object(b'{"a": {"b": {"c": [1, {"d": 2}]}}}')

    def test_characters_postgres_cannot_store_are_replaced(self) -> None:
        parsed = parse_json_object(
            '{"k\\u0000ey": "a\\u0000b", "s": "\\ud800x", "ok": "п"}'.encode()
        )

        assert parsed == {f"k{_FFFD}ey": f"a{_FFFD}b", "s": f"{_FFFD}x", "ok": "п"}

    def test_scrub_json_reaches_nested_values(self) -> None:
        assert scrub_json({"a": ["x\x00", {"b": chr(0xDFFF)}], "n": 1}) == {
            "a": [f"x{_FFFD}", {"b": _FFFD}],
            "n": 1,
        }

    def test_scrub_json_replaces_non_finite_numbers_with_null(self) -> None:
        scrubbed = scrub_json({"a": float("nan"), "b": [float("inf"), -float("inf"), 1.5], "c": 2})

        assert scrubbed == {"a": None, "b": [None, None, 1.5], "c": 2}

    def test_scrub_json_keeps_valid_text_and_astral_characters(self) -> None:
        assert scrub_json({"ключ": "значение 😀"}) == {"ключ": "значение 😀"}


class TestRawEvidence:
    def test_short_body_is_kept_whole(self) -> None:
        assert raw_evidence(b'{"a": 1}') == {
            "_raw": '{"a": 1}',
            "_size_bytes": 8,
            "_truncated": False,
        }

    def test_long_body_is_cut_and_marked(self) -> None:
        evidence = raw_evidence(b"x" * 10_000, limit=100)

        assert len(evidence["_raw"]) == 100
        assert (evidence["_size_bytes"], evidence["_truncated"]) == (10_000, True)

    def test_garbage_bytes_and_nul_are_storable(self) -> None:
        evidence = raw_evidence(b"\xff\xfe\x00abc")

        assert "\x00" not in evidence["_raw"]
        assert evidence["_raw"].endswith("abc")


class TestKeys:
    def test_english_keys(self) -> None:
        lead = parse_lead_payload(
            {
                "first_name": "Иван",
                "last_name": "Иванов",
                "middle_name": "Иванович",
                "phone": "+7 (900) 111-22-33",
                "email": "Ivanov@Example.RU",
                "product_name": "Python",
                "comment": "Позвоните",
                "source_url": "https://site/x",
                "order_number": "ORD-1",
                "stream_number": 2,
                "amount": "1500,5",
                "external_id": "site-9",
                "created_at": "2026-09-25T12:00:00+03:00",
            }
        )

        assert lead == LeadPayload(
            first_name="Иван",
            last_name="Иванов",
            middle_name="Иванович",
            email="ivanov@example.ru",
            phone="+79001112233",
            course="Python",
            course_candidates=("Python",),
            comment="Позвоните",
            source_url="https://site/x",
            order_number="ORD-1",
            stream_number=2,
            amount=Decimal("1500.50"),
            external_id="site-9",
            created_at=dt.datetime(2026, 9, 25, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=3))),
        )

    def test_russian_keys_of_the_payments_file(self) -> None:
        lead = parse_lead_payload(
            {
                "Номер заявки": "ORD-20260313051569-OYJRVN",
                "Курс": "Управление ИТ-проектами на базе программного продукта ПАО «Ростелеком»",
                "Фамилия": "Осипенко",
                "Имя": "Дарья",
                "Отчество": "Игоревна",
                "Телефон": "7 (999) 023-43-65",
                "Email": "Osipenko833484@mail.ru",
                "Номер потока": 4,
            }
        )

        assert lead.order_number == "ORD-20260313051569-OYJRVN"
        assert lead.course is not None and lead.course.endswith("«Ростелеком»")
        assert (lead.last_name, lead.first_name, lead.middle_name) == (
            "Осипенко",
            "Дарья",
            "Игоревна",
        )
        assert lead.phone == "+79990234365"
        assert lead.email == "osipenko833484@mail.ru"
        assert lead.stream_number == 4
        assert lead.amount is None
        assert lead.is_order

    def test_amount_key_of_the_payments_file(self) -> None:
        assert parse_lead_payload({"Email": "a@b.ru", "Сумма": "12 500"}).amount == Decimal("12500")

    @pytest.mark.parametrize(
        "key",
        ["email", "EMAIL", " Email ", "e-mail", "E_Mail", "e mail", "Почта", "почта"],
    )
    def test_email_key_variants(self, key: str) -> None:
        assert parse_lead_payload({key: "a@b.ru"}).email == "a@b.ru"

    @pytest.mark.parametrize(
        "key", ["Номер заявки", "номер заявки", "НОМЕР  ЗАЯВКИ", " номер_заявки ", "Номер-заявки"]
    )
    def test_russian_key_variants_are_whitespace_and_case_tolerant(self, key: str) -> None:
        assert (
            parse_lead_payload(
                {"email": "a@b.ru", key: "O-1", "Курс": "К", "Имя": "И", "Фамилия": "Ф"}
            ).order_number
            == "O-1"
        )

    def test_non_breaking_space_in_a_key(self) -> None:
        assert (
            parse_lead_payload(
                {"Номер заявки": "O-1", "email": "a@b.ru", "курс": "К", "имя": "И", "фамилия": "Ф"}
            ).order_number
            == "O-1"
        )

    def test_yo_and_ye_are_the_same_letter_in_keys(self) -> None:
        assert (
            parse_lead_payload({"Email": "a@b.ru", "Отчёство": "Олегович"}).middle_name
            == "Олегович"
        )

    def test_first_non_empty_alias_wins(self) -> None:
        lead = parse_lead_payload(
            {"email": "", "Email": "b@c.ru", "phone": None, "Телефон": "+79001112233"}
        )

        assert (lead.email, lead.phone) == ("b@c.ru", "+79001112233")

    @pytest.mark.parametrize("field", ["name", "full_name", "ФИО", "фио"])
    def test_full_name_is_split(self, field: str) -> None:
        lead = parse_lead_payload({"email": "a@b.ru", field: "  Иванов   Иван Иванович "})

        assert (lead.last_name, lead.first_name, lead.middle_name) == ("Иванов", "Иван", "Иванович")

    def test_full_name_fills_only_the_missing_parts(self) -> None:
        lead = parse_lead_payload(
            {"email": "a@b.ru", "first_name": "Пётр", "name": "Иванов Иван Иванович"}
        )

        assert (lead.last_name, lead.first_name, lead.middle_name) == ("Иванов", "Пётр", "Иванович")

    def test_single_word_name_is_a_surname(self) -> None:
        lead = parse_lead_payload({"email": "a@b.ru", "name": "Иванов"})

        assert (lead.last_name, lead.first_name) == ("Иванов", None)

    def test_unknown_keys_are_ignored(self) -> None:
        assert parse_lead_payload({"email": "a@b.ru", "utm": {"a": 1}, "x": [1]}).email == "a@b.ru"

    def test_course_candidates_keep_the_order_and_drop_duplicates(self) -> None:
        lead = parse_lead_payload(
            {
                "email": "a@b.ru",
                "product_code": "py-101",
                "product_name": "Python",
                "Курс": "Python",
                "course": "  python  ",
            }
        )

        assert lead.course == "Python"
        assert lead.course_candidates == ("Python", "python", "py-101")


class TestLeadNames:
    def test_full_name_has_no_placeholders(self) -> None:
        lead = parse_lead_payload({"email": "a@b.ru", "first_name": "Иван", "last_name": "Иванов"})

        assert lead.lead_names() == ("Иван", "Иванов", None, False)

    def test_no_name_gets_both_placeholders(self) -> None:
        lead = parse_lead_payload({"email": "a@b.ru"})

        assert lead.lead_names() == (NAMELESS_FIRST_NAME, NAMELESS_LAST_NAME, None, True)
        assert (NAMELESS_FIRST_NAME, NAMELESS_LAST_NAME) == ("Без имени", "—")

    def test_partial_name_is_completed_and_marked(self) -> None:
        lead = parse_lead_payload({"email": "a@b.ru", "last_name": "Иванов", "middle_name": "И"})

        assert lead.lead_names() == (NAMELESS_FIRST_NAME, "Иванов", "И", True)


class TestContactValues:
    def test_email_and_phone_are_normalised(self) -> None:
        lead = parse_lead_payload({"email": "  A.B@Mail.RU ", "phone": "8 900 111-22-33 доб. 5"})

        assert (lead.email, lead.phone) == ("a.b@mail.ru", "+79001112233")

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (79990234365, "+79990234365"),
            (79990234365.0, "+79990234365"),
            ("79990234365", "+79990234365"),
        ],
    )
    def test_phone_may_be_a_number(self, value: Any, expected: str) -> None:
        assert parse_lead_payload({"phone": value}).phone == expected

    def test_email_or_phone_alone_is_enough(self) -> None:
        assert parse_lead_payload({"email": "a@b.ru"}).phone is None
        assert parse_lead_payload({"phone": "+79001112233"}).email is None

    @pytest.mark.parametrize("payload", [{}, {"first_name": "Иван"}, {"email": "", "phone": " "}])
    def test_no_contact_data_is_refused_with_both_fields(self, payload: dict[str, Any]) -> None:
        with pytest.raises(ValidationError) as caught:
            parse_lead_payload(payload)

        assert caught.value.detail == "Укажите email или телефон"
        assert {e.field for e in caught.value.errors} == {"email", "phone"}

    def test_invalid_email_is_not_reported_as_missing(self) -> None:
        assert _errors({"email": "nope"}) == {"email": ["некорректный адрес электронной почты"]}

    def test_invalid_phone_is_not_reported_as_missing(self) -> None:
        assert _errors({"phone": "123"}) == {"phone": ["некорректный номер телефона"]}

    def test_invalid_email_is_refused_even_with_a_good_phone(self) -> None:
        assert list(_errors({"email": "nope", "phone": "+79001112233"})) == ["email"]

    @pytest.mark.parametrize("value", [True, ["a"], {"a": 1}, 1.5])
    def test_wrong_type_of_a_phone(self, value: Any) -> None:
        assert list(_errors({"email": "a@b.ru", "phone": value})) == ["phone"]

    def test_over_long_values(self) -> None:
        errors = _errors(
            {
                "email": "a@b.ru",
                "first_name": "И" * 129,
                "last_name": "Ф" * 129,
                "middle_name": "О" * 129,
                "course": "К" * 256,
                "order_number": "N" * 65,
            }
        )

        assert set(errors) == {"first_name", "last_name", "middle_name", "course", "order_number"}

    def test_limits_are_inclusive(self) -> None:
        lead = parse_lead_payload(
            {
                "email": "a@b.ru",
                "first_name": "И" * 128,
                "last_name": "Ф" * 128,
                "course": "К" * 255,
                "order_number": "N" * 64,
            }
        )

        assert len(lead.first_name or "") == 128 and len(lead.order_number or "") == 64


class TestStreamNumber:
    @pytest.mark.parametrize("value", [1, 2, "2", " 3 ", 4.0, "4.0", 2_147_483_647])
    def test_accepted(self, value: Any) -> None:
        assert parse_lead_payload({"email": "a@b.ru", "stream_number": value}).stream_number == int(
            float(value)
        )

    @pytest.mark.parametrize(
        "value",
        [
            0,
            -1,
            "0",
            "abc",
            2.5,
            "2.5",
            True,
            False,
            [2],
            {"n": 2},
            2_147_483_648,
            "inf",
            "nan",
            "1e999",
        ],
    )
    def test_refused(self, value: Any) -> None:
        assert list(_errors({"email": "a@b.ru", "stream_number": value})) == ["stream_number"]

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_empty_means_absent(self, value: Any) -> None:
        assert parse_lead_payload({"email": "a@b.ru", "stream_number": value}).stream_number is None

    def test_russian_key(self) -> None:
        assert parse_lead_payload({"email": "a@b.ru", "Номер потока": "3"}).stream_number == 3


class TestAmount:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (1, "1.00"),
            (1500, "1500.00"),
            (1500.5, "1500.50"),
            (0.01, "0.01"),
            ("1500", "1500.00"),
            ("1500,50", "1500.50"),
            ("1500.50", "1500.50"),
            ("15 000,50", "15000.50"),
            ("15 000", "15000.00"),
            ("15 000,5", "15000.50"),
            ("1,500.25", "1500.25"),
            ("1.500,25", "1500.25"),
            (" 99,99 ", "99.99"),
            ("1e3", "1000.00"),
            ("999999999999.99", "999999999999.99"),
        ],
    )
    def test_accepted(self, value: Any, expected: str) -> None:
        assert parse_lead_payload({"email": "a@b.ru", "amount": value}).amount == Decimal(expected)

    @pytest.mark.parametrize(
        "value",
        [
            0,
            0.0,
            -1,
            -0.5,
            "0",
            "0,00",
            "-1,5",
            0.001,
            "0,004",
            "abc",
            "1 руб.",
            "1.2.3",
            "NaN",
            "nan",
            "inf",
            "-inf",
            "Infinity",
            10**12,
            10**30,
            "1e12",
            "1e999999",
            True,
            False,
            [1],
            {"a": 1},
        ],
    )
    def test_refused(self, value: Any) -> None:
        assert list(_errors({"email": "a@b.ru", "amount": value})) == ["amount"]

    @pytest.mark.parametrize("value", [None, "", "  "])
    def test_empty_means_absent(self, value: Any) -> None:
        assert parse_lead_payload({"email": "a@b.ru", "amount": value}).amount is None


class TestOrderRules:
    _ORDER = {
        "email": "a@b.ru",
        "order_number": "ORD-1",
        "course": "Курс",
        "first_name": "Иван",
        "last_name": "Иванов",
    }

    def test_full_order_is_accepted(self) -> None:
        assert parse_lead_payload(self._ORDER).is_order

    def test_lead_is_not_an_order(self) -> None:
        assert not parse_lead_payload({"email": "a@b.ru"}).is_order

    def test_course_is_required(self) -> None:
        payload = {k: v for k, v in self._ORDER.items() if k != "course"}

        with pytest.raises(ValidationError) as caught:
            parse_lead_payload(payload)

        assert caught.value.detail == "Для заказа укажите курс"
        assert [(e.field, e.reason) for e in caught.value.errors] == [
            ("course", "Для заказа укажите курс")
        ]

    def test_names_are_required(self) -> None:
        payload = {k: v for k, v in self._ORDER.items() if k not in ("first_name", "last_name")}

        assert _errors(payload) == {
            "last_name": ["обязательное поле"],
            "first_name": ["обязательное поле"],
        }

    def test_name_may_come_as_a_full_name_string(self) -> None:
        payload = {k: v for k, v in self._ORDER.items() if k not in ("first_name", "last_name")}

        lead = parse_lead_payload({**payload, "ФИО": "Иванов Иван"})

        assert (lead.last_name, lead.first_name) == ("Иванов", "Иван")

    def test_several_problems_are_reported_together_with_a_general_detail(self) -> None:
        with pytest.raises(ValidationError) as caught:
            parse_lead_payload({"order_number": "ORD-1", "stream_number": 0})

        assert caught.value.detail == "Некорректные данные заявки"
        assert {e.field for e in caught.value.errors} >= {
            "email",
            "phone",
            "stream_number",
            "course",
            "last_name",
            "first_name",
        }

    def test_blank_order_number_makes_a_lead(self) -> None:
        assert not parse_lead_payload({"email": "a@b.ru", "order_number": "  "}).is_order


class TestInformationalFields:
    def test_long_comment_and_url_are_cut_not_refused(self) -> None:
        lead = parse_lead_payload(
            {
                "email": "a@b.ru",
                "comment": "к" * 10_000,
                "source_url": "u" * 5000,
                "external_id": "e" * 300,
            }
        )

        assert (
            len(lead.comment or ""),
            len(lead.source_url or ""),
            len(lead.external_id or ""),
        ) == (
            4000,
            2000,
            255,
        )

    @pytest.mark.parametrize("value", [["a"], {"a": 1}, True, 1.5])
    def test_wrongly_typed_comment_is_ignored_not_refused(self, value: Any) -> None:
        lead = parse_lead_payload({"email": "a@b.ru", "comment": value, "source_url": value})

        assert (lead.comment, lead.source_url) == (None, None)

    def test_number_becomes_text(self) -> None:
        assert parse_lead_payload({"email": "a@b.ru", "comment": 12345}).comment == "12345"

    @pytest.mark.parametrize("value", ["вчера", 12345, None, "", ["x"]])
    def test_created_at_of_unknown_form_is_ignored(self, value: Any) -> None:
        assert parse_lead_payload({"email": "a@b.ru", "created_at": value}).created_at is None

    def test_naive_created_at_is_taken_as_utc(self) -> None:
        lead = parse_lead_payload({"email": "a@b.ru", "created_at": "2026-09-25T12:30:00"})

        assert lead.created_at == dt.datetime(2026, 9, 25, 12, 30, tzinfo=dt.UTC)

    def test_z_suffix(self) -> None:
        lead = parse_lead_payload({"email": "a@b.ru", "created_at": "2026-09-25T12:30:00Z"})

        assert lead.created_at == dt.datetime(2026, 9, 25, 12, 30, tzinfo=dt.UTC)
