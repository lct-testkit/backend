"""Автоподбор маппинга колонок (dop.md §4.12, п.3).

«Левенштейн + словарь синонимов»: точное совпадение и известный синоним —
раньше нечёткого сравнения, оно только для колонок, которые не нашлись
ни одним из двух способов. Без внешней зависимости (`python-Levenshtein`/
`rapidfuzz`) — расстояние в 15 строк не стоит нового пакета в закрытом контуре.

Словарь синонимов свой у каждого типа сущности и предлагает только поля этого типа. Раньше он был
один на все типы, и для продуктов и лицензий автоподбор подсказывал поля организации («ИНН»,
«Телефон»): бэкенд потом отклонял такой маппинг как «неизвестные целевые поля».
"""

from __future__ import annotations

from app.modules.catalog import learner
from app.modules.imports.fields import FieldSpec

# Русские синонимы для колонок исходного файла -> код целевого поля, по типам сущности.
# Ключи — в нижнем регистре, без пробелов по краям.
_ORGANIZATION: dict[str, str] = {
    "наименование": "name",
    "название": "name",
    "полное наименование": "name",
    "полное название": "name",
    "название организации": "name",
    "наименование организации": "name",
    "название вуза": "name",
    "наименование вуза": "name",
    "организация": "name",
    "вуз": "name",
    "краткое название": "short_name",
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
    "регион": "region_code",
    "код региона": "region_code",
}
_PRODUCT: dict[str, str] = {
    "код": "code",
    "артикул": "code",
    "наименование": "name",
    "название": "name",
    "продукт": "name",
    "описание": "description",
    "направление": "direction_code",
    "код направления": "direction_code",
    "формат": "format",
    "цена": "base_price",
    "стоимость": "base_price",
    "валюта": "currency",
    "длительность": "duration_hours",
    "часы": "duration_hours",
    "длительность, часы": "duration_hours",
}
_LICENSE: dict[str, str] = {
    "название вуза": "organization_name",
    "вуз": "organization_name",
    "университет": "organization_name",
    "вендор": "vendor",
    "производитель": "vendor",
    "по": "product_name",
    "программное обеспечение": "product_name",
    "продукт": "product_name",
    "номер договора": "contract_number",
    "договор": "contract_number",
    "подписание лицензии": "license_signed_at",
    "дата подписания": "license_signed_at",
    "срок действия лицензии (год)": "license_valid_year",
    "срок действия": "license_valid_year",
    "статус по передаче": "transfer_status",
    "статус по передачи": "transfer_status",  # так в списке полей кейса (опечатка заказчика)
    "фио менеджера": "manager_full_name",
    "менеджер": "manager_full_name",
    "ответственные от вуза": "responsible_contacts",
    "ответственные": "responsible_contacts",
    "комментарий": "comment",
    "примечание": "comment",
}
_PERSON: dict[str, str] = {
    "фио": "full_name",
    "ф.и.о.": "full_name",
    "фамилия имя отчество": "full_name",
    "фамилия": "last_name",
    "имя": "first_name",
    "отчество": "middle_name",
    "телефон": "phone",
    "номер телефона": "phone",
    "тел.": "phone",
    "мобильный телефон": "phone",
    "email": "email",
    "e-mail": "email",
    "почта": "email",
    "электронная почта": "email",
    "адрес электронной почты": "email",
}
_VENDOR_CONTACT: dict[str, str] = {
    **_PERSON,
    "компания": "vendor_name",
    "вендор": "vendor_name",
    "организация": "vendor_name",
    "название компании": "vendor_name",
    "продукт": "product_names",
    "продукты": "product_names",
    "по": "product_names",
    "программное обеспечение": "product_names",
    "контактное лицо": "full_name",
    "ответственный": "full_name",
    "способ связи": "contact_methods",
    "способы связи": "contact_methods",
    "канал связи": "contact_methods",
    "связь": "contact_methods",
}
_PAYMENT: dict[str, str] = {
    **_PERSON,
    "номер заявки": "order_number",
    "номер заказа": "order_number",
    "заявка": "order_number",
    "заказ": "order_number",
    "id заказа": "order_number",
    "order": "order_number",
    "order id": "order_number",
    "курс": "product_name",
    "название курса": "product_name",
    "программа": "product_name",
    "продукт": "product_name",
    "номер потока": "stream_number",
    "поток": "stream_number",
    "сумма": "amount",
    "сумма оплаты": "amount",
    "оплата": "amount",
    "стоимость": "amount",
    "цена": "amount",
    "валюта": "currency",
}
_LEARNER: dict[str, str] = {}  # заголовки шаблона — через `catalog.learner.target_for_header`

_SYNONYMS_BY_ENTITY: dict[str, dict[str, str]] = {
    "organization": _ORGANIZATION,
    "product": _PRODUCT,
    "license": _LICENSE,
    "vendor_contact": _VENDOR_CONTACT,
    "payment": _PAYMENT,
    "learner": _LEARNER,
}
# Без типа сущности (вызов из старого кода/тестов): объединение словарей каталожных типов.
_LEGACY_SYNONYMS: dict[str, str] = {**_LICENSE, **_PRODUCT, **_ORGANIZATION}


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


def suggest_mapping(
    headers: list[str], fields: list[FieldSpec], entity_type: str | None = None
) -> dict[str, str]:
    """Возвращает `{исходная_колонка: код_целевого_поля}` — только для колонок,
    для которых нашлось совпадение, и только с полями переданного типа сущности. Остальное
    сопоставляется вручную (раздел 4.12: «диалоговое окно», это лишь предзаполнение)."""
    synonyms = (
        _SYNONYMS_BY_ENTITY.get(entity_type, _LEGACY_SYNONYMS) if entity_type else _LEGACY_SYNONYMS
    )
    allowed = {f.target for f in fields}
    mapping: dict[str, str] = {}
    used_targets: set[str] = set()

    for header in headers:
        normalized = _normalize(header)
        if not normalized:
            continue
        target = synonyms.get(normalized)
        if target is None and entity_type == "learner":
            target = learner.target_for_header(header)
        if target is None:
            exact = next((f for f in fields if f.target == normalized), None)
            target = exact.target if exact else None
        if target and target in allowed and target not in used_targets:
            mapping[header] = target
            used_targets.add(target)

    remaining_fields = [f for f in fields if f.target not in used_targets]
    for header in headers:
        if header in mapping or not _normalize(header):
            continue
        match = _best_fuzzy_match(header, remaining_fields)
        if match is not None:
            mapping[header] = match.target
            used_targets.add(match.target)
            remaining_fields = [f for f in remaining_fields if f.target != match.target]

    return mapping
