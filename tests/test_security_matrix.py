"""B6-1: матрица мер ФСТЭК 117/152-ФЗ не должна устаревать молча.

Разбирает `backend/docs/security/measures-matrix.md`: для каждой ссылки вида `путь:строка`
(и `путь:строка1,строка2`, `путь:начало-конец`) проверяет, что файл существует в пределах
`backend/` и в нём не меньше указанного числа строк; строка со статусом «есть» без единой
ссылки на код — ошибка. Пути, начинающиеся с `deploy/`, пропускаются (другой репозиторий —
см. докстринг самой матрицы). Тест не проверяет смысл ссылки, он ловит явное устаревание:
файл переименовали/укоротили, а строку в таблице не поправили.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MATRIX_PATH = _REPO_ROOT / "docs" / "security" / "measures-matrix.md"

#: `` `path/to/file.ext:12`, `path:12,34`, `path:12-34` ``. Путь обязан содержать `/` (не
#: голое имя без каталога) — так не путается с обычным кодом в других backtick-спанах;
#: расширение не обязательно (`deploy/Caddyfile` без точки — тоже реальный файл).
_REF_RE = re.compile(r"`([\w\-./]+/[\w\-.]+):([\d,\-]+)`")
#: Строка таблицы markdown: `| a | b | c | d |` (минимум 3 разделителя `|` внутри).
_TABLE_ROW_RE = re.compile(r"^\|(.+)\|$")


def _matrix_text() -> str:
    assert _MATRIX_PATH.exists(), f"Матрица не найдена: {_MATRIX_PATH}"
    return _MATRIX_PATH.read_text(encoding="utf-8")


def _table_rows(text: str) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in text.splitlines():
        match = _TABLE_ROW_RE.match(line.strip())
        if not match:
            continue
        cells = [c.strip() for c in match.group(1).split("|")]
        if not cells or set(cells[0]) <= {"-", " "}:  # строка-разделитель "---|---|..."
            continue
        rows.append(cells)
    return rows


def _line_numbers(spec: str) -> list[int]:
    """`"12,34"` -> `[12, 34]`; `"12-34"` -> `[12, 34]` (границы, не весь диапазон)."""
    numbers: list[int] = []
    for chunk in spec.split(","):
        for part in chunk.split("-"):
            if part.strip().isdigit():
                numbers.append(int(part))
    return numbers


class TestMeasuresMatrix:
    def test_file_exists_and_has_a_table(self) -> None:
        text = _matrix_text()
        rows = _table_rows(text)
        # Шапка + разделитель уже отфильтрованы _table_rows; должно остаться много строк мер.
        assert len(rows) > 15, "Таблица мер пуста или не распознана"

    def test_every_reference_points_at_a_real_file_with_enough_lines(self) -> None:
        text = _matrix_text()
        refs = _REF_RE.findall(text)
        assert refs, "В матрице не найдено ни одной ссылки `путь:строка`"
        problems: list[str] = []
        for path, line_spec in refs:
            if path.startswith("deploy/"):
                continue  # другой репозиторий (см. докстринг матрицы)
            full_path = _REPO_ROOT / path
            if not full_path.is_file():
                problems.append(f"{path}: файл не существует")
                continue
            line_count = sum(1 for _ in full_path.open(encoding="utf-8", errors="replace"))
            for number in _line_numbers(line_spec):
                if number > line_count:
                    problems.append(f"{path}:{number}: в файле только {line_count} строк")
        assert not problems, "Устаревшие ссылки в матрице:\n" + "\n".join(problems)

    def test_status_есть_has_at_least_one_code_reference(self) -> None:
        text = _matrix_text()
        rows = _table_rows(text)
        data_rows = [r for r in rows if len(r) >= 3 and r[1] in {"есть", "частично", "нет"}]
        assert data_rows, "Не нашлось строк со статусом"
        missing = [row[0] for row in data_rows if row[1] == "есть" and not _REF_RE.search(row[2])]
        assert not missing, f"Статус «есть» без ссылки на код: {missing}"
