"""Нормализация ключей дедупликации (`app.core.normalize`): чистые функции без БД.

Значения взяты из трёх файлов заказчика («Вендоры», «Данные оплат», «Загрузка пользователей»):
один и тот же человек приходит в них с телефоном в трёх разных написаниях, а курс и компания — с
кавычками-«ёлочками» внутри названия.
"""

from __future__ import annotations

import pytest

from app.core.normalize import (
    clean_text,
    company_key,
    name_key,
    normalize_email,
    normalize_phone,
    parse_contact_methods,
    slugify_code,
    split_full_name,
    split_list,
)


class TestNormalizePhone:
    @pytest.mark.parametrize(
        "raw",
        [
            "7 (999) 023-43-65",  # «Данные оплат.json»
            79990234365,  # «Загрузка пользователей.xlsx»: число в ячейке
            "79990234365",
            "+7 (999) 023-43-65",
            "8 999 023 43 65",
            "9990234365",
            "+7-999-023-43-65",
        ],
    )
    def test_same_person_gives_the_same_key(self, raw: object) -> None:
        assert normalize_phone(raw) == "+79990234365"

    def test_vendor_file_format(self) -> None:
        assert normalize_phone("+7 (900) 111-22-33") == "+79001112233"

    def test_extension_is_dropped(self) -> None:
        assert normalize_phone("+7 495 123-45-67 доб. 123") == "+74951234567"

    def test_foreign_number_with_plus_is_kept(self) -> None:
        assert normalize_phone("+1 (415) 555-2671") == "+14155552671"

    @pytest.mark.parametrize(
        "raw", ["12345", "", "   ", None, "abc", "+7 999 12", "1 415 555 2671"]
    )
    def test_unparsable_is_none(self, raw: object) -> None:
        assert normalize_phone(raw) is None


class TestNormalizeEmail:
    def test_lowercased(self) -> None:
        assert normalize_email("Osipenko833484@Mail.RU") == "osipenko833484@mail.ru"

    def test_stripped(self) -> None:
        assert normalize_email("  A@B.RU ") == "a@b.ru"

    @pytest.mark.parametrize("raw", ["not-an-email", "a@", "@b.ru", "", None, "a b@c.ru"])
    def test_invalid_or_empty_is_none(self, raw: object) -> None:
        assert normalize_email(raw) is None


class TestKeys:
    def test_company_key_ignores_quotes_case_yo_and_spaces(self) -> None:
        keys = {
            company_key("ООО «Базис»"),
            company_key('ооо "Базис"'),
            company_key("ООО  Базис"),
            company_key("  ООО «БАЗИС» "),
        }
        assert len(keys) == 1

    def test_legal_form_is_part_of_the_key(self) -> None:
        assert company_key("ПАО «Ростелеком»") != company_key("ООО «Ростелеком»")

    def test_product_quotes_do_not_matter(self) -> None:
        assert company_key("«RT.DataLake»") == company_key("RT.DataLake")

    def test_course_with_nested_quotes_is_stable(self) -> None:
        course = "Управление ИТ-проектами на базе программного продукта ПАО «Ростелеком»"
        assert company_key(course) == company_key(course.replace("«", '"').replace("»", '"'))

    def test_name_key_yo_and_case(self) -> None:
        assert name_key("Фёдоров") == name_key("ФЕДОРОВ")

    def test_clean_text_collapses_spaces(self) -> None:
        assert clean_text("  Иван \n  Иванов ") == "Иван Иванов"
        assert clean_text("   ") is None
        assert clean_text(None) is None


class TestSplitFullName:
    def test_three_parts(self) -> None:
        assert split_full_name("Иванов Иван Иванович") == ("Иванов", "Иван", "Иванович")

    def test_two_parts(self) -> None:
        assert split_full_name("Иванов Иван") == ("Иванов", "Иван", None)

    def test_one_part(self) -> None:
        assert split_full_name("Иванов") == ("Иванов", None, None)

    def test_middle_name_of_several_words(self) -> None:
        assert split_full_name("Алиев Расул Мамед оглы") == ("Алиев", "Расул", "Мамед оглы")

    def test_empty(self) -> None:
        assert split_full_name("  ") == (None, None, None)


class TestSplitList:
    def test_vendor_products_cell(self) -> None:
        assert split_list("«RT.DataLake», «RT.Warehouse»") == ["RT.DataLake", "RT.Warehouse"]

    def test_single_value(self) -> None:
        assert split_list("«Базис Dynamix»") == ["Базис Dynamix"]

    def test_separator_inside_quotes_does_not_split(self) -> None:
        assert split_list("«Аврора, SDK», Яга") == ["Аврора, SDK", "Яга"]

    def test_other_separators_and_duplicates(self) -> None:
        assert split_list("a; b\nc | A") == ["a", "b", "c"]

    def test_empty(self) -> None:
        assert split_list("") == []
        assert split_list(None) == []


class TestSlugify:
    def test_latin_and_cyrillic(self) -> None:
        assert slugify_code("«Базис Dynamix»") == "bazis-dynamix"
        assert slugify_code("RT.DataLake") == "rt-datalake"

    def test_long_course_is_truncated_without_trailing_dash(self) -> None:
        slug = slugify_code("Python-разработчик с использованием инструментов ИИ")
        assert len(slug) <= 48
        assert slug.startswith("python-razrabotchik-s-ispolzovaniem")
        assert not slug.endswith("-")

    def test_nothing_usable_falls_back(self) -> None:
        assert slugify_code("«»") == "item"


class TestContactMethods:
    def test_email_and_telegram(self) -> None:
        assert parse_contact_methods("Почта, Чат в ТГ") == (["email", "telegram"], [])

    def test_telegram_only(self) -> None:
        assert parse_contact_methods("Чат в ТГ") == (["telegram"], [])

    def test_email_only(self) -> None:
        assert parse_contact_methods("Почта") == (["email"], [])

    def test_phone_and_whatsapp(self) -> None:
        assert parse_contact_methods("Звонок; WhatsApp") == (["phone", "whatsapp"], [])

    def test_unknown_token_is_reported_not_lost(self) -> None:
        assert parse_contact_methods("Почта, Голубь") == (["email"], ["Голубь"])

    def test_empty(self) -> None:
        assert parse_contact_methods("") == ([], [])
