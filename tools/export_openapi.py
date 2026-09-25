"""Экспорт OpenAPI-схемы приложения в детерминированный JSON.

    python tools/export_openapi.py            # записать openapi.json в корень репо
    python tools/export_openapi.py --check    # упасть, если файл отличается от схемы

Схема — контракт с фронтендом (frontend/tools/gen-api.mjs строит из неё
`schema.d.ts`). Закоммиченный `openapi.json` + `--check` в CI делают любое
изменение API видимым в diff'е PR и не дают контракту разойтись с кодом.

Не требует БД/Redis/Keycloak: приложение только собирается, не стартует
(lifespan не запускается). Настройки берутся из окружения или `.env`;
без них подставляются значения из `.env.example`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "openapi.json"


def _load_defaults() -> None:
    """Подставляет значения из .env.example для переменных, которых нет в окружении."""
    import os

    example = ROOT / ".env.example"
    for raw in example.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def render() -> str:
    _load_defaults()
    sys.path.insert(0, str(ROOT))
    from app.main import create_app

    schema = create_app().openapi()
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
                "openapi.json устарел: выполните `python tools/export_openapi.py` "
                "и закоммитьте результат.",
                file=sys.stderr,
            )
            return 1
        print("openapi.json актуален")
        return 0

    TARGET.write_text(rendered, encoding="utf-8", newline="\n")
    print(f"записано {TARGET.relative_to(ROOT)} ({len(rendered)} байт)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
