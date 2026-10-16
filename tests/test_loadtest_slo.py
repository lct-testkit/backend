"""Оценщик SLO нагрузочного теста (loadtest/assert_slo.py): граничные случаи."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "assert_slo", Path(__file__).resolve().parent.parent / "loadtest" / "assert_slo.py"
)
assert _SPEC and _SPEC.loader
assert_slo = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(assert_slo)


def _row(**overrides: str) -> dict[str, str]:
    row = {
        "Name": "Aggregated",
        "Request Count": "3000",
        "Failure Count": "0",
        "Requests/s": "50.0",
        "50%": "40",
        "95%": "120",
        "99%": "200",
    }
    row.update(overrides)
    return row


def _evaluate(row: dict[str, str]) -> list[str]:
    problems, _ = assert_slo.evaluate(
        row, p95_ms=300, max_fail_pct=1, min_rps=45, min_requests=1000
    )
    return problems


def test_healthy_run_passes() -> None:
    assert _evaluate(_row()) == []


def test_p95_over_threshold_fails() -> None:
    assert any("p95" in p for p in _evaluate(_row(**{"95%": "301"})))


def test_p95_exactly_at_threshold_passes() -> None:
    assert _evaluate(_row(**{"95%": "300"})) == []


def test_error_rate_over_threshold_fails() -> None:
    problems = _evaluate(_row(**{"Failure Count": "31"}))  # 1.03%
    assert any("ошибок" in p for p in problems)


def test_low_throughput_fails_instead_of_passing_vacuously() -> None:
    assert any("RPS" in p for p in _evaluate(_row(**{"Requests/s": "20.0"})))


def test_too_few_requests_fails() -> None:
    assert any("запросов" in p for p in _evaluate(_row(**{"Request Count": "10"})))


def test_missing_aggregated_row_is_an_error(tmp_path: Path) -> None:
    csv_path = tmp_path / "x_stats.csv"
    csv_path.write_text("Name,Request Count\nGET /,1\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        assert_slo.read_aggregated(csv_path)


def test_loadtest_pair_has_no_guards() -> None:
    """B2-4: нагрузочный сценарий `transition` (`locustfile_transition.py`,
    `provision.py`) шлёт переход без комментария и без файлов — пара, на которой он
    работает, обязана быть без `conditions`, `actions` и `requires_comment`. После
    B2-1 пара сменилась с `identification → first_contact` воронки v1 на
    `meeting → documents_exchange` воронки `b2b_university_v2`."""
    from app.modules.workflow.seed import _b2b_spec

    spec = _b2b_spec()
    pair = next(
        t
        for t in spec.transitions
        if t.from_code == "meeting" and t.to_code == "documents_exchange"
    )
    assert pair.conditions in (None, {})
    assert pair.actions in (None, [])
    assert pair.requires_comment is False
