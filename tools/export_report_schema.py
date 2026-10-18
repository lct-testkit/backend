"""Экспорт JSON Schema результирующего json-файла отчёта (B9-9) в детерминированный JSON.

    python tools/export_report_schema.py          # записать docs/schemas/rtk-crm-report-1...
    python tools/export_report_schema.py --check   # упасть, если файл отличается от схемы

Схема строится из `app.modules.reporting.result_schema.ReportResultV1.model_json_schema()` —
контракт результирующего json-файла (rtk_requiriments.md §6 п.4, B1-5). Закоммиченный файл +
`--check` в CI делают любое изменение формы файла видимым в diff'е PR, тем же приёмом, что
`tools/export_openapi.py` уже применяет к `openapi.json`.

Не требует БД/Redis/Keycloak: модель собирается без сети.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "docs" / "schemas" / "rtk-crm-report-1.schema.json"


def render() -> str:
    sys.path.insert(0, str(ROOT))
    from app.modules.reporting.result_schema import ReportResultV1

    schema = ReportResultV1.model_json_schema(by_alias=True)
    return json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="только сравнить, не писать")
    args = parser.parse_args()

    rendered = render()
    if args.check:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != rendered:
            print(
                f"{TARGET.relative_to(ROOT)} устарела: выполните "
                "`python tools/export_report_schema.py` и закоммитьте результат.",
                file=sys.stderr,
            )
            return 1
        print(f"{TARGET.relative_to(ROOT)} актуальна")
        return 0

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"записано {TARGET.relative_to(ROOT)} ({len(rendered)} байт)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
