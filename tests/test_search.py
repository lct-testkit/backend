"""Q-1 (QA 20.10): общий поиск исправляет раскладку. «ппу», набранное как `gge`, находится, а ответ
несёт `corrected_query`, чтобы интерфейс написал «Показаны результаты для «ппу»».

Перевод раскладки — чистая функция; сквозные проверки идут на настоящей PostgreSQL
(`TEST_DATABASE_URL`, см. `tests/conftest.py`)."""

from __future__ import annotations

import uuid

import pytest

from app.core.layout import switch_layout
from tests.conftest import TEST_DATABASE_URL
from tests.crm_helpers import create_organization, login


class TestSwitchLayout:
    @pytest.mark.parametrize(
        ("typed", "expected"),
        [
            ("gge", "ппу"),
            ("ппу", "gge"),
            ("Ghbdtn", "Привет"),
            ("Привет", "Ghbdtn"),
            ("ghbdtn vbh", "привет мир"),
            ("[kt,", "хлеб"),
            ("vjc;", "мосж"),
            ("Aстра", None),  # латиница и кириллица вперемешку — не переводим
        ],
    )
    def test_converts_between_layouts(self, typed: str, expected: str | None) -> None:
        assert switch_layout(typed) == expected

    @pytest.mark.parametrize("text", ["", "   ", "7707083893", "12-34", "№ 5"])
    def test_nothing_to_convert_is_none(self, text: str) -> None:
        assert switch_layout(text) is None

    def test_digits_and_spaces_stay(self) -> None:
        assert switch_layout("ghbdtn 2026") == "привет 2026"

    def test_round_trip(self) -> None:
        for word in ("Московский", "институт", "Ёлка", "Съезд"):
            typed = switch_layout(word)
            assert typed is not None
            assert switch_layout(typed) == word


needs_db = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _unique_name() -> str:
    # Только кириллица и цифры: латинские буквы в названии сделали бы строку смешанной.
    digits = "".join(str(int(ch, 16) % 10) for ch in uuid.uuid4().hex[:10])
    return f"Ппуфыв {digits}"


@needs_db
class TestSearchEndpoint:
    def test_finds_by_what_was_typed_without_a_correction(self, client) -> None:
        login(client, "ADMIN")
        name = _unique_name()
        org_id = create_organization(client, name)

        response = client.get("/api/search", params={"q": name})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["corrected_query"] is None
        assert [o["id"] for o in body["organizations"]] == [org_id]

    def test_wrong_layout_is_retried_and_marked(self, client) -> None:
        login(client, "ADMIN")
        name = _unique_name()
        org_id = create_organization(client, name)
        typed = switch_layout(name)
        assert typed is not None

        response = client.get("/api/search", params={"q": typed})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["query"] == typed
        assert body["corrected_query"] == name
        assert [o["id"] for o in body["organizations"]] == [org_id]

    def test_cyrillic_typed_instead_of_latin_is_retried_too(self, client) -> None:
        login(client, "ADMIN")
        suffix = "".join(str(int(ch, 16) % 10) for ch in uuid.uuid4().hex[:8])
        latin_name = f"Qgxhd {suffix}"
        org_id = create_organization(client, latin_name)
        typed = switch_layout(latin_name)  # то, что получилось бы в русской раскладке
        assert typed is not None

        body = client.get("/api/search", params={"q": typed}).json()
        assert body["corrected_query"] == latin_name
        assert [o["id"] for o in body["organizations"]] == [org_id]

    def test_a_direct_hit_is_never_replaced_by_the_converted_query(self, client) -> None:
        login(client, "ADMIN")
        suffix = "".join(str(int(ch, 16) % 10) for ch in uuid.uuid4().hex[:8])
        # «Gge …» (латиница) находится как есть, хотя «Ппу …» тоже нашлось бы.
        direct_id = create_organization(client, f"Gge {suffix}")
        create_organization(client, f"Ппу {suffix}")

        body = client.get("/api/search", params={"q": f"Gge {suffix}"}).json()
        assert body["corrected_query"] is None
        assert [o["id"] for o in body["organizations"]] == [direct_id]

    def test_nothing_found_anywhere_has_no_correction(self, client) -> None:
        login(client, "ADMIN")
        body = client.get("/api/search", params={"q": "zzzqxj"}).json()
        assert body == {
            "query": "zzzqxj",
            "corrected_query": None,
            "deals": [],
            "organizations": [],
            "contacts": [],
        }

    def test_limit_and_length_are_validated(self, client) -> None:
        login(client, "ADMIN")
        assert client.get("/api/search", params={"q": "a"}).status_code == 422
        assert client.get("/api/search", params={"q": "x" * 201}).status_code == 422
        assert client.get("/api/search", params={"q": "ab", "limit": 0}).status_code == 422
        assert client.get("/api/search", params={"q": "ab", "limit": 21}).status_code == 422

    def test_each_group_is_limited(self, client) -> None:
        login(client, "ADMIN")
        suffix = "".join(str(int(ch, 16) % 10) for ch in uuid.uuid4().hex[:8])
        for index in range(3):
            create_organization(client, f"Лимит {suffix} {index}")

        body = client.get("/api/search", params={"q": f"Лимит {suffix}", "limit": 2}).json()
        assert len(body["organizations"]) == 2

    def test_results_respect_the_callers_scope(self, client) -> None:
        login(client, "ADMIN")
        name = _unique_name()
        create_organization(client, name)

        login(client, "KAM")
        body = client.get("/api/search", params={"q": name}).json()
        assert body["organizations"] == []
        assert body["corrected_query"] is None

    def test_a_role_without_read_rights_is_forbidden(self, client) -> None:
        login(client, "AUDITOR")
        assert client.get("/api/search", params={"q": "ab"}).status_code == 403

    def test_deals_and_contacts_are_searched_too(self, client) -> None:
        from tests.crm_helpers import create_contact, create_deal, create_published_workflow

        login(client, "ADMIN")
        suffix = "".join(str(int(ch, 16) % 10) for ch in uuid.uuid4().hex[:8])
        surname = f"Ппуфыв{suffix}"
        contact_id = create_contact(client, last_name=surname)
        workflow_id = create_published_workflow(client)["workflow"]["id"]
        deal = create_deal(client, workflow_id, title=f"Сделка Ппуфыв{suffix}")

        typed = switch_layout(f"Ппуфыв{suffix}")
        assert typed is not None
        body = client.get("/api/search", params={"q": typed}).json()
        assert body["corrected_query"] == f"Ппуфыв{suffix}"
        assert [c["id"] for c in body["contacts"]] == [contact_id]
        assert [d["id"] for d in body["deals"]] == [deal["id"]]
        assert body["contacts"][0]["last_name"] == surname
