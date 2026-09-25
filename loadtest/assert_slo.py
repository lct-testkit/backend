"""Оценщик результатов Locust: превращает CSV в проходит/не проходит.

Критерий успеха №1 паспорта (new_spec §0): p95 времени ответа API на операции
«переход по статусу» и «добавление комментария» ≤ 300 мс при 50 RPS.

    python loadtest/assert_slo.py loadtest/results/transition_stats.csv \\
        --p95-ms 300 --max-fail-pct 1 --min-rps 45 --min-requests 1000

Проверяет строку `Aggregated` файла `<prefix>_stats.csv`:
  * p95 (`95%`) ≤ --p95-ms;
  * доля неуспешных запросов ≤ --max-fail-pct;
  * достигнутый RPS ≥ --min-rps — иначе прогон «зелёный вхолостую»: сервис мог
    просто не дать заявленной нагрузки;
  * запросов не меньше --min-requests.

Сводка печатается в stdout и, если задан, дописывается в $GITHUB_STEP_SUMMARY.
Код возврата 1 — SLO нарушен.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path


def read_aggregated(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("Name") == "Aggregated":
                return row
    raise SystemExit(f"{path}: нет строки Aggregated")


def evaluate(
    row: dict[str, str],
    *,
    p95_ms: float,
    max_fail_pct: float,
    min_rps: float,
    min_requests: int,
) -> tuple[list[str], dict[str, float]]:
    requests_count = int(row["Request Count"])
    failures = int(row["Failure Count"])
    metrics = {
        "requests": float(requests_count),
        "failures": float(failures),
        "fail_pct": (failures / requests_count * 100) if requests_count else 100.0,
        "rps": float(row["Requests/s"]),
        "p50": float(row["50%"]),
        "p95": float(row["95%"]),
        "p99": float(row["99%"]),
    }
    problems: list[str] = []
    if requests_count < min_requests:
        problems.append(f"запросов {requests_count} < {min_requests}")
    if metrics["p95"] > p95_ms:
        problems.append(f"p95 {metrics['p95']:.0f} мс > {p95_ms:.0f} мс")
    if metrics["fail_pct"] > max_fail_pct:
        problems.append(f"ошибок {metrics['fail_pct']:.2f}% > {max_fail_pct}%")
    if metrics["rps"] < min_rps:
        problems.append(f"достигнуто {metrics['rps']:.1f} RPS < {min_rps} RPS")
    return problems, metrics


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stats", type=Path, help="<prefix>_stats.csv")
    parser.add_argument("--name", default=None, help="подпись в сводке (по умолчанию — имя файла)")
    parser.add_argument("--p95-ms", type=float, default=300.0)
    parser.add_argument("--max-fail-pct", type=float, default=1.0)
    parser.add_argument("--min-rps", type=float, default=45.0)
    parser.add_argument("--min-requests", type=int, default=1000)
    args = parser.parse_args()

    problems, m = evaluate(
        read_aggregated(args.stats),
        p95_ms=args.p95_ms,
        max_fail_pct=args.max_fail_pct,
        min_rps=args.min_rps,
        min_requests=args.min_requests,
    )
    name = args.name or args.stats.stem.removesuffix("_stats")
    verdict = "✅ ПРОЙДЕН" if not problems else "❌ НАРУШЕН"
    lines = [
        f"### Нагрузка `{name}` — {verdict}",
        "",
        "| запросов | ошибок | RPS | p50 | p95 | p99 | порог p95 |",
        "|---|---|---|---|---|---|---|",
        f"| {int(m['requests'])} | {m['fail_pct']:.2f}% | {m['rps']:.1f} | {m['p50']:.0f} мс "
        f"| **{m['p95']:.0f} мс** | {m['p99']:.0f} мс | {args.p95_ms:.0f} мс |",
    ]
    if problems:
        lines += ["", *[f"- {p}" for p in problems]]
    text = "\n".join(lines)
    print(text)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(text + "\n\n")

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
