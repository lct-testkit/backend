"""Крайние значения DSL условий переходов (`app.modules.workflow.dsl`).

Найдено внешним тестированием: `NaN` проходил проверку условия и ронял вычисление 500-й ошибкой, а
строка `"false"` считалась истиной при сравнении с булевым значением.
"""

from __future__ import annotations

import pytest

from app.modules.workflow import dsl


def _ok(node: dict, context: dict) -> bool:
    return dsl.evaluate(node, context).ok


class TestNonFiniteNumbers:
    @pytest.mark.parametrize(
        "bad", ["NaN", "nan", "Infinity", "-Infinity", float("nan"), float("inf")]
    )
    def test_comparison_with_non_finite_actual_is_simply_false(self, bad) -> None:
        for op in ("gt", "gte", "lt", "lte"):
            assert _ok({"field": "amount", "op": op, "value": 0}, {"amount": bad}) is False

    @pytest.mark.parametrize("bad", ["NaN", "Infinity", float("nan")])
    def test_non_finite_expected_value_is_rejected_by_validation(self, bad) -> None:
        errors = dsl.validate_condition({"field": "amount", "op": "gt", "value": bad})
        assert any("число" in e for e in errors)

    def test_equality_does_not_treat_nan_as_a_number(self) -> None:
        assert _ok({"field": "amount", "op": "eq", "value": 5}, {"amount": "NaN"}) is False

    def test_regular_numbers_still_work(self) -> None:
        assert _ok(
            {"field": "amount", "op": "gt", "value": 0}, {"amount": "1500,5".replace(",", ".")}
        )
        assert _ok({"field": "amount", "op": "lte", "value": "10"}, {"amount": 10})


class TestBooleanComparison:
    @pytest.mark.parametrize("stored", ["false", "False", "0", "нет", "no", False, 0])
    def test_falsy_words_are_not_equal_to_true(self, stored) -> None:
        node = {"field": "custom_fields.payment_confirmed", "op": "eq", "value": True}
        assert _ok(node, {"custom_fields": {"payment_confirmed": stored}}) is False

    @pytest.mark.parametrize("stored", ["true", "True", "1", "да", "yes", True, 1])
    def test_truthy_words_are_equal_to_true(self, stored) -> None:
        node = {"field": "custom_fields.payment_confirmed", "op": "eq", "value": True}
        assert _ok(node, {"custom_fields": {"payment_confirmed": stored}}) is True

    def test_false_condition_matches_false_words(self) -> None:
        node = {"field": "custom_fields.flag", "op": "eq", "value": False}
        assert _ok(node, {"custom_fields": {"flag": "false"}}) is True
        assert _ok(node, {"custom_fields": {"flag": "true"}}) is False

    def test_arbitrary_text_is_neither_true_nor_false(self) -> None:
        eq_true = {"field": "custom_fields.flag", "op": "eq", "value": True}
        eq_false = {"field": "custom_fields.flag", "op": "eq", "value": False}
        context = {"custom_fields": {"flag": "возможно"}}
        assert _ok(eq_true, context) is False
        assert _ok(eq_false, context) is False

    def test_missing_field_is_not_equal_to_false(self) -> None:
        node = {"field": "custom_fields.flag", "op": "eq", "value": False}
        assert _ok(node, {"custom_fields": {}}) is False

    def test_neq_true_for_false_word(self) -> None:
        node = {"field": "custom_fields.flag", "op": "neq", "value": True}
        assert _ok(node, {"custom_fields": {"flag": "false"}}) is True
