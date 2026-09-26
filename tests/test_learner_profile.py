"""Профиль учащегося: ПДн шаблона LMS «Загрузка пользователей» (СНИЛС, паспорт, адрес, диплом).

`GET/PUT /api/contacts/{id}/learner-profile` и `POST .../reveal`. Полный профиль не должен попадать
ни в `ContactOut`, ни в аудит, ни в лог: наружу он выходит либо маскированным, либо через `reveal` с
записью аудита (категория данных, без значений). Настоящая Postgres обязательна — см.
`tests/conftest.py`; проверки маскирования ключей — чистые, БД не требуют.
"""

from __future__ import annotations

import json
import uuid

import pytest

from app.core.logging import _redact_secrets
from app.core.masking import (
    LEARNER_PII_KEYS,
    REDACTED,
    mask_mapping,
    mask_tail,
    mask_year,
)
from app.modules.catalog import learner
from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import login
from tests.people_helpers import audit_entries, create_contact

needs_db = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

# Все 25 полей профиля в порядке шаблона плюс метка образования.
PROFILE_FIELDS = [*learner.PROFILE_TARGETS, "education_label"]

FULL_PROFILE = {
    "snils": "112-233-445 95",
    "passport_series": "45 12",
    "passport_number": "123 456",
    "passport_issued_by": "ОУФМС России по г. Москве",
    "passport_issued_at": "13.03.2020",
    "passport_dept_code": "770001",
    "sex": "М",
    "birth_date": "17.05.1990",
    "reg_region": "Москва",
    "reg_city": "Москва",
    "reg_street": "Тверская",
    "reg_house": "1",
    "reg_apartment": "15",
    "reg_zip": "125009",
    "first_name_dative": "Ивану",
    "last_name_dative": "Иванову",
    "middle_name_dative": "Ивановичу",
    "education": "Высшее образование – бакалавриат",
    "diploma_profession": "Программист",
    "diploma_institution": "Московский государственный университет",
    "diploma_surname": "Иванов",
    "diploma_number": "1234567",
    "diploma_series": "АБ",
    "diploma_reg_number": "123456789",
    "diploma_issued_at": "2015-06-30",
}

# Что лежит в БД после разбора значений выше (`catalog.learner.parse_profile_value`).
STORED = {
    "snils": "11223344595",
    "passport_series": "4512",
    "passport_number": "123456",
    "passport_dept_code": "770-001",
    "passport_issued_at": "2020-03-13",
    "birth_date": "1990-05-17",
    "sex": "M",
    "education": "higher_bachelor",
    "diploma_issued_at": "2015-06-30",
}


def _url(contact_id: str, tail: str = "") -> str:
    return f"/api/contacts/{contact_id}/learner-profile{tail}"


def _put(client, contact_id: str, **values):
    return client.put(_url(contact_id), json=values)


class TestMaskingHelpers:
    def test_tail_keeps_three_characters_of_a_long_value(self) -> None:
        assert mask_tail("11223344595") == "***595"
        assert mask_tail("Санкт-Петербург") == "***ург"

    def test_short_values_are_closed_completely(self) -> None:
        # Хвост из трёх знаков у серии паспорта раскрыл бы три цифры из четырёх.
        assert mask_tail("4512") == REDACTED
        assert mask_tail("15") == REDACTED
        assert mask_tail("123456") == "***456"

    def test_empty_stays_empty(self) -> None:
        assert mask_tail(None) is None
        assert mask_tail("") == ""

    def test_year_only(self) -> None:
        import datetime as dt

        assert mask_year(dt.date(1990, 5, 17)) == "1990"
        assert mask_year(None) is None

    def test_audit_and_log_keys_are_fully_redacted(self) -> None:
        assert {"snils", "passport_series", "birth_date", "reg_zip", "diploma_number"} <= set(
            LEARNER_PII_KEYS
        )
        masked = mask_mapping(
            {
                "snils": "11223344595",
                "passport_number": {"old": "123456", "new": "654321"},
                "reg_city": "Москва",
                "diploma_surname": "Иванов",
                "nested": {"birth_date": "1990-05-17", "comment": "оставить как есть"},
            }
        )
        assert masked["snils"] == REDACTED
        assert masked["passport_number"] == REDACTED
        assert masked["reg_city"] == REDACTED
        assert masked["diploma_surname"] == REDACTED
        assert masked["nested"] == {"birth_date": REDACTED, "comment": "оставить как есть"}

    def test_existing_masking_is_unchanged(self) -> None:
        masked = mask_mapping({"phone": "+79991234512", "email": "ivanov@domain.ru", "token": "x"})
        assert masked == {"phone": "+7 (9**) ***-**-12", "email": "i***@domain.ru", "token": "***"}

    def test_log_barrier_redacts_the_same_keys(self) -> None:
        event = {
            "event": "profile",
            "snils": "11223344595",
            "passport_series": "4512",
            "note": "ok",
        }
        cleaned = _redact_secrets(None, "info", dict(event))
        assert cleaned == {
            "event": "profile",
            "snils": REDACTED,
            "passport_series": REDACTED,
            "note": "ok",
        }


@needs_db
class TestReadAndWrite:
    def test_absent_profile_is_a_null_filled_object(self, client) -> None:
        login(client, "KAM")
        contact = create_contact(client)

        response = client.get(_url(contact["id"]))
        assert response.status_code == 200, response.text
        assert response.json() == dict.fromkeys(PROFILE_FIELDS)

    def test_put_saves_and_answers_with_the_masked_view(self, client) -> None:
        login(client, "KAM")
        contact = create_contact(client)

        saved = _put(client, contact["id"], **FULL_PROFILE)
        assert saved.status_code == 200, saved.text
        masked = saved.json()
        assert set(masked) == set(PROFILE_FIELDS)
        assert masked["snils"] == "***595"
        assert masked["passport_series"] == REDACTED  # четыре цифры закрыты целиком
        assert masked["passport_number"] == "***456"
        assert masked["passport_dept_code"] == "***001"
        assert masked["reg_house"] == REDACTED
        assert masked["reg_zip"] == "***009"
        # Даты — только год; пол, образование и падежные формы — как есть.
        assert masked["birth_date"] == "1990"
        assert masked["passport_issued_at"] == "2020"
        assert masked["diploma_issued_at"] == "2015"
        assert masked["sex"] == "M"
        assert masked["education"] == "higher_bachelor"
        assert masked["education_label"] == "Высшее образование – бакалавриат"
        assert masked["first_name_dative"] == "Ивану"
        # Ни одно исходное значение из закрытых полей в ответ не попало.
        body = json.dumps(masked, ensure_ascii=False)
        for name in ("snils", "passport_issued_by", "reg_street", "diploma_number"):
            assert FULL_PROFILE[name] not in body, name
        assert "11223344595" not in body
        assert "1990-05-17" not in body

        assert client.get(_url(contact["id"])).json() == masked

    def test_put_is_partial_and_an_empty_string_clears_a_field(self, client) -> None:
        login(client, "KAM")
        contact = create_contact(client)
        assert (
            _put(client, contact["id"], snils="112-233-445 95", reg_city="Москва").status_code
            == 200
        )

        cleared = _put(client, contact["id"], reg_city="", reg_zip="125009")
        assert cleared.status_code == 200, cleared.text
        assert cleared.json()["reg_city"] is None
        assert cleared.json()["reg_zip"] == "***009"
        assert cleared.json()["snils"] == "***595"  # не переданное поле не тронуто

        nulled = _put(client, contact["id"], snils=None)
        assert nulled.json()["snils"] is None

    def test_clearing_a_missing_profile_creates_nothing(self, client) -> None:
        login(client, "KAM")
        contact = create_contact(client)

        response = _put(client, contact["id"], snils="", reg_city="")
        assert response.status_code == 200, response.text
        assert response.json() == dict.fromkeys(PROFILE_FIELDS)

    def test_validation_lists_every_bad_field_and_saves_nothing(self, client) -> None:
        login(client, "KAM")
        contact = create_contact(client)

        response = _put(
            client,
            contact["id"],
            snils="123",
            passport_series="1",
            birth_date="завтра",
            sex="?",
            education="Высшее",
            reg_city="Москва",
        )
        assert response.status_code == 422, response.text
        errors = {error["field"]: error["reason"] for error in response.json()["errors"]}
        assert set(errors) == {"snils", "passport_series", "birth_date", "sex", "education"}
        assert "11 цифр" in errors["snils"]
        # Сообщения не повторяют введённое значение.
        assert "123" not in errors["snils"]
        assert client.get(_url(contact["id"])).json() == dict.fromkeys(PROFILE_FIELDS)

    def test_unknown_field_and_oversized_value_are_refused(self, client) -> None:
        login(client, "KAM")
        contact = create_contact(client)

        assert _put(client, contact["id"], passport="4512 123456").status_code == 422
        assert _put(client, contact["id"], reg_street="я" * 2000).status_code == 422

    def test_full_values_never_appear_in_the_contact_card(self, client) -> None:
        login(client, "KAM")
        contact = create_contact(client)
        assert _put(client, contact["id"], **FULL_PROFILE).status_code == 200

        card = client.get(f"/api/contacts/{contact['id']}").text
        revealed = client.post(f"/api/contacts/{contact['id']}/reveal").text
        listed = client.get("/api/contacts", params={"q": contact["last_name"]}).text
        for text in (card, revealed, listed):
            assert "11223344595" not in text
            assert "Тверская" not in text
            assert "1990-05-17" not in text


@needs_db
class TestReveal:
    def test_reveal_returns_the_full_profile(self, client) -> None:
        login(client, "KAM")
        contact = create_contact(client)
        assert _put(client, contact["id"], **FULL_PROFILE).status_code == 200

        response = client.post(_url(contact["id"], "/reveal"))
        assert response.status_code == 200, response.text
        full = response.json()
        assert full["snils"] == STORED["snils"]
        assert full["passport_series"] == STORED["passport_series"]
        assert full["passport_dept_code"] == STORED["passport_dept_code"]
        assert full["birth_date"] == STORED["birth_date"]
        assert full["passport_issued_at"] == STORED["passport_issued_at"]
        assert full["diploma_issued_at"] == STORED["diploma_issued_at"]
        assert full["sex"] == STORED["sex"]
        assert full["education"] == STORED["education"]
        assert full["education_label"] == FULL_PROFILE["education"]
        assert full["reg_street"] == "Тверская"
        assert full["passport_issued_by"] == FULL_PROFILE["passport_issued_by"]

    def test_reveal_of_a_missing_profile_is_null_filled(self, client) -> None:
        login(client, "KAM")
        contact = create_contact(client)

        response = client.post(_url(contact["id"], "/reveal"))
        assert response.status_code == 200, response.text
        assert response.json() == dict.fromkeys(PROFILE_FIELDS)

    def test_every_reveal_is_audited_with_the_category_and_no_values(self, client) -> None:
        login(client, "KAM")
        contact = create_contact(client)
        assert _put(client, contact["id"], **FULL_PROFILE).status_code == 200

        for _ in range(2):
            assert client.post(_url(contact["id"], "/reveal")).status_code == 200

        entries = [
            entry
            for entry in audit_entries(client, "PII_REVEALED", contact["id"])
            if entry["changes"] == {"category": {"old": None, "new": "learner_profile"}}
        ]
        assert len(entries) == 2
        assert entries[0]["entity_type"] == "contact"
        assert entries[0]["actor_id"] is not None
        assert "11223344595" not in json.dumps(entries, ensure_ascii=False)


@needs_db
class TestAudit:
    def test_update_lists_field_names_only(self, client) -> None:
        login(client, "KAM")
        contact = create_contact(client)

        assert (
            _put(client, contact["id"], snils="112-233-445 95", reg_city="Москва").status_code
            == 200
        )

        updates = [
            entry
            for entry in audit_entries(client, "CONTACT_UPDATED", contact["id"])
            if "learner_profile" in (entry["changes"] or {})
        ]
        assert len(updates) == 1
        assert updates[0]["changes"] == {
            "learner_profile": {"old": None, "new": ["reg_city", "snils"]}
        }
        dumped = json.dumps(updates, ensure_ascii=False)
        assert "11223344595" not in dumped
        assert "112-233-445" not in dumped
        assert "Москва" not in dumped

    def test_a_repeat_without_changes_writes_nothing(self, client) -> None:
        login(client, "KAM")
        contact = create_contact(client)
        for _ in range(3):
            assert _put(client, contact["id"], snils="112-233-445 95").status_code == 200

        updates = [
            entry
            for entry in audit_entries(client, "CONTACT_UPDATED", contact["id"])
            if "learner_profile" in (entry["changes"] or {})
        ]
        assert len(updates) == 1

    def test_only_the_changed_names_are_listed_on_the_next_update(self, client) -> None:
        login(client, "KAM")
        contact = create_contact(client)
        assert (
            _put(client, contact["id"], snils="112-233-445 95", reg_city="Москва").status_code
            == 200
        )
        assert (
            _put(client, contact["id"], snils="112-233-445 95", reg_city="Казань").status_code
            == 200
        )

        updates = [
            entry["changes"]["learner_profile"]["new"]
            for entry in audit_entries(client, "CONTACT_UPDATED", contact["id"])
            if "learner_profile" in (entry["changes"] or {})
        ]
        assert updates == [["reg_city", "snils"], ["reg_city"]]


@needs_db
class TestAccess:
    def test_other_managers_do_not_see_the_contact_or_its_profile(self, client) -> None:
        login(client, "KAM")
        contact = create_contact(client)
        assert _put(client, contact["id"], snils="112-233-445 95").status_code == 200

        login(client, "KAM")  # другой менеджер: чужой контакт ему не виден
        assert client.get(_url(contact["id"])).status_code == 404
        assert client.post(_url(contact["id"], "/reveal")).status_code == 404
        assert _put(client, contact["id"], snils="").status_code == 404

    def test_auditor_and_integration_have_no_access(self, client) -> None:
        login(client, "KAM")
        contact = create_contact(client)

        for role in ("AUDITOR", "INTEGRATION"):
            login(client, role)
            assert client.get(_url(contact["id"])).status_code == 403, role
            assert client.post(_url(contact["id"], "/reveal")).status_code == 403, role

    def test_unknown_contact_is_a_404(self, client) -> None:
        login(client, "ADMIN")
        assert client.get(_url(str(uuid.uuid4()))).status_code == 404


@needs_db
class TestErasure:
    def test_anonymizing_a_contact_removes_its_learner_profile(self, client) -> None:
        login(client, "ADMIN")
        contact = create_contact(client)
        assert _put(client, contact["id"], **FULL_PROFILE).status_code == 200

        async def _anonymize() -> int:
            from sqlalchemy import func, select

            from app.core.db import session_scope
            from app.modules.catalog.models import Contact, ContactLearnerProfile
            from app.modules.catalog.service import ContactService

            async with session_scope() as session:
                stored = await session.get(Contact, uuid.UUID(contact["id"]))
                assert stored is not None
                await ContactService(session).anonymize(stored)
            async with session_scope() as session:
                return int(
                    await session.scalar(
                        select(func.count())
                        .select_from(ContactLearnerProfile)
                        .where(ContactLearnerProfile.contact_id == uuid.UUID(contact["id"]))
                    )
                    or 0
                )

        assert run(client, _anonymize) == 0
        assert client.get(_url(contact["id"])).json() == dict.fromkeys(PROFILE_FIELDS)
        # Записать профиль обезличенному контакту нельзя.
        assert _put(client, contact["id"], snils="112-233-445 95").status_code == 422
