"""Стриминговый разбор выгрузки ЕГРЮЛ (dop.md §11.3, п.1-3).

**Важная оговорка**, честная, а не спрятанная в углу: реальный XML-дамп
открытых данных ФНС недоступен в этой среде (закрытый контур без интернета,
раздел «Стек»), поэтому разбор ориентируется на общеизвестную публичную
схему открытых данных ЕГРЮЛ (элементы `СвЮЛ`/`СвНаимЮЛ`/`СвАдресЮЛ`/
`СвОКВЭД`/`СвСтатус`/...), но имена части второстепенных атрибутов
подтверждены только по документации, не по образцу реального файла.
Поэтому:

* Обязательное поле — только `ИНН`. Запись без него пропускается и
  учитывается как ошибка партии, но не роняет импорт целиком (раздел 3:
  «битые файлы» — это ожидаемый, не исключительный случай).
* Извлечение необязательных полей (адрес, руководитель, ОКВЭД) — через
  поиск по имени тега/атрибута с несколькими вариантами написания, а не
  жёсткий путь по дереву: один незнакомый вариант схемы не должен обнулять
  всю запись, только отдельное поле.
* Если реальный дамп ФНС всё-таки станет доступен, эту функцию нужно
  свериться с ним и поправить — но стриминговая архитектура (`iterparse`,
  батч 5000, версия реестра, фильтр по ОКВЭД) от точных имён тегов не
  зависит и переносится без изменений.

ЕГРИП (индивидуальные предприниматели) намеренно не разбирается здесь:
dop.md §11.8 заводит ИП как `contacts` с `org_type=individual_entrepreneur`,
а не как строку `egrul_entries` — раз данные ИП это ПДн физлица, а не
сведения о юрлице.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import IO, Any

from lxml import etree

from app.modules.registry.models import EgrulStatus, is_educational_okved

# `{*}` — любое пространство имён, lxml matches по локальному имени тега:
# реальная выгрузка ФНС может как использовать неймспейс, так и нет.
_LEGAL_ENTITY_TAGS = ("{*}СвЮЛ",)

_STATUS_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("ликвидирован", EgrulStatus.LIQUIDATED.value),
    ("в процессе ликвидации", EgrulStatus.LIQUIDATING.value),
    ("ликвидац", EgrulStatus.LIQUIDATING.value),
    ("реорганиз", EgrulStatus.REORGANIZING.value),
    ("недействующ", EgrulStatus.INVALID.value),
    ("прекратил", EgrulStatus.LIQUIDATED.value),
)

_ADDRESS_ATTR_ORDER = (
    "Регион",
    "Район",
    "Город",
    "НаселенПункт",
    "НаселПункт",
    "УлицаНаселенПункт",
    "Улица",
    "НаимУлица",
    "Дом",
    "Номер",
    "Корпус",
    "Кварт",
)


@dataclass(slots=True)
class ParsedEntry:
    inn: str
    ogrn: str | None = None
    kpp: str | None = None
    full_name: str = ""
    short_name: str | None = None
    opf_code: str | None = None
    opf_name: str | None = None
    status: str = EgrulStatus.ACTIVE.value
    registration_date: dt.date | None = None
    termination_date: dt.date | None = None
    region_code: str | None = None
    legal_address: str | None = None
    address_parts: dict[str, Any] = field(default_factory=dict)
    okved_main: str | None = None
    okved_extra: list[str] = field(default_factory=list)
    director_name: str | None = None
    director_position: str | None = None
    capital: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_educational(self) -> bool:
        return is_educational_okved(self.okved_main, self.okved_extra)


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _attr(elem: etree._Element, *names: str) -> str | None:
    for name in names:
        value = elem.get(name)
        if value:
            return value
    return None


def _find_first(elem: etree._Element, *local_names: str) -> etree._Element | None:
    wanted = set(local_names)
    for descendant in elem.iter():
        if _localname(descendant.tag) in wanted:
            return descendant
    return None


def _find_all(elem: etree._Element, *local_names: str) -> list[etree._Element]:
    wanted = set(local_names)
    return [d for d in elem.iter() if _localname(d.tag) in wanted]


def _parse_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            return dt.datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _map_status(name: str | None, termination_date: dt.date | None) -> str:
    if name:
        lowered = name.lower()
        for keyword, status in _STATUS_KEYWORDS:
            if keyword in lowered:
                return status
    if termination_date is not None:
        return EgrulStatus.LIQUIDATED.value
    return EgrulStatus.ACTIVE.value


def _address(elem: etree._Element) -> tuple[str | None, str | None, dict[str, Any]]:
    # "АдресРФ" — вложенный элемент, который реально несёт атрибуты
    # (КодРегион, Регион); обёртка "СвАдресЮЛ" сама по себе их не имеет.
    # `_find_first` идёт в document order, поэтому важно искать конкретный
    # тег отдельно, а не одним вызовом с обоими именами — иначе совпадёт
    # родительская обёртка раньше, чем нужный потомок.
    address_elem = _find_first(elem, "АдресРФ")
    if address_elem is None:
        address_elem = _find_first(elem, "СвАдресЮЛ", "СвМНЮЛ")
    if address_elem is None:
        return None, None, {}

    region_code = _attr(address_elem, "КодРегион")
    parts: dict[str, Any] = {}
    for descendant in address_elem.iter():
        for key, value in descendant.attrib.items():
            if key in _ADDRESS_ATTR_ORDER and value:
                parts.setdefault(key, value)

    ordered = [parts[key] for key in _ADDRESS_ATTR_ORDER if key in parts]
    region_name = _attr(address_elem, "Регион", "НаимРегион")
    if region_name and region_name not in ordered:
        ordered.insert(0, region_name)
    address = ", ".join(ordered) if ordered else None
    return region_code, address, parts


def _okved(elem: etree._Element) -> tuple[str | None, list[str]]:
    main_elem = _find_first(elem, "СвОКВЭДОсн")
    main_code = _attr(main_elem, "КодОКВЭД") if main_elem is not None else None
    extra_codes = [code for e in _find_all(elem, "СвОКВЭДДоп") if (code := _attr(e, "КодОКВЭД"))]
    return main_code, extra_codes


def _director(elem: etree._Element) -> tuple[str | None, str | None]:
    fio_elem = _find_first(elem, "ФИОРуководителя", "ФИОРук", "СведФЛ")
    name = None
    if fio_elem is not None:
        last = _attr(fio_elem, "Фамилия") or ""
        first = _attr(fio_elem, "Имя") or ""
        middle = _attr(fio_elem, "Отчество") or ""
        name = " ".join(p for p in (last, first, middle) if p) or None
    position_elem = _find_first(elem, "СвДолжн")
    position = _attr(position_elem, "НаимДолжн") if position_elem is not None else None
    return name, position


def parse_entry(elem: etree._Element) -> ParsedEntry | None:
    inn = _attr(elem, "ИНН")
    if not inn:
        return None

    name_elem = _find_first(elem, "СвНаимЮЛ")
    full_name = (_attr(name_elem, "НаимЮЛПолн") if name_elem is not None else None) or inn
    short_name = _attr(name_elem, "НаимЮЛСокр") if name_elem is not None else None

    opf_elem = _find_first(elem, "СвОПФ", "СведОПФ")
    opf_code = _attr(opf_elem, "КодОПФ") if opf_elem is not None else None
    opf_name = _attr(opf_elem, "НаимОПФ") if opf_elem is not None else None

    status_elem = _find_first(elem, "СвСтатус")
    status_name = _attr(status_elem, "НаимСтатус") if status_elem is not None else None

    termin_elem = _find_first(elem, "СвПрекрЮЛ")
    termination_date = (
        _parse_date(_attr(termin_elem, "ДатаПрекр")) if termin_elem is not None else None
    )

    region_code, address, address_parts = _address(elem)
    okved_main, okved_extra = _okved(elem)
    director_name, director_position = _director(elem)

    kpp_elem = _find_first(elem, "СвУчетНО")
    kpp = (_attr(kpp_elem, "КПП") if kpp_elem is not None else None) or _attr(elem, "КПП")

    capital_elem = _find_first(elem, "СвУстКап", "СведУстКап")
    capital = _attr(capital_elem, "СумКап") if capital_elem is not None else None

    return ParsedEntry(
        inn=inn,
        ogrn=_attr(elem, "ОГРН"),
        kpp=kpp,
        full_name=full_name,
        short_name=short_name,
        opf_code=opf_code,
        opf_name=opf_name,
        status=_map_status(status_name, termination_date),
        registration_date=_parse_date(_attr(elem, "ДатаОГРН")),
        termination_date=termination_date,
        region_code=region_code,
        legal_address=address,
        address_parts=address_parts,
        okved_main=okved_main,
        okved_extra=okved_extra,
        director_name=director_name,
        director_position=director_position,
        capital=capital,
        raw=dict(elem.attrib),
    )


def iter_entries(stream: IO[bytes]) -> Iterator[ParsedEntry]:
    """Стримингом разбирает выгрузку, отдавая по одной записи за раз —
    dop.md §11.3: «полный ЕГРЮЛ это миллионы записей, в память не влезает»."""
    context = etree.iterparse(stream, events=("end",), tag=_LEGAL_ENTITY_TAGS, recover=True)
    for _event, elem in context:
        entry = parse_entry(elem)
        if entry is not None:
            yield entry
        # Освобождаем память: элемент и всё, что перед ним у родителя.
        elem.clear(keep_tail=False)
        parent = elem.getparent()
        while parent is not None and elem.getprevious() is not None:
            del parent[0]
