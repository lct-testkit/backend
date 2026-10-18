"""Сопоставление вуза, вендора и ПО с каталогом для лицензий (B3-4, спринт
B3, BUG-11).

Реальный файл заказчика называет вуз по-разному («МГТУ им. Баумана»,
полное имя с «ФГБОУ ВО», с опечаткой), а колонки ИНН среди десяти полей
кейса нет вовсе. До этой задачи вуз искался только по точному совпадению
`Organization.name` (`imports.service._FK_TARGETS["organization_name"]`) —
список без точного названия (а оно почти никогда не совпадает буква в
букву с тем, что в ЕГРЮЛ или в разговорной форме) не загружался вовсе.

`LicenseMatcher.match_university` — пять ступеней, останавливается на
первой, где кандидат ровно один: ИНН (если передан), точное `name`, точное
`short_name`, нормализованный `company_key`, ключ без организационно-
правовых слов-обёрток (`_ORG_NOISE`). Два и более кандидата на ступени —
`ambiguous`; ни одного после всех ступеней — `not_found` с подсказками по
расстоянию Левенштейна.

`match_vendor`/`match_software` — проще (один ключ сравнения,
`company_key`), без автосоздания: создание отсутствующего вендора или ПО
— решение вызывающего кода (API B3-6), сам матчер только ищет и
сообщает, что нашёл.

`match_vendor`/`match_software` используются API лицензий (B3-6, текстовый
ввод вендора/ПО с подбором `vendor_id`/`product_id`) и импортом (B3-2,
`imports.service.ImportService._apply_license_row`, один инстанс на
партию — кандидаты кэшируются внутри него) — раньше (до B3-2)
`imports.service` вызывал только точное совпадение организации-вуза, а
`vendor_id`/`product_id` лицензии, заведённой импортом, оставались
`NULL` независимо от точности совпадения текста с каталогом. Поиск вуза
(`organization_name` в `imports.fields`) остаётся отдельным от этого
модуля точным совпадением по имени — `match_university` реализован и
протестирован (`tests/test_license_matching.py`), но ни один вызывающий
код его пока не использует."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Generic, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.normalize import clean_text, company_key
from app.modules.catalog.models import Organization, OrgType, Product

# Организационно-правовые слова-обёртки реального названия вуза: «ФГБОУ ВО
# "МГТУ им. Баумана"» и «МГТУ им. Баумана» — один и тот же вуз. Слова
# «технический», «государственный» НЕ входят в список: их удаление склеило
# бы разные вузы с похожими названиями (см. задачу B3-4).
_ORG_NOISE: tuple[str, ...] = (
    "федеральное государственное бюджетное образовательное учреждение высшего образования",
    "федеральное государственное автономное образовательное учреждение высшего образования",
    "федеральное государственное бюджетное образовательное учреждение",
    "государственное бюджетное образовательное учреждение высшего образования",
    "фгбоу во",
    "фгаоу во",
    "гбоу во",
    "им.",
    "имени",
)
_UNIVERSITY_TYPES = frozenset({OrgType.UNIVERSITY.value, OrgType.COLLEGE.value})
_SPACES = re.compile(r"\s+")
_SUGGESTION_LIMIT = 3


def _strip_noise(key: str) -> str:
    text = key
    for phrase in _ORG_NOISE:
        text = text.replace(phrase, " ")
    return _SPACES.sub(" ", text).strip()


def _levenshtein(a: str, b: str) -> int:
    """Расстояние редактирования для подсказок «возможно, вы имели в виду …».
    Отдельная копия (не импорт `imports.mapping._levenshtein` и не импорт
    `catalog.license_values._levenshtein`): три одинаковых маленьких функции
    дешевле, чем цикл зависимостей между модулями одного уровня."""
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
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
        previous = current
    return previous[-1]


_EntityT = TypeVar("_EntityT")


@dataclass(frozen=True, slots=True)
class MatchResult(Generic[_EntityT]):
    """Итог одного вызова `match_*`.

    `how`: `inn`, `name`, `short_name`, `normalized`, `stripped` — найден
    однозначно (ступень указывает, по какому правилу); `ambiguous` —
    несколько кандидатов (`candidates` заполнен, `entity` пуст);
    `not_found` — ни одного (`suggestions` — до трёх ближайших названий).
    """

    entity: _EntityT | None
    how: str
    candidates: tuple[_EntityT, ...] = ()
    suggestions: tuple[str, ...] = ()

    @property
    def is_unique(self) -> bool:
        return self.entity is not None


def _suggest(key: str, labels: dict[str, str]) -> tuple[str, ...]:
    """До трёх названий из `labels` (ключ сравнения -> читаемая форма), ближайших к `key` по
    Левенштейну, в пределах четверти длины строки — иначе подсказка бессмысленна."""
    if not key:
        return ()
    threshold = max(3, len(key) // 4)
    scored = sorted(
        ((_levenshtein(key, candidate_key), label) for candidate_key, label in labels.items()),
        key=lambda pair: pair[0],
    )
    return tuple(label for distance, label in scored[:_SUGGESTION_LIMIT] if distance <= threshold)


class LicenseMatcher:
    """Поиск вуза/вендора/ПО по каталогу для одной лицензии. Кандидаты
    загружаются один раз на инстанс (как `catalog.lookup.OrganizationIndex`)
    — повторные вызовы `match_*` на том же инстансе не делают новых
    запросов."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._universities: list[Organization] | None = None
        self._vendors: list[Organization] | None = None
        self._software: list[Product] | None = None

    async def _load_universities(self) -> list[Organization]:
        if self._universities is None:
            rows = await self._session.execute(
                select(Organization).where(
                    Organization.org_type.in_(_UNIVERSITY_TYPES),
                    Organization.deleted_at.is_(None),
                )
            )
            self._universities = list(rows.scalars().all())
        return self._universities

    async def _load_vendors(self) -> list[Organization]:
        if self._vendors is None:
            rows = await self._session.execute(
                select(Organization).where(
                    Organization.org_type == OrgType.COMPANY.value,
                    Organization.deleted_at.is_(None),
                )
            )
            self._vendors = list(rows.scalars().all())
        return self._vendors

    async def _load_software(self) -> list[Product]:
        if self._software is None:
            rows = await self._session.execute(
                select(Product).where(Product.kind == "software", Product.deleted_at.is_(None))
            )
            self._software = list(rows.scalars().all())
        return self._software

    async def match_university(self, name: object, inn: object = None) -> MatchResult[Organization]:
        candidates = await self._load_universities()
        title = clean_text(name) or ""
        inn_value = clean_text(inn)

        if inn_value:
            by_inn = [org for org in candidates if org.inn == inn_value]
            if len(by_inn) == 1:
                return MatchResult(by_inn[0], "inn")
            if len(by_inn) > 1:
                return MatchResult(None, "ambiguous", candidates=tuple(by_inn))

        by_name = [org for org in candidates if org.name == title]
        if len(by_name) == 1:
            return MatchResult(by_name[0], "name")
        if len(by_name) > 1:
            return MatchResult(None, "ambiguous", candidates=tuple(by_name))

        by_short = [org for org in candidates if org.short_name == title]
        if len(by_short) == 1:
            return MatchResult(by_short[0], "short_name")
        if len(by_short) > 1:
            return MatchResult(None, "ambiguous", candidates=tuple(by_short))

        key = company_key(title)
        by_normalized = [
            org
            for org in candidates
            if key and (company_key(org.name) == key or company_key(org.short_name) == key)
        ]
        if len(by_normalized) == 1:
            return MatchResult(by_normalized[0], "normalized")
        if len(by_normalized) > 1:
            return MatchResult(None, "ambiguous", candidates=tuple(by_normalized))

        stripped_key = _strip_noise(key)
        by_stripped = [
            org
            for org in candidates
            if stripped_key
            and (
                _strip_noise(company_key(org.name)) == stripped_key
                or _strip_noise(company_key(org.short_name)) == stripped_key
            )
        ]
        if len(by_stripped) == 1:
            return MatchResult(by_stripped[0], "stripped")
        if len(by_stripped) > 1:
            return MatchResult(None, "ambiguous", candidates=tuple(by_stripped))

        labels = {company_key(org.name): org.name for org in candidates if org.name}
        suggestions = _suggest(stripped_key or key, labels)
        return MatchResult(None, "not_found", suggestions=suggestions)

    async def match_vendor(self, name: object) -> MatchResult[Organization]:
        candidates = await self._load_vendors()
        key = company_key(name)
        if not key:
            return MatchResult(None, "not_found")
        matches = [
            org
            for org in candidates
            if company_key(org.name) == key or company_key(org.short_name) == key
        ]
        if len(matches) == 1:
            return MatchResult(matches[0], "normalized")
        if len(matches) > 1:
            return MatchResult(None, "ambiguous", candidates=tuple(matches))
        labels = {company_key(org.name): org.name for org in candidates if org.name}
        return MatchResult(None, "not_found", suggestions=_suggest(key, labels))

    async def match_software(
        self, name: object, vendor_id: uuid.UUID | None = None
    ) -> MatchResult[Product]:
        candidates = await self._load_software()
        key = company_key(name)
        if not key:
            return MatchResult(None, "not_found")
        matches = [p for p in candidates if company_key(p.name) == key]
        if len(matches) == 1:
            product = matches[0]
            how = "normalized" if vendor_id is None or product.vendor_id == vendor_id else "name"
            return MatchResult(product, how)
        if len(matches) > 1:
            # Несколько ПО с одним названием у разных вендоров — предпочесть то, что принадлежит
            # запрошенному вендору, если он известен и однозначен.
            if vendor_id is not None:
                by_vendor = [p for p in matches if p.vendor_id == vendor_id]
                if len(by_vendor) == 1:
                    return MatchResult(by_vendor[0], "normalized")
            return MatchResult(None, "ambiguous", candidates=tuple(matches))
        labels = {company_key(p.name): p.name for p in candidates if p.name}
        return MatchResult(None, "not_found", suggestions=_suggest(key, labels))


__all__ = ["LicenseMatcher", "MatchResult"]
