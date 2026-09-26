"""Поиск продуктов и организаций по названию из внешних файлов и вебхуков.

«Курс» из данных оплат и «Компания» из вендорского каталога приходят названием, а не кодом: регистр,
кавычки-«ёлочки», «ё» и лишние пробелы в разных выгрузках разные. Сравнение идёт по
`app.core.normalize.company_key`, поэтому `ООО «Базис»`, `ооо "Базис"` и `ООО  Базис` — одна
компания, а не три.

Каталог продуктов и вендоров мал (десятки–сотни записей), поэтому индекс строится одним запросом
и живёт, пока живёт объект: импорт на 100 000 строк не делает по запросу на строку.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import FieldError, ValidationError
from app.core.normalize import clean_text, company_key, slugify_code
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.catalog.models import Organization, Product

_PRODUCT_NAME_MAX = 255
_ORGANIZATION_NAME_MAX = 512


class ProductIndex:
    """Продукты по `company_key` названия и кода. Учитывает только неудалённые записи; код,
    занятый удалённым продуктом, при создании нового всё равно считается занятым (уникальный
    индекс `uq_products_code` не смотрит на `deleted_at`)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._by_key: dict[str, Product] | None = None

    async def _load(self) -> dict[str, Product]:
        if self._by_key is None:
            rows = (
                (await self._session.execute(select(Product).where(Product.deleted_at.is_(None))))
                .scalars()
                .all()
            )
            index: dict[str, Product] = {}
            for product in rows:
                # Название сильнее кода: продукт с кодом, совпавшим с чужим названием, не
                # перехватывает поиск.
                index.setdefault(company_key(product.code), product)
            for product in rows:
                index[company_key(product.name)] = product
            self._by_key = index
        return self._by_key

    async def get(self, name: object) -> Product | None:
        key = company_key(name)
        if not key:
            return None
        return (await self._load()).get(key)

    async def _free_code(self, base: str) -> str:
        taken = set(
            (await self._session.execute(select(Product.code).where(Product.code.like(f"{base}%"))))
            .scalars()
            .all()
        )
        if base not in taken:
            return base
        number = 2
        while f"{base}-{number}" in taken:
            number += 1
        return f"{base}-{number}"

    async def create(
        self,
        name: str,
        *,
        vendor_id: uuid.UUID | None = None,
        import_job_id: uuid.UUID | None = None,
        source: str = "import",
        base_price: Decimal | None = None,
    ) -> Product:
        """Заводит продукт, которого нет в каталоге: курс из оплаты или продукт из вендорского
        файла. Помечается `custom_fields.auto_created`, чтобы методист мог разобрать заведённое
        автоматически (опечатка в названии курса тоже породила бы продукт)."""
        title = clean_text(name)
        if title is None:
            raise ValidationError(
                "Пустое название продукта", [FieldError(field="name", reason="обязательное поле")]
            )
        if len(title) > _PRODUCT_NAME_MAX:
            raise ValidationError(
                "Слишком длинное название продукта",
                [FieldError(field="name", reason=f"не больше {_PRODUCT_NAME_MAX} символов")],
            )
        index = await self._load()
        existing = index.get(company_key(title))
        if existing is not None:
            return existing

        code = await self._free_code(slugify_code(title))
        product = Product(
            code=code,
            name=title,
            vendor_id=vendor_id,
            import_job_id=import_job_id,
            base_price=base_price,
            is_active=True,
            custom_fields={"auto_created": True, "auto_created_from": source},
        )
        try:
            async with self._session.begin_nested():
                self._session.add(product)
                await self._session.flush()
        except IntegrityError:
            # Тот же продукт одновременно завёл другой запрос — берём его, а не падаем.
            self._by_key = None
            found = await self.get(title)
            if found is None:
                raise
            return found

        await AuditService(self._session).record(
            AuditAction.PRODUCT_CREATED,
            entity_type="product",
            entity_id=product.id,
            changes={"code": {"old": None, "new": code}, "source": {"old": None, "new": source}},
        )
        index[company_key(title)] = product
        index.setdefault(company_key(code), product)
        return product


class OrganizationIndex:
    """Организации по `company_key` полного и краткого названия (неудалённые)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._by_key: dict[str, Organization] | None = None

    async def _load(self) -> dict[str, Organization]:
        if self._by_key is None:
            rows = (
                (
                    await self._session.execute(
                        select(Organization)
                        .where(Organization.deleted_at.is_(None))
                        .order_by(Organization.created_at, Organization.id)
                    )
                )
                .scalars()
                .all()
            )
            index: dict[str, Organization] = {}
            for organization in rows:
                for label in (organization.name, organization.short_name):
                    key = company_key(label)
                    if key:
                        index.setdefault(key, organization)
            self._by_key = index
        return self._by_key

    async def get(self, name: object) -> Organization | None:
        key = company_key(name)
        if not key:
            return None
        return (await self._load()).get(key)

    async def create_vendor(
        self,
        name: str,
        *,
        owner_id: uuid.UUID | None,
        import_job_id: uuid.UUID | None = None,
        source: str = "import",
    ) -> Organization:
        """Вендор без ИНН: обычная организация типа «компания». Реквизиты (ИНН, ОГРН) в вендорском
        файле не приходят — их добавит менеджер или сверка с ЕГРЮЛ."""
        title = clean_text(name)
        if title is None:
            raise ValidationError(
                "Пустое название компании", [FieldError(field="name", reason="обязательное поле")]
            )
        if len(title) > _ORGANIZATION_NAME_MAX:
            raise ValidationError(
                "Слишком длинное название компании",
                [FieldError(field="name", reason=f"не больше {_ORGANIZATION_NAME_MAX} символов")],
            )
        index = await self._load()
        existing = index.get(company_key(title))
        if existing is not None:
            return existing

        organization = Organization(
            name=title,
            org_type="company",
            source=source,
            import_job_id=import_job_id,
            owner_id=owner_id,
            created_by=owner_id,
        )
        self._session.add(organization)
        await self._session.flush()
        await AuditService(self._session).record(
            AuditAction.ORGANIZATION_CREATED,
            entity_type="organization",
            entity_id=organization.id,
            changes={"name": {"old": None, "new": title}, "source": {"old": None, "new": source}},
        )
        index[company_key(title)] = organization
        return organization
