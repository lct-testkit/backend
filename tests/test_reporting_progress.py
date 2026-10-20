"""B1-10: отчёт «Прогресс обучения» на данных LMS (`learning_progress`). Раньше честная
заглушка — таблица и приём данных (`integration.lms.upsert_progress`) существовали, builder их
не читал. Настоящая Postgres обязательна (`tests/conftest.py`) — своя мини-фабрика, не
`tests/lms_helpers.py:make_scene` (та не возвращает `owner_id`/не даёт задать команду, а тесты
скоупа здесь нужны именно по владельцу сделки)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.core.security import Principal
from app.modules.reporting.builders import build_learning_progress, estimate_learning_progress
from tests.conftest import TEST_DATABASE_URL, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


async def _make_scene() -> dict:
    """КАМ1 (вуз O1) и КАМ2 (вуз O2) под общим HEAD, продукт P (направление «Программирование»),
    два контакта-учащихся с прогрессом 100% и 40% по сделке КАМ1, одна сделка КАМ2 без прогресса
    (для проверки скоупа) и одна мягко удалённая сделка КАМ1 с прогрессом (не должна попасть)."""
    from app.core.db import session_scope
    from app.modules.catalog.models import Contact, Direction, Organization, Product
    from app.modules.crm.models import Deal, DealProduct
    from app.modules.identity.models import Team, User
    from app.modules.integration.models import LearningProgress
    from app.modules.workflow.models import Workflow, WorkflowStatus

    token = uuid.uuid4().hex[:10]
    async with session_scope() as session:
        head = User(
            keycloak_id=str(uuid.uuid4()),
            email=f"head-progress-{token}@rt-it-school.ru",
            full_name="Руководителев Р. Р.",
            role="HEAD",
            status="active",
            consent_version="1.0",
        )
        session.add(head)
        await session.flush()
        team = Team(name=f"Команда прогресса {token}", head_id=head.id)
        session.add(team)
        await session.flush()

        kam1 = User(
            keycloak_id=str(uuid.uuid4()),
            email=f"kam1-progress-{token}@rt-it-school.ru",
            full_name="Камов К. К.",
            role="KAM",
            status="active",
            consent_version="1.0",
            team_id=team.id,
        )
        kam2 = User(
            keycloak_id=str(uuid.uuid4()),
            email=f"kam2-progress-{token}@rt-it-school.ru",
            full_name="Камова К. К.",
            role="KAM",
            status="active",
            consent_version="1.0",
            team_id=team.id,
        )
        session.add_all([kam1, kam2])
        await session.flush()

        direction = Direction(code=f"dir-progress-{token}", name="Программирование")
        session.add(direction)
        await session.flush()
        product = Product(
            code=f"p-progress-{token}", name="Python: базовый курс", direction_id=direction.id
        )
        session.add(product)
        await session.flush()

        org1 = Organization(
            name=f"ВУЗ 1 прогресса {token}", org_type="university", owner_id=kam1.id
        )
        org2 = Organization(
            name=f"ВУЗ 2 прогресса {token}", org_type="university", owner_id=kam2.id
        )
        session.add_all([org1, org2])
        await session.flush()

        workflow = Workflow(code=f"wf-progress-{token}", name="Воронка прогресса", deal_type="b2b")
        session.add(workflow)
        await session.flush()
        status = WorkflowStatus(
            workflow_id=workflow.id,
            code="negotiation",
            name="Переговоры",
            type="intermediate",
            sort_order=0,
        )
        session.add(status)
        await session.flush()

        deal1 = Deal(
            number=f"D-progress-{token}-1",
            title="Сделка КАМ1",
            deal_type="b2b",
            workflow_id=workflow.id,
            status_id=status.id,
            organization_id=org1.id,
            owner_id=kam1.id,
        )
        deal2 = Deal(
            number=f"D-progress-{token}-2",
            title="Сделка КАМ2",
            deal_type="b2b",
            workflow_id=workflow.id,
            status_id=status.id,
            organization_id=org2.id,
            owner_id=kam2.id,
        )
        deal_deleted = Deal(
            number=f"D-progress-{token}-3",
            title="Сделка КАМ1 (удалена)",
            deal_type="b2b",
            workflow_id=workflow.id,
            status_id=status.id,
            organization_id=org1.id,
            owner_id=kam1.id,
        )
        session.add_all([deal1, deal2, deal_deleted])
        await session.flush()
        deal_deleted.deleted_at = dt.datetime.now(dt.UTC)

        session.add_all(
            [
                DealProduct(deal_id=deal1.id, product_id=product.id, quantity=1, stream_number=2),
                DealProduct(
                    deal_id=deal_deleted.id, product_id=product.id, quantity=1, stream_number=9
                ),
            ]
        )

        contact_a = Contact(first_name="Дарья", last_name="Осипенко", middle_name="Игоревна")
        contact_b = Contact(first_name="Пётр", last_name="Сидоров")
        session.add_all([contact_a, contact_b])
        await session.flush()

        now = dt.datetime.now(dt.UTC)
        progress_a = LearningProgress(
            deal_id=deal1.id,
            contact_id=contact_a.id,
            product_id=product.id,
            external_course_id="course-a",
            progress_pct=100,
            score=4.8,
            enrolled_at=now - dt.timedelta(days=30),
            completed_at=now - dt.timedelta(days=1),
            last_activity_at=now - dt.timedelta(days=1),
        )
        progress_b = LearningProgress(
            deal_id=deal1.id,
            contact_id=contact_b.id,
            product_id=product.id,
            external_course_id="course-b",
            progress_pct=40,
            score=None,
            enrolled_at=now - dt.timedelta(days=10),
            completed_at=None,
            last_activity_at=now - dt.timedelta(hours=2),
        )
        progress_deleted_deal = LearningProgress(
            deal_id=deal_deleted.id,
            contact_id=contact_a.id,
            product_id=product.id,
            external_course_id="course-c",
            progress_pct=50,
            last_activity_at=now,
        )
        session.add_all([progress_a, progress_b, progress_deleted_deal])
        await session.flush()

        return {
            "head_id": head.id,
            "kam1_id": kam1.id,
            "kam2_id": kam2.id,
            "team_id": team.id,
            "product_id": product.id,
            "direction_id": direction.id,
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

    team_id = scene["team_id"] if user_key == "HEAD" else None
    role = "HEAD" if user_key == "HEAD" else "KAM"
    user_id = scene[f"{user_key.lower()}_id"]
    principal = _principal(user_id, role, team_id=team_id)
    async with session_scope() as session:
        return await build_learning_progress(session, principal, params)


class TestLearningProgressContent:
    def test_two_progress_rows_show_up_with_initials_vuz_and_stream(self, client) -> None:
        scene = run(client, _make_scene)
        dataset = run(client, _dataset, scene, "KAM1", {})
        assert len(dataset.rows) == 2

        students = {row[0] for row in dataset.rows}
        # `mask_name`: "Осипенко Д. И." — не полное ФИО.
        assert "Осипенко Д. И." in students
        assert "Сидоров П." in students
        for row in dataset.rows:
            vuz_index = dataset.keys.index("organization")
            stream_index = dataset.keys.index("stream_number")
            assert row[vuz_index] != "—"
            assert row[stream_index] == 2

    def test_soft_deleted_deal_is_excluded(self, client) -> None:
        scene = run(client, _make_scene)
        dataset = run(client, _dataset, scene, "KAM1", {})
        progress_values = {row[dataset.keys.index("progress_pct")] for row in dataset.rows}
        assert 50 not in progress_values

    def test_manager_of_another_deal_does_not_see_them(self, client) -> None:
        scene = run(client, _make_scene)
        dataset = run(client, _dataset, scene, "KAM2", {})
        assert dataset.rows == []

    def test_head_of_the_team_sees_them(self, client) -> None:
        scene = run(client, _make_scene)
        dataset = run(client, _dataset, scene, "HEAD", {})
        assert len(dataset.rows) == 2

    def test_product_filter_matches_the_course_product_not_deal_rows(self, client) -> None:
        scene = run(client, _make_scene)
        dataset = run(client, _dataset, scene, "HEAD", {"product_ids": [str(scene["product_id"])]})
        assert len(dataset.rows) == 2

    def test_unrelated_product_filter_gives_no_rows(self, client) -> None:
        scene = run(client, _make_scene)
        dataset = run(client, _dataset, scene, "HEAD", {"product_ids": [str(uuid.uuid4())]})
        assert dataset.rows == []

    def test_limit_below_the_total_marks_truncated(self, client) -> None:
        scene = run(client, _make_scene)
        dataset = run(client, _dataset, scene, "HEAD", {"limit": 1})
        assert len(dataset.rows) == 1
        assert dataset.truncated is True
        assert dataset.total_rows == 2

    def test_note_summarizes_progress_when_rows_are_present(self, client) -> None:
        scene = run(client, _make_scene)
        dataset = run(client, _dataset, scene, "HEAD", {})
        assert dataset.note is not None
        assert "Записей LMS: 2" in dataset.note
        assert "завершили 1" in dataset.note

    def test_estimate_matches_the_row_count(self, client) -> None:
        scene = run(client, _make_scene)

        async def _estimate() -> int:
            from app.core.db import session_scope

            async with session_scope() as session:
                return await estimate_learning_progress(
                    session, _principal(scene["head_id"], "HEAD", team_id=scene["team_id"]), {}
                )

        assert run(client, _estimate) == 2


class TestLearningProgressEmptySource:
    def test_empty_table_gives_a_note_about_the_lms_source_not_not_implemented(
        self, client
    ) -> None:
        """«Нет строк вообще» — про всю таблицу `learning_progress`, не только про выборку этого
        пользователя (B1-10, п.4). Таблица общая для сессии тестов — другие тесты этого файла
        уже вставили в неё строки раньше по порядку выполнения, поэтому перед проверкой она
        очищается явно (это тестовая БД этого прогона, не реальные данные)."""
        from sqlalchemy import delete

        from app.core.db import session_scope
        from app.modules.identity.models import User
        from app.modules.integration.models import LearningProgress

        async def _clear_and_make_admin() -> uuid.UUID:
            async with session_scope() as session:
                await session.execute(delete(LearningProgress))
                admin = User(
                    keycloak_id=str(uuid.uuid4()),
                    email=f"admin-progress-{uuid.uuid4().hex[:10]}@rt-it-school.ru",
                    full_name="Админов А. А.",
                    role="ADMIN",
                    status="active",
                    consent_version="1.0",
                )
                session.add(admin)
                await session.flush()
                return admin.id

        admin_id = run(client, _clear_and_make_admin)
        dataset = run(client, _dataset, {"head_id": admin_id, "team_id": None}, "HEAD", {})
        assert dataset.rows == []
        assert dataset.note is not None
        assert "не реализована" not in dataset.note
        # R-16: без жаргона (LMS_BASE_URL), с понятным адресом настройки.
        assert "LMS" in dataset.note and "«Интеграции»" in dataset.note
        assert "LMS_BASE_URL" not in dataset.note
