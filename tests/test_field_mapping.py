"""B5-3: `integration.field_mapping` — чистый модуль (без БД), по образцу
`tests/test_lead_payload.py`.
"""

from __future__ import annotations

import pytest

from app.modules.integration.field_mapping import FieldMapping, MappingRule, apply_mapping


def test_none_mapping_is_a_no_op() -> None:
    raw = {"lead_id": "x", "name": "Иван"}
    result = apply_mapping(raw, None, "cms")
    assert result.payload == raw
    assert result.ignored_keys == []


def test_rename_with_trim() -> None:
    mapping = FieldMapping(
        mode="strict",
        rules=[MappingRule(target="first_name", source="Имя ", transform="trim")],
    )
    result = apply_mapping({"Имя ": "  Мария  "}, mapping, "cms")
    assert result.payload == {"first_name": "Мария"}


def test_value_map_with_hit_miss_and_default() -> None:
    hit = MappingRule(
        target="course",
        source="product_interest",
        transform="value_map",
        value_map={"python-basics": "py-base"},
        default="unknown",
    )
    mapping = FieldMapping(rules=[hit])

    assert apply_mapping({"product_interest": "python-basics"}, mapping, "cms").payload == {
        "course": "py-base"
    }
    miss = apply_mapping({"product_interest": "rust-adv"}, mapping, "cms")
    assert miss.payload == {"course": "unknown"}
    assert miss.warnings  # промах value_map предупреждает, но не отклоняет


def test_template_substitutes_value_only() -> None:
    mapping = FieldMapping(
        rules=[
            MappingRule(
                target="comment",
                source="source_campaign",
                transform="template",
                template="Кампания: {value}",
            )
        ]
    )
    result = apply_mapping({"source_campaign": "yandex-direct-sept"}, mapping, "cms")
    assert result.payload == {"comment": "Кампания: yandex-direct-sept"}


def test_join_combines_two_fields() -> None:
    mapping = FieldMapping(
        rules=[MappingRule(target="name", sources=["last_name", "first_name"], transform="join")]
    )
    result = apply_mapping({"last_name": "Иванов", "first_name": "Иван"}, mapping, "cms")
    assert result.payload == {"name": "Иванов Иван"}


def test_constant_does_not_need_a_source() -> None:
    mapping = FieldMapping(
        rules=[MappingRule(target="source_url", transform="constant", constant="site")]
    )
    result = apply_mapping({}, mapping, "cms")
    assert result.payload == {"source_url": "site"}


def test_fallback_chain_first_non_empty_wins() -> None:
    mapping = FieldMapping(
        rules=[
            MappingRule(target="phone", source="mobile"),
            MappingRule(target="phone", source="phone"),
        ]
    )
    result = apply_mapping({"phone": "+70000000000"}, mapping, "cms")
    assert result.payload == {"phone": "+70000000000"}


def test_strict_mode_drops_unmapped_keys() -> None:
    mapping = FieldMapping(rules=[MappingRule(target="phone", source="phone")])
    result = apply_mapping({"phone": "1", "junk": "2"}, mapping, "cms")
    assert result.payload == {"phone": "1"}
    assert result.ignored_keys == ["junk"]


def test_passthrough_mode_keeps_unmapped_keys() -> None:
    mapping = FieldMapping(mode="passthrough", rules=[MappingRule(target="phone", source="phone")])
    result = apply_mapping({"phone": "1", "course": "py"}, mapping, "cms")
    assert result.payload == {"phone": "1", "course": "py"}
    assert result.ignored_keys == []


def test_dotted_target_nests_into_custom_fields() -> None:
    mapping = FieldMapping(
        rules=[MappingRule(target="custom_fields.utm", source="source_campaign")]
    )
    result = apply_mapping({"source_campaign": "yandex"}, mapping, "cms")
    assert result.payload == {"custom_fields": {"utm": "yandex"}}


def test_key_matching_ignores_case_spaces_and_dashes() -> None:
    mapping = FieldMapping(rules=[MappingRule(target="course", source="Product_Interest")])
    result = apply_mapping({"product interest": "py-base"}, mapping, "cms")
    assert result.payload == {"course": "py-base"}


def test_required_without_value_warns() -> None:
    mapping = FieldMapping(rules=[MappingRule(target="email", source="email", required=True)])
    result = apply_mapping({}, mapping, "cms")
    assert result.payload == {}
    assert any("email" in w for w in result.warnings)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"target": "course", "transform": "join"}, "«склейка» нужно не меньше двух"),
        (
            {"target": "course", "source": "x", "transform": "value_map"},
            "«таблица значений» нужен словарь",
        ),
        (
            {"target": "course", "source": "x", "transform": "template"},
            "«шаблон» нужна строка",
        ),
        ({"target": "course"}, "Нужен источник значения"),
    ],
)
def test_rule_shape_validation(kwargs: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        MappingRule(**kwargs)


def test_more_than_fifty_rules_is_rejected_by_the_schema() -> None:
    rules = [MappingRule(target=f"custom_fields.f{i}", source=f"s{i}") for i in range(51)]
    with pytest.raises(ValueError):
        FieldMapping(rules=rules)
