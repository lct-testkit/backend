"""B3-8: импорт направлений (`direction`) и ответственных за вуз (`responsible`).

Оба типа — только ADMIN (`ImportService.create_job`): смена `Organization.owner_id` в фоновой
задаче идёт без интерактивного `Principal`, решение не для КАМ/руководителя. Вуз ищется тем же
многоступенчатым поиском, что и лицензии (B3-4, `LicenseMatcher.match_university`).
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import login
from tests.import_helpers import (
    apply_job,
    job_rows,
    make_inn,
    rollback_job,
    start_import,
    token,
    xlsx_bytes,
)

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _in_session(fn, *args):
    """`run(client, fn)` вызывает `fn()` без аргументов (`tests.conftest.run`) — оборачивает
    функцию вида `async def fn(session, ...)` в сессию БД, открытую здесь."""

    async def wrapper():
        from app.core.db import session_scope

        async with session_scope() as session:
            return await fn(session, *args)

    return wrapper


DIRECTION_HEADERS = ["Код", "Название", "Родитель"]
RESPONSIBLE_HEADERS = ["Вуз", "ИНН вуза", "Почта менеджера", "ФИО менеджера"]


def _summary(client, job_id: str) -> dict:
    response = client.get(f"/api/imports/{job_id}/summary")
    assert response.status_code == 200, response.text
    return response.json()


async def _seed_university(session, name: str, *, inn: str | None = None):
    from app.modules.catalog.models import Organization

    org = Organization(name=name, org_type="university", inn=inn)
    session.add(org)
    await session.flush()
    return org


async def _seed_manager(session, *, full_name: str, email: str | None, role: str = "KAM"):
    from app.modules.identity.models import User

    user = User(
        keycloak_id=str(uuid.uuid4()),
        email=email,
        full_name=full_name,
        role=role,
        status="active",
        consent_version="1.0",
    )
    session.add(user)
    await session.flush()
    return user


async def _get_direction(session, code: str):
    from sqlalchemy import select

    from app.modules.catalog.models import Direction

    return await session.scalar(select(Direction).where(Direction.code == code))


async def _get_org(session, org_id: uuid.UUID):
    from app.modules.catalog.models import Organization

    return await session.get(Organization, org_id)


class TestDirectionImport:
    def test_parent_below_child_is_created_with_correct_links(self, client, monkeypatch) -> None:
        tok = token()
        child_code = f"child-{tok}"
        parent_code = f"parent-{tok}"
        admin = login(client, "ADMIN")
        content = xlsx_bytes(
            DIRECTION_HEADERS,
            [
                [child_code, f"Дочернее {tok}", parent_code],
                [parent_code, f"Родитель {tok}", ""],
            ],
        )
        started = start_import(
            client,
            monkeypatch,
            admin,
            entity_type="direction",
            content=content,
            source_format="xlsx",
        )
        assert started["dry"]["error_rows"] == 0, job_rows(client, started["id"])
        apply_job(client, started["id"])

        async def scenario(session):
            child = await _get_direction(session, child_code)
            parent = await _get_direction(session, parent_code)
            return child, parent

        child, parent = run(client, _in_session(scenario))
        assert child is not None and parent is not None
        assert child.parent_id == parent.id

    def test_reimporting_the_same_file_does_not_bump_version(self, client, monkeypatch) -> None:
        tok = token()
        code = f"dir-{tok}"
        admin = login(client, "ADMIN")
        content = xlsx_bytes(DIRECTION_HEADERS, [[code, f"Направление {tok}", ""]])

        first = start_import(
            client,
            monkeypatch,
            admin,
            entity_type="direction",
            content=content,
            source_format="xlsx",
        )
        apply_job(client, first["id"])

        second = start_import(
            client,
            monkeypatch,
            admin,
            entity_type="direction",
            content=content,
            source_format="xlsx",
        )
        assert second["dry"]["ok_rows"] == 1
        apply_job(client, second["id"])

        async def scenario(session):
            return await _get_direction(session, code)

        direction = run(client, _in_session(scenario))
        assert direction.version == 1

    def test_cycle_in_parent_chain_is_a_row_error_on_both_rows(self, client, monkeypatch) -> None:
        tok = token()
        code_a, code_b = f"a-{tok}", f"b-{tok}"
        admin = login(client, "ADMIN")
        content = xlsx_bytes(
            DIRECTION_HEADERS,
            [
                [code_a, f"A {tok}", code_b],
                [code_b, f"B {tok}", code_a],
            ],
        )
        started = start_import(
            client,
            monkeypatch,
            admin,
            entity_type="direction",
            content=content,
            source_format="xlsx",
        )
        assert started["dry"]["error_rows"] == 2
        rows = job_rows(client, started["id"], status="error")
        assert all("циклич" in e.lower() for row in rows for e in row["errors"])

    def test_code_of_a_deleted_direction_is_an_error(self, client, monkeypatch) -> None:
        tok = token()
        code = f"gone-{tok}"
        admin = login(client, "ADMIN")

        async def seed(session):
            import datetime as dt

            from app.modules.catalog.models import Direction

            direction = Direction(code=code, name=f"Удалённое {tok}")
            session.add(direction)
            await session.flush()
            direction.deleted_at = dt.datetime.now(dt.UTC)
            await session.flush()

        run(client, _in_session(seed))
        content = xlsx_bytes(DIRECTION_HEADERS, [[code, f"Новое {tok}", ""]])
        started = start_import(
            client,
            monkeypatch,
            admin,
            entity_type="direction",
            content=content,
            source_format="xlsx",
        )
        assert started["dry"]["error_rows"] == 1

    def test_rollback_removes_created_direction(self, client, monkeypatch) -> None:
        tok = token()
        code = f"rb-{tok}"
        admin = login(client, "ADMIN")
        content = xlsx_bytes(DIRECTION_HEADERS, [[code, f"Откат {tok}", ""]])
        started = start_import(
            client,
            monkeypatch,
            admin,
            entity_type="direction",
            content=content,
            source_format="xlsx",
        )
        apply_job(client, started["id"])
        rollback_job(client, started["id"])

        async def scenario(session):
            return await _get_direction(session, code)

        direction = run(client, _in_session(scenario))
        assert direction.deleted_at is not None

    def test_parent_that_is_only_in_the_catalog_is_accepted(self, client, monkeypatch) -> None:
        # Новое дочернее направление к уже существующему корню: корня в файле нет.
        tok = token()
        root_code, child_code = f"root-{tok}", f"leaf-{tok}"
        admin = login(client, "ADMIN")

        async def seed(session):
            from app.modules.catalog.models import Direction

            session.add(Direction(code=root_code, name=f"Корень {tok}"))
            await session.flush()

        run(client, _in_session(seed))
        content = xlsx_bytes(DIRECTION_HEADERS, [[child_code, f"Лист {tok}", root_code]])
        started = start_import(
            client,
            monkeypatch,
            admin,
            entity_type="direction",
            content=content,
            source_format="xlsx",
        )
        assert started["dry"]["error_rows"] == 0, job_rows(client, started["id"])
        apply_job(client, started["id"])

        async def scenario(session):
            return await _get_direction(session, child_code), await _get_direction(
                session, root_code
            )

        child, root = run(client, _in_session(scenario))
        assert child is not None and root is not None
        assert child.parent_id == root.id

    def test_cycle_closing_through_the_catalog_is_a_row_error(self, client, monkeypatch) -> None:
        # В каталоге `b` уже под `a`; файл ставит `a` под `b` — цикл замыкается через БД.
        tok = token()
        code_a, code_b = f"a-{tok}", f"b-{tok}"
        admin = login(client, "ADMIN")

        async def seed(session):
            from app.modules.catalog.models import Direction

            root = Direction(code=code_a, name=f"A {tok}")
            session.add(root)
            await session.flush()
            session.add(Direction(code=code_b, name=f"B {tok}", parent_id=root.id))
            await session.flush()

        run(client, _in_session(seed))
        content = xlsx_bytes(DIRECTION_HEADERS, [[code_a, f"A {tok}", code_b]])
        started = start_import(
            client,
            monkeypatch,
            admin,
            entity_type="direction",
            content=content,
            source_format="xlsx",
        )
        assert started["dry"]["error_rows"] == 1
        (row,) = job_rows(client, started["id"], status="error")
        assert [issue["code"] for issue in row["issues"]] == ["direction_cycle"]

    def test_rollback_removes_a_parent_created_ahead_of_its_own_row(
        self, client, monkeypatch
    ) -> None:
        tok = token()
        child_code, parent_code = f"child-{tok}", f"parent-{tok}"
        admin = login(client, "ADMIN")
        content = xlsx_bytes(
            DIRECTION_HEADERS,
            [
                [child_code, f"Дочернее {tok}", parent_code],
                [parent_code, f"Родитель {tok}", ""],
            ],
        )
        started = start_import(
            client,
            monkeypatch,
            admin,
            entity_type="direction",
            content=content,
            source_format="xlsx",
        )
        apply_job(client, started["id"])

        async def created_audit(session):
            from sqlalchemy import select

            from app.modules.audit.models import AuditLog

            parent = await _get_direction(session, parent_code)
            rows = await session.execute(
                select(AuditLog.id).where(
                    AuditLog.entity_id == parent.id, AuditLog.action == "DIRECTION_CREATED"
                )
            )
            return len(rows.all())

        # Родитель создан строкой-ребёнком (она раньше в файле) - создание попало в журнал.
        assert run(client, _in_session(created_audit)) == 1

        rollback_job(client, started["id"])

        async def scenario(session):
            return await _get_direction(session, child_code), await _get_direction(
                session, parent_code
            )

        child, parent = run(client, _in_session(scenario))
        assert child.deleted_at is not None
        assert parent.deleted_at is not None

    def test_update_is_audited_with_the_new_value_and_rolled_back(
        self, client, monkeypatch
    ) -> None:
        tok = token()
        code = f"upd-{tok}"
        admin = login(client, "ADMIN")

        async def seed(session):
            from app.modules.catalog.models import Direction

            session.add(Direction(code=code, name=f"Старое {tok}"))
            await session.flush()

        run(client, _in_session(seed))
        content = xlsx_bytes(DIRECTION_HEADERS, [[code, f"Новое {tok}", ""]])
        started = start_import(
            client,
            monkeypatch,
            admin,
            entity_type="direction",
            content=content,
            source_format="xlsx",
        )
        apply_job(client, started["id"])

        async def audit_changes(session):
            from sqlalchemy import select

            from app.modules.audit.models import AuditLog

            direction = await _get_direction(session, code)
            row = await session.scalar(
                select(AuditLog).where(
                    AuditLog.entity_id == direction.id, AuditLog.action == "DIRECTION_UPDATED"
                )
            )
            return direction.name, row.changes

        name, changes = run(client, _in_session(audit_changes))
        assert name == f"Новое {tok}"
        assert changes == {"name": {"old": f"Старое {tok}", "new": f"Новое {tok}"}}

        rollback_job(client, started["id"])

        async def after_rollback(session):
            return await _get_direction(session, code)

        restored = run(client, _in_session(after_rollback))
        assert restored.name == f"Старое {tok}"
        assert restored.deleted_at is None

    def test_summary_counts_a_reimported_direction_as_skipped_not_created(
        self, client, monkeypatch
    ) -> None:
        tok = token()
        code = f"sum-{tok}"
        admin = login(client, "ADMIN")
        content = xlsx_bytes(DIRECTION_HEADERS, [[code, f"Направление {tok}", ""]])

        first = start_import(
            client,
            monkeypatch,
            admin,
            entity_type="direction",
            content=content,
            source_format="xlsx",
        )
        apply_job(client, first["id"])
        assert _summary(client, first["id"])["applied"]["create"] == {"direction": 1}

        second = start_import(
            client,
            monkeypatch,
            admin,
            entity_type="direction",
            content=content,
            source_format="xlsx",
        )
        apply_job(client, second["id"])
        assert _summary(client, second["id"])["applied"] == {
            "create": {"direction": 0},
            "update": {"direction": 0},
            "skip": {"direction": 1},
        }

    def test_head_cannot_create_a_direction_import_job(self, client, monkeypatch) -> None:
        head = login(client, "HEAD")
        content = xlsx_bytes(DIRECTION_HEADERS, [["x", "X", ""]])
        from tests.import_helpers import stub_storage

        stub_storage(monkeypatch, content)
        from tests.import_helpers import _seed_file

        file_id = run(client, _seed_file, content, head.id, "d.xlsx", "application/octet-stream")
        response = client.post(
            "/api/imports",
            json={
                "file_id": str(file_id),
                "entity_type": "direction",
                "mode": "upsert",
                "source_format": "xlsx",
            },
        )
        assert response.status_code == 403, response.text


class TestAdminOnlyJobsAreClosedToHead:
    """Список и карточки заданий видны любому HEAD/ADMIN, поэтому «только ADMIN» для направлений и
    ответственных проверяется не только при создании, но и на каждом действии над заданием."""

    def test_head_cannot_map_dry_run_apply_or_roll_back_an_admin_job(
        self, client, monkeypatch
    ) -> None:
        from tests.crm_helpers import create_user, sign_in

        tok = token()
        admin = login(client, "ADMIN")
        content = xlsx_bytes(DIRECTION_HEADERS, [[f"adm-{tok}", f"Направление {tok}", ""]])
        started = start_import(
            client,
            monkeypatch,
            admin,
            entity_type="direction",
            content=content,
            source_format="xlsx",
        )
        job_id = started["id"]

        head = create_user(client, "HEAD")
        sign_in(client, head)

        assert client.get(f"/api/imports/{job_id}").status_code == 200  # чтение открыто
        mapping = client.put(f"/api/imports/{job_id}/mapping", json={"mapping": started["mapping"]})
        assert mapping.status_code == 403, mapping.text
        assert client.post(f"/api/imports/{job_id}/dry-run").status_code == 403
        assert client.post(f"/api/imports/{job_id}/apply").status_code == 403
        assert client.post(f"/api/imports/{job_id}/rollback").status_code == 403

        # Администратору всё по-прежнему доступно: отказ не испортил задание.
        sign_in(client, admin)
        applied = client.post(f"/api/imports/{job_id}/apply")
        assert applied.status_code == 200, applied.text
        assert applied.json()["status"] in ("completed", "completed_with_errors")

    def test_head_still_drives_the_jobs_of_the_open_types(self, client, monkeypatch) -> None:
        tok = token()
        head = login(client, "HEAD")
        content = xlsx_bytes(["Наименование", "ИНН"], [[f"Организация {tok}", make_inn()]])
        started = start_import(
            client,
            monkeypatch,
            head,
            entity_type="organization",
            content=content,
            source_format="xlsx",
        )
        response = client.post(f"/api/imports/{started['id']}/apply")
        assert response.status_code == 200, response.text


class TestResponsibleImport:
    def test_found_by_email_changes_owner_and_audits(self, client, monkeypatch) -> None:
        tok = token()
        admin = login(client, "ADMIN")
        manager_email = f"kam-{tok}@example.com"

        async def seed(session):
            org = await _seed_university(session, f"Вуз {tok}")
            manager = await _seed_manager(session, full_name=f"Менеджер {tok}", email=manager_email)
            return org.id, manager.id

        org_id, manager_id = run(client, _in_session(seed))
        content = xlsx_bytes(RESPONSIBLE_HEADERS, [[f"Вуз {tok}", "", manager_email, ""]])
        started = start_import(
            client,
            monkeypatch,
            admin,
            entity_type="responsible",
            content=content,
            source_format="xlsx",
        )
        assert started["dry"]["error_rows"] == 0, job_rows(client, started["id"])
        apply_job(client, started["id"])

        async def scenario(session):
            from sqlalchemy import select

            from app.modules.audit.models import AuditLog

            org = await _get_org(session, org_id)
            audit = (
                (
                    await session.execute(
                        select(AuditLog).where(
                            AuditLog.entity_id == org_id,
                            AuditLog.action == "ORGANIZATION_OWNER_CHANGED",
                        )
                    )
                )
                .scalars()
                .all()
            )
            return org, audit

        org, audit = run(client, _in_session(scenario))
        assert org.owner_id == manager_id
        assert len(audit) == 1

    def test_two_managers_with_the_same_name_is_ambiguous(self, client, monkeypatch) -> None:
        tok = token()
        admin = login(client, "ADMIN")
        shared_name = f"Однофамилец {tok}"

        async def seed(session):
            org = await _seed_university(session, f"Вуз {tok}")
            await _seed_manager(session, full_name=shared_name, email=f"m1-{tok}@example.com")
            await _seed_manager(session, full_name=shared_name, email=f"m2-{tok}@example.com")
            return org.id

        run(client, _in_session(seed))
        content = xlsx_bytes(RESPONSIBLE_HEADERS, [[f"Вуз {tok}", "", "", shared_name]])
        started = start_import(
            client,
            monkeypatch,
            admin,
            entity_type="responsible",
            content=content,
            source_format="xlsx",
        )
        assert started["dry"]["error_rows"] == 1
        rows = job_rows(client, started["id"], status="error")
        assert any("неоднозначен" in e for row in rows for e in row["errors"])

    def test_inactive_role_is_not_an_eligible_manager(self, client, monkeypatch) -> None:
        tok = token()
        admin = login(client, "ADMIN")
        auditor_email = f"auditor-{tok}@example.com"

        async def seed(session):
            org = await _seed_university(session, f"Вуз {tok}")
            await _seed_manager(
                session, full_name=f"Аудитор {tok}", email=auditor_email, role="AUDITOR"
            )
            return org.id

        run(client, _in_session(seed))
        content = xlsx_bytes(RESPONSIBLE_HEADERS, [[f"Вуз {tok}", "", auditor_email, ""]])
        started = start_import(
            client,
            monkeypatch,
            admin,
            entity_type="responsible",
            content=content,
            source_format="xlsx",
        )
        assert started["dry"]["error_rows"] == 1

    def test_match_by_inn_is_not_flagged_as_a_fuzzy_match(self, client, monkeypatch) -> None:
        # Вуз найден по ИНН и по точному названию - это самая надёжная ступень поиска, строка
        # `ok`, а не предупреждение «найден как ...».
        tok = token()
        admin = login(client, "ADMIN")
        manager_email = f"kam-{tok}@example.com"
        inn = make_inn()

        async def seed(session):
            await _seed_university(session, f"Вуз {tok}", inn=inn)
            await _seed_manager(session, full_name=f"Менеджер {tok}", email=manager_email)

        run(client, _in_session(seed))
        content = xlsx_bytes(RESPONSIBLE_HEADERS, [[f"Вуз {tok}", inn, manager_email, ""]])
        started = start_import(
            client,
            monkeypatch,
            admin,
            entity_type="responsible",
            content=content,
            source_format="xlsx",
        )
        assert started["dry"]["error_rows"] == 0, job_rows(client, started["id"])
        assert started["dry"]["warn_rows"] == 0, job_rows(client, started["id"])
        assert started["dry"]["ok_rows"] == 1

    def test_summary_tells_a_changed_owner_from_an_unchanged_one(self, client, monkeypatch) -> None:
        tok = token()
        admin = login(client, "ADMIN")
        manager_email = f"kam-{tok}@example.com"

        async def seed(session):
            await _seed_university(session, f"Вуз {tok}")
            await _seed_manager(session, full_name=f"Менеджер {tok}", email=manager_email)

        run(client, _in_session(seed))
        content = xlsx_bytes(RESPONSIBLE_HEADERS, [[f"Вуз {tok}", "", manager_email, ""]])

        first = start_import(
            client,
            monkeypatch,
            admin,
            entity_type="responsible",
            content=content,
            source_format="xlsx",
        )
        assert _summary(client, first["id"])["would"] == {"update": {"responsible": 1}}
        apply_job(client, first["id"])
        assert _summary(client, first["id"])["applied"] == {
            "create": {"responsible": 0},
            "update": {"responsible": 1},
            "skip": {"responsible": 0},
        }

        # Тот же файл ещё раз: ответственный уже назначен - ничего не меняется и не «создаётся».
        second = start_import(
            client,
            monkeypatch,
            admin,
            entity_type="responsible",
            content=content,
            source_format="xlsx",
        )
        assert _summary(client, second["id"])["would"] == {"skip": {"responsible": 1}}
        apply_job(client, second["id"])
        assert _summary(client, second["id"])["applied"] == {
            "create": {"responsible": 0},
            "update": {"responsible": 0},
            "skip": {"responsible": 1},
        }

    def test_organization_not_found_is_a_row_error(self, client, monkeypatch) -> None:
        tok = token()
        admin = login(client, "ADMIN")
        content = xlsx_bytes(
            RESPONSIBLE_HEADERS, [[f"Несуществующий вуз {tok}", "", "", f"ФИО {tok}"]]
        )
        started = start_import(
            client,
            monkeypatch,
            admin,
            entity_type="responsible",
            content=content,
            source_format="xlsx",
        )
        assert started["dry"]["error_rows"] == 1

    def test_rollback_restores_the_previous_owner(self, client, monkeypatch) -> None:
        tok = token()
        admin = login(client, "ADMIN")
        manager_email = f"kam-{tok}@example.com"

        async def seed(session):
            previous_owner = await _seed_manager(
                session, full_name=f"Прежний {tok}", email=f"prev-{tok}@example.com"
            )
            org = await _seed_university(session, f"Вуз {tok}")
            org.owner_id = previous_owner.id
            await session.flush()
            manager = await _seed_manager(session, full_name=f"Менеджер {tok}", email=manager_email)
            return org.id, previous_owner.id, manager.id

        org_id, previous_owner_id, _manager_id = run(client, _in_session(seed))
        content = xlsx_bytes(RESPONSIBLE_HEADERS, [[f"Вуз {tok}", "", manager_email, ""]])
        started = start_import(
            client,
            monkeypatch,
            admin,
            entity_type="responsible",
            content=content,
            source_format="xlsx",
        )
        assert started["dry"]["warn_rows"] == 1  # "текущий ответственный будет заменён"
        apply_job(client, started["id"])
        rollback_job(client, started["id"])

        async def scenario(session):
            return await _get_org(session, org_id)

        org = run(client, _in_session(scenario))
        assert org.owner_id == previous_owner_id

    def test_head_cannot_create_a_responsible_import_job(self, client, monkeypatch) -> None:
        head = login(client, "HEAD")
        content = xlsx_bytes(RESPONSIBLE_HEADERS, [["x", "", "", "y"]])
        from tests.import_helpers import _seed_file, stub_storage

        stub_storage(monkeypatch, content)
        file_id = run(client, _seed_file, content, head.id, "r.xlsx", "application/octet-stream")
        response = client.post(
            "/api/imports",
            json={
                "file_id": str(file_id),
                "entity_type": "responsible",
                "mode": "upsert",
                "source_format": "xlsx",
            },
        )
        assert response.status_code == 403, response.text
