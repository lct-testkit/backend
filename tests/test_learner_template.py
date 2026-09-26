"""Шаблон LMS «Загрузка пользователей» (`app.modules.catalog.learner`): колонки, справочники,
проверки."""

from __future__ import annotations

import datetime as dt

import pytest

from app.modules.catalog import learner


class TestTemplateColumns:
    def test_thirty_columns_in_template_order(self) -> None:
        headers = [header for _target, header in learner.LMS_USER_COLUMNS]
        assert len(headers) == 30
        assert headers[:5] == ["Фамилия", "Имя", "Отчествопри наличии)", "Номер телефона", "Email"]
        assert headers[11] == "Пол"  # колонка L — на ней висит выпадающий список
        assert headers[22] == "Образование"  # колонка W — второй выпадающий список
        assert headers[-1] == "Дата выдачи диплома"

    def test_targets_are_unique(self) -> None:
        targets = [target for target, _header in learner.LMS_USER_COLUMNS]
        assert len(set(targets)) == len(targets)

    def test_profile_targets_exclude_contact_fields(self) -> None:
        assert "email" not in learner.PROFILE_TARGETS
        assert "snils" in learner.PROFILE_TARGETS
        assert len(learner.PROFILE_TARGETS) == 25

    def test_education_lookup_has_seven_levels(self) -> None:
        assert len(learner.EDUCATION_LEVELS) == 7
        assert learner.EDUCATION_LABELS["higher_bachelor"] == "Высшее образование – бакалавриат"


class TestHeaderMatching:
    @pytest.mark.parametrize(
        "header",
        ["Отчество(при наличии)", "Отчество (при наличии)", "Отчествопри наличии)", "ОТЧЕСТВО"],
    )
    def test_middle_name_variants(self, header: str) -> None:
        assert learner.target_for_header(header) == "middle_name"

    @pytest.mark.parametrize("header", ["Имя (дательный падеж)", "Имядательный падеж)"])
    def test_dative_variants(self, header: str) -> None:
        assert learner.target_for_header(header) == "first_name_dative"

    def test_unknown_header(self) -> None:
        assert learner.target_for_header("Совершенно постороннее") is None
        assert learner.target_for_header("") is None


class TestSnils:
    def test_valid_with_separators(self) -> None:
        assert learner.normalize_snils("112-233-445 95") == "11223344595"

    def test_wrong_checksum(self) -> None:
        with pytest.raises(ValueError, match="контрольное"):
            learner.normalize_snils("112-233-445 96")

    def test_wrong_length(self) -> None:
        with pytest.raises(ValueError, match="11 цифр"):
            learner.normalize_snils("123")

    def test_old_numbers_have_no_checksum(self) -> None:
        assert learner.normalize_snils("001-001-001 00") == "00100100100"

    def test_format(self) -> None:
        assert learner.format_snils("11223344595") == "112-233-445 95"


class TestPassportAndAddress:
    def test_series_and_number(self) -> None:
        assert learner.normalize_passport_series("45 12") == "4512"
        assert learner.normalize_passport_number("123 456") == "123456"

    def test_series_wrong_length(self) -> None:
        with pytest.raises(ValueError):
            learner.normalize_passport_series("451")

    def test_dept_code_gets_dash(self) -> None:
        assert learner.normalize_dept_code("770001") == "770-001"
        assert learner.normalize_dept_code("770-001") == "770-001"

    def test_zip(self) -> None:
        assert learner.normalize_zip("101000") == "101000"
        with pytest.raises(ValueError):
            learner.normalize_zip("1010")


class TestSexAndEducation:
    @pytest.mark.parametrize(("raw", "expected"), [("М", "M"), ("ж", "F"), ("Мужской", "M")])
    def test_sex(self, raw: str, expected: str) -> None:
        assert learner.parse_sex(raw) == expected

    def test_sex_unknown(self) -> None:
        with pytest.raises(ValueError):
            learner.parse_sex("?")

    def test_education_exact_label(self) -> None:
        assert (
            learner.parse_education("Среднее профессиональное образование")
            == "secondary_vocational"
        )

    def test_education_dash_variants_are_equal(self) -> None:
        assert learner.parse_education("Высшее образование - бакалавриат") == "higher_bachelor"
        assert learner.parse_education("Высшее образование – бакалавриат") == "higher_bachelor"

    def test_education_code_is_accepted(self) -> None:
        assert learner.parse_education("none") == "none"

    def test_education_unknown(self) -> None:
        with pytest.raises(ValueError, match="справочника"):
            learner.parse_education("Высшее")


class TestDates:
    @pytest.mark.parametrize(
        "raw", ["13.03.2020", "2020-03-13", "13/03/2020", "2020-03-13T00:00:00"]
    )
    def test_formats(self, raw: str) -> None:
        assert learner.parse_date(raw) == dt.date(2020, 3, 13)

    def test_garbage(self) -> None:
        with pytest.raises(ValueError):
            learner.parse_date("вчера")

    def test_birth_date_in_future(self) -> None:
        future = (dt.date.today() + dt.timedelta(days=5)).isoformat()
        with pytest.raises(ValueError, match="будущем"):
            learner.parse_birth_date(future)

    def test_ancient_date(self) -> None:
        with pytest.raises(ValueError):
            learner.parse_past_date("01.01.1800")


class TestParseProfileValue:
    def test_dispatches_to_specific_parser(self) -> None:
        assert learner.parse_profile_value("snils", "112-233-445 95") == "11223344595"
        assert learner.parse_profile_value("sex", "Ж") == "F"

    def test_plain_text_is_squeezed_and_limited(self) -> None:
        assert learner.parse_profile_value("reg_city", "  Санкт-Петербург ") == "Санкт-Петербург"
        with pytest.raises(ValueError, match="длинное"):
            learner.parse_profile_value("reg_house", "1" * 65)
