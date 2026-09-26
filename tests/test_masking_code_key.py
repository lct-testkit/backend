"""Ключ `code` не секрет: коды продуктов и справочников остаются в аудите, одноразовые — нет."""

from __future__ import annotations

from app.core.masking import mask_mapping


def test_catalog_code_survives_audit_masking() -> None:
    masked = mask_mapping({"code": {"old": None, "new": "rt-datalake"}})
    assert masked == {"code": {"old": None, "new": "rt-datalake"}}


def test_field_error_code_survives_in_problem_extra() -> None:
    masked = mask_mapping({"errors": [{"field": "email", "reason": "неверный", "code": "E1"}]})
    assert masked["errors"][0]["code"] == "E1"  # type: ignore[index]


def test_one_time_codes_are_still_redacted() -> None:
    masked = mask_mapping(
        {"otp": "123456", "otp_code": "123456", "authorization_code": "abc", "auth_code": "d"}
    )
    assert set(masked.values()) == {"***"}  # type: ignore[union-attr]
