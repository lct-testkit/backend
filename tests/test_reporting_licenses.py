"""B1-11: отчёт «Реестр лицензий» (ТЗ §4 п.1). Настоящая Postgres обязательна
(`tests/conftest.py`)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.core.security import Principal
from app.modules.reporting.builders import (
    LICENSE_REGISTER_COLUMNS,
    build_license_register,
    estimate_license_register,
)
from app.modules.reporting.spec import REPORT_SPECS, validate_params
from tests.conftest import TEST_DATABASE_URL, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


async def _make_scene() -> dict:
    """Три лицензии у O1 (КАМ1 — владелец, одна мягко удалена), одна у O2 (там есть сделка
    команды HEAD, но владелец — КАМ2) — тот же состав, что описывает тест-план задачи."""
    from app.core.db import session_scope
    from app.modules.catalog.license_keys import LicenseKey
    from app.modules.catalog.models import Organization, OrganizationLicense
    from app.modules.crm.models import Deal
    from app.modules.identity.models import Team, User
    from app.modules.workflow.models import Workflow, WorkflowStatus

    token = uuid.uuid4().hex[:10]
    async with session_scope() as session:
        head = User(
            keycloak_id=str(uuid.uuid4()),
            email=f"head-lic-{token}@rt-it-school.ru",
            full_name="Руководителев Р. Р.",
            role="HEAD",
            status="active",
            consent_version="1.0",
        )
        session.add(head)
        await session.flush()
        team = Team(name=f"Команда лицензий {token}", head_id=head.id)
        session.add(team)
        await session.flush()

        kam1 = User(
            keycloak_id=str(uuid.uuid4()),
            email=f"kam1-lic-{token}@rt-it-school.ru",
            full_name="Камов К. К.",
            role="KAM",
            status="active",
            consent_version="1.0",
            team_id=team.id,
        )
        kam2 = User(
            keycloak_id=str(uuid.uuid4()),
            email=f"kam2-lic-{token}@rt-it-school.ru",
            full_name="Камова К. К.",
            role="KAM",
            status="active",
            consent_version="1.0",
            team_id=team.id,
        )
        session.add_all([kam1, kam2])
        await session.flush()

        org1 = Organization(name=f"ВУЗ 1 лицензий {token}", org_type="university", owner_id=kam1.id)
        org2 = Organization(name=f"ВУЗ 2 лицензий {token}", org_type="university", owner_id=kam2.id)
        session.add_all([org1, org2])
        await session.flush()

        # `organization_scope_clause` (B3, `catalog.service`) видит организацию, если КТО-ТО из
        # скоупа принципала — её владелец, либо у неё есть сделка в скоупе сделок принципала.
        # HEAD не владеет ни одной организацией — ему нужна сделка от каждого КАМа своей
        # команды, иначе он не увидел бы O1/O2 вовсе (не через `owner_id`, не через сделку).
        workflow = Workflow(code=f"wf-lic-{token}", name="Воронка лицензий", deal_type="b2b")
        session.add(workflow)
        await session.flush()
        status = WorkflowStatus(
            workflow_id=workflow.id, code="new", name="Новая", type="initial", sort_order=0
        )
        session.add(status)
        await session.flush()
        deal1 = Deal(
            number=f"D-lic-{token}-1",
            title="Сделка КАМ1",
            deal_type="b2b",
            workflow_id=workflow.id,
            status_id=status.id,
            organization_id=org1.id,
            owner_id=kam1.id,
        )
        deal2 = Deal(
            number=f"D-lic-{token}-2",
            title="Сделка КАМ2",
            deal_type="b2b",
            workflow_id=workflow.id,
            status_id=status.id,
            organization_id=org2.id,
            owner_id=kam2.id,
        )
        session.add_all([deal1, deal2])
        await session.flush()

        now = dt.date.today()
        licenses_data = [
            (org1, "Яндекс", "Yandex DataSphere", "transferred", now.year, True),
            (org1, "Контур", "Диадок", "in_progress", now.year + 2, True),
            (org1, "1С", "1С:Предприятие", "not_started", now.year - 1, None),
            (org2, "Microsoft", "Office 365", "declined", now.year, False),
        ]
        license_rows = []
        for organization, vendor, product_name, status_code, valid_year, is_signed in licenses_data:
            key = LicenseKey.build(
                organization.id,
                vendor=vendor,
                product_name=product_name,
                contract_number=None,
            )
            license_ = OrganizationLicense(
                organization_id=organization.id,
                vendor=vendor,
                vendor_key=key.vendor_key,
                product_name=product_name,
                product_key=key.product_key,
                contract_key=key.contract_key,
                transfer_status=status_code,
                license_valid_year=valid_year,
                is_signed=is_signed,
                license_signed_at=dt.date(now.year, 1, 15),
                manager_full_name="Иванов Иван Иванович",
                responsible_contacts="Петров Пётр Петрович, +7 999 123-45-67",
                comment="",
            )
            session.add(license_)
            license_rows.append(license_)
        await session.flush()

        # Мягко удалённая лицензия O1 — не должна попасть ни в один отчёт.
        deleted_key = LicenseKey.build(
            org1.id, vendor="Удалённый", product_name="Удалённое ПО", contract_number=None
        )
        deleted_license = OrganizationLicense(
            organization_id=org1.id,
            vendor="Удалённый",
            vendor_key=deleted_key.vendor_key,
            product_name="Удалённое ПО",
            product_key=deleted_key.product_key,
            contract_key=deleted_key.contract_key,
            transfer_status="transferred",
            license_valid_year=now.year,
        )
        session.add(deleted_license)
        await session.flush()
        deleted_license.deleted_at = dt.datetime.now(dt.UTC)

        return {
            "head_id": head.id,
            "kam1_id": kam1.id,
            "kam2_id": kam2.id,
            "team_id": team.id,
            "org1_id": org1.id,
            "org2_id": org2.id,
        }


def _principal(user_id: uuid.UUID, role: str, *, team_id: uuid.UUID | None = None) -> Principal:
    return Principal(
        user_id=user_id,
        keycloak_id="k",
        role=role,
        status="active",
        email=None,
        full_name="x",
        team_id=team_id,
        manager_id=None,
        perm_epoch=1,
        session_id=None,
        consent_version="1.0",
        must_change_password=False,
        claims=None,  # type: ignore[arg-type]
    )


async def _dataset(scene: dict, user_key: str, params: dict):
    from app.core.db import session_scope

    role = "HEAD" if user_key == "head" else "KAM"
    team_id = scene["team_id"] if user_key == "head" else None
    principal = _principal(scene[f"{user_key}_id"], role, team_id=team_id)
    async with session_scope() as session:
        return await build_license_register(session, principal, params)


class TestLicenseRegisterScope:
    def test_kam_owner_of_o1_sees_three_licenses(self, client) -> None:
        scene = run(client, _make_scene)
        dataset = run(client, _dataset, scene, "kam1", {})
        assert len(dataset.rows) == 3

    def test_head_of_the_team_sees_all_four(self, client) -> None:
        scene = run(client, _make_scene)
        dataset = run(client, _dataset, scene, "head", {})
        assert len(dataset.rows) == 4

    def test_kam2_sees_only_their_own_organizations_license(self, client) -> None:
        """KAM2 владеет O2 (одна лицензия) — O1 ему не виден ни через владение, ни через
        сделки (сделка O1 принадлежит KAM1, не КАМ2)."""
        scene = run(client, _make_scene)
        dataset = run(client, _dataset, scene, "kam2", {})
        assert len(dataset.rows) == 1

    def test_soft_deleted_license_never_shows_up(self, client) -> None:
        scene = run(client, _make_scene)
        dataset = run(client, _dataset, scene, "head", {})
        vendors = {row[1] for row in dataset.rows}
        assert "Удалённый" not in vendors


class TestLicenseRegisterFilters:
    def test_transfer_statuses_filter(self, client) -> None:
        scene = run(client, _make_scene)
        dataset = run(client, _dataset, scene, "head", {"transfer_statuses": ["transferred"]})
        assert len(dataset.rows) == 1
        assert dataset.rows[0][1] == "Яндекс"

    def test_valid_year_filter(self, client) -> None:
        scene = run(client, _make_scene)
        this_year = dt.date.today().year
        dataset = run(client, _dataset, scene, "head", {"valid_year": this_year + 2})
        assert len(dataset.rows) == 1
        assert dataset.rows[0][1] == "Контур"

    def test_expired_filter(self, client) -> None:
        scene = run(client, _make_scene)
        dataset = run(client, _dataset, scene, "head", {"expired": True})
        assert len(dataset.rows) == 1
        assert dataset.rows[0][1] == "1С"

    def test_vendors_filter(self, client) -> None:
        scene = run(client, _make_scene)
        dataset = run(client, _dataset, scene, "head", {"vendors": ["Microsoft"]})
        assert len(dataset.rows) == 1

    def test_invalid_transfer_status_is_rejected(self, client) -> None:
        from app.core.errors import ValidationError

        scene = run(client, _make_scene)
        with pytest.raises(ValidationError):
            run(client, _dataset, scene, "head", {"transfer_statuses": ["bogus"]})

    def test_estimate_matches_row_count(self, client) -> None:
        scene = run(client, _make_scene)

        async def _estimate() -> int:
            from app.core.db import session_scope

            async with session_scope() as session:
                return await estimate_license_register(
                    session, _principal(scene["head_id"], "HEAD", team_id=scene["team_id"]), {}
                )

        assert run(client, _estimate) == 4

    def test_limit_below_the_total_marks_truncated(self, client) -> None:
        scene = run(client, _make_scene)
        dataset = run(client, _dataset, scene, "head", {"limit": 2})
        assert len(dataset.rows) == 2
        assert dataset.truncated is True
        assert dataset.total_rows == 4


class TestLicenseRegisterMasking:
    def test_manager_name_is_masked_without_contact_reveal(self, client, monkeypatch) -> None:
        import app.modules.reporting.builders as builders_module

        monkeypatch.setattr(builders_module, "has_permission", lambda role, perm: False)
        scene = run(client, _make_scene)
        dataset = run(client, _dataset, scene, "head", {"vendors": ["Яндекс"]})
        manager_index = dataset.keys.index("manager_full_name")
        assert dataset.rows[0][manager_index] == "Иванов И. И."

    def test_manager_name_is_shown_in_full_with_contact_reveal(self, client) -> None:
        scene = run(client, _make_scene)
        dataset = run(client, _dataset, scene, "head", {"vendors": ["Яндекс"]})
        manager_index = dataset.keys.index("manager_full_name")
        assert dataset.rows[0][manager_index] == "Иванов Иван Иванович"


class TestLicenseRegisterColumns:
    def test_default_column_order_matches_the_tz_table(self, client) -> None:
        scene = run(client, _make_scene)
        dataset = run(client, _dataset, scene, "head", {})
        assert dataset.columns == [
            "Вуз",
            "Вендор",
            "ПО",
            "Номер договора",
            "Подписание лицензии",
            "Срок действия, год",
            "Статус по передаче",
            "ФИО менеджера",
            "Ответственные от вуза",
            "Комментарий",
        ]

    def test_all_ten_columns_are_the_default(self) -> None:
        defaults = [c.key for c in LICENSE_REGISTER_COLUMNS if c.default]
        assert len(defaults) == 10
        assert "validity" not in defaults

    def test_columns_subset_works(self, client) -> None:
        scene = run(client, _make_scene)
        dataset = run(client, _dataset, scene, "head", {"columns": ["organization", "vendor"]})
        assert dataset.columns == ["Вуз", "Вендор"]

    def test_product_ids_is_rejected_by_spec_validation(self) -> None:
        from app.core.errors import ValidationError

        with pytest.raises(ValidationError):
            validate_params(REPORT_SPECS["license_register"], {"product_ids": [str(uuid.uuid4())]})
