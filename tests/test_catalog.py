"""Тесты каталога: организации, контакты, справочники (спринт 4, раздел 6).

Как и `tests/test_deals.py`, здесь нет реального Postgres/Redis: покрываются
чистые функции (контрольные суммы ИНН/КПП/ОГРН, маскирование, права),
которые ломаются молча и не требуют сессии БД. Проверка ИНН — самая
рискованная часть: реальные компании (Сбербанк, Яндекс, Ростелеком) взяты
как фикстуры, потому что придуманный вручную номер легко случайно окажется
валидным по контрольной сумме и не поймает регрессию в весах.

Исключение — `TestDeleteDirection`/`TestDeleteLossReason` (П4),
`TestDirectionHierarchy`, `TestProductValidityPeriod`, `TestDirectionsAll`:
настоящая Postgres обязательна, тот же приём, что `tests/test_imports.py::
TestLicenseImportEndToEnd` — см. `tests/conftest.py`.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.permissions import Permission, has_permission
from app.modules.catalog.schemas import ContactOut, OrganizationOut
from app.modules.catalog.validators import (
    validate_inn,
    validate_kpp,
    validate_ogrn,
    validate_ogrnip,
    validate_requisite,
)
from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run


class _Contact:
    """Минимальный дублёр `Contact` ORM-модели — только поля, которые
    читает `ContactOut.from_model`."""

    def __init__(self, **overrides: object) -> None:
        self.id = uuid.uuid4()
        self.organization_id = None
        self.first_name = "Иван"
        self.last_name = "Петров"
        self.middle_name = None
        self.position = "Проректор"
        self.email = "ivanov@university.ru"
        self.phone = "+79991234567"
        self.is_decision_maker = True
        self.is_anonymized = False
        self.source = None
        self.external_ids: dict[str, object] = {}
        self.version = 1
        import datetime as dt

        now = dt.datetime.now(dt.UTC)
        self.created_at = now
        self.updated_at = now
        for key, value in overrides.items():
            setattr(self, key, value)


class _Organization:
    """Минимальный дублёр `Organization` ORM-модели — только поля, которые
    читает `OrganizationOut.from_model`."""

    def __init__(self, **overrides: object) -> None:
        import datetime as dt

        self.id = uuid.uuid4()
        self.name = "ИП Петров Пётр Петрович"
        self.short_name = None
        self.org_type = "individual_entrepreneur"
        self.inn = "500100732259"
        self.kpp = None
        self.ogrn = None
        self.legal_address = "г. Москва, ул. Примерная, д. 1"
        self.actual_address = None
        self.region_id = None
        self.website = None
        self.main_phone = "+79991234567"
        self.main_email = "petrov@example.ru"
        self.students_count = None
        self.external_ids: dict[str, object] = {}
        self.owner_id = None
        self.source = None
        self.custom_fields: dict[str, object] = {}
        self.verified_source = None
        self.verified_at = None
        self.registry_status = None
        self.registry_checked_at = None
        self.requisites_drift = None
        self.manual_overrides: list[str] = []
        self.is_accredited = None
        self.accreditation_until = None
        self.version = 1
        now = dt.datetime.now(dt.UTC)
        self.created_at = now
        self.updated_at = now
        for key, value in overrides.items():
            setattr(self, key, value)


class TestInnChecksum:
    """Реальные ИНН юрлиц (общедоступные регистрационные данные)."""

    def test_sberbank_inn_10_digits_valid(self) -> None:
        assert validate_inn("7707083893").ok

    def test_yandex_inn_10_digits_valid(self) -> None:
        assert validate_inn("7736207543").ok

    def test_rostelecom_inn_10_digits_valid(self) -> None:
        assert validate_inn("7707049388").ok

    def test_known_valid_12_digit_inn(self) -> None:
        assert validate_inn("500100732259").ok

    def test_wrong_checksum_rejected(self) -> None:
        # Последняя цифра настоящего ИНН Ростелекома испорчена намеренно.
        result = validate_inn("7707049380")
        assert not result.ok
        assert result.reason

    def test_non_digit_rejected(self) -> None:
        assert not validate_inn("770704938X").ok

    def test_wrong_length_rejected(self) -> None:
        assert not validate_inn("77070493").ok

    def test_all_same_digit_rejected(self) -> None:
        assert not validate_inn("1111111111").ok

    def test_invalid_region_code_rejected(self) -> None:
        # "00" не является кодом субъекта РФ.
        assert not validate_inn("0007049388").ok

    def test_none_rejected(self) -> None:
        assert not validate_inn(None).ok


class TestKppOgrnChecksum:
    def test_valid_kpp(self) -> None:
        assert validate_kpp("770701001").ok

    def test_kpp_wrong_length(self) -> None:
        assert not validate_kpp("7707010").ok

    def test_valid_ogrn(self) -> None:
        # ОГРН Сбербанка.
        assert validate_ogrn("1027700132195").ok

    def test_ogrn_wrong_checksum(self) -> None:
        assert not validate_ogrn("1027700132196").ok

    def test_ogrnip_wrong_length(self) -> None:
        assert not validate_ogrnip("12345").ok


class TestValidateRequisiteDispatch:
    def test_dispatches_by_kind(self) -> None:
        assert validate_requisite("inn", "7707049388").ok
        assert not validate_requisite("inn", "bad").ok

    def test_unknown_kind_rejected(self) -> None:
        result = validate_requisite("passport", "1234")
        assert not result.ok
        assert result.reason


class TestContactMasking:
    def test_from_model_masks_phone_and_email(self) -> None:
        contact = _Contact()
        out = ContactOut.from_model(contact)
        assert out.phone == "+7 (9**) ***-**-67"
        assert out.email == "i***@university.ru"
        # Незамаскированные поля переносятся как есть.
        assert out.first_name == "Иван"
        assert out.is_decision_maker is True

    def test_from_model_handles_missing_contacts(self) -> None:
        contact = _Contact(phone=None, email=None)
        out = ContactOut.from_model(contact)
        assert out.phone is None
        assert out.email is None


class TestOrganizationMasking:
    """dop.md §11.8: данные ИП — ПДн физлица, маскируются как у контакта."""

    def test_individual_entrepreneur_phone_and_email_masked(self) -> None:
        org = _Organization()
        out = OrganizationOut.from_model(org)
        assert out.main_phone == "+7 (9**) ***-**-67"
        assert out.main_email == "p***@example.ru"
        # Имя не маскируется — тот же принцип, что у ContactOut
        # (first_name/last_name видны, маскируются только каналы связи).
        assert out.name == "ИП Петров Пётр Петрович"

    def test_company_and_university_not_masked(self) -> None:
        # Сведения о юрлице — не ПДн (dop.md §11.8): маскировать нечего.
        for org_type in ("company", "university", "college"):
            org = _Organization(org_type=org_type)
            out = OrganizationOut.from_model(org)
            assert out.main_phone == "+79991234567"
            assert out.main_email == "petrov@example.ru"

    def test_missing_contacts_handled(self) -> None:
        org = _Organization(main_phone=None, main_email=None)
        out = OrganizationOut.from_model(org)
        assert out.main_phone is None
        assert out.main_email is None


class TestCatalogPermissions:
    def test_kam_can_read_and_write_organizations_and_contacts(self) -> None:
        for perm in (
            Permission.ORG_READ,
            Permission.ORG_WRITE,
            Permission.ORG_REVEAL,
            Permission.CONTACT_READ,
            Permission.CONTACT_WRITE,
            Permission.CONTACT_REVEAL,
            Permission.CATALOG_READ,
        ):
            assert has_permission("KAM", perm)

    def test_kam_cannot_write_catalog_reference_data(self) -> None:
        # Продукты/направления/причины отказа/календарь — только ADMIN.
        assert not has_permission("KAM", Permission.CATALOG_WRITE)
        assert has_permission("ADMIN", Permission.CATALOG_WRITE)

    def test_auditor_has_no_organization_or_contact_access(self) -> None:
        for perm in (
            Permission.ORG_READ,
            Permission.CONTACT_READ,
            Permission.CONTACT_REVEAL,
            Permission.CATALOG_READ,
        ):
            assert not has_permission("AUDITOR", perm)

    def test_integration_can_read_organizations_but_not_contacts(self) -> None:
        # dop.md §11.8: организации — не ПДн, контакты — ПДн.
        assert has_permission("INTEGRATION", Permission.ORG_READ)
        assert has_permission("INTEGRATION", Permission.CONTACT_WRITE)
        assert not has_permission("INTEGRATION", Permission.CONTACT_READ)

    def test_only_head_and_admin_delete_files(self) -> None:
        assert not has_permission("KAM", Permission.FILE_DELETE)
        assert has_permission("HEAD", Permission.FILE_DELETE)
        assert has_permission("ADMIN", Permission.FILE_DELETE)


class TestDriftNewValue:
    """`apply_drift` берёт новое значение из расхождения реквизитов.

    Сверка с ЕГРЮЛ пишет `{поле: {"old": …, "new": …}}`. Раньше принятие
    присваивало колонке весь словарь → `DBAPIError` и 500 на
    `POST /organizations/{id}/apply-drift` (найдено при ручной проверке UI)."""

    def test_reads_new_out_of_the_registry_form(self) -> None:
        from app.modules.catalog.drift import drift_new_value

        assert drift_new_value({"old": "г. Казань", "new": "г. Москва"}) == "г. Москва"

    def test_keeps_a_bare_value(self) -> None:
        from app.modules.catalog.drift import drift_new_value

        assert drift_new_value("г. Москва") == "г. Москва"

    def test_a_dict_without_new_is_left_alone(self) -> None:
        from app.modules.catalog.drift import drift_new_value

        assert drift_new_value({"old": "x"}) == {"old": "x"}


def _admin(client) -> None:
    admin = run(client, _make_user, "ADMIN")
    csrf = authenticate(client, admin)
    client.headers["X-CSRF-Token"] = csrf


class TestDeleteDirection:
    """П4: `DELETE /api/directions/{id}` — только без дочерних направлений и
    без продуктов, которые на него ссылаются. Настоящая Postgres
    обязательна — см. докстринг модуля."""

    pytestmark = pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
    )

    def test_deletes_a_leaf_direction(self, client) -> None:
        _admin(client)
        code = f"dir-{uuid.uuid4().hex[:8]}"
        create = client.post(
            "/api/directions", json={"code": code, "name": "Направление на удаление"}
        )
        assert create.status_code == 201, create.text
        direction_id = create.json()["id"]

        delete = client.delete(f"/api/directions/{direction_id}")
        assert delete.status_code == 204, delete.text
        # Список направлений больше не находит удалённое (мягкое удаление).
        listing = client.get("/api/directions", params={"q": code}).json()
        assert all(item["id"] != direction_id for item in listing["items"])

    def test_direction_with_a_child_cannot_be_deleted(self, client) -> None:
        _admin(client)
        parent_code = f"dir-parent-{uuid.uuid4().hex[:8]}"
        parent = client.post(
            "/api/directions", json={"code": parent_code, "name": "Родитель"}
        ).json()
        client.post(
            "/api/directions",
            json={
                "code": f"dir-child-{uuid.uuid4().hex[:8]}",
                "name": "Потомок",
                "parent_id": parent["id"],
            },
        )

        delete = client.delete(f"/api/directions/{parent['id']}")
        assert delete.status_code == 409, delete.text
        assert delete.json()["code"] == "CRM-1303"


class TestDeleteLossReason:
    """П4: `DELETE /api/loss-reasons/{id}` — только если не используется ни
    в одной сделке. Настоящая Postgres обязательна — см. докстринг модуля."""

    pytestmark = pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
    )

    def test_deletes_an_unused_reason(self, client) -> None:
        _admin(client)
        code = f"reason-{uuid.uuid4().hex[:8]}"
        create = client.post(
            "/api/loss-reasons",
            json={"code": code, "name": "Причина на удаление", "category": "other"},
        )
        assert create.status_code == 201, create.text
        reason_id = create.json()["id"]

        delete = client.delete(f"/api/loss-reasons/{reason_id}")
        assert delete.status_code == 204, delete.text

    def test_reason_used_by_a_deal_cannot_be_deleted(self, client) -> None:
        _admin(client)
        code = f"reason-used-{uuid.uuid4().hex[:8]}"
        reason_id = client.post(
            "/api/loss-reasons",
            json={"code": code, "name": "Используемая причина", "category": "other"},
        ).json()["id"]

        async def _attach_to_a_deal() -> uuid.UUID:
            from app.core.db import session_scope
            from app.core.ids import uuid7
            from app.modules.catalog.models import Organization
            from app.modules.crm.models import Deal
            from app.modules.identity.models import User
            from app.modules.workflow.models import Workflow, WorkflowStatus

            async with session_scope() as session:
                workflow = Workflow(
                    code=f"wf-{uuid7().hex[:8]}",
                    name="Тестовая воронка",
                    deal_type="b2b",
                    state="draft",
                )
                session.add(workflow)
                await session.flush()
                wf_status = WorkflowStatus(
                    workflow_id=workflow.id,
                    code="new",
                    name="Новая",
                )
                session.add(wf_status)
                org = Organization(name="Тестовый вуз для П4", org_type="university")
                session.add(org)
                owner = User(
                    keycloak_id=str(uuid.uuid4()),
                    email=f"{uuid.uuid4().hex[:8]}@rt-it-school.ru",
                    full_name="Сидоров С.С.",
                    role="KAM",
                    status="active",
                    consent_version="1.0",
                )
                session.add(owner)
                await session.flush()
                deal = Deal(
                    number=f"D-{uuid.uuid4().hex[:10]}",
                    title="Сделка для П4",
                    deal_type="b2b",
                    workflow_id=workflow.id,
                    status_id=wf_status.id,
                    organization_id=org.id,
                    owner_id=owner.id,
                    loss_reason_id=uuid.UUID(reason_id),
                )
                session.add(deal)
                await session.flush()
                return deal.id

        run(client, _attach_to_a_deal)

        delete = client.delete(f"/api/loss-reasons/{reason_id}")
        assert delete.status_code == 409, delete.text
        assert delete.json()["code"] == "CRM-1303"


class TestDirectionHierarchy:
    """B#10: направление нельзя сделать потомком самого себя или своего потомка (цикл в
    иерархии), а родитель должен существовать. Настоящая Postgres обязательна — см.
    докстринг модуля."""

    pytestmark = pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
    )

    def _direction(self, client, name: str, parent_id: str | None = None) -> dict:
        response = client.post(
            "/api/directions",
            json={"code": f"dir-{uuid.uuid4().hex[:8]}", "name": name, "parent_id": parent_id},
        )
        assert response.status_code == 201, response.text
        return response.json()

    def _reparent(self, client, direction: dict, parent_id: str | None):
        return client.patch(
            f"/api/directions/{direction['id']}",
            json={"parent_id": parent_id},
            headers={"If-Match": str(direction["version"])},
        )

    def _chain(self, client) -> tuple[dict, dict, dict]:
        _admin(client)
        root = self._direction(client, "Корень")
        child = self._direction(client, "Ребёнок", root["id"])
        grandchild = self._direction(client, "Внук", child["id"])
        return root, child, grandchild

    def test_direction_cannot_be_its_own_parent(self, client) -> None:
        root, _child, _grandchild = self._chain(client)

        response = self._reparent(client, root, root["id"])
        assert response.status_code == 422, response.text
        assert response.json()["errors"][0]["field"] == "parent_id"

    def test_direction_cannot_move_under_its_descendant(self, client) -> None:
        root, _child, grandchild = self._chain(client)

        response = self._reparent(client, root, grandchild["id"])
        assert response.status_code == 422, response.text
        assert response.json()["errors"][0]["field"] == "parent_id"

    def test_direction_can_move_to_another_branch_or_to_the_root(self, client) -> None:
        root, _child, grandchild = self._chain(client)

        moved = self._reparent(client, grandchild, root["id"])
        assert moved.status_code == 200, moved.text
        assert moved.json()["parent_id"] == root["id"]

        detached = self._reparent(client, moved.json(), None)
        assert detached.status_code == 200, detached.text
        assert detached.json()["parent_id"] is None

    def test_parent_must_exist(self, client) -> None:
        root, _child, _grandchild = self._chain(client)

        assert self._reparent(client, root, str(uuid.uuid4())).status_code == 404
        created = client.post(
            "/api/directions",
            json={
                "code": f"dir-{uuid.uuid4().hex[:8]}",
                "name": "Сирота",
                "parent_id": str(uuid.uuid4()),
            },
        )
        assert created.status_code == 404, created.text


class TestProductValidityPeriod:
    """B#11: `valid_from` не позже `valid_to`. Настоящая Postgres обязательна — см.
    докстринг модуля."""

    pytestmark = pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
    )

    def _create(self, client, **period):
        return client.post(
            "/api/products",
            json={"code": f"prod-{uuid.uuid4().hex[:8]}", "name": "Курс", **period},
        )

    def test_inverted_period_is_refused_on_create(self, client) -> None:
        _admin(client)

        response = self._create(client, valid_from="2026-09-01", valid_to="2026-01-01")
        assert response.status_code == 422, response.text
        assert response.json()["errors"][0]["field"] == "valid_to"

    def test_one_day_period_and_open_ends_are_fine(self, client) -> None:
        _admin(client)

        for period in (
            {"valid_from": "2026-09-01", "valid_to": "2026-09-01"},
            {"valid_from": "2026-09-01"},
            {"valid_to": "2026-09-01"},
            {},
        ):
            assert self._create(client, **period).status_code == 201, period

    def test_update_checks_the_new_date_against_the_stored_one(self, client) -> None:
        _admin(client)
        product = self._create(client, valid_from="2026-09-01").json()

        response = client.patch(
            f"/api/products/{product['id']}",
            json={"valid_to": "2026-01-01"},
            headers={"If-Match": str(product["version"])},
        )
        assert response.status_code == 422, response.text
        assert response.json()["errors"][0]["field"] == "valid_to"

        ok = client.patch(
            f"/api/products/{product['id']}",
            json={"valid_to": "2026-12-31"},
            headers={"If-Match": str(product["version"])},
        )
        assert ok.status_code == 200, ok.text


class TestDirectionsAll:
    """B#12: `GET /directions?all=true` — весь справочник одним ответом для дерева, без
    `limit`/`cursor`. Настоящая Postgres обязательна — см. докстринг модуля."""

    pytestmark = pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
    )

    def test_all_ignores_the_page_size(self, client) -> None:
        _admin(client)
        created = {
            client.post(
                "/api/directions",
                json={"code": f"dir-{uuid.uuid4().hex[:8]}", "name": f"Направление {i}"},
            ).json()["id"]
            for i in range(3)
        }

        paged = client.get("/api/directions", params={"limit": 1}).json()
        assert len(paged["items"]) == 1
        assert paged["next_cursor"] is not None

        everything = client.get("/api/directions", params={"all": "true", "limit": 1}).json()
        assert created <= {item["id"] for item in everything["items"]}
        assert everything["next_cursor"] is None

    def test_all_keeps_the_filters(self, client) -> None:
        _admin(client)
        marker = uuid.uuid4().hex[:8]
        wanted = client.post(
            "/api/directions", json={"code": f"dir-{marker}", "name": f"Нужное {marker}"}
        ).json()["id"]
        client.post(
            "/api/directions",
            json={"code": f"dir-{uuid.uuid4().hex[:8]}", "name": "Постороннее направление"},
        )

        found = client.get("/api/directions", params={"all": "true", "q": marker}).json()
        assert [item["id"] for item in found["items"]] == [wanted]
