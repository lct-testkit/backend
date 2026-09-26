"""Дубли контактов и нормализация email/телефона через API (`POST/PATCH /api/contacts`).

Раньше `POST /api/contacts` дублей не искал: те же пять человек, что уже пришли с сайта, заводились
вторыми экземплярами, «Ivanov@Mail.ru» и «ivanov@mail.ru» были разными людьми, а телефон
сравнивался строкой. Здесь проверяется правило целиком: совпал email (без регистра) — тот же
человек; телефон в любом написании совпадает только при той же фамилии (общий номер кафедры или
семьи — не дубль). Настоящая Postgres обязательна — см. `tests/conftest.py`.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _sign_in(client, role: str = "KAM"):
    user = run(client, _make_user, role)
    client.headers["X-CSRF-Token"] = authenticate(client, user)
    return user


def _email(prefix: str = "person") -> str:
    """Уникальный на каждый вызов адрес в смешанном регистре: БД переживает прогоны тестов."""
    return f"{prefix}.{uuid.uuid4().hex[:10]}@Example.RU"


def _phone_tail() -> str:
    """Семь цифр, уникальных на каждый вызов: `+7 999` + хвост."""
    return f"{int(uuid.uuid4().hex[:8], 16) % 10_000_000:07d}"


def _surname() -> str:
    return f"Тестов{uuid.uuid4().hex[:8]}"


def _create(client, **fields):
    return client.post(
        "/api/contacts", json={"first_name": "Иван", "last_name": _surname(), **fields}
    )


def _reveal(client, contact_id: str) -> dict:
    response = client.post(f"/api/contacts/{contact_id}/reveal")
    assert response.status_code == 200, response.text
    return response.json()


def _patch(client, contact: dict, **fields):
    return client.patch(
        f"/api/contacts/{contact['id']}", json=fields, headers={"If-Match": str(contact["version"])}
    )


class TestCreateDuplicates:
    def test_same_email_in_another_case_is_a_duplicate(self, client) -> None:
        _sign_in(client)
        email = _email()
        first = _create(client, email=email.lower())
        assert first.status_code == 201, first.text

        # Фамилия другая: по email совпадение решает само по себе.
        second = _create(client, email=email.upper())
        assert second.status_code == 409, second.text
        body = second.json()
        assert body["code"] == "CRM-1301"
        assert second.headers["content-type"].startswith("application/problem+json")
        assert body["candidates"] == [
            {"id": first.json()["id"], "match": "email", "accessible": True}
        ]

    def test_email_and_phone_are_stored_in_canonical_form(self, client) -> None:
        _sign_in(client)
        tail = _phone_tail()
        email = _email("Ivan.Petrov")

        created = _create(
            client, email=f"  {email}  ", phone=f"8 (999) {tail[:3]}-{tail[3:5]}-{tail[5:]}"
        )
        assert created.status_code == 201, created.text

        full = _reveal(client, created.json()["id"])
        assert full["email"] == email.lower()
        assert full["phone"] == f"+7999{tail}"

    def test_same_phone_written_three_ways_with_the_same_surname_is_a_duplicate(
        self, client
    ) -> None:
        _sign_in(client)
        tail = _phone_tail()
        surname = _surname()
        first = client.post(
            "/api/contacts",
            json={
                "first_name": "Иван",
                "last_name": surname,
                "phone": f"+7 (999) {tail[:3]}-{tail[3:5]}-{tail[5:]}",
            },
        )
        assert first.status_code == 201, first.text

        for written in (f"8999{tail}", f"7 999 {tail[:3]} {tail[3:5]} {tail[5:]}", f"+7999{tail}"):
            again = client.post(
                "/api/contacts",
                json={"first_name": "Иван", "last_name": surname, "phone": written},
            )
            assert again.status_code == 409, (written, again.text)
            assert again.json()["code"] == "CRM-1301"
            assert again.json()["candidates"][0]["match"] == "phone"

    def test_surname_is_compared_without_case_and_yo(self, client) -> None:
        _sign_in(client)
        tail = _phone_tail()
        suffix = uuid.uuid4().hex[:6]
        first = client.post(
            "/api/contacts",
            json={"first_name": "Пётр", "last_name": f"Королёв{suffix}", "phone": f"+7999{tail}"},
        )
        assert first.status_code == 201, first.text

        again = client.post(
            "/api/contacts",
            json={"first_name": "Пётр", "last_name": f"КОРОЛЕВ{suffix}", "phone": f"+7999{tail}"},
        )
        assert again.status_code == 409, again.text

    def test_same_phone_with_another_surname_is_a_shared_office_phone(self, client) -> None:
        _sign_in(client)
        tail = _phone_tail()
        phone = f"+7999{tail}"
        first = _create(client, phone=phone)
        assert first.status_code == 201, first.text

        # Общий номер кафедры или семьи: другой человек, а не дубль.
        second = _create(client, phone=phone)
        assert second.status_code == 201, second.text
        assert second.json()["id"] != first.json()["id"]

    def test_a_contact_without_email_and_phone_is_not_matched_to_anyone(self, client) -> None:
        _sign_in(client)
        surname = _surname()
        for _ in range(2):
            response = client.post(
                "/api/contacts", json={"first_name": "Иван", "last_name": surname}
            )
            assert response.status_code == 201, response.text

    def test_invalid_email_is_a_422_with_the_field(self, client) -> None:
        _sign_in(client)
        response = _create(client, email="это-не-адрес")
        assert response.status_code == 422, response.text
        errors = response.json()["errors"]
        assert [error["field"] for error in errors] == ["email"]
        assert "Value error" not in errors[0]["reason"]

    def test_invalid_phone_is_a_422_with_the_field(self, client) -> None:
        _sign_in(client)
        response = _create(client, phone="12-34")
        assert response.status_code == 422, response.text
        assert [error["field"] for error in response.json()["errors"]] == ["phone"]

    def test_empty_strings_mean_no_value(self, client) -> None:
        _sign_in(client)
        created = _create(client, email="", phone="   ")
        assert created.status_code == 201, created.text

        full = _reveal(client, created.json()["id"])
        assert full["email"] is None
        assert full["phone"] is None

    def test_contact_methods_are_stored_without_repeats(self, client) -> None:
        _sign_in(client)
        created = _create(client, email=_email(), contact_methods=["email", "telegram", "email"])
        assert created.status_code == 201, created.text
        assert created.json()["contact_methods"] == ["email", "telegram"]
        assert _reveal(client, created.json()["id"])["contact_methods"] == ["email", "telegram"]

    def test_unknown_contact_method_is_a_422(self, client) -> None:
        _sign_in(client)
        response = _create(client, contact_methods=["sms"])
        assert response.status_code == 422, response.text
        assert response.json()["errors"][0]["field"].startswith("contact_methods")

    def test_repeat_with_the_same_idempotency_key_is_replayed_not_a_duplicate(self, client) -> None:
        _sign_in(client)
        key = uuid.uuid4().hex
        body = {"first_name": "Иван", "last_name": _surname(), "email": _email()}

        first = client.post("/api/contacts", json=body, headers={"Idempotency-Key": key})
        assert first.status_code == 201, first.text
        again = client.post("/api/contacts", json=body, headers={"Idempotency-Key": key})
        assert again.status_code == 201, again.text
        assert again.json()["id"] == first.json()["id"]


class TestUpdateDuplicates:
    def test_patch_to_an_email_of_another_contact_is_a_duplicate(self, client) -> None:
        _sign_in(client)
        taken = _email()
        assert _create(client, email=taken).status_code == 201
        mine = _create(client, email=_email()).json()

        response = _patch(client, mine, email=taken.upper())
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "CRM-1301"
        # Отказ ничего не изменил.
        assert client.get(f"/api/contacts/{mine['id']}").json()["version"] == mine["version"]

    def test_patch_own_email_in_another_case_is_not_a_conflict(self, client) -> None:
        _sign_in(client)
        email = _email()
        mine = _create(client, email=email).json()

        response = _patch(client, mine, email=email.upper())
        assert response.status_code == 200, response.text
        assert _reveal(client, mine["id"])["email"] == email.lower()

    def test_patch_normalizes_the_phone_and_clears_the_email_with_an_empty_string(
        self, client
    ) -> None:
        _sign_in(client)
        tail = _phone_tail()
        mine = _create(client, email=_email()).json()

        updated = _patch(client, mine, phone=f"8 999 {tail}", email="")
        assert updated.status_code == 200, updated.text
        full = _reveal(client, mine["id"])
        assert full["phone"] == f"+7999{tail}"
        assert full["email"] is None

    def test_patch_with_an_invalid_email_is_a_422(self, client) -> None:
        _sign_in(client)
        mine = _create(client, email=_email()).json()

        response = _patch(client, mine, email="нет-собаки.ru")
        assert response.status_code == 422, response.text
        assert response.json()["errors"][0]["field"] == "email"

    def test_null_in_a_required_field_is_a_422_not_a_500(self, client) -> None:
        _sign_in(client)
        mine = _create(client).json()

        response = _patch(client, mine, first_name=None)
        assert response.status_code == 422, response.text
        assert response.json()["errors"][0]["field"] == "first_name"

    def test_patch_without_contact_methods_leaves_them_alone(self, client) -> None:
        _sign_in(client)
        mine = _create(client, contact_methods=["email", "telegram"]).json()

        updated = _patch(client, mine, position="Директор")
        assert updated.status_code == 200, updated.text
        assert updated.json()["position"] == "Директор"
        assert updated.json()["contact_methods"] == ["email", "telegram"]

    def test_contact_methods_are_replaced_and_null_is_refused(self, client) -> None:
        _sign_in(client)
        mine = _create(client, contact_methods=["email"]).json()

        updated = _patch(client, mine, contact_methods=["phone", "whatsapp"])
        assert updated.status_code == 200, updated.text
        assert updated.json()["contact_methods"] == ["phone", "whatsapp"]

        refused = _patch(client, updated.json(), contact_methods=None)
        assert refused.status_code == 422, refused.text


class TestVisibilityOfAFreshContact:
    def test_kam_reads_the_contact_he_just_created(self, client) -> None:
        """Раньше `POST` отвечал 201, а следующий `GET` — 404: скоуп контактов строится через
        организации и сделки, а у только что заведённого контакта их ещё нет."""
        _sign_in(client)
        created = _create(client)
        assert created.status_code == 201, created.text
        contact_id = created.json()["id"]

        assert client.get(f"/api/contacts/{contact_id}").status_code == 200
        listed = client.get("/api/contacts", params={"q": created.json()["last_name"]})
        assert [item["id"] for item in listed.json()["items"]] == [contact_id]

    def test_another_kam_does_not_see_it(self, client) -> None:
        _sign_in(client)
        contact_id = _create(client).json()["id"]

        _sign_in(client)  # другой менеджер
        assert client.get(f"/api/contacts/{contact_id}").status_code == 404

    def test_duplicate_of_a_contact_out_of_scope_hides_the_id(self, client) -> None:
        _sign_in(client)
        email = _email()
        assert _create(client, email=email).status_code == 201

        _sign_in(client)  # другой менеджер: чужой контакт ему не виден
        response = _create(client, email=email)
        assert response.status_code == 409, response.text
        assert response.json()["candidates"] == [
            {"id": None, "match": "email", "accessible": False}
        ]


class TestFindOrCreate:
    """`ContactService.find_or_create` — общий путь вебхука сайта и импорта файлов: человека
    находят по правилу дублей, а существующему только ДОЗАПОЛНЯЮТ пустое."""

    def test_fills_only_empty_fields_and_never_overwrites(self, client) -> None:
        email = _email()
        tail = _phone_tail()
        surname = _surname()

        async def scenario() -> dict:
            from app.core.db import session_scope
            from app.modules.catalog.models import Contact, Organization
            from app.modules.catalog.service import ContactService

            async with session_scope() as session:
                first_org = Organization(name=f"Вуз {uuid.uuid4().hex[:8]}", org_type="university")
                other_org = Organization(name=f"Вуз {uuid.uuid4().hex[:8]}", org_type="university")
                session.add_all([first_org, other_org])
                await session.flush()
                service = ContactService(session)

                created = await service.find_or_create(
                    first_name="Иван", last_name=surname, email=email, source="import"
                )
                filled = await service.find_or_create(
                    first_name="Другой",
                    last_name=surname,
                    middle_name="Иванович",
                    email=email.upper(),
                    phone=f"8 999 {tail}",
                    position="Директор",
                    organization_id=first_org.id,
                    contact_methods=["email"],
                    source="import",
                )
                untouched = await service.find_or_create(
                    first_name="Третий",
                    last_name=surname,
                    middle_name="Сергеевич",
                    email=email,
                    phone="+7 999 000 00 00",
                    position="Заместитель",
                    organization_id=other_org.id,
                    contact_methods=["email", "telegram"],
                    source="import",
                )
                contact_id = created.contact.id

            async with session_scope() as session:
                stored = await session.get(Contact, contact_id)
                assert stored is not None
                return {
                    "created": (created.created, filled.created, untouched.created),
                    "same": {created.contact.id, filled.contact.id, untouched.contact.id}
                    == {contact_id},
                    "filled_changed": filled.changed,
                    "untouched_changed": untouched.changed,
                    "untouched_notes": untouched.notes,
                    "stored": {
                        "first_name": stored.first_name,
                        "middle_name": stored.middle_name,
                        "email": stored.email,
                        "phone": stored.phone,
                        "position": stored.position,
                        "organization_id": stored.organization_id,
                        "contact_methods": stored.contact_methods,
                    },
                    "first_org": first_org.id,
                }

        result = run(client, scenario)

        assert result["created"] == (True, False, False)
        assert result["same"] is True
        # Второй вызов дозаполнил пустое и не тронул заполненное (имя, email).
        assert set(result["filled_changed"]) == {
            "middle_name",
            "phone",
            "position",
            "organization_id",
            "contact_methods",
        }
        assert result["filled_changed"]["middle_name"] is None
        # Третий принёс другие значения для всего: добавилось только новое из «способов связи».
        assert result["untouched_changed"] == {"contact_methods": ["email"]}
        assert any("другой организации" in note for note in result["untouched_notes"])
        assert result["stored"] == {
            "first_name": "Иван",
            "middle_name": "Иванович",
            "email": email.lower(),
            "phone": f"+7999{tail}",
            "position": "Директор",
            "organization_id": result["first_org"],
            "contact_methods": ["email", "telegram"],
        }


class TestPlaceholderSurname:
    """Лид «только телефон» заводит контакт с фамилией-заглушкой «—». Она не фамилия: такой
    контакт и следующий с тем же телефоном — один человек, с какой стороны ни стоит заглушка."""

    def test_a_named_contact_matches_the_placeholder_one_by_phone(self, client) -> None:
        _sign_in(client)
        phone = f"+7999{_phone_tail()}"
        first = client.post(
            "/api/contacts", json={"first_name": "Без имени", "last_name": "—", "phone": phone}
        )
        assert first.status_code == 201, first.text

        second = _create(client, phone=phone)

        assert second.status_code == 409, second.text
        candidates = second.json()["candidates"]
        assert [c["id"] for c in candidates] == [first.json()["id"]]
        assert candidates[0]["match"] == "phone"

    def test_a_placeholder_request_matches_a_named_contact_by_phone(self, client) -> None:
        _sign_in(client)
        phone = f"+7999{_phone_tail()}"
        first = _create(client, phone=phone)
        assert first.status_code == 201, first.text

        second = client.post(
            "/api/contacts", json={"first_name": "Без имени", "last_name": "—", "phone": phone}
        )

        assert second.status_code == 409, second.text

    @pytest.mark.parametrize("dash", ["—", "–", "-"])
    def test_two_placeholders_with_one_phone_are_one_person(self, client, dash) -> None:
        _sign_in(client)
        phone = f"+7999{_phone_tail()}"
        payload = {"first_name": "Без имени", "last_name": dash, "phone": phone}
        assert client.post("/api/contacts", json=payload).status_code == 201

        assert client.post("/api/contacts", json=payload).status_code == 409

    def test_a_placeholder_does_not_match_anyone_by_a_different_phone(self, client) -> None:
        _sign_in(client)
        first = _create(client, phone=f"+7999{_phone_tail()}")
        assert first.status_code == 201, first.text

        second = client.post(
            "/api/contacts",
            json={"first_name": "Без имени", "last_name": "—", "phone": f"+7999{_phone_tail()}"},
        )

        assert second.status_code == 201, second.text

    def test_two_real_surnames_on_one_phone_are_still_two_people(self, client) -> None:
        _sign_in(client)
        phone = f"+7999{_phone_tail()}"
        assert _create(client, phone=phone).status_code == 201

        assert _create(client, phone=phone).status_code == 201

    def test_the_lead_path_finds_the_placeholder_contact(self, client) -> None:
        phone = f"+7999{_phone_tail()}"

        async def scenario() -> tuple[bool, bool, bool, uuid.UUID, uuid.UUID]:
            from app.core.db import session_scope
            from app.modules.catalog.service import ContactService

            async with session_scope() as session:
                service = ContactService(session)
                nameless = await service.find_or_create(
                    first_name="Без имени", last_name="—", phone=phone, source="cms"
                )
                named = await service.find_or_create(
                    first_name="Пётр", last_name=_surname(), phone=phone, source="cms"
                )
                again = await service.find_or_create(
                    first_name="Без имени", last_name="—", phone=phone, source="cms"
                )
                return (
                    nameless.created,
                    named.created,
                    again.created,
                    nameless.contact.id,
                    named.contact.id,
                )

        created_first, created_named, created_again, first_id, named_id = run(client, scenario)

        assert (created_first, created_named, created_again) == (True, False, False)
        assert first_id == named_id
