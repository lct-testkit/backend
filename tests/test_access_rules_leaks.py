"""B4-6: ограничения видимости (B4-5) в scope-запросах — тесты на утечки.

Сцена: вуз A (разрешён) и вуз B (запрещён) — каждый со своей сделкой, контактом,
задачей; владелец обеих сделок — один и тот же ограниченный KAM (`allow` на вуз A).
Без ограничения по `organization_scope_clause`/`deal_scope_clause` (B4-6) KAM увидел
бы обе сделки через обычный скоуп OWN — ограничение обязано их развести.

Критично: прямые обращения к запрещённому объекту — 404, не 403 (раздел 3.2, не
подтверждаем даже факт существования), и названия/номера запрещённых объектов не
должны встречаться в ответах нигде, включая вложенные списки.

Мета-тест `TestRouteCoverage` обходит реальные маршруты приложения под
`/api/deals`, `/api/organizations`, `/api/contacts`, `/api/tasks`,
`/api/attachments` и падает, если появившийся новый `GET` не внесён в `COVERED`
ни явно, ни через `EXEMPT` (с причиной) — так, чтобы будущий забытый эндпоинт не
прошёл тихо.

Открытый вопрос (см. итоговый отчёт агента): `/api/reports`, `/api/dashboards` и
`/api/signature-*` не вошли в поведенческую проверку этого файла (отдельный сетап
подписи/отчётов/дашбордов) — они размечены `EXEMPT` с причиной, а не `COVERED`, то
есть мета-тест НЕ утверждает, что они проверены, только что про них принято
осознанное решение.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run
from tests.crm_helpers import create_published_workflow, login, sign_in


def _initial_status_id(graph: dict) -> str:
    return next(s["id"] for s in graph["statuses"] if s["type"] == "initial")


pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _login(client, user) -> None:
    client.headers["X-CSRF-Token"] = authenticate(client, user)


async def _make_scene(owner_id: uuid.UUID, workflow_id: str, initial_status_id: str) -> dict:
    """Два вуза (A — будет разрешён, B — будет запрещён), по одной сделке с продуктом
    своего направления, по контакту, по задаче — всё на одного владельца."""
    from app.core.db import session_scope
    from app.modules.catalog.models import Contact, Direction, Organization, Product
    from app.modules.crm.models import Deal, DealProduct, Task

    async with session_scope() as session:
        workflow_uuid = uuid.UUID(workflow_id)
        status_uuid = uuid.UUID(initial_status_id)

        # `owner_id=owner_id` на обоих вузах: видимость вуза по направлению не должна
        # зависеть от того, остаётся ли у KAM хоть одна незаблокированная сделка этого
        # вуза (иначе тест на "направление не трогает вузы напрямую" зависел бы от
        # побочного эффекта фильтра по сделкам, а не от organization_restriction).
        org_a = Organization(
            name=f"Вуз-РАЗРЕШЁН-{uuid.uuid4().hex[:8]}", org_type="university", owner_id=owner_id
        )
        org_b = Organization(
            name=f"Вуз-ЗАПРЕЩЁН-{uuid.uuid4().hex[:8]}", org_type="university", owner_id=owner_id
        )
        session.add_all([org_a, org_b])
        await session.flush()

        direction_a = Direction(code=f"dir-a-{uuid.uuid4().hex[:8]}", name="Направление А")
        direction_b = Direction(code=f"dir-b-{uuid.uuid4().hex[:8]}", name="Направление Б")
        session.add_all([direction_a, direction_b])
        await session.flush()

        product_a = Product(
            code=f"prod-a-{uuid.uuid4().hex[:8]}", name="Продукт А", direction_id=direction_a.id
        )
        product_b = Product(
            code=f"prod-b-{uuid.uuid4().hex[:8]}", name="Продукт Б", direction_id=direction_b.id
        )
        session.add_all([product_a, product_b])
        await session.flush()

        deal_a = Deal(
            number=f"D-A-{uuid.uuid4().hex[:8]}",
            title="Сделка вуза А",
            deal_type="b2b",
            workflow_id=workflow_uuid,
            status_id=status_uuid,
            organization_id=org_a.id,
            owner_id=owner_id,
        )
        deal_b = Deal(
            number=f"D-B-{uuid.uuid4().hex[:8]}",
            title="Сделка-ЗАПРЕЩЁН",
            deal_type="b2b",
            workflow_id=workflow_uuid,
            status_id=status_uuid,
            organization_id=org_b.id,
            owner_id=owner_id,
        )
        session.add_all([deal_a, deal_b])
        await session.flush()

        session.add_all(
            [
                DealProduct(deal_id=deal_a.id, product_id=product_a.id, quantity=1),
                DealProduct(deal_id=deal_b.id, product_id=product_b.id, quantity=1),
            ]
        )

        contact_a = Contact(
            first_name="Разрешённый",
            last_name="Контакт",
            organization_id=org_a.id,
        )
        contact_b = Contact(
            first_name="Запрещённый",
            last_name="Контакт",
            organization_id=org_b.id,
        )
        session.add_all([contact_a, contact_b])
        await session.flush()

        task_a = Task(deal_id=deal_a.id, title="Задача А", assignee_id=owner_id)
        task_b = Task(deal_id=deal_b.id, title="Задача-ЗАПРЕЩЁН", assignee_id=owner_id)
        session.add_all([task_a, task_b])
        await session.flush()

        return {
            "org_a": str(org_a.id),
            "org_b": str(org_b.id),
            "org_b_name": org_b.name,
            "direction_a": str(direction_a.id),
            "direction_b": str(direction_b.id),
            "deal_a": str(deal_a.id),
            "deal_b": str(deal_b.id),
            "deal_b_title": deal_b.title,
            "contact_a": str(contact_a.id),
            "contact_b": str(contact_b.id),
            "task_a": str(task_a.id),
            "task_b": str(task_b.id),
        }


def _restrict_to_organization(client, kam_id: uuid.UUID, org_id: str) -> None:
    response = client.put(
        f"/api/admin/users/{kam_id}/access-rules",
        json={
            "organizations": {"mode": "allow", "ids": [org_id]},
            "directions": {"mode": "all", "ids": []},
            "reason": "Тест утечек B4-6",
        },
        headers={"If-Match": "1"},
    )
    assert response.status_code == 200, response.text


def _restrict_direction_deny(client, kam_id: uuid.UUID, direction_id: str) -> None:
    response = client.put(
        f"/api/admin/users/{kam_id}/access-rules",
        json={
            "organizations": {"mode": "all", "ids": []},
            "directions": {"mode": "deny", "ids": [direction_id]},
            "reason": "Тест утечек B4-6",
        },
        headers={"If-Match": "1"},
    )
    assert response.status_code == 200, response.text


def _body_text(response) -> str:
    return response.text


class TestOrganizationBasedRestriction:
    """`allow` на вуз A — вуз B и всё, что к нему привязано, не должно быть видно ни
    в каком эндпоинте, ни по прямой ссылке."""

    @pytest.fixture
    def scene(self, client):
        login(client)  # ADMIN
        graph = create_published_workflow(client, deal_type="b2b")
        kam = run(client, _make_user, "KAM")
        data = run(client, _make_scene, kam.id, graph["workflow"]["id"], _initial_status_id(graph))
        _restrict_to_organization(client, kam.id, data["org_a"])
        data["kam_id"] = str(kam.id)
        sign_in(client, kam)
        return data

    def test_deal_list_hides_the_forbidden_deal(self, client, scene) -> None:
        response = client.get("/api/deals")
        assert response.status_code == 200, response.text
        ids = {item["id"] for item in response.json()["items"]}
        assert scene["deal_a"] in ids
        assert scene["deal_b"] not in ids
        assert scene["deal_b_title"] not in _body_text(response)

    def test_deal_detail_is_not_found(self, client, scene) -> None:
        response = client.get(f"/api/deals/{scene['deal_b']}")
        assert response.status_code == 404, response.text

    def test_deal_history_is_not_found(self, client, scene) -> None:
        response = client.get(f"/api/deals/{scene['deal_b']}/history")
        assert response.status_code == 404, response.text

    def test_deal_comments_is_not_found(self, client, scene) -> None:
        response = client.get(f"/api/deals/{scene['deal_b']}/comments")
        assert response.status_code == 404, response.text

    def test_deal_participants_is_not_found(self, client, scene) -> None:
        response = client.get(f"/api/deals/{scene['deal_b']}/participants")
        assert response.status_code == 404, response.text

    def test_deal_available_transitions_is_not_found(self, client, scene) -> None:
        response = client.get(f"/api/deals/{scene['deal_b']}/available-transitions")
        assert response.status_code == 404, response.text

    def test_organization_list_hides_the_forbidden_organization(self, client, scene) -> None:
        response = client.get("/api/organizations")
        assert response.status_code == 200, response.text
        ids = {item["id"] for item in response.json()["items"]}
        assert scene["org_a"] in ids
        assert scene["org_b"] not in ids
        assert scene["org_b_name"] not in _body_text(response)

    def test_organization_detail_is_not_found(self, client, scene) -> None:
        response = client.get(f"/api/organizations/{scene['org_b']}")
        assert response.status_code == 404, response.text

    def test_check_duplicate_does_not_reveal_the_forbidden_organization(
        self, client, scene
    ) -> None:
        response = client.get(
            "/api/organizations/check-duplicate", params={"name": scene["org_b_name"][:12]}
        )
        assert response.status_code == 200, response.text
        for candidate in response.json()["candidates"]:
            if candidate["id"] == scene["org_b"]:
                assert candidate["accessible"] is False
                assert candidate["name"] is None
                assert candidate["inn"] is None

    def test_contact_list_hides_the_forbidden_contact(self, client, scene) -> None:
        response = client.get("/api/contacts")
        assert response.status_code == 200, response.text
        ids = {item["id"] for item in response.json()["items"]}
        assert scene["contact_a"] in ids
        assert scene["contact_b"] not in ids

    def test_contact_detail_is_not_found(self, client, scene) -> None:
        response = client.get(f"/api/contacts/{scene['contact_b']}")
        assert response.status_code == 404, response.text

    def test_contact_channels_is_not_found(self, client, scene) -> None:
        # `/contacts/{id}/channels` (B4-9) идёт через тот же `ContactService.get_or_404`, что и
        # карточка контакта, но отдаёт отдельные данные (маскированные каналы связи) — чужой
        # контакт не должен выдавать их даже по прямой ссылке.
        response = client.get(f"/api/contacts/{scene['contact_b']}/channels")
        assert response.status_code == 404, response.text

        allowed = client.get(f"/api/contacts/{scene['contact_a']}/channels")
        assert allowed.status_code == 200, allowed.text

    def test_task_list_hides_the_forbidden_task(self, client, scene) -> None:
        response = client.get("/api/tasks")
        assert response.status_code == 200, response.text
        ids = {item["id"] for item in response.json()["items"]}
        assert scene["task_a"] in ids
        assert scene["task_b"] not in ids

    def test_attachments_of_the_forbidden_deal_are_not_found(self, client, scene) -> None:
        response = client.get(
            "/api/attachments", params={"entity_type": "deal", "entity_id": scene["deal_b"]}
        )
        assert response.status_code == 404, response.text

    def test_an_approved_access_request_does_not_override_the_restriction(
        self, client, scene
    ) -> None:
        """B4-12: `organization_scope_clause_for_rules` AND-ирует одобренный временный
        доступ с ограничением B4-5/B4-6 — approved-запрос на запрещённый вуз B не должен
        отменять `allow` на вуз A. Запрос вставлен напрямую в БД (минуя `create()`,
        которая и так отказала бы по `user_organization_allowed`) — проверяем именно
        слой видимости, а не создание запроса."""

        async def _insert_approved(org_id: str, requester_id: uuid.UUID) -> None:
            import datetime as dt

            from app.core.db import session_scope
            from app.modules.catalog.models import AccessRequest

            async with session_scope() as session:
                session.add(
                    AccessRequest(
                        requester_id=requester_id,
                        entity_type="organization",
                        entity_id=uuid.UUID(org_id),
                        reason="Утечка B4-12 — не должна случиться",
                        status="approved",
                        expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(days=30),
                    )
                )
                await session.flush()

        run(client, lambda: _insert_approved(scene["org_b"], uuid.UUID(scene["kam_id"])))

        response = client.get(f"/api/organizations/{scene['org_b']}")
        assert response.status_code == 404, response.text
        assert scene["org_b_name"] not in _body_text(response)


class TestDirectionBasedRestriction:
    """`deny` на направление Б — сделка вуза Б (единственный продукт этого
    направления) скрывается, даже когда вуз сам по себе не ограничен."""

    @pytest.fixture
    def scene(self, client):
        login(client)
        graph = create_published_workflow(client, deal_type="b2b")
        kam = run(client, _make_user, "KAM")
        data = run(client, _make_scene, kam.id, graph["workflow"]["id"], _initial_status_id(graph))
        _restrict_direction_deny(client, kam.id, data["direction_b"])
        sign_in(client, kam)
        return data

    def test_deal_of_the_denied_direction_is_hidden(self, client, scene) -> None:
        response = client.get("/api/deals")
        assert response.status_code == 200, response.text
        ids = {item["id"] for item in response.json()["items"]}
        assert scene["deal_a"] in ids
        assert scene["deal_b"] not in ids

    def test_deal_detail_is_not_found(self, client, scene) -> None:
        response = client.get(f"/api/deals/{scene['deal_b']}")
        assert response.status_code == 404, response.text

    def test_organizations_are_not_restricted_by_direction(self, client, scene) -> None:
        # Направление не сужает список вузов напрямую (B4-6) — вуз Б остаётся
        # виден, только его сделка этого направления скрыта.
        response = client.get("/api/organizations")
        assert response.status_code == 200, response.text
        ids = {item["id"] for item in response.json()["items"]}
        assert scene["org_b"] in ids


class TestRouteCoverage:
    """Каждый `GET` под перечисленными префиксами — осознанное решение: либо
    поведенческая проверка выше (`COVERED`), либо явная причина (`EXEMPT`)."""

    PREFIXES = (
        "/deals",
        "/organizations",
        "/contacts",
        "/tasks",
        "/attachments",
        "/signature-documents",
        "/signature-requests",
        "/signature-templates",
        "/reports",
        "/dashboards",
    )

    # Поведенчески проверено в этом файле (выше).
    COVERED = {
        "/deals",
        "/deals/{deal_id}",
        "/deals/{deal_id}/history",
        "/deals/{deal_id}/comments",
        "/deals/{deal_id}/participants",
        "/deals/{deal_id}/available-transitions",
        "/organizations",
        "/organizations/{organization_id}",
        "/organizations/check-duplicate",
        "/contacts",
        "/contacts/{contact_id}",
        "/contacts/{contact_id}/channels",
        "/tasks",
        "/attachments",
    }

    # Не покрыто поведенческим тестом в этом файле — осознанно, с причиной.
    EXEMPT = {
        # Идут через ContactService.get_or_404 (тот же contact_scope_clause,
        # что уже покрыт выше для /contacts/{contact_id}) — архитектурно то же
        # решение, отдельный сетап (профиль учащегося, вендорские продукты) не
        # добавляет нового пути утечки, но и не был здесь прогнан.
        "/contacts/{contact_id}/learner-profile": "наследует contact_scope_clause (не прогнано)",
        "/contacts/{contact_id}/products": "наследует contact_scope_clause (не прогнано)",
        # B4-9: каналы связи контакта — тот же ContactService.get_or_404, что уже покрыт для
        # /contacts/{contact_id}.
        "/contacts/{contact_id}/channels": "наследует contact_scope_clause (не прогнано)",
        # B2-7/B2-8: DealService.get_or_404 — тот же deal_scope_clause, что уже покрыт для
        # /deals/{deal_id} и /deals/{deal_id}/history.
        "/deals/{deal_id}/history/{history_id}/attachments": (
            "наследует deal_scope_clause через DealService.get_or_404 (не прогнано)"
        ),
        # ПЭП, отчёты и дашборды — отдельный сетап (подписанты, очередь
        # генерации, виджеты), не поднятый в этом проходе. Отчёты уже вызывают
        # deal_scope_clause в builders.py (B1), дашборды и ПЭП — открытый вопрос.
        "/signature-documents": "ПЭП не в этом проходе B4-6 — открытый вопрос",
        "/signature-documents/batch": "ПЭП не в этом проходе B4-6 — открытый вопрос",
        "/signature-documents/{document_id}": "ПЭП не в этом проходе B4-6 — открытый вопрос",
        "/signature-documents/{document_id}/protocol": "ПЭП — открытый вопрос",
        "/signature-requests/{request_id}/file": "ПЭП — открытый вопрос",
        "/signature-templates": "ПЭП — открытый вопрос",
        "/reports": "список своих задач отчётов, не список сделок — вне матрицы утечек",
        "/reports/{report_id}": "ensure_read_access ограничивает автору/ADMIN; не прогнано",
        "/reports/{report_id}/data": "builders.py уже несёт deal_scope_clause (B1); не прогнано",
        "/reports/{report_id}/download": "builders.py несёт deal_scope_clause (B1); не прогнано",
        "/dashboards": "дашборды — отдельная сущность, открытый вопрос B4-6",
        "/dashboards/{dashboard_id}": "дашборды — отдельная сущность, открытый вопрос B4-6",
        "/dashboards/{dashboard_id}/widgets": "дашборды — отдельная сущность, открытый вопрос B4-6",
        # B4-10: своей проверки видимости у филиала нет — роутер достаёт организацию через
        # OrganizationService.get_or_404 (тот же organization_scope_clause, что уже покрыт выше
        # для /organizations/{organization_id}); поведенчески проверено отдельным файлом
        # (tests/test_organization_branches.py::TestCrudAndScope::
        # test_someone_elses_organization_is_not_found), не здесь.
        "/organizations/{organization_id}/branches": (
            "наследует organization_scope_clause, проверено в test_organization_branches.py"
        ),
    }

    def test_every_get_route_is_covered_or_exempt(self, client) -> None:
        app = client.app
        found: set[str] = set()
        for route in app.routes:
            router = getattr(route, "original_router", None)
            if router is None:
                continue
            for sub in router.routes:
                path = getattr(sub, "path", None)
                methods = getattr(sub, "methods", None) or set()
                if path and "GET" in methods and path.startswith(self.PREFIXES):
                    found.add(path)

        known = self.COVERED | set(self.EXEMPT)
        uncovered = found - known
        assert not uncovered, (
            f"Новый GET-маршрут без решения «как с ограничениями» (B4-6): {sorted(uncovered)}. "
            "Добавьте его в COVERED (с тестом выше) или в EXEMPT (с причиной)."
        )
        # Контроль от второй стороны: списки не должны ссылаться на маршруты,
        # которых не существует (опечатка в самом тесте маскировала бы дыру).
        stale = known - found
        assert not stale, f"В COVERED/EXEMPT есть маршруты, которых не существует: {sorted(stale)}"
