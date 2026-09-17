"""Автоподбор маппинга колонок (dop.md §4.12, п.3).

«Левенштейн + словарь синонимов»: точное совпадение и известный синоним —
раньше нечёткого сравнения, оно только для колонок, которые не нашлись
ни одним из двух способов. Без внешней зависимости (`python-Levenshtein`/
`rapidfuzz`) — расстояние в 15 строк не стоит нового пакета в закрытом контуре.
"""

from __future__ import annotations

from app.modules.imports.fields import FieldSpec

# Русские синонимы для колонок исходного файла -> код целевого поля.
# Ключи — в нижнем регистре, без пробуждающих пробелов по краям.
_SYNONYMS: dict[str, str] = {
    "наименование": "name",
    "название": "name",
    "полное наименование": "name",
    "организация": "name",
    "вуз": "name",
    "краткое наименование": "short_name",
    "сокращённое наименование": "short_name",
    "сокращенное наименование": "short_name",
    "инн": "inn",
    "кпп": "kpp",
    "огрн": "ogrn",
    "юридический адрес": "legal_address",
    "адрес": "legal_address",
    "фактический адрес": "actual_address",
    "сайт": "website",
    "веб-сайт": "website",
    "телефон": "main_phone",
    "email": "main_email",
    "e-mail": "main_email",
    "почта": "main_email",
    "количество студентов": "students_count",
    "число студентов": "students_count",
    "контингент": "students_count",
    "код": "code",
    "продукт": "name",
    "направление": "direction_code",
    "формат": "format",
    "цена": "base_price",
    "стоимость": "base_price",
    "валюта": "currency",
    "длительность": "duration_hours",
    "часы": "duration_hours",
}


def _normalize(header: str) -> str:
    return " ".join(header.strip().lower().split())


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            current[j] = min(
                previous[j] + 1,  # удаление
                current[j - 1] + 1,  # вставка
                previous[j - 1] + cost,  # замена
            )
        previous = current
    return previous[-1]


def _best_fuzzy_match(header: str, candidates: list[FieldSpec]) -> FieldSpec | None:
    normalized = _normalize(header)
    best: tuple[int, FieldSpec] | None = None
    for spec in candidates:
        for label in (spec.target, spec.label.lower()):
            distance = _levenshtein(normalized, label.lower())
            # Порог пропорционален длине: короткие строки не должны совпадать
            # почти произвольно, длинные — терпят пару опечаток.
            threshold = max(1, len(label) // 3)
            if distance <= threshold and (best is None or distance < best[0]):
                best = (distance, spec)
    return best[1] if best else None


def suggest_mapping(headers: list[str], fields: list[FieldSpec]) -> dict[str, str]:
    """Возвращает `{исходная_колонка: код_целевого_поля}` — только для колонок,
    для которых нашлось совпадение. Остальное сопоставляется вручную (раздел
    4.12: «диалоговое окно», это лишь предзаполнение)."""
    mapping: dict[str, str] = {}
    used_targets: set[str] = set()

    for header in headers:
        normalized = _normalize(header)
        target = _SYNONYMS.get(normalized)
        if target is None:
            exact = next((f for f in fields if f.target == normalized), None)
            target = exact.target if exact else None
        if target and target not in used_targets:
            mapping[header] = target
            used_targets.add(target)

    remaining_fields = [f for f in fields if f.target not in used_targets]
    for header in headers:
        if header in mapping:
            continue
        match = _best_fuzzy_match(header, remaining_fields)
        if match is not None:
            mapping[header] = match.target
            used_targets.add(match.target)
            remaining_fields = [f for f in remaining_fields if f.target != match.target]

    return mapping
