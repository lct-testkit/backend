"""Составной ключ лицензии: вуз, вендор, ПО, номер договора (B3-1, спринт B3).

ТЗ §4 п.1 просит каталог «вуз – вендор – ПО» с загрузкой из xls/xlsx. До этой
задачи естественным ключом строки был только ``contract_number``: один
договор на несколько единиц ПО молча терял строки при импорте (BUG-9), а
файл без номеров договоров было невозможно загрузить вовсе (многие реальные
прайс-листы номер договора не указывают).

Ключ строки — четвёрка ``(organization_id, vendor_key, product_key,
contract_key)``: вуз и вендор с ПО обязаны совпасть, а номер договора может
быть пустым (пустая строка ``''``, не ``NULL`` — частичный уникальный индекс
Postgres не ловит дубли по ``NULL`` колонке, два ``NULL`` не равны друг
другу).

``vendor_key``/``product_key`` — это `app.core.normalize.company_key`
(регистр, «ё», кавычки-«ёлочки», тире, пробелы не различают названия).
``contract_key`` — то же самое плюс снятие типографских префиксов номера
(«№», «No», «N») и схлопывание пробелов вокруг `/` и `-`, чтобы «№ Д-12 /
2024» и «д-12/2024» считались одним и тем же договором.

Модуль чистый (без БД и ORM): копия нормализации номера договора живёт и в
миграции `0041_license_key_catalog_link` — см. комментарий там, зачем
(миграция не должна зависеть от будущих правок этого файла).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from app.core.normalize import company_key

# «№», «No.», «N» на конце — типографские префиксы номера договора, а не
# часть самого номера. После `company_key` текст уже в нижнем регистре,
# поэтому «No»/«N» матчатся без учёта регистра без доп. флагов.
_CONTRACT_PREFIX = re.compile(r"^(?:№|no\.?|n\.?)\s*")
# Пробелы вокруг «/» и «-» внутри номера не различают номера: «12 / 2024» и
# «12/2024» — один договор.
_CONTRACT_SEPARATOR_SPACES = re.compile(r"\s*([/-])\s*")


def vendor_key(value: object) -> str:
    """Ключ сравнения названия вендора — alias `company_key` (п.1 задачи)."""
    return company_key(value)


def product_key(value: object) -> str:
    """Ключ сравнения названия ПО — alias `company_key` (п.1 задачи)."""
    return company_key(value)


def contract_key(value: object) -> str:
    """Ключ сравнения номера договора. Пустое/пробельное значение -> ``''``."""
    text = company_key(value)
    if not text:
        return ""
    text = _CONTRACT_PREFIX.sub("", text)
    text = _CONTRACT_SEPARATOR_SPACES.sub(r"\1", text)
    return text


@dataclass(frozen=True, slots=True)
class LicenseKey:
    """Составной ключ одной строки лицензии — для сравнения и построения
    фильтра `WHERE` без ручной сборки четырёх условий в нескольких местах."""

    organization_id: uuid.UUID
    vendor_key: str
    product_key: str
    contract_key: str

    @classmethod
    def build(
        cls,
        organization_id: uuid.UUID,
        *,
        vendor: object,
        product_name: object,
        contract_number: object,
    ) -> LicenseKey:
        return cls(
            organization_id=organization_id,
            vendor_key=vendor_key(vendor),
            product_key=product_key(product_name),
            contract_key=contract_key(contract_number),
        )

    def as_tuple(self) -> tuple[uuid.UUID, str, str, str]:
        return (self.organization_id, self.vendor_key, self.product_key, self.contract_key)
