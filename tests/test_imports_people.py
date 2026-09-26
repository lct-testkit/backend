"""Импорт трёх файлов заказчика: «Вендоры», «Данные оплат» (JSON), «Загрузка пользователей» (LMS).

Данные повторяют структуру и значения настоящих файлов (те же заголовки, кавычки в названиях, `null`
первым элементом JSON, телефон числом в шаблоне LMS), но уникальны на каждый прогон.

Чистая часть — разбор, автоподбор колонок, проверки полей. Сквозная часть (нужна БД,
`TEST_DATABASE_URL`) проходит весь путь «файл -> профиль -> маппинг -> проверка -> применение ->
повтор -> откат».
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest

from app.core.errors import AppError
from app.modules.catalog import learner
from app.modules.imports import fields as import_fields
from app.modules.imports.fields import (
    LEARNER_FIELDS,
    PAYMENT_FIELDS,
    PRODUCT_FIELDS,
    VENDOR_CONTACT_FIELDS,
    FieldSpec,
    missing_mapping_labels,
    validate_field,
)
from app.modules.imports.mapping import suggest_mapping
from app.modules.imports.parsing import parse_json, parse_table, parse_xlsx
from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import create_published_workflow, graph_body, login, sign_in
from tests.import_helpers import (
    apply_job,
    get_job,
    job_rows,
    make_inn,
    phone_parts,
    rollback_job,
    start_import,
    token,
    xlsx_bytes,
)

VENDOR_HEADERS = ["Компания", "Продукт", "ФИО", "Телефон", "Почта", "Способ связи"]
PAYMENT_HEADERS = [
    "Номер заявки",
    "Курс",
    "Фамилия",
    "Имя",
    "Отчество",
    "Телефон",
    "Email",
    "Номер потока",
]


def payment_json(tok: str, phones: list[str]) -> bytes:
    """Как «Данные оплат.json»: первый элемент — `null`, ключи русские, телефон `7 (999) …`."""
    people = [
        ("Черепанова", "Светлана", "Васильевна", f"cherepanona.{tok}@test.ru"),
        ("Кричанов", "Максим", "Сергеевич", f"Max_Crich.{tok}@mail.ru"),
        ("Григорьев", "Станислав", "Семенович", f"grigorev.{tok}@gmail.com"),
        ("Осипенко", "Ирина", "Викторовна", f"Osipenko.{tok}@mail.ru"),
        ("Иванов", "Михаил", "Петрович", f"mp_ivanov.{tok}@mail.ru"),
    ]
    courses = [
        f"Анализ данных без программирования {tok}",
        f"Инженер-тестировщик {tok}",
        f"Управление ИТ-проектами на базе программного продукта ПАО «Ростелеком» {tok}",
        f"Промпт-инжиниринг {tok}",
        f"Python-разработчик с использованием инструментов ИИ {tok}",
    ]
    streams = [1, 1, 2, 3, 4]
    records: list[Any] = [None]
    for index, ((last, first, middle, email), course, stream) in enumerate(
        zip(people, courses, streams, strict=True)
    ):
        records.append(
            {
                "Номер заявки": f"ORD-{tok}-{index + 1}",
                "Курс": course,
                "Фамилия": last,
                "Имя": first,
                "Отчество": middle,
                "Телефон": phones[index],
                "Email": email,
                "Номер потока": stream,
            }
        )
    return json.dumps(records, ensure_ascii=False, indent=1).encode()


# ---------------------------------------------------------------------------
# Разбор JSON
# ---------------------------------------------------------------------------


class TestParseJson:
    def test_null_first_element_is_skipped_and_numbers_become_text(self) -> None:
        content = payment_json("aaaa1111", ["7 (999) 023-43-65"] * 5)
        table = parse_json(content)
        assert table.headers == PAYMENT_HEADERS
        assert len(table.rows) == 5
        assert table.rows[0][0] == "ORD-aaaa1111-1"
        assert table.rows[0][7] == "1"  # число из JSON -> «1», не «1.0»
        assert table.rows[2][7] == "2"

    def test_dispatched_by_source_format(self) -> None:
        table = parse_table(b'[{"a": 1}]', source_format="json")
        assert table.headers == ["a"] and table.rows == [["1"]]

    def test_object_with_single_array_is_accepted(self) -> None:
        table = parse_json(b'{"orders": [{"a": "x"}, {"a": "y"}]}')
        assert table.rows == [["x"], ["y"]]

    def test_keys_are_united_across_records(self) -> None:
        table = parse_json(b'[{"a": 1}, {"b": 2}]')
        assert table.headers == ["a", "b"]
        assert table.rows == [["1", ""], ["", "2"]]

    def test_nested_values_are_kept_as_json_text(self) -> None:
        table = parse_json('[{"a": {"k": "ключ"}}]'.encode())
        assert table.rows == [['{"k": "ключ"}']]

    def test_bom_is_accepted(self) -> None:
        assert parse_json(b'\xef\xbb\xbf[{"a": 1}]').rows == [["1"]]

    @pytest.mark.parametrize("body", [b"NaN", b'[{"a": NaN}]', b'[{"a": Infinity}]', b"{oops"])
    def test_invalid_json_is_a_clean_error(self, body: bytes) -> None:
        with pytest.raises(AppError) as excinfo:
            parse_json(body)
        assert excinfo.value.status == 422

    @pytest.mark.parametrize(
        "body", [b"[]", b"[null, 1, 2]", '"строка"'.encode(), b"42", b'{"a": 1}']
    )
    def test_no_records_is_an_error(self, body: bytes) -> None:
        with pytest.raises(AppError):
            parse_json(body)

    def test_huge_exponent_does_not_become_a_number(self) -> None:
        # `1e999` Python читает как `inf`: раньше он доезжал до `Decimal`/`int` и ронял применение.
        table = parse_json(b'[{"a": 1e999}]')
        assert table.rows == [[""]]


# ---------------------------------------------------------------------------
# Автоподбор колонок по типу сущности
# ---------------------------------------------------------------------------


class TestSuggestMappingPerEntity:
    def test_vendor_headers(self) -> None:
        mapping = suggest_mapping(VENDOR_HEADERS, list(VENDOR_CONTACT_FIELDS), "vendor_contact")
        assert mapping == {
            "Компания": "vendor_name",
            "Продукт": "product_names",
            "ФИО": "full_name",
            "Телефон": "phone",
            "Почта": "email",
            "Способ связи": "contact_methods",
        }

    def test_payment_headers(self) -> None:
        mapping = suggest_mapping(PAYMENT_HEADERS, list(PAYMENT_FIELDS), "payment")
        assert mapping == {
            "Номер заявки": "order_number",
            "Курс": "product_name",
            "Фамилия": "last_name",
            "Имя": "first_name",
            "Отчество": "middle_name",
            "Телефон": "phone",
            "Email": "email",
            "Номер потока": "stream_number",
        }

    def test_learner_template_headers_map_one_to_one(self) -> None:
        headers = [header for _target, header in learner.LMS_USER_COLUMNS]
        mapping = suggest_mapping([*headers, ""], list(LEARNER_FIELDS), "learner")
        assert [mapping[h] for h in headers] == [t for t, _h in learner.LMS_USER_COLUMNS]
        assert "" not in mapping  # пустой 31-й столбец шаблона (AE) игнорируется

    def test_learner_headers_with_parentheses_map_too(self) -> None:
        mapping = suggest_mapping(
            ["Отчество(при наличии)", "Имя (дательный падеж)"], list(LEARNER_FIELDS), "learner"
        )
        assert mapping == {
            "Отчество(при наличии)": "middle_name",
            "Имя (дательный падеж)": "first_name_dative",
        }

    def test_product_import_is_not_offered_organization_fields(self) -> None:
        # Было: для продуктов предлагались «ИНН», «Телефон» — и бэкенд отклонял такой маппинг.
        mapping = suggest_mapping(
            ["ИНН", "Телефон", "Почта", "Наименование", "Код"], list(PRODUCT_FIELDS), "product"
        )
        assert mapping == {"Наименование": "name", "Код": "code"}

    def test_every_suggested_target_exists_for_the_entity(self) -> None:
        for entity in ("organization", "product", "license", "vendor_contact", "payment"):
            targets = {f.target for f in import_fields.fields_for(entity)}
            headers = ["ИНН", "Телефон", "Почта", "Наименование", "Код", "Вендор", "Курс", "ФИО"]
            mapping = suggest_mapping(headers, list(import_fields.fields_for(entity)), entity)
            assert set(mapping.values()) <= targets, entity


class TestMappingRequirements:
    def test_vendor_needs_company_name_and_contact(self) -> None:
        assert missing_mapping_labels("vendor_contact", {"vendor_name", "full_name", "email"}) == []
        missing = missing_mapping_labels("vendor_contact", {"product_names"})
        assert "Компания" in missing
        assert "ФИО (или Фамилия и Имя)" in missing
        assert "Email или Телефон" in missing

    def test_last_and_first_name_columns_replace_full_name(self) -> None:
        ok = {"order_number", "product_name", "last_name", "first_name", "phone"}
        assert missing_mapping_labels("payment", ok) == []
        assert missing_mapping_labels("payment", ok - {"first_name"}) == ["ФИО (или Фамилия и Имя)"]

    def test_catalog_types_require_their_required_fields_too(self) -> None:
        # Раньше требовался только ключ: без «Наименования» строки падали уже на применении.
        assert missing_mapping_labels("product", {"code"}) == ["Наименование"]
        assert missing_mapping_labels("product", {"code", "name"}) == []


# ---------------------------------------------------------------------------
# Проверки значений
# ---------------------------------------------------------------------------


class TestNewFieldKinds:
    def test_stream_number(self) -> None:
        spec = FieldSpec("stream_number", "Номер потока", "stream")
        assert validate_field(spec, "2") == (2, None)
        assert validate_field(spec, "2.0") == (2, None)
        for bad in ("0", "1.5", "два", "-1", "NaN"):
            assert validate_field(spec, bad)[1] is not None, bad

    def test_amount(self) -> None:
        spec = FieldSpec("amount", "Сумма", "money")
        assert validate_field(spec, "15 000,50") == (Decimal("15000.50"), None)
        for bad in ("0", "-5", "NaN", "inf", "1e999", "много"):
            assert validate_field(spec, bad)[1] is not None, bad

    def test_int_rejects_non_finite_and_fractions(self) -> None:
        spec = FieldSpec("students_count", "Студенты", "int")
        for bad in ("inf", "NaN", "1e999", "12.7", "99999999999"):
            assert validate_field(spec, bad)[1] is not None, bad
        assert validate_field(spec, "1 200") == (1200, None)

    def test_decimal_rejects_non_finite_and_overflow(self) -> None:
        spec = FieldSpec("base_price", "Цена", "decimal")
        for bad in ("NaN", "Infinity", "1e999", "1000000000000"):
            assert validate_field(spec, bad)[1] is not None, bad

    def test_email_is_lowercased(self) -> None:
        spec = FieldSpec("email", "Email", "email")
        assert validate_field(spec, "Osipenko833484@Mail.RU") == ("osipenko833484@mail.ru", None)

    def test_person_name_is_squeezed(self) -> None:
        spec = FieldSpec("full_name", "ФИО", "person_name")
        assert validate_field(spec, "  Иванов   Иван ") == ("Иванов Иван", None)

    def test_too_long_value_is_a_row_error_not_a_crash(self) -> None:
        spec = FieldSpec("name", "Наименование", "text", max_length=10)
        assert validate_field(spec, "x" * 11)[1] is not None

    def test_product_list_and_contact_methods(self) -> None:
        products = FieldSpec("product_names", "Продукт", "product_list")
        assert validate_field(products, "«RT.DataLake», «RT.Warehouse»") == (
            ["RT.DataLake", "RT.Warehouse"],
            None,
        )
        methods = FieldSpec("contact_methods", "Способ связи", "contact_methods")
        assert validate_field(methods, "Почта, Чат в ТГ") == (["email", "telegram"], None)

    def test_learner_kind_delegates_to_template_validators(self) -> None:
        snils = next(f for f in LEARNER_FIELDS if f.target == "snils")
        assert validate_field(snils, "112-233-445 95") == ("11223344595", None)
        assert validate_field(snils, "112-233-445 96")[1] is not None
        sex = next(f for f in LEARNER_FIELDS if f.target == "sex")
        assert validate_field(sex, "Ж") == ("F", None)


class TestLearnerTemplateParsing:
    def test_first_sheet_only_and_numeric_phone(self) -> None:
        headers = [h for _t, h in learner.LMS_USER_COLUMNS]
        row: list[Any] = ["Черепанова", "Светлана", "Васильевна", 79990234365, "c@test.ru"]
        content = xlsx_bytes(headers, [row + [None] * 25], lookup=True)
        table = parse_xlsx(content)
        assert table.headers == headers
        assert table.rows[0][3] == "79990234365"  # число -> строка без «.0»
        assert table.rows[0][5] == ""  # СНИЛС пуст


# ---------------------------------------------------------------------------
# Сквозные сценарии (нужна БД)
# ---------------------------------------------------------------------------


needs_db = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _prepare_b2c_workflow(client) -> None:
    """Опубликованная воронка B2C по умолчанию со статусом «Оплата и договор оферты» и служебная
    учётка интеграций — то, что в проде даёт сид."""
    login(client, "ADMIN")
    create_published_workflow(
        client,
        graph_body(
            statuses=[
                {"code": "site_application", "name": "Заявка", "type": "initial", "sort_order": 10},
                {
                    "code": "payment_contract",
                    "name": "Оплата и договор оферты",
                    "type": "intermediate",
                    "sort_order": 20,
                },
                {"code": "won", "name": "Закрыта", "type": "won", "sort_order": 30},
            ],
            transitions=[
                {
                    "from_status": "site_application",
                    "to_status": "payment_contract",
                    "name": "Оплата",
                },
                {"from_status": "payment_contract", "to_status": "won", "name": "Закрыть"},
            ],
            sla_rules=[],
        ),
        deal_type="b2c",
        is_default=True,
    )

    async def _seed_integration() -> None:
        from app.core.db import session_scope
        from app.modules.integration.seed import seed_integration_account

        async with session_scope() as session:
            await seed_integration_account(session)

    run(client, _seed_integration)


async def _query(sql_factory):
    from app.core.db import session_scope

    async with session_scope() as session:
        return await sql_factory(session)


def db(client, fn):
    return run(client, _query, fn)


@needs_db
class TestVendorImport:
    def _rows(self, tok: str) -> tuple[list[list[str]], list[str]]:
        phones = [phone_parts()[0] for _ in range(8)]
        rows = [
            [
                f"ООО «Базис {tok}»",
                "«Базис Dynamix»",
                "Иванов Иван Иванович",
                phones[0],
                f"ivanov.{tok}@example.ru",
                "Почта, Чат в ТГ",
            ],
            [
                f"ООО «ТДата {tok}»",
                "«RT.DataLake», «RT.Warehouse»",
                "Смирнова Анна Петровна",
                phones[1],
                f"smirnova.{tok}@example.ru",
                "Чат в ТГ",
            ],
            [
                f"ПАО «Ростелеком {tok}»",
                "«RT.DataVision»",
                "Кузнецов Дмитрий Сергеевич",
                phones[2],
                f"kuznetsov.{tok}@example.ru",
                "Чат в ТГ",
            ],
            [
                f"ООО «РТК ИТ Плюс {tok}»",
                "«AKOLA»",
                "Попова Мария Владимировна",
                phones[3],
                f"popova.{tok}@example.ru",
                "Чат в ТГ",
            ],
            [
                f"ООО «РТК ИТ Плюс {tok}»",
                "«Яга»",
                "Соколов Алексей Андреевич",
                phones[4],
                f"sokolov.{tok}@example.ru",
                "Чат в ТГ",
            ],
            [
                f"ООО «РТК ИТ {tok}»",
                "«Web3Gate»",
                "Лебедева Елена Дмитриевна",
                phones[5],
                f"lebedeva.{tok}@example.ru",
                "Чат в ТГ",
            ],
            [
                f"ООО «РТК ИТ {tok}»",
                "«Аврора SDK»",
                "Козлов Максим Игоревич",
                phones[6],
                f"kozlov.{tok}@example.ru",
                "Чат в ТГ",
            ],
            [
                f"ООО «РТК ИТ {tok}»",
                "«Нейрошлюз»",
                "Новикова Ольга Александровна",
                phones[7],
                f"novikova.{tok}@example.ru",
                "Почта",
            ],
        ]
        return rows, phones

    def test_vendor_file_creates_companies_products_contacts_and_links(
        self, client, monkeypatch
    ) -> None:
        from sqlalchemy import func, select

        from app.modules.catalog.models import Contact, ContactProduct, Organization, Product

        tok = token()
        # Продукты вендорского файла в БД общие: уникализируем названия, не ломая структуру.
        rows, _phones = self._rows(tok)
        for row in rows:
            row[1] = row[1].replace("»", f" {tok}»")
        head = login(client, "HEAD")
        started = start_import(
            client,
            monkeypatch,
            head,
            entity_type="vendor_contact",
            content=xlsx_bytes(VENDOR_HEADERS, rows),
            source_format="xlsx",
        )
        assert started["mapping"]["Способ связи"] == "contact_methods"
        assert started["dry"]["total_rows"] == 8
        assert started["dry"]["error_rows"] == 0, job_rows(client, started["id"], status="error")
        assert started["dry"]["ok_rows"] == 8

        job = apply_job(client, started["id"])
        assert job["status"] == "completed"
        assert job["processed_rows"] == 8

        async def snapshot(session):
            orgs = (
                (
                    await session.execute(
                        select(Organization).where(Organization.name.like(f"%{tok}%"))
                    )
                )
                .scalars()
                .all()
            )
            products = (
                (await session.execute(select(Product).where(Product.name.like(f"%{tok}%"))))
                .scalars()
                .all()
            )
            contacts = (
                (await session.execute(select(Contact).where(Contact.email.like(f"%.{tok}@%"))))
                .scalars()
                .all()
            )
            links = (
                await session.execute(
                    select(func.count())
                    .select_from(ContactProduct)
                    .where(ContactProduct.product_id.in_([p.id for p in products]))
                )
            ).scalar_one()
            return orgs, products, contacts, links

        orgs, products, contacts, links = db(client, snapshot)
        # 5 компаний, а не 8: «РТК ИТ Плюс» — две строки, «РТК ИТ» — три.
        assert len(orgs) == 5
        assert {o.org_type for o in orgs} == {"company"}
        assert len(products) == 9  # «RT.DataLake, RT.Warehouse» — два продукта из одной ячейки
        assert all(p.vendor_id is not None for p in products)
        assert len(contacts) == 8
        by_email = {c.email: c for c in contacts}
        ivanov = by_email[f"ivanov.{tok}@example.ru"]
        assert ivanov.contact_methods == ["email", "telegram"]
        assert ivanov.phone.startswith("+79") and len(ivanov.phone) == 12
        assert (ivanov.last_name, ivanov.first_name, ivanov.middle_name) == (
            "Иванов",
            "Иван",
            "Иванович",
        )
        assert by_email[f"novikova.{tok}@example.ru"].contact_methods == ["email"]
        assert links == 9

        # Повторная загрузка того же файла ничего не размножает.
        again = start_import(
            client,
            monkeypatch,
            head,
            entity_type="vendor_contact",
            content=xlsx_bytes(VENDOR_HEADERS, rows),
            source_format="xlsx",
        )
        assert again["dry"]["error_rows"] == 0
        apply_job(client, again["id"])
        orgs2, products2, contacts2, links2 = db(client, snapshot)
        assert (len(orgs2), len(products2), len(contacts2), links2) == (5, 9, 8, 9)

    def test_existing_company_is_reused_regardless_of_quotes_and_case(
        self, client, monkeypatch
    ) -> None:
        from sqlalchemy import func, select

        from app.modules.catalog.models import Organization

        tok = token()
        rows, _ = self._rows(tok)
        rows = rows[:1]
        rows[0][1] = f"«Продукт {tok}»"

        async def pre_create(session):
            org = Organization(name=f'ооо "базис {tok}"', org_type="company", source="test")
            session.add(org)
            await session.flush()
            return org.id

        existing_id = db(client, pre_create)
        head = login(client, "HEAD")
        started = start_import(
            client,
            monkeypatch,
            head,
            entity_type="vendor_contact",
            content=xlsx_bytes(VENDOR_HEADERS, rows),
            source_format="xlsx",
        )
        apply_job(client, started["id"])

        async def count_and_vendor(session):
            from app.modules.catalog.models import Product

            # Токен латинский, поэтому LIKE не зависит от регистра кириллицы в локали БД.
            count = (
                await session.execute(
                    select(func.count())
                    .select_from(Organization)
                    .where(Organization.name.like(f"%{tok}%"), Organization.deleted_at.is_(None))
                )
            ).scalar_one()
            product = (
                await session.execute(select(Product).where(Product.name == f"Продукт {tok}"))
            ).scalar_one()
            return count, product.vendor_id

        count, vendor_id = db(client, count_and_vendor)
        assert count == 1
        assert vendor_id == existing_id

    def test_rollback_removes_what_the_import_created_and_keeps_the_rest(
        self, client, monkeypatch
    ) -> None:
        from sqlalchemy import select

        from app.modules.catalog.models import Contact, ContactProduct, Organization, Product

        tok = token()
        rows, _ = self._rows(tok)
        rows = rows[:2]
        for row in rows:
            row[1] = row[1].replace("»", f" {tok}»")

        async def pre_create_contact(session):
            # Смирнова уже есть в БД (например, из вебхука): импорт её не создаёт и откат не удалит.
            contact = Contact(
                first_name="Анна",
                last_name="Смирнова",
                email=f"smirnova.{tok}@example.ru",
                source="cms",
            )
            session.add(contact)
            await session.flush()
            return contact.id

        smirnova_id = db(client, pre_create_contact)
        head = login(client, "HEAD")
        started = start_import(
            client,
            monkeypatch,
            head,
            entity_type="vendor_contact",
            content=xlsx_bytes(VENDOR_HEADERS, rows),
            source_format="xlsx",
        )
        assert apply_job(client, started["id"])["status"] == "completed"
        rolled = rollback_job(client, started["id"])
        assert rolled["status"] == "rolled_back", job_rows(
            client, started["id"], status="rollback_blocked"
        )

        async def state(session):
            orgs = (
                (
                    await session.execute(
                        select(Organization).where(Organization.name.like(f"%{tok}%"))
                    )
                )
                .scalars()
                .all()
            )
            products = (
                (await session.execute(select(Product).where(Product.name.like(f"%{tok}%"))))
                .scalars()
                .all()
            )
            ivanov = (
                await session.execute(
                    select(Contact).where(Contact.email == f"ivanov.{tok}@example.ru")
                )
            ).scalar_one()
            smirnova = await session.get(Contact, smirnova_id)
            links = (
                (
                    await session.execute(
                        select(ContactProduct).where(
                            ContactProduct.contact_id.in_([ivanov.id, smirnova_id])
                        )
                    )
                )
                .scalars()
                .all()
            )
            return orgs, products, ivanov, smirnova, links

        orgs, products, ivanov, smirnova, links = db(client, state)
        assert all(o.deleted_at is not None for o in orgs) and len(orgs) == 2
        assert all(p.deleted_at is not None for p in products) and len(products) == 3
        assert ivanov.deleted_at is not None  # создан импортом -> снят
        assert smirnova.deleted_at is None  # была до импорта -> осталась
        assert smirnova.organization_id is None  # дозаполненное организацией — возвращено
        assert links == []

    def test_bad_rows_are_reported_and_good_ones_still_apply(self, client, monkeypatch) -> None:
        tok = token()
        rows, _ = self._rows(tok)
        rows = rows[:3]
        rows[0][2] = "Иванов"  # только фамилия
        rows[1][3] = "123"  # телефон
        rows[1][4] = ""  # и нет email — идентифицировать нечем
        rows[2][5] = "Почта, Голубь"  # нераспознанный способ связи — предупреждение
        head = login(client, "HEAD")
        started = start_import(
            client,
            monkeypatch,
            head,
            entity_type="vendor_contact",
            content=xlsx_bytes(VENDOR_HEADERS, rows),
            source_format="xlsx",
        )
        dry = started["dry"]
        assert (dry["ok_rows"], dry["warn_rows"], dry["error_rows"]) == (0, 1, 2)
        errors = {r["row_number"]: r["errors"] for r in job_rows(client, started["id"])}
        assert any("фамилию и имя" in e for e in errors[1])
        assert any("телефон" in e for e in errors[2])
        assert any("Голубь" in e for e in errors[3])
        assert dry["result_file_id"] is not None  # xlsx с причинами
        job = apply_job(client, started["id"])
        assert job["status"] == "completed_with_errors"

    def test_insert_mode_skips_existing_contact_and_update_needs_one(
        self, client, monkeypatch
    ) -> None:
        tok = token()
        rows, _ = self._rows(tok)
        rows = rows[:2]
        for row in rows:
            row[1] = row[1].replace("»", f" {tok}»")
        head = login(client, "HEAD")
        content = xlsx_bytes(VENDOR_HEADERS, rows)
        first = start_import(
            client,
            monkeypatch,
            head,
            entity_type="vendor_contact",
            content=content,
            source_format="xlsx",
            mode="update",
        )
        assert first["dry"]["error_rows"] == 2  # обновлять пока нечего
        second = start_import(
            client,
            monkeypatch,
            head,
            entity_type="vendor_contact",
            content=content,
            source_format="xlsx",
            mode="insert",
        )
        assert second["dry"]["error_rows"] == 0
        apply_job(client, second["id"])
        third = start_import(
            client,
            monkeypatch,
            head,
            entity_type="vendor_contact",
            content=content,
            source_format="xlsx",
            mode="insert",
        )
        assert third["dry"]["warn_rows"] == 2  # «уже существует — будет пропущена»
        assert apply_job(client, third["id"])["status"] == "completed"
        assert {r["status"] for r in job_rows(client, third["id"])} == {"skipped"}


@needs_db
class TestPaymentImport:
    def test_json_payments_become_paid_b2c_deals_with_stream_numbers(
        self, client, monkeypatch
    ) -> None:
        from sqlalchemy import select

        from app.modules.catalog.models import Contact, Product
        from app.modules.crm.models import Deal, DealProduct
        from app.modules.workflow.models import WorkflowStatus

        tok = token()
        _prepare_b2c_workflow(client)
        phones = [phone_parts()[0] for _ in range(5)]
        content = payment_json(tok, phones)
        head = login(client, "HEAD")
        started = start_import(
            client,
            monkeypatch,
            head,
            entity_type="payment",
            content=content,
            source_format="json",
        )
        assert started["mapping"]["Номер потока"] == "stream_number"
        dry = started["dry"]
        assert dry["total_rows"] == 5  # `null` в начале файла не строка
        assert dry["error_rows"] == 0, job_rows(client, started["id"], status="error")
        # Курсов нет в каталоге, суммы в файле нет — два предупреждения на строку, но не ошибки.
        assert dry["warn_rows"] == 5
        warnings = job_rows(client, started["id"])[0]["errors"]
        assert any("не найден в каталоге" in w for w in warnings)
        assert any("Сумма не указана" in w for w in warnings)

        job = apply_job(client, started["id"])
        assert job["status"] == "completed", job

        async def snapshot(session):
            deals = (
                (await session.execute(select(Deal).where(Deal.order_number.like(f"ORD-{tok}-%"))))
                .scalars()
                .all()
            )
            lines = {
                d.order_number: (
                    await session.execute(select(DealProduct).where(DealProduct.deal_id == d.id))
                )
                .scalars()
                .all()
                for d in deals
            }
            status_codes = {
                d.order_number: (await session.get(WorkflowStatus, d.status_id)).code for d in deals
            }
            contacts = (
                (await session.execute(select(Contact).where(Contact.email.like(f"%.{tok}@%"))))
                .scalars()
                .all()
            )
            products = (
                (await session.execute(select(Product).where(Product.name.like(f"%{tok}%"))))
                .scalars()
                .all()
            )
            return deals, lines, status_codes, contacts, products

        deals, lines, status_codes, contacts, products = db(client, snapshot)
        assert len(deals) == 5 and len(contacts) == 5 and len(products) == 5
        assert set(status_codes.values()) == {"payment_contract"}
        assert all(d.deal_type == "b2c" and d.source == "import" for d in deals)
        assert all(d.custom_fields.get("payment_confirmed") is True for d in deals)
        assert all(d.amount is None for d in deals)  # суммы нет ни в файле, ни в прайсе
        assert sorted(line.stream_number for ls in lines.values() for line in ls) == [1, 1, 2, 3, 4]
        # «Max_Crich.…@mail.ru» из файла хранится в нижнем регистре.
        assert f"max_crich.{tok}@mail.ru" in {c.email for c in contacts}
        assert all(c.phone.startswith("+79") for c in contacts)
        assert all(p.custom_fields.get("auto_created") for p in products)

        # Повторная загрузка — не дубли: ни сделок, ни людей, ни курсов.
        again = start_import(
            client, monkeypatch, head, entity_type="payment", content=content, source_format="json"
        )
        apply_job(client, again["id"])
        deals2, _l, _s, contacts2, products2 = db(client, snapshot)
        assert (len(deals2), len(contacts2), len(products2)) == (5, 5, 5)

    def test_price_from_catalog_becomes_the_deal_amount_and_file_amount_wins(
        self, client, monkeypatch
    ) -> None:
        from sqlalchemy import select

        from app.modules.catalog.models import Product
        from app.modules.crm.models import Deal

        tok = token()
        _prepare_b2c_workflow(client)
        pretty, _norm = phone_parts()
        course = f"Промпт-инжиниринг {tok}"

        async def add_product(session):
            session.add(Product(code=f"prompt-{tok}", name=course, base_price=Decimal("15000.00")))

        db(client, add_product)
        head = login(client, "HEAD")
        rows = [
            [
                f"ORD-{tok}-1",
                course,
                "Осипенко",
                "Ирина",
                "Викторовна",
                pretty,
                f"o.{tok}@mail.ru",
                3,
            ],
        ]
        started = start_import(
            client,
            monkeypatch,
            head,
            entity_type="payment",
            content=xlsx_bytes(PAYMENT_HEADERS, rows),
            source_format="xlsx",
        )
        assert started["dry"]["error_rows"] == 0
        apply_job(client, started["id"])

        async def amount(session):
            return (
                await session.execute(
                    select(Deal.amount).where(Deal.order_number == f"ORD-{tok}-1")
                )
            ).scalar_one()

        assert db(client, amount) == Decimal("15000.00")

        # Сумма в файле важнее прайса: новый заказ с колонкой «Сумма».
        rows2 = [
            [
                f"ORD-{tok}-2",
                course,
                "Кричанов",
                "Максим",
                "",
                phone_parts()[0],
                f"k.{tok}@mail.ru",
                1,
                "12 500,00",
            ]
        ]
        started2 = start_import(
            client,
            monkeypatch,
            head,
            entity_type="payment",
            content=xlsx_bytes([*PAYMENT_HEADERS, "Сумма"], rows2),
            source_format="xlsx",
        )
        assert started2["mapping"]["Сумма"] == "amount"
        apply_job(client, started2["id"])

        async def amount2(session):
            return (
                await session.execute(
                    select(Deal.amount).where(Deal.order_number == f"ORD-{tok}-2")
                )
            ).scalar_one()

        assert db(client, amount2) == Decimal("12500.00")

    def test_same_person_in_two_orders_and_two_formats_is_one_contact(
        self, client, monkeypatch
    ) -> None:
        from sqlalchemy import func, select

        from app.modules.catalog.models import Contact
        from app.modules.crm.models import Deal

        tok = token()
        _prepare_b2c_workflow(client)
        pretty, normalized = phone_parts()
        digits = normalized[1:]
        rows = [
            [
                f"ORD-{tok}-1",
                f"Курс А {tok}",
                "Осипенко",
                "Ирина",
                "Викторовна",
                pretty,
                f"Osipenko.{tok}@Mail.ru",
                1,
            ],
            # Тот же человек: другой регистр email, телефон числом без плюса, другой курс.
            [
                f"ORD-{tok}-2",
                f"Курс Б {tok}",
                "Осипенко",
                "Ирина",
                "Викторовна",
                digits,
                f"osipenko.{tok}@mail.ru",
                2,
            ],
        ]
        head = login(client, "HEAD")
        started = start_import(
            client,
            monkeypatch,
            head,
            entity_type="payment",
            content=xlsx_bytes(PAYMENT_HEADERS, rows),
            source_format="xlsx",
        )
        apply_job(client, started["id"])

        async def counts(session):
            contacts = (
                await session.execute(
                    select(func.count()).select_from(Contact).where(Contact.phone == normalized)
                )
            ).scalar_one()
            deals = (
                await session.execute(
                    select(func.count())
                    .select_from(Deal)
                    .where(Deal.order_number.like(f"ORD-{tok}-%"))
                )
            ).scalar_one()
            return contacts, deals

        assert db(client, counts) == (1, 2)

        # Откат идёт с конца файла: заказ 2, затем заказ 1 вместе с контактом — без блокировок.
        rolled = rollback_job(client, started["id"])
        assert rolled["status"] == "rolled_back"

    def test_rollback_blocked_by_a_deal_in_work_then_retried(self, client, monkeypatch) -> None:
        from sqlalchemy import select, update

        from app.modules.catalog.models import Contact
        from app.modules.crm.models import Deal

        tok = token()
        _prepare_b2c_workflow(client)
        rows = [
            [
                f"ORD-{tok}-1",
                f"Курс {tok}",
                "Кричанов",
                "Максим",
                "",
                phone_parts()[0],
                f"k.{tok}@mail.ru",
                1,
            ]
        ]
        head = login(client, "HEAD")
        started = start_import(
            client,
            monkeypatch,
            head,
            entity_type="payment",
            content=xlsx_bytes(PAYMENT_HEADERS, rows),
            source_format="xlsx",
        )
        apply_job(client, started["id"])

        async def put_in_work(session):
            await session.execute(
                update(Deal).where(Deal.order_number == f"ORD-{tok}-1").values(version=2)
            )

        db(client, put_in_work)
        job = rollback_job(client, started["id"])
        assert job["status"] == "completed_with_errors"  # не «успешно откатан»
        assert job["rollback_available"] is True
        blocked = job_rows(client, started["id"], status="rollback_blocked")
        assert blocked and any("в работе" in e for e in blocked[0]["errors"])

        async def contact_alive(session):
            return (
                await session.execute(select(Contact).where(Contact.email == f"k.{tok}@mail.ru"))
            ).scalar_one().deleted_at is None

        assert db(client, contact_alive) is True

        async def close_deal(session):  # менеджер закрыл вопрос со сделкой
            await session.execute(
                update(Deal)
                .where(Deal.order_number == f"ORD-{tok}-1")
                .values(deleted_at=Deal.created_at)
            )

        db(client, close_deal)
        job = rollback_job(client, started["id"])
        assert job["status"] == "rolled_back"
        assert db(client, contact_alive) is False

    def test_invalid_rows_are_errors_with_reasons(self, client, monkeypatch) -> None:
        tok = token()
        _prepare_b2c_workflow(client)
        pretty, _ = phone_parts()
        good = [
            f"ORD-{tok}-1",
            f"Курс {tok}",
            "Иванов",
            "Михаил",
            "",
            pretty,
            f"i.{tok}@mail.ru",
            4,
        ]
        rows = [
            good,
            [f"ORD-{tok}-2", f"Курс {tok}", "Иванов", "Михаил", "", pretty, f"i.{tok}@mail.ru", 0],
            [f"ORD-{tok}-3", "", "Иванов", "Михаил", "", pretty, f"i.{tok}@mail.ru", 1],
            [f"ORD-{tok}-4", f"Курс {tok}", "Иванов", "Михаил", "", "", "не-почта", 1],
            ["", f"Курс {tok}", "Иванов", "Михаил", "", pretty, f"i.{tok}@mail.ru", 1],
        ]
        head = login(client, "HEAD")
        started = start_import(
            client,
            monkeypatch,
            head,
            entity_type="payment",
            content=xlsx_bytes(PAYMENT_HEADERS, rows),
            source_format="xlsx",
        )
        dry = started["dry"]
        assert dry["error_rows"] == 4
        by_row = {r["row_number"]: " ".join(r["errors"]) for r in job_rows(client, started["id"])}
        assert "от 1" in by_row[2]  # поток 0
        assert "Курс" in by_row[3]
        assert "email" in by_row[4].lower()
        assert "Номер заявки" in by_row[5]
        job = apply_job(client, started["id"])
        assert job["status"] == "completed_with_errors"

    def test_duplicate_order_number_inside_the_file_is_flagged(self, client, monkeypatch) -> None:
        tok = token()
        _prepare_b2c_workflow(client)
        pretty, _ = phone_parts()
        row = [f"ORD-{tok}-1", f"Курс {tok}", "Иванов", "Михаил", "", pretty, f"i.{tok}@mail.ru", 1]
        head = login(client, "HEAD")
        started = start_import(
            client,
            monkeypatch,
            head,
            entity_type="payment",
            content=xlsx_bytes(PAYMENT_HEADERS, [row, row]),
            source_format="xlsx",
        )
        second = job_rows(client, started["id"])[1]
        assert any("Повтор внутри файла" in e for e in second["errors"])
        apply_job(client, started["id"])

        from sqlalchemy import func, select

        from app.modules.crm.models import Deal

        async def deals(session):
            return (
                await session.execute(
                    select(func.count())
                    .select_from(Deal)
                    .where(Deal.order_number == f"ORD-{tok}-1")
                )
            ).scalar_one()

        assert db(client, deals) == 1


@needs_db
class TestLearnerImport:
    def _template(self, tok: str, extra: dict[str, Any] | None = None) -> tuple[bytes, str, str]:
        headers = [h for _t, h in learner.LMS_USER_COLUMNS]
        index = {t: i for i, (t, _h) in enumerate(learner.LMS_USER_COLUMNS)}
        pretty, normalized = phone_parts()
        row: list[Any] = [None] * 30
        row[index["last_name"]] = "Черепанова"
        row[index["first_name"]] = "Светлана"
        row[index["middle_name"]] = "Васильевна"
        row[index["phone"]] = int(normalized[1:])  # число в ячейке, как в шаблоне
        row[index["email"]] = f"Cherepanova.{tok}@test.ru"
        for key, value in (extra or {}).items():
            row[index[key]] = value
        return xlsx_bytes(headers, [row], lookup=True), normalized, f"cherepanova.{tok}@test.ru"

    def test_template_with_only_basic_columns_matches_existing_contact(
        self, client, monkeypatch
    ) -> None:
        from sqlalchemy import func, select

        from app.modules.catalog.models import Contact, ContactLearnerProfile

        tok = token()
        content, normalized, email = self._template(tok)

        async def pre_create(session):
            contact = Contact(
                first_name="Светлана", last_name="Черепанова", email=email, phone=None, source="cms"
            )
            session.add(contact)
            await session.flush()
            return contact.id

        existing_id = db(client, pre_create)
        head = login(client, "HEAD")
        started = start_import(
            client, monkeypatch, head, entity_type="learner", content=content, source_format="xlsx"
        )
        assert started["dry"]["error_rows"] == 0, job_rows(client, started["id"])
        apply_job(client, started["id"])

        async def state(session):
            contacts = (
                await session.execute(
                    select(func.count()).select_from(Contact).where(Contact.email == email)
                )
            ).scalar_one()
            contact = await session.get(Contact, existing_id)
            profile = await session.get(ContactLearnerProfile, existing_id)
            return contacts, contact.phone, contact.middle_name, profile

        contacts, phone, middle, profile = db(client, state)
        assert contacts == 1  # тот же человек — не второй экземпляр
        assert phone == normalized  # телефон из шаблона дозаполнил карточку
        assert middle == "Васильевна"
        assert profile is None  # в файле нет ни одного поля профиля — пустую запись не заводим

    def test_profile_fields_are_validated_stored_and_kept_out_of_the_audit(
        self, client, monkeypatch
    ) -> None:
        import datetime as dt

        from sqlalchemy import select

        from app.modules.audit.models import AuditLog
        from app.modules.catalog.models import Contact, ContactLearnerProfile

        tok = token()
        content, _normalized, email = self._template(
            tok,
            {
                "snils": "112-233-445 95",
                "passport_series": "45 12",
                "passport_number": 123456,
                "passport_dept_code": "770001",
                "sex": "Ж",
                "birth_date": dt.datetime(1990, 5, 17),
                "reg_city": "Москва",
                "education": "Высшее образование – бакалавриат",
            },
        )
        head = login(client, "HEAD")
        started = start_import(
            client, monkeypatch, head, entity_type="learner", content=content, source_format="xlsx"
        )
        assert started["dry"]["error_rows"] == 0, job_rows(client, started["id"])
        apply_job(client, started["id"])

        async def state(session):
            contact = (
                await session.execute(select(Contact).where(Contact.email == email))
            ).scalar_one()
            profile = await session.get(ContactLearnerProfile, contact.id)
            audit = (
                (await session.execute(select(AuditLog).where(AuditLog.entity_id == contact.id)))
                .scalars()
                .all()
            )
            return contact, profile, audit

        contact, profile, audit = db(client, state)
        assert profile.snils == "11223344595"
        assert (profile.passport_series, profile.passport_number) == ("4512", "123456")
        assert profile.passport_dept_code == "770-001"
        assert profile.sex == "F" and profile.education == "higher_bachelor"
        assert profile.birth_date == dt.date(1990, 5, 17)
        blob = json.dumps([a.changes for a in audit], ensure_ascii=False)
        for secret in ("11223344595", "4512", "123456", "Москва", "1990"):
            assert secret not in blob, secret

        # Откат: профиль снят, контакт (создан импортом) — тоже.
        assert rollback_job(client, started["id"])["status"] == "rolled_back"

        async def after(session):
            return await session.get(ContactLearnerProfile, contact.id)

        assert db(client, after) is None

    def test_bad_snils_sex_and_education_are_row_errors(self, client, monkeypatch) -> None:
        tok = token()
        content, _n, _e = self._template(
            tok, {"snils": "112-233-445 96", "sex": "?", "education": "Высшее"}
        )
        head = login(client, "HEAD")
        started = start_import(
            client, monkeypatch, head, entity_type="learner", content=content, source_format="xlsx"
        )
        assert started["dry"]["error_rows"] == 1
        text = " ".join(job_rows(client, started["id"])[0]["errors"])
        assert "СНИЛС" in text and "Пол" in text and "Образование" in text


@needs_db
class TestImportFramework:
    def test_poisoned_row_does_not_hang_the_job(self, client, monkeypatch) -> None:
        from app.modules.catalog.models import Product

        tok = token()
        deleted_code = f"del-{tok}"

        async def pre_create_deleted(session):
            import datetime as dt

            session.add(
                Product(
                    code=deleted_code, name=f"Удалённый {tok}", deleted_at=dt.datetime.now(dt.UTC)
                )
            )

        db(client, pre_create_deleted)
        head = login(client, "HEAD")
        rows = [
            [deleted_code, f"Первый {tok}"],  # код занят удалённым продуктом: сорвётся на вставке
            [f"ok-{tok}", f"Второй {tok}"],
        ]
        started = start_import(
            client,
            monkeypatch,
            head,
            entity_type="product",
            content=xlsx_bytes(["Код", "Наименование"], rows),
            source_format="xlsx",
        )
        assert started["dry"]["ok_rows"] == 2  # проверка этого не видит
        job = apply_job(client, started["id"])
        assert job["status"] == "completed_with_errors"  # раньше — вечный `applying`
        assert job["processed_rows"] == 2
        assert (job["ok_rows"], job["error_rows"]) == (1, 1)
        statuses = {r["row_number"]: r for r in job_rows(client, started["id"])}
        assert statuses[1]["status"] == "error"
        assert "удалённым продуктом" in " ".join(statuses[1]["errors"])
        assert statuses[2]["status"] == "ok" and statuses[2]["entity_id"]

    def test_rollback_of_an_unchanged_upsert_keeps_the_record_created_earlier(
        self, client, monkeypatch
    ) -> None:
        from sqlalchemy import select

        from app.modules.catalog.models import Organization

        inn = make_inn()
        rows = [[f"Университет {inn}", inn]]
        head = login(client, "HEAD")
        content = xlsx_bytes(["Наименование", "ИНН"], rows)
        first = start_import(
            client,
            monkeypatch,
            head,
            entity_type="organization",
            content=content,
            source_format="xlsx",
        )
        assert first["dry"]["error_rows"] == 0, job_rows(client, first["id"])
        assert apply_job(client, first["id"])["status"] == "completed"
        second = start_import(
            client,
            monkeypatch,
            head,
            entity_type="organization",
            content=content,
            source_format="xlsx",
        )
        assert apply_job(client, second["id"])["status"] == "completed"

        async def org(session):
            return (
                await session.execute(
                    select(Organization).where(
                        Organization.inn == inn, Organization.deleted_at.is_(None)
                    )
                )
            ).scalar_one_or_none()

        # Второй импорт ничего не менял. Его откат раньше удалял организацию, созданную первым.
        assert rollback_job(client, second["id"])["status"] == "rolled_back"
        assert db(client, org) is not None
        assert rollback_job(client, first["id"])["status"] == "rolled_back"
        assert db(client, org) is None

    def test_mapping_cannot_be_changed_after_apply_and_must_be_complete(
        self, client, monkeypatch
    ) -> None:
        tok = token()
        head = login(client, "HEAD")
        content = xlsx_bytes(["Код", "Наименование"], [[f"c-{tok}", f"Продукт {tok}"]])
        started = start_import(
            client, monkeypatch, head, entity_type="product", content=content, source_format="xlsx"
        )
        job_id = started["id"]
        incomplete = client.put(f"/api/imports/{job_id}/mapping", json={"mapping": {"Код": "code"}})
        assert incomplete.status_code == 422
        assert "Наименование" in incomplete.text
        repeated = client.put(
            f"/api/imports/{job_id}/mapping",
            json={"mapping": {"Код": "code", "Наименование": "code"}},
        )
        assert repeated.status_code == 422
        apply_job(client, job_id)
        late = client.put(f"/api/imports/{job_id}/mapping", json={"mapping": started["mapping"]})
        assert late.status_code == 422
        assert get_job(client, job_id)["status"] == "completed"  # задание не сброшено

    def test_entity_types_endpoint_lists_all_six_with_fields(self, client) -> None:
        login(client, "HEAD")
        response = client.get("/api/imports/entity-types")
        assert response.status_code == 200, response.text
        items = {i["code"]: i for i in response.json()["items"]}
        assert set(items) == {
            "organization",
            "product",
            "license",
            "vendor_contact",
            "payment",
            "learner",
        }
        assert len(items["learner"]["fields"]) == 30
        assert "json" in items["payment"]["source_formats"]
        assert "Компания" in items["vendor_contact"]["requirements"]
        assert [f["target"] for f in items["payment"]["fields"]][:2] == [
            "order_number",
            "product_name",
        ]

    def test_rows_endpoint_masks_personal_data(self, client, monkeypatch) -> None:
        tok = token()
        pretty, _ = phone_parts()
        head = login(client, "HEAD")
        rows = [
            [
                "ООО «Тест»",
                f"«Продукт {tok}»",
                "Иванов Иван",
                pretty,
                f"iv.{tok}@example.ru",
                "Почта",
            ]
        ]
        started = start_import(
            client,
            monkeypatch,
            head,
            entity_type="vendor_contact",
            content=xlsx_bytes(VENDOR_HEADERS, rows),
            source_format="xlsx",
        )
        row = job_rows(client, started["id"])[0]
        assert row["row_data"]["email"].startswith("i***@")
        assert "***" in row["row_data"]["phone"]

    def test_someone_elses_file_is_not_found_for_head_but_admin_may_use_it(
        self, client, monkeypatch
    ) -> None:
        from tests.import_helpers import _seed_file, stub_storage

        content = xlsx_bytes(["Код", "Наименование"], [["x", "y"]])
        stub_storage(monkeypatch, content)
        owner = login(client, "HEAD")
        file_id = run(client, _seed_file, content, owner.id, "own.xlsx", "application/octet-stream")
        other = login(client, "HEAD")  # другой руководитель
        payload = {
            "file_id": str(file_id),
            "entity_type": "product",
            "mode": "upsert",
            "source_format": "xlsx",
        }
        assert client.post("/api/imports", json=payload).status_code == 404
        sign_in(client, owner)
        assert client.post("/api/imports", json=payload).status_code == 201
        login(client, "ADMIN")
        assert client.post("/api/imports", json=payload).status_code == 201
        assert other.id != owner.id
