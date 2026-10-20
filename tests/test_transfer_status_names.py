"""R-14 / M-5 (QA 20.10): один набор названий статусов передачи лицензии в отчёте, шаблоне
импорта и подсказках — те же, что в интерфейсе («Передана / В процессе / Не начата / Не
состоялась»); импорт по-прежнему принимает все прежние написания."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest

from app.modules.catalog.license_values import TRANSFER_STATUS_LABELS, parse_transfer_status
from app.modules.reporting.builders import _license_signed_label

UI_LABELS = {
    "transferred": "Передана",
    "in_progress": "В процессе",
    "not_started": "Не начата",
    "declined": "Не состоялась",
}


def test_report_labels_equal_the_interface_labels() -> None:
    assert TRANSFER_STATUS_LABELS == UI_LABELS


@pytest.mark.parametrize(("code", "label"), list(UI_LABELS.items()))
def test_every_label_is_accepted_back_by_the_import(code: str, label: str) -> None:
    assert parse_transfer_status(label) == (code, None)
    assert parse_transfer_status(label.lower()) == (code, None)


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("передано", "transferred"),
        ("Выдана", "transferred"),
        ("в работе", "in_progress"),
        ("На согласовании", "in_progress"),
        ("не начато", "not_started"),
        ("ожидает", "not_started"),
        ("отказ", "declined"),
        ("Отклонено", "declined"),
        ("not_started", "not_started"),
    ],
)
def test_old_names_and_synonyms_still_import(raw: str, code: str) -> None:
    assert parse_transfer_status(raw) == (code, None)


def test_the_error_hint_lists_the_interface_names() -> None:
    _, error = parse_transfer_status("непонятно что")
    assert error is not None
    for label in UI_LABELS.values():
        assert label.lower() in error


@pytest.mark.parametrize(
    ("signed_at", "is_signed", "expected"),
    [
        (dt.date(2024, 3, 15), True, "15.03.2024"),
        (dt.date(2024, 3, 15), None, "15.03.2024"),
        (None, True, "Подписана"),
        (None, False, "Не подписана"),
        (None, None, "—"),
    ],
)
def test_signed_column_shows_the_date_like_the_interface(signed_at, is_signed, expected) -> None:
    license_ = SimpleNamespace(license_signed_at=signed_at, is_signed=is_signed)
    assert _license_signed_label(license_) == expected


class TestLicenseEndYear:
    """Форма лицензии пишет дату окончания или срок в годах, а не `license_valid_year`: без вывода
    года из них «Срок действия, год» и «Срок» в реестре оставались пустыми."""

    @staticmethod
    def _license(**fields):
        base = {
            "license_valid_year": None,
            "license_valid_until": None,
            "license_signed_at": None,
            "license_term_years": None,
        }
        return SimpleNamespace(**{**base, **fields})

    def test_explicit_year_wins(self) -> None:
        from app.modules.reporting.builders import _license_end_year

        license_ = self._license(license_valid_year=2027, license_valid_until=dt.date(2030, 1, 1))
        assert _license_end_year(license_) == 2027

    def test_year_from_the_end_date(self) -> None:
        from app.modules.reporting.builders import _license_end_year

        assert _license_end_year(self._license(license_valid_until=dt.date(2028, 5, 21))) == 2028

    def test_year_from_signing_date_plus_term(self) -> None:
        from app.modules.reporting.builders import _license_end_year

        license_ = self._license(license_signed_at=dt.date(2026, 9, 1), license_term_years=3)
        assert _license_end_year(license_) == 2029

    def test_nothing_known(self) -> None:
        from app.modules.reporting.builders import _license_end_year

        assert _license_end_year(self._license()) is None

    def test_validity_label_for_perpetual(self) -> None:
        from app.modules.reporting.builders import _license_validity_label

        assert _license_validity_label(None, current_year=2026, perpetual=True) == "Бессрочная"
        assert _license_validity_label(2025, current_year=2026) == "Истёк"
