"""B6-14: перечень ПДн (`docs/security/org/02-pdn-inventory.md`) не должен отставать от схемы.

Без БД: разбирает `app.db.models.Base.metadata` напрямую. Колонка, чьё имя целиком совпадает
с одним из шаблонов (`phone`, `email`, `snils`, `passport_.*`, `birth_date`, `first_name`,
`last_name`, `middle_name`, `full_name`, `display_name`, `reg_.*`, `.*_dative`, `ip`,
`user_agent`) или входит в список явных добавок, обязана встречаться в перечне как
`таблица.колонка`, либо быть в `NOT_PDN` с причиной (например, «название юридического
лица, не физлица»). Обратная проверка: каждая строка перечня существует в метаданных —
переименование/удаление колонки не должно оставлять делу устаревшую строку.

После слияния эпиков таблиц больше, чем в исходном плане спринта (B4/B5-доступы, лицензии,
импорт и т.п.) — список явных исключений (`NOT_PDN`) рассчитан на ЭТУ объединённую схему, не
на черновик плана; при новом слиянии сообщение падения называет ровно то, что добавить.
"""

from __future__ import annotations

import re
from pathlib import Path

import app.db.models as db_models

_REPO_ROOT = Path(__file__).resolve().parent.parent
_INVENTORY_PATH = _REPO_ROOT / "docs" / "security" / "org" / "02-pdn-inventory.md"

_PATTERNS = [
    re.compile(p)
    for p in (
        r"phone",
        r"email",
        r"snils",
        r"passport_.*",
        r"birth_date",
        r"first_name",
        r"last_name",
        r"middle_name",
        r"full_name",
        r"display_name",
        r"reg_.*",
        r".*_dative",
        r"ip",
        r"user_agent",
    )
]
#: Добавки, которые не подходят ни под один шаблон по имени, но прямо названы в задаче B6-14.
_EXPLICIT_ADDITIONS = frozenset(
    {
        ("contact_channels", "value"),
        ("deal_comments", "body"),
        ("files", "original_filename"),
    }
)

#: Колонки, которые подходят под шаблон по имени, но не являются ПДн физлица — с причиной,
#: читаемой в сообщении об ошибке теста.
NOT_PDN: dict[str, str] = {
    "egrul_entries.full_name": "наименование юридического лица из ЕГРЮЛ, не физлица",
}


def _candidate_columns() -> set[str]:
    found: set[str] = set()
    for table in db_models.Base.metadata.tables.values():
        for column in table.columns:
            name = column.name
            matches_pattern = any(p.fullmatch(name) for p in _PATTERNS)
            if matches_pattern or (table.name, name) in _EXPLICIT_ADDITIONS:
                found.add(f"{table.name}.{name}")
    return found


def _inventory_rows() -> set[str]:
    text = _INVENTORY_PATH.read_text(encoding="utf-8")
    # Строки таблицы перечня: `| \`users.email\` | ... |` — первая ячейка `таблица.колонка`,
    # обёрнутая в обратные кавычки (код-стиль Markdown).
    return set(re.findall(r"^\|\s*`([a-z_]+\.[a-z_]+)`\s*\|", text, flags=re.MULTILINE))


class TestPdnInventory:
    def test_the_inventory_file_exists(self) -> None:
        assert _INVENTORY_PATH.exists(), f"Перечень ПДн не найден: {_INVENTORY_PATH}"

    def test_every_pii_shaped_column_is_listed_or_excused(self) -> None:
        candidates = _candidate_columns()
        rows = _inventory_rows()
        missing = sorted(col for col in candidates if col not in rows and col not in NOT_PDN)
        assert not missing, (
            "Колонки похожи на ПДн, но не упомянуты ни в перечне "
            f"({_INVENTORY_PATH.relative_to(_REPO_ROOT)}), ни в NOT_PDN "
            f"(tests/test_pdn_inventory.py): {missing}. Добавьте строку `таблица.колонка` "
            "в перечень или в NOT_PDN с причиной."
        )

    def test_every_not_pdn_entry_is_still_a_real_column(self) -> None:
        candidates = _candidate_columns()
        stale = sorted(col for col in NOT_PDN if col not in candidates)
        assert not stale, f"NOT_PDN ссылается на колонки, которых больше нет: {stale}"

    def test_every_inventory_row_still_exists_in_the_models(self) -> None:
        all_columns = {
            f"{table.name}.{column.name}"
            for table in db_models.Base.metadata.tables.values()
            for column in table.columns
        }
        rows = _inventory_rows()
        stale = sorted(row for row in rows if row not in all_columns)
        assert not stale, (
            f"Перечень ПДн ссылается на колонки, которых больше нет в моделях: {stale} "
            "(колонку переименовали/удалили — поправьте строку перечня)"
        )
