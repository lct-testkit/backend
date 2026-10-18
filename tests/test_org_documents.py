"""B6-14: пакет организационных документов (`docs/security/org/`) — каркас, не смысл.

Без БД. Проверяет форму, а не юридическое содержание (это не юридическая консультация —
тексты — черновики, `status: draft`, требуют утверждения юристом оператора, см. README
каталога): у каждого файла есть блок `---` с четырьмя ключами, в готовых документах нет
`TODO`/`XXX`, относительные ссылки в README указывают на существующие файлы.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ORG_DIR = _REPO_ROOT / "docs" / "security" / "org"
_REQUIRED_FRONT_MATTER_KEYS = {"version", "status", "owner", "review_by"}
_EXPECTED_FILES = [
    "README.md",
    "01-pdn-policy.md",
    "02-pdn-inventory.md",
    "03-threat-model.md",
    "04-order-responsible.md",
    "05-destruction-act-manual.md",
]

_FRONT_MATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def _front_matter(text: str) -> dict[str, str]:
    match = _FRONT_MATTER_RE.match(text)
    if not match:
        return {}
    keys: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            keys[key.strip()] = value.strip()
    return keys


class TestCatalogExists:
    def test_all_six_files_are_present(self) -> None:
        assert _ORG_DIR.is_dir(), f"Каталог не найден: {_ORG_DIR}"
        missing = [name for name in _EXPECTED_FILES if not (_ORG_DIR / name).is_file()]
        assert not missing, f"Отсутствуют файлы пакета: {missing}"


class TestFrontMatter:
    def test_every_file_declares_the_four_required_keys(self) -> None:
        problems = []
        for name in _EXPECTED_FILES:
            path = _ORG_DIR / name
            if not path.is_file():
                continue
            keys = set(_front_matter(path.read_text(encoding="utf-8")))
            missing = _REQUIRED_FRONT_MATTER_KEYS - keys
            if missing:
                problems.append(f"{name}: нет ключей {sorted(missing)}")
        assert not problems, problems

    def test_status_is_either_draft_or_approved(self) -> None:
        problems = []
        for name in _EXPECTED_FILES:
            path = _ORG_DIR / name
            if not path.is_file():
                continue
            status = _front_matter(path.read_text(encoding="utf-8")).get("status")
            if status not in {"draft", "approved"}:
                problems.append(f"{name}: status={status!r}")
        assert not problems, problems


class TestNoLeftoverMarkers:
    def test_finished_documents_have_no_todo_or_xxx(self) -> None:
        problems = []
        for name in _EXPECTED_FILES:
            path = _ORG_DIR / name
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            for marker in ("TODO", "XXX"):
                if marker in text:
                    problems.append(f"{name}: содержит {marker}")
        assert not problems, problems


class TestReadmeLinks:
    def test_relative_links_point_at_existing_files(self) -> None:
        readme = _ORG_DIR / "README.md"
        text = readme.read_text(encoding="utf-8")
        problems = []
        for target in _LINK_RE.findall(text):
            if target.startswith(("http://", "https://", "#")):
                continue
            if not (_ORG_DIR / target).is_file():
                problems.append(target)
        assert not problems, f"README ссылается на несуществующие файлы: {problems}"
