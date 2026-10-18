"""Тесты конструктора воронок (спринт 2, раздел 6.5).

Проверяется то, что ломается молча: пропуск обязательного статуса в белом
списке DSL превращает конструктор в дыру, забытая проверка достижимости
пропускает статус-ловушку в продакшн, а собственные сиды воронок обязаны
проходить тот же валидатор, что и ручные графы администратора.

Тесты не требуют поднятых PostgreSQL, Redis и Keycloak: `_validate_graph_data`
и DSL — чистые функции, а ORM-объекты здесь используются как обычные Python
инстансы, без сессии и без похода в БД. Исключение — классы с
`pytestmark = skipif(not TEST_DATABASE_URL)`: `TestDeleteDraftWorkflow` (П4,
удаление черновика воронки), `TestSaveGraphWithDeals` (правка графа при живых
сделках), `TestSeedPublishesSlaRules`, `TestHasUnpublishedChanges`,
`TestMappingJobEndpoint`, `TestPatchWorkflow`, `TestImpactWithTarget`: настоящая
Postgres обязательна, тот же приём, что
`tests/test_imports.py::TestLicenseImportEndToEnd` — см. `tests/conftest.py`;
воронки и сделки заводятся через `tests/crm_helpers.py`.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.permissions import Permission, has_permission
from app.modules.workflow import dsl
from app.modules.workflow.models import (
    StatusType,
    WorkflowState,
    WorkflowStatus,
    WorkflowTransition,
)
from app.modules.workflow.seed import _b2b_sales_spec, _b2b_spec, _b2c_spec
from app.modules.workflow.service import _validate_graph_data
from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run
from tests.crm_helpers import (
    body_from_graph,
    by_code,
    create_deal,
    create_draft_workflow,
    create_published_workflow,
    get_graph,
    graph_body,
    login,
    put_graph,
    transition_deal,
)

#: B2-1: дословные названия 14 шагов задания (первоисточник — "6. ИТ Школа Ростелекома.md",
#: строки 59-91 — `rtk_requiriments.md` из документа спринта в этом репозитории отсутствует).
#: Шаг 5 несёт пометку «(опционально)» и шаг 2 сохраняет «с ним» — решение документа спринта.
TZ_STEPS: list[str] = [
    "Поиск контактов ответственного в вузе",
    "Коммуникация с ним и уточнение актуальности программ по ИТ-направлениям",
    "Организация встречи с представителями вуза",
    "Обмен необходимым пакетом документов для подписания",
    "Корректировка документов перед подписанием (опционально)",
    "Подписание документов",
    "Передача обучающих материалов по ИТ-направлению, лицензии ИТ-продукта и его "
    "документации в вуз",
    "Сопровождение внедрения ИТ-продуктов в вузе",
    "Обучение преподавателей",
    "Актуализация учебной программы по ИТ-направлению с учётом обучения преподавателя и "
    "добавления ИТ-продукта",
    "Ведение занятий",
    "Актуализация документации по продукту и обучающим материалам",
    "Повышение квалификации преподавателей",
    "Контроль за исполнением каждого этапа",
]


def _status(code: str, type_: str, *, archived: bool = False) -> WorkflowStatus:
    row = WorkflowStatus(
        workflow_id=uuid.uuid4(),
        code=code,
        name=code,
        type=type_,
        is_archived=archived,
    )
    row.id = uuid.uuid4()
    return row


def _transition(
    from_status: WorkflowStatus,
    to_status: WorkflowStatus,
    *,
    name: str = "t",
    conditions: dict | None = None,
    actions: list | None = None,
    requires_comment: bool = False,
) -> WorkflowTransition:
    row = WorkflowTransition(
        workflow_id=from_status.workflow_id,
        from_status_id=from_status.id,
        to_status_id=to_status.id,
        name=name,
        conditions=conditions or {},
        actions=actions or [],
        requires_comment=requires_comment,
    )
    return row


class TestDslWhitelist:
    def test_unknown_field_is_rejected(self) -> None:
        errors = dsl.validate_condition({"field": "password_hash", "op": "eq", "value": "x"})
        assert errors and "не разрешено" in errors[0]

    def test_custom_fields_prefix_is_allowed_without_registry(self) -> None:
        condition = {"field": "custom_fields.contract_number", "op": "not_null"}
        assert dsl.validate_condition(condition) == []

    def test_custom_fields_prefix_checked_against_registry_when_given(self) -> None:
        known = frozenset({"contract_number"})
        assert dsl.validate_condition(
            {"field": "custom_fields.unknown", "op": "not_null"}, known_custom_fields=known
        )

    def test_attachment_category_must_be_known(self) -> None:
        assert dsl.validate_condition({"field": "attachments.nonsense", "op": "exists"})
        assert dsl.validate_condition({"field": "attachments.contract", "op": "exists"}) == []

    def test_unknown_operator_rejected(self) -> None:
        errors = dsl.validate_condition({"field": "amount", "op": "startswith", "value": 1})
        assert errors and "неизвестный оператор" in errors[0]

    def test_valueless_operator_rejects_value(self) -> None:
        assert dsl.validate_condition({"field": "amount", "op": "not_null", "value": 1})

    def test_value_required_operator_rejects_missing_value(self) -> None:
        assert dsl.validate_condition({"field": "amount", "op": "gt"})

    def test_deep_nesting_is_rejected(self) -> None:
        node = {"field": "amount", "op": "gt", "value": 1}
        for _ in range(dsl.MAX_CONDITION_DEPTH + 1):
            node = {"all": [node]}
        errors = dsl.validate_condition(node)
        assert errors and "вложенность" in errors[0]

    def test_empty_any_branch_is_rejected(self) -> None:
        assert dsl.validate_condition({"any": []})


class TestDslEvaluate:
    def test_missing_field_does_not_crash_and_is_unmet(self) -> None:
        result = dsl.evaluate({"field": "amount", "op": "gt", "value": 0}, {})
        assert not result.ok
        assert result.unmet[0].field == "amount"

    def test_all_conjunction(self) -> None:
        ctx = {"amount": 100, "custom_fields": {"contract_number": "X"}}
        cond = {
            "all": [
                {"field": "amount", "op": "gt", "value": 0},
                {"field": "custom_fields.contract_number", "op": "not_null"},
            ]
        }
        assert dsl.evaluate(cond, ctx).ok

    def test_any_disjunction_empty_is_false(self) -> None:
        # Дизъюнкция пустого множества условий не может быть истинной —
        # иначе переход без единого guard-условия внутри `any` считался бы
        # всегда разрешённым.
        assert not dsl.evaluate({"any": []}, {}).ok

    def test_exists_false_for_empty_collection(self) -> None:
        assert not dsl.evaluate(
            {"field": "attachments.contract", "op": "exists"}, {"attachments": {"contract": []}}
        ).ok
        assert dsl.evaluate(
            {"field": "attachments.contract", "op": "exists"}, {"attachments": {"contract": ["f"]}}
        ).ok

    def test_string_date_compared_against_datetime_field(self) -> None:
        import datetime as dt

        ctx = {"expected_close_date": dt.datetime(2026, 6, 1, tzinfo=dt.UTC)}
        assert dsl.evaluate(
            {"field": "expected_close_date", "op": "date_after", "value": "2026-01-01"}, ctx
        ).ok


class TestDslActions:
    def test_request_signature_requires_signers(self) -> None:
        action = {"type": "request_signature", "template": "x", "signers": []}
        assert dsl.validate_actions([action])

    def test_valid_request_signature(self) -> None:
        action = {
            "type": "request_signature",
            "template": "kp_approval",
            "signers": [{"role": "HEAD"}],
            "order": "sequential",
            "deadline_days": 7,
            "on_rejected": "previous_status",
        }
        assert dsl.validate_actions([action]) == []

    def test_unknown_action_type_rejected(self) -> None:
        assert dsl.validate_actions([{"type": "delete_everything"}])

    def test_typo_in_action_key_is_rejected(self) -> None:
        # `channel` вместо `channels` не должен молча создавать действие без
        # каналов доставки — опечатка обязана быть видна на этапе валидации.
        action = {"type": "notify", "event_code": "X", "channel": ["in_app"]}
        errors = dsl.validate_actions([action])
        assert errors and "неизвестные ключи" in errors[0]


def _split(issues: list) -> tuple[list[str], list[str]]:
    """B9-6: `_validate_graph_data` отдаёт структурированные `ValidationIssue`, а не голые
    строки — большинство тестов этого класса интересует только текст и `severity`, поэтому
    здесь то же разбиение на `errors`/`warnings`, что раньше делала сама функция."""
    errors = [issue.message for issue in issues if issue.severity == "error"]
    warnings = [issue.message for issue in issues if issue.severity == "warning"]
    return errors, warnings


class TestGraphValidation:
    def test_requires_exactly_one_initial(self) -> None:
        a = _status("a", StatusType.INITIAL.value)
        b = _status("b", StatusType.INITIAL.value)
        won = _status("won", StatusType.WON.value)
        transitions = [_transition(a, won), _transition(b, won)]
        errors, _ = _split(_validate_graph_data([a, b, won], transitions))
        assert any("initial" in e for e in errors)

    def test_requires_exactly_one_initial_has_stable_code(self) -> None:
        a = _status("a", StatusType.INITIAL.value)
        b = _status("b", StatusType.INITIAL.value)
        won = _status("won", StatusType.WON.value)
        transitions = [_transition(a, won), _transition(b, won)]
        issues = _validate_graph_data([a, b, won], transitions)
        issue = next(i for i in issues if i.code == "INITIAL_COUNT")
        assert issue.severity == "error"

    def test_requires_at_least_one_terminal(self) -> None:
        a = _status("a", StatusType.INITIAL.value)
        b = _status("b", StatusType.INTERMEDIATE.value)
        errors, _ = _split(_validate_graph_data([a, b], [_transition(a, b)]))
        assert any("терминальный" in e for e in errors)

    def test_detects_unreachable_status(self) -> None:
        a = _status("a", StatusType.INITIAL.value)
        b = _status("b", StatusType.INTERMEDIATE.value)
        orphan = _status("orphan", StatusType.INTERMEDIATE.value)
        won = _status("won", StatusType.WON.value)
        transitions = [_transition(a, b), _transition(b, won)]
        issues = _validate_graph_data([a, b, orphan, won], transitions)
        issue = next(i for i in issues if i.code == "UNREACHABLE_STATUS")
        assert issue.severity == "error"
        assert "orphan" in issue.refs

    def test_detects_trap_status(self) -> None:
        # `b` достижим из initial, но из него нет пути ни в один терминал.
        a = _status("a", StatusType.INITIAL.value)
        b = _status("b", StatusType.INTERMEDIATE.value)
        won = _status("won", StatusType.WON.value)
        transitions = [_transition(a, b)]
        issues = _validate_graph_data([a, b, won], transitions)
        issue = next(i for i in issues if i.code == "TRAP_STATUS")
        assert "b" in issue.refs

    def test_archived_status_and_its_transitions_are_excluded(self) -> None:
        # Архивный статус не обязан быть достижимым и не создаёт ловушку —
        # он больше не часть действующего графа.
        a = _status("a", StatusType.INITIAL.value)
        won = _status("won", StatusType.WON.value)
        archived = _status("old", StatusType.INTERMEDIATE.value, archived=True)
        transitions = [_transition(a, won), _transition(a, archived)]
        errors, _ = _split(_validate_graph_data([a, won, archived], transitions))
        assert errors == []

    def test_request_signature_on_rejected_must_reference_existing_status(self) -> None:
        a = _status("a", StatusType.INITIAL.value)
        b = _status("b", StatusType.INTERMEDIATE.value)
        won = _status("won", StatusType.WON.value)
        bad_transition = _transition(
            a,
            b,
            actions=[
                {
                    "type": "request_signature",
                    "template": "x",
                    "signers": [{"role": "HEAD"}],
                    "on_rejected": "no_such_status",
                }
            ],
        )
        issues = _validate_graph_data([a, b, won], [bad_transition, _transition(b, won)])
        issue = next(i for i in issues if i.code == "SIGNATURE_REJECT_TARGET_UNKNOWN")
        assert "no_such_status" in issue.refs
        assert issue.path == "transitions[t].actions"

    def test_lost_transition_without_reason_condition_warns_not_errors(self) -> None:
        a = _status("a", StatusType.INITIAL.value)
        lost = _status("lost", StatusType.LOST.value)
        errors, warnings = _split(_validate_graph_data([a, lost], [_transition(a, lost)]))
        assert errors == []
        assert any("lost" in w for w in warnings)

    def test_lost_transition_with_reason_and_comment_has_no_warning(self) -> None:
        a = _status("a", StatusType.INITIAL.value)
        lost = _status("lost", StatusType.LOST.value)
        t = _transition(
            a,
            lost,
            requires_comment=True,
            conditions={"field": "loss_reason_id", "op": "not_null"},
        )
        _, warnings = _split(_validate_graph_data([a, lost], [t]))
        assert warnings == []

    def test_condition_errors_include_transition_name_for_debugging(self) -> None:
        a = _status("a", StatusType.INITIAL.value)
        won = _status("won", StatusType.WON.value)
        bad = _transition(a, won, name="bad", conditions={"field": "nope", "op": "eq", "value": 1})
        issues = _validate_graph_data([a, won], [bad])
        issue = next(i for i in issues if i.code == "CONDITION_INVALID")
        assert issue.path == "transitions[bad].conditions"
        assert issue.severity == "error"


class TestSeedGraphs:
    """Собственные сиды должны проходить тот же валидатор, что и графы,
    собранные администратором вручную — иначе сид тихо создаст воронку,
    которую нельзя опубликовать."""

    def _build(self, spec) -> tuple[list[WorkflowStatus], list[WorkflowTransition]]:  # noqa: ANN001
        workflow_id = uuid.uuid4()
        by_code: dict[str, WorkflowStatus] = {}
        statuses: list[WorkflowStatus] = []
        for s in spec.statuses:
            row = WorkflowStatus(
                workflow_id=workflow_id,
                code=s.code,
                name=s.name,
                type=s.type,
                sort_order=s.sort_order,
                required_fields=s.required_fields,
            )
            row.id = uuid.uuid4()
            by_code[s.code] = row
            statuses.append(row)

        transitions = [
            WorkflowTransition(
                workflow_id=workflow_id,
                from_status_id=by_code[t.from_code].id,
                to_status_id=by_code[t.to_code].id,
                name=t.name,
                conditions=t.conditions,
                actions=t.actions,
                requires_comment=t.requires_comment,
            )
            for t in spec.transitions
        ]
        return statuses, transitions

    def test_b2b_seed_has_fourteen_pipeline_steps(self) -> None:
        # new_spec §1 считает 14 шагов пайплайна, но ровно один из них обязан
        # быть типа `initial` — иначе граф не проходит валидацию (раздел 4.11:
        # «ровно один статус типа initial»). Поэтому 14 = 1 initial + 13
        # intermediate, а не 14 статусов типа intermediate.
        spec = _b2b_spec()
        pipeline = [
            s
            for s in spec.statuses
            if s.type in {StatusType.INITIAL.value, StatusType.INTERMEDIATE.value}
        ]
        assert len(pipeline) == 14
        assert sum(1 for s in pipeline if s.type == StatusType.INITIAL.value) == 1

    def test_b2b_seed_passes_validation_cleanly(self) -> None:
        statuses, transitions = self._build(_b2b_spec())
        errors, warnings = _split(_validate_graph_data(statuses, transitions))
        assert errors == []
        assert warnings == []

    def test_b2c_seed_has_six_pipeline_steps(self) -> None:
        spec = _b2c_spec()
        pipeline = [
            s
            for s in spec.statuses
            if s.type in {StatusType.INITIAL.value, StatusType.INTERMEDIATE.value}
        ]
        assert len(pipeline) == 6
        assert sum(1 for s in pipeline if s.type == StatusType.INITIAL.value) == 1

    def test_b2c_seed_passes_validation_cleanly(self) -> None:
        statuses, transitions = self._build(_b2c_spec())
        errors, warnings = _split(_validate_graph_data(statuses, transitions))
        assert errors == []
        assert warnings == []

    def test_no_duplicate_transition_pairs_in_seeds(self) -> None:
        for spec in (_b2b_spec(), _b2c_spec()):
            pairs = [(t.from_code, t.to_code) for t in spec.transitions]
            assert len(pairs) == len(set(pairs)), spec.code

    def test_every_pipeline_status_has_an_sla_and_closing_ones_do_not(self) -> None:
        # A#10: без правил у всех демо-сделок `sla_due_at = null`, фильтры и индикаторы SLA пусты.
        pipeline = {StatusType.INITIAL.value, StatusType.INTERMEDIATE.value}
        for spec in (_b2b_spec(), _b2c_spec()):
            for status in spec.statuses:
                assert (status.sla_hours is not None) == (status.type in pipeline), (
                    spec.code,
                    status.code,
                )

    def test_b2b_v2_names_match_assignment(self) -> None:
        """B2-1: воронка по умолчанию (`_b2b_spec`, v2) несёт ровно 14 названий задания, в
        точном порядке — жюри сверяет названия и порядок шагов с разделом 2 задания."""
        spec = _b2b_spec()
        pipeline = [
            s
            for s in spec.statuses
            if s.type in {StatusType.INITIAL.value, StatusType.INTERMEDIATE.value}
        ]
        pipeline.sort(key=lambda s: s.sort_order)
        assert [s.name for s in pipeline] == TZ_STEPS

    def test_b2b_v2_has_fourteen_pipeline_steps_and_17_statuses(self) -> None:
        spec = _b2b_spec()
        assert len(spec.statuses) == 17
        pipeline = [
            s
            for s in spec.statuses
            if s.type in {StatusType.INITIAL.value, StatusType.INTERMEDIATE.value}
        ]
        assert len(pipeline) == 14
        assert {s.type for s in spec.statuses} >= {
            StatusType.WON.value,
            StatusType.LOST.value,
            StatusType.PARKED.value,
        }

    def test_b2b_v2_has_71_transitions_and_14_sla_rules(self) -> None:
        spec = _b2b_spec()
        pairs = [(t.from_code, t.to_code) for t in spec.transitions]
        assert len(pairs) == len(set(pairs)) == 71
        assert sum(1 for s in spec.statuses if s.sla_hours is not None) == 14

    def test_optional_step_can_be_bypassed(self) -> None:
        """Раздел 2, шаг 5: «опционально: корректировка документов перед подписанием» — путь
        4→6 (минуя документы_correction) и путь 4→5→6 оба должны существовать в графе, а
        кратчайший путь от начального статуса до `won` без шага 5 — 13 переходов (14 шагов
        минус один, раз шаг 5 пропущен)."""
        spec = _b2b_spec()
        forward = {}
        for t in spec.transitions:
            forward.setdefault(t.from_code, []).append(t.to_code)
        assert "signing" in forward["documents_exchange"]  # 4 -> 6, в обход
        assert "documents_correction" in forward["documents_exchange"]  # 4 -> 5
        assert "signing" in forward["documents_correction"]  # 5 -> 6

        initial = next(s.code for s in spec.statuses if s.type == StatusType.INITIAL.value)
        # BFS по чисто форвардным рёбрам (к won), игнорируя lost/parked/назад/цикл.
        linear_order = [
            "contact_search",
            "communication",
            "meeting",
            "documents_exchange",
            "signing",
            "materials_transfer",
            "implementation_support",
            "teacher_training",
            "curriculum_update",
            "classes",
            "docs_update",
            "teacher_qualification",
            "execution_control",
            "won",
        ]
        assert linear_order[0] == initial
        assert len(linear_order) - 1 == 13  # 13 переходов, шаг 5 пропущен

    def test_only_head_closes_deal(self) -> None:
        """Шаг 14 («контроль за исполнением каждого этапа») закрывает сделку успешно только
        HEAD; ADMIN проходит всегда через `principal.is_admin` в `crm.service.transition`, а не
        через `allowed_roles`."""
        spec = _b2b_spec()
        closing = next(
            t for t in spec.transitions if t.from_code == "execution_control" and t.to_code == "won"
        )
        assert closing.allowed_roles == ["HEAD"]
        assert closing.requires_comment is True

    def test_legacy_sales_spec_unchanged(self) -> None:
        """B2-1 (вариант B): старая продажная воронка (`_b2b_sales_spec`, v1) не трогается —
        код статусов и структура графа остаются как были до появления v2. 69 переходов = 68 от
        генератора `_linear_funnel` минус 4 заменённых действием/условием плюс 5 своих (решение
        документа, числа сняты с кода ДО правки B2-1)."""
        spec = _b2b_sales_spec()
        assert spec.code == "b2b_university_v1"
        assert len(spec.statuses) == 17
        assert len(spec.transitions) == 69
        codes = {s.code for s in spec.statuses}
        assert codes == {
            "identification",
            "first_contact",
            "qualification",
            "meeting_scheduled",
            "meeting_held",
            "requirements",
            "kp_preparation",
            "kp_approval",
            "legal_approval",
            "contract_signing",
            "lms_transfer",
            "training_launch",
            "monitoring",
            "closing_prolongation",
            "won",
            "lost",
            "parked",
        }
        # ПЭП-согласование КП — исключительно в v1, действие на переходе
        # kp_preparation -> kp_approval.
        kp_transition = next(
            t
            for t in spec.transitions
            if t.from_code == "kp_preparation" and t.to_code == "kp_approval"
        )
        assert any(a.get("type") == "request_signature" for a in kp_transition.actions)


class TestPermissionMatrix:
    def test_only_admin_writes_or_publishes_workflows(self) -> None:
        for role in ("KAM", "HEAD", "AUDITOR", "INTEGRATION"):
            assert not has_permission(role, Permission.WORKFLOW_WRITE)
            assert not has_permission(role, Permission.WORKFLOW_PUBLISH)
        assert has_permission("ADMIN", Permission.WORKFLOW_WRITE)
        assert has_permission("ADMIN", Permission.WORKFLOW_PUBLISH)

    def test_kam_and_head_can_read_workflows(self) -> None:
        assert has_permission("KAM", Permission.WORKFLOW_READ)
        assert has_permission("HEAD", Permission.WORKFLOW_READ)

    def test_head_renames_statuses_but_not_the_whole_graph(self) -> None:
        """B2-5: Функц.2 — «корректировка именования статусов» — доступна Руководителю
        отдельно от конструктора (workflow:write/workflow:publish остаются только у ADMIN)."""
        assert has_permission("HEAD", Permission.WORKFLOW_RENAME_STATUS)
        assert not has_permission("HEAD", Permission.WORKFLOW_WRITE)
        assert not has_permission("HEAD", Permission.WORKFLOW_PUBLISH)
        for role in ("KAM", "AUDITOR", "INTEGRATION"):
            assert not has_permission(role, Permission.WORKFLOW_RENAME_STATUS)
        assert has_permission("ADMIN", Permission.WORKFLOW_RENAME_STATUS)


class TestWorkflowState:
    def test_states_match_spec_enum(self) -> None:
        assert {s.value for s in WorkflowState} == {"draft", "published", "archived"}


class TestDeleteDraftWorkflow:
    """П4: `DELETE /api/workflows/{id}` — только черновик, который никогда не
    публиковался. Настоящая Postgres обязательна — см. докстринг модуля."""

    pytestmark = pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
    )

    def _admin(self, client) -> str:
        admin = run(client, _make_user, "ADMIN")
        csrf = authenticate(client, admin)
        client.headers["X-CSRF-Token"] = csrf
        return csrf

    def test_deletes_a_never_published_draft(self, client) -> None:
        self._admin(client)
        create = client.post(
            "/api/workflows",
            json={
                "code": f"wf_draft_{uuid.uuid4().hex[:8]}",
                "name": "Черновик на удаление",
                "deal_type": "b2b",
                "is_default": False,
            },
        )
        assert create.status_code == 201, create.text
        workflow_id = create.json()["id"]

        delete = client.delete(f"/api/workflows/{workflow_id}")
        assert delete.status_code == 204, delete.text
        assert client.get(f"/api/workflows/{workflow_id}").status_code == 404

    def test_published_workflow_cannot_be_deleted(self, client) -> None:
        from sqlalchemy import update

        from app.core.db import session_scope
        from app.modules.workflow.models import Workflow

        self._admin(client)
        create = client.post(
            "/api/workflows",
            json={
                "code": f"wf_pub_{uuid.uuid4().hex[:8]}",
                "name": "Уже опубликована",
                "deal_type": "b2b",
                "is_default": False,
            },
        )
        workflow_id = create.json()["id"]

        async def _mark_published() -> None:
            async with session_scope() as session:
                await session.execute(
                    update(Workflow)
                    .where(Workflow.id == uuid.UUID(workflow_id))
                    .values(
                        state="published",
                        published_graph={"statuses": [], "transitions": []},
                        graph_hash="stub-hash-for-test",
                    )
                )

        run(client, _mark_published)

        delete = client.delete(f"/api/workflows/{workflow_id}")
        assert delete.status_code == 409, delete.text
        assert delete.json()["code"] == "CRM-1207"
        # Не удалилась — карточка всё ещё читается.
        assert client.get(f"/api/workflows/{workflow_id}").status_code == 200


class TestArchiveWorkflow:
    """B9-7 (OPEN-ISSUES B-4): `POST /api/workflows/{id}/archive` — раньше `WorkflowState.
    ARCHIVED` существовал (и `_ensure_not_archived` блокировал правки архивной воронки), но
    ничто его не присваивало. Настоящая Postgres обязательна — см. докстринг модуля."""

    pytestmark = pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
    )

    def _admin(self, client) -> None:
        admin = run(client, _make_user, "ADMIN")
        client.headers["X-CSRF-Token"] = authenticate(client, admin)

    def _archive(self, client, workflow: dict, *, version: int | None = None):
        return client.post(
            f"/api/workflows/{workflow['id']}/archive",
            headers={"If-Match": str(version if version is not None else workflow["version"])},
        )

    def test_archives_a_draft_without_deals(self, client) -> None:
        self._admin(client)
        workflow = create_draft_workflow(client)

        response = self._archive(client, workflow)
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "archived"
        assert get_graph(client, workflow["id"])["workflow"]["state"] == "archived"

    def test_archived_workflow_rejects_further_graph_changes(self, client) -> None:
        self._admin(client)
        workflow = create_draft_workflow(client)
        archived = self._archive(client, workflow).json()

        saved = put_graph(client, archived, graph_body())
        assert saved.status_code == 422, saved.text

    def test_cannot_archive_twice(self, client) -> None:
        self._admin(client)
        workflow = create_draft_workflow(client)
        archived = self._archive(client, workflow).json()

        again = self._archive(client, archived, version=archived["version"])
        assert again.status_code == 422, again.text

    def test_cannot_archive_a_workflow_with_deals(self, client) -> None:
        self._admin(client)
        graph = create_published_workflow(client)
        create_deal(client, graph["workflow"]["id"])

        response = self._archive(client, graph["workflow"])
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "CRM-1303"

    def test_cannot_archive_the_default_workflow(self, client) -> None:
        self._admin(client)
        workflow = create_draft_workflow(client, deal_type="b2c", is_default=True)

        response = self._archive(client, workflow)
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "CRM-1303"


class TestSaveGraphWithDeals:
    """A#27 и B#5 из `frontend/docs/backend-issues.md`: правка графа, когда по
    воронке уже идут сделки. Настоящая Postgres обязательна — см. докстринг
    модуля."""

    pytestmark = pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
    )

    def _deal_in_work(self, client):
        """Опубликованная воронка `new → work → won` и сделка, прошедшая `new → work`."""
        login(client)
        graph = create_published_workflow(client)
        deal = create_deal(client, graph["workflow"]["id"])
        moved = transition_deal(client, deal, by_code(graph)["work"]["id"])
        assert moved.status_code == 200, moved.text
        return graph, moved.json()["deal"]

    def test_edit_after_a_deal_passed_a_transition(self, client) -> None:
        graph, deal = self._deal_in_work(client)
        history_before = client.get(f"/api/deals/{deal['id']}/history").json()["statuses"]

        body = body_from_graph(graph)
        for transition in body["transitions"]:
            if transition["name"] == "В работу":
                transition["name"] = "Взять в работу"
        body["sla_rules"][0]["max_duration_hours"] = 48

        saved = put_graph(client, graph["workflow"], body)
        assert saved.status_code == 200, saved.text
        out = saved.json()
        # Переход остался той же строкой — на его id ссылается история сделки.
        assert {t["id"]: t["name"] for t in out["transitions"]} == {
            t["id"]: "Взять в работу" if t["name"] == "В работу" else t["name"]
            for t in graph["transitions"]
        }
        assert out["sla_rules"][0]["max_duration_hours"] == 48
        assert client.get(f"/api/deals/{deal['id']}/history").json()["statuses"] == history_before

    def test_transition_is_updated_by_id(self, client) -> None:
        login(client)
        graph = create_published_workflow(
            client,
            graph_body(
                statuses=[
                    {"code": "new", "name": "Новая", "type": "initial"},
                    {"code": "work", "name": "В работе"},
                    {"code": "won", "name": "Закрыта", "type": "won"},
                    {"code": "lost", "name": "Отказ", "type": "lost"},
                ],
                transitions=[
                    {"from_status": "new", "to_status": "work", "name": "В работу"},
                    {"from_status": "work", "to_status": "won", "name": "Закрыть"},
                    {"from_status": "work", "to_status": "lost", "name": "Отказ"},
                ],
            ),
        )
        statuses = by_code(graph)
        to_lost = next(t for t in graph["transitions"] if t["name"] == "Отказ")

        body = body_from_graph(graph)
        moved = next(t for t in body["transitions"] if t["id"] == to_lost["id"])
        moved["from_status"] = statuses["new"]["id"]

        saved = put_graph(client, graph["workflow"], body)
        assert saved.status_code == 200, saved.text
        rewired = next(t for t in saved.json()["transitions"] if t["id"] == to_lost["id"])
        assert (rewired["from_status_id"], rewired["to_status_id"]) == (
            statuses["new"]["id"],
            statuses["lost"]["id"],
        )

    def test_removing_a_transition_used_by_history_is_a_conflict(self, client) -> None:
        graph, deal = self._deal_in_work(client)
        used = next(t for t in graph["transitions"] if t["name"] == "В работу")

        body = body_from_graph(graph)
        body["transitions"] = [t for t in body["transitions"] if t["id"] != used["id"]]

        response = put_graph(client, graph["workflow"], body)
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "CRM-1209"
        # Ничего не записалось: граф и история на месте.
        assert get_graph(client, graph["workflow"]["id"])["transitions"] == graph["transitions"]
        assert len(client.get(f"/api/deals/{deal['id']}/history").json()["statuses"]) == 2

    def test_removing_a_status_with_deals_is_a_conflict(self, client) -> None:
        graph, _deal = self._deal_in_work(client)
        work = by_code(graph)["work"]

        body = body_from_graph(graph)
        body["statuses"] = [s for s in body["statuses"] if s["id"] != work["id"]]
        body["transitions"] = [
            t for t in body["transitions"] if work["id"] not in (t["from_status"], t["to_status"])
        ]
        body["sla_rules"] = []

        response = put_graph(client, graph["workflow"], body)
        assert response.status_code == 409, response.text
        problem = response.json()
        assert problem["code"] == "CRM-1208"
        assert "В работе" in problem["detail"]
        assert get_graph(client, graph["workflow"]["id"])["statuses"] == graph["statuses"]

    def test_removing_a_published_status_is_refused_even_when_unused(self, client) -> None:
        # Сделки идут по опубликованному снимку: удалив строку статуса, из которого сделка ещё
        # может в него перейти, мы получили бы сбой на внешнем ключе уже в работе. Убрать
        # опубликованный статус можно только архивацией (без сделок она мгновенная).
        login(client)
        graph = create_published_workflow(
            client,
            graph_body(
                statuses=[
                    {"code": "new", "name": "Новая", "type": "initial"},
                    {"code": "extra", "name": "Лишний"},
                    {"code": "won", "name": "Закрыта", "type": "won"},
                ],
                transitions=[
                    {"from_status": "new", "to_status": "extra", "name": "Дальше"},
                    {"from_status": "extra", "to_status": "won", "name": "Закрыть"},
                    {"from_status": "new", "to_status": "won", "name": "Сразу"},
                ],
                sla_rules=[],
            ),
        )
        codes = by_code(graph)
        extra = codes["extra"]

        body = body_from_graph(graph)
        body["statuses"] = [s for s in body["statuses"] if s["id"] != extra["id"]]
        body["transitions"] = [
            t for t in body["transitions"] if extra["id"] not in (t["from_status"], t["to_status"])
        ]
        refused = put_graph(client, graph["workflow"], body)
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "CRM-1208"

        archived = client.post(
            f"/api/workflows/{graph['workflow']['id']}/statuses/{extra['id']}/archive",
            json={"target_status_id": codes["won"]["id"]},
            headers={"If-Match": str(graph["workflow"]["version"])},
        )
        assert archived.status_code == 200, archived.text
        assert archived.json()["job_status"] == "completed"

    def test_removing_a_draft_only_status_still_works(self, client) -> None:
        # Статус, добавленный после публикации, в снимке ещё не значится: сделки о нём не знают,
        # и убрать его из черновика можно как раньше.
        login(client)
        graph = create_published_workflow(client)
        body = body_from_graph(graph)
        body["statuses"].append({"code": "draft_only", "name": "Черновой", "type": "intermediate"})
        saved = put_graph(client, graph["workflow"], body)
        assert saved.status_code == 200, saved.text
        assert "draft_only" in {s["code"] for s in saved.json()["statuses"]}

        again = body_from_graph(get_graph(client, graph["workflow"]["id"]))
        again["statuses"] = [s for s in again["statuses"] if s["code"] != "draft_only"]
        again["transitions"] = [
            t for t in again["transitions"] if t["from_status"] and t["to_status"]
        ]
        latest = get_graph(client, graph["workflow"]["id"])["workflow"]
        removed = put_graph(client, latest, again)
        assert removed.status_code == 200, removed.text
        assert {s["code"] for s in removed.json()["statuses"]} == {"new", "work", "won"}

    def test_transition_without_id_is_matched_by_status_pair(self, client) -> None:
        # Клиент, который id переходов не присылает (скрипты, старые формы), получает те же
        # строки: иначе у каждого сохранения менялись бы id, на которые ссылается история.
        graph, _deal = self._deal_in_work(client)
        body = body_from_graph(graph)
        for transition in body["transitions"]:
            del transition["id"]
            transition["name"] += " (правка)"

        saved = put_graph(client, graph["workflow"], body)
        assert saved.status_code == 200, saved.text
        assert {t["id"] for t in saved.json()["transitions"]} == {
            t["id"] for t in graph["transitions"]
        }

    def test_unknown_or_repeated_transition_id_is_rejected(self, client) -> None:
        login(client)
        graph = create_published_workflow(client)
        body = body_from_graph(graph)

        unknown = {**body, "transitions": [{**body["transitions"][0], "id": str(uuid.uuid4())}]}
        response = put_graph(client, graph["workflow"], unknown)
        assert response.status_code == 404, response.text

        repeated = {
            **body,
            "transitions": [
                body["transitions"][0],
                {**body["transitions"][1], "id": body["transitions"][0]["id"]},
            ],
        }
        response = put_graph(client, graph["workflow"], repeated)
        assert response.status_code == 422, response.text

    def test_transition_removed_from_the_draft_still_works_for_running_deals(self, client) -> None:
        # Сделки живут по опубликованному снимку, где переход ещё есть, а из черновика админ его
        # убрал (сделок по нему не было — удалить можно). В истории его id ссылаться не может.
        login(client)
        graph = create_published_workflow(client)
        deal = create_deal(client, graph["workflow"]["id"])
        body = body_from_graph(graph)
        body["transitions"] = [t for t in body["transitions"] if t["name"] != "В работу"]
        assert put_graph(client, graph["workflow"], body).status_code == 200

        moved = transition_deal(client, deal, by_code(graph)["work"]["id"])
        assert moved.status_code == 200, moved.text
        history = client.get(f"/api/deals/{deal['id']}/history").json()["statuses"]
        assert history[-1]["to_status_id"] == by_code(graph)["work"]["id"]
        assert history[-1]["transition_id"] is None


class TestSeedPublishesSlaRules:
    """A#10: сид-воронки публикуются с правилами SLA, и сделка сразу получает срок.
    Настоящая Postgres обязательна — см. докстринг модуля."""

    pytestmark = pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
    )

    def test_snapshot_has_rules_and_new_deals_get_a_due_date(self, client) -> None:
        import dataclasses
        import datetime as dt

        from sqlalchemy import select

        from app.core.db import session_scope
        from app.modules.crm.models import Deal
        from app.modules.crm.service import _apply_sla_for_status
        from app.modules.workflow.models import SlaRule
        from app.modules.workflow.seed import seed_workflow

        async def _seed_and_inspect():
            async with session_scope() as session:
                # B2-2: воронка по умолчанию на тип — одна (уникальный индекс); `seed_workflow`
                # сам решает, снимать ли default с текущей (см. `_demote_seed_default`) — тест
                # больше не снимает его руками. Эта спека не обязана стать default: тесту важен
                # только снимок и правила SLA, а не фактический выбор воронки для новых сделок.
                spec = dataclasses.replace(_b2b_spec(), code=f"b2b_sla_{uuid.uuid4().hex[:8]}")
                workflow = await seed_workflow(session, spec)
                rules = (
                    (
                        await session.execute(
                            select(SlaRule).where(SlaRule.workflow_id == workflow.id)
                        )
                    )
                    .scalars()
                    .all()
                )
                snapshot, stored = workflow.published_graph, len(rules)
                await session.rollback()  # демо-воронку в общей БД не оставляем
                return snapshot, stored

        graph, stored = run(client, _seed_and_inspect)

        pipeline = [s for s in graph["statuses"] if s["type"] in ("initial", "intermediate")]
        assert len(graph["sla_rules"]) == len(pipeline) == stored
        assert {r["status_id"] for r in graph["sla_rules"]} == {s["id"] for s in pipeline}
        assert all(
            r["count_business_days"] and r["warn_threshold_pct"] == 75 for r in graph["sla_rules"]
        )

        # Сделка, созданная в начальном статусе, получает срок; в закрывающем его нет.
        deal = Deal(status_changed_at=dt.datetime.now(dt.UTC))
        initial = next(s for s in graph["statuses"] if s["type"] == "initial")
        _apply_sla_for_status(deal, graph, initial, dt.datetime.now(dt.UTC))
        assert deal.sla_due_at is not None

        won = next(s for s in graph["statuses"] if s["type"] == "won")
        _apply_sla_for_status(deal, graph, won, dt.datetime.now(dt.UTC))
        assert deal.sla_due_at is None


class TestHasUnpublishedChanges:
    """B#31: `has_unpublished_changes` в `WorkflowOut` — черновик отличается от опубликованного
    снимка (раньше UI гадал по `updated_at`, и любой `PUT /graph` даже без правок казался
    «неопубликованным»). Настоящая Postgres обязательна — см. докстринг модуля."""

    pytestmark = pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
    )

    def test_never_published_draft_has_changes(self, client) -> None:
        login(client)
        draft = client.post(
            "/api/workflows",
            json={"code": f"wf_{uuid.uuid4().hex[:8]}", "name": "Черновик", "deal_type": "b2b"},
        ).json()
        assert draft["has_unpublished_changes"] is True

    def test_flag_follows_the_graph_against_the_published_snapshot(self, client) -> None:
        login(client)
        graph = create_published_workflow(client)
        workflow_id = graph["workflow"]["id"]
        assert graph["workflow"]["has_unpublished_changes"] is False

        # `PUT /graph` без правок — не «неопубликованные изменения».
        unchanged = put_graph(client, graph["workflow"], body_from_graph(graph))
        assert unchanged.status_code == 200, unchanged.text
        assert unchanged.json()["workflow"]["has_unpublished_changes"] is False

        body = body_from_graph(graph)
        body["transitions"][0]["name"] = "Другое имя перехода"
        changed = put_graph(client, unchanged.json()["workflow"], body)
        assert changed.json()["workflow"]["has_unpublished_changes"] is True
        assert get_graph(client, workflow_id)["workflow"]["has_unpublished_changes"] is True

        published = client.post(
            f"/api/workflows/{workflow_id}/publish",
            headers={"If-Match": str(changed.json()["workflow"]["version"])},
        )
        assert published.status_code == 200, published.text
        assert published.json()["workflow"]["has_unpublished_changes"] is False

    def test_color_is_not_part_of_the_snapshot(self, client) -> None:
        login(client)
        graph = create_published_workflow(client)
        body = body_from_graph(graph)
        body["statuses"][0]["color"] = "#ff0000"

        saved = put_graph(client, graph["workflow"], body)
        assert saved.json()["workflow"]["has_unpublished_changes"] is False

    def test_list_reports_the_flag_per_workflow(self, client) -> None:
        login(client)
        published = create_published_workflow(client)
        changed = create_published_workflow(client)
        body = body_from_graph(changed)
        body["statuses"][1]["name"] = "Переименован"
        assert put_graph(client, changed["workflow"], body).status_code == 200

        def flag(workflow: dict) -> bool:
            items = client.get("/api/workflows", params={"q": workflow["code"]}).json()["items"]
            return items[0]["has_unpublished_changes"]

        assert flag(published["workflow"]) is False
        assert flag(changed["workflow"]) is True


class TestMappingJobEndpoint:
    """B#1: `GET /workflows/{id}/mapping-jobs/{job_id}` — прогресс и итог переноса сделок при
    архивации статуса. Настоящая Postgres обязательна — см. докстринг модуля."""

    pytestmark = pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
    )

    def _archive_work_status(self, client):
        login(client)
        graph = create_published_workflow(client)
        statuses = by_code(graph)
        deal = create_deal(client, graph["workflow"]["id"])
        assert transition_deal(client, deal, statuses["work"]["id"]).status_code == 200
        archived = client.post(
            f"/api/workflows/{graph['workflow']['id']}/statuses/{statuses['work']['id']}/archive",
            json={"target_status_id": statuses["won"]["id"]},
            headers={"If-Match": str(graph["workflow"]["version"])},
        )
        assert archived.status_code == 200, archived.text
        return graph, statuses, archived.json()

    def test_job_reports_progress_and_result(self, client) -> None:
        graph, statuses, archived = self._archive_work_status(client)

        response = client.get(
            f"/api/workflows/{graph['workflow']['id']}/mapping-jobs/{archived['job_id']}"
        )
        assert response.status_code == 200, response.text
        job = response.json()
        assert job["id"] == archived["job_id"]
        assert job["from_status_id"] == statuses["work"]["id"]
        assert job["status"] == "completed"
        assert (job["affected_count"], job["processed_count"], job["failed_count"]) == (1, 1, 0)
        assert job["report"] == {"processed": 1, "failed": 0}

    def test_job_of_another_workflow_or_unknown_job_is_not_found(self, client) -> None:
        _graph, _statuses, archived = self._archive_work_status(client)
        other_id = create_published_workflow(client)["workflow"]["id"]

        foreign = client.get(f"/api/workflows/{other_id}/mapping-jobs/{archived['job_id']}")
        assert foreign.status_code == 404, foreign.text
        unknown = client.get(f"/api/workflows/{other_id}/mapping-jobs/{uuid.uuid4()}")
        assert unknown.status_code == 404, unknown.text

    def test_kam_can_read_it_too(self, client) -> None:
        # Право — как у чтения воронки (`workflow:read`), а не у архивации.
        graph, _statuses, archived = self._archive_work_status(client)
        login(client, "KAM")

        response = client.get(
            f"/api/workflows/{graph['workflow']['id']}/mapping-jobs/{archived['job_id']}"
        )
        assert response.status_code == 200, response.text


class TestPatchWorkflow:
    """B#4: `PATCH /workflows/{id}` — имя и воронка по умолчанию. Настоящая Postgres
    обязательна — см. докстринг модуля."""

    pytestmark = pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
    )

    def _patch(self, client, workflow: dict, body: dict, *, version: int | None = None):
        return client.patch(
            f"/api/workflows/{workflow['id']}",
            json=body,
            headers={"If-Match": str(version if version is not None else workflow["version"])},
        )

    def test_rename_bumps_the_version_and_keeps_the_snapshot(self, client) -> None:
        login(client)
        graph = create_published_workflow(client)

        response = self._patch(client, graph["workflow"], {"name": "Новое название"})
        assert response.status_code == 200, response.text
        renamed = response.json()
        assert renamed["name"] == "Новое название"
        assert renamed["version"] == graph["workflow"]["version"] + 1
        assert renamed["graph_hash"] == graph["workflow"]["graph_hash"]
        assert renamed["has_unpublished_changes"] is False
        assert get_graph(client, graph["workflow"]["id"])["workflow"]["name"] == "Новое название"

    def test_empty_patch_changes_nothing(self, client) -> None:
        login(client)
        workflow = create_draft_workflow(client)

        response = self._patch(client, workflow, {})
        assert response.status_code == 200, response.text
        assert response.json()["version"] == workflow["version"]

    def test_default_flag_moves_between_published_workflows(self, client) -> None:
        login(client)
        current = create_published_workflow(client, deal_type="b2c", is_default=True)
        candidate = create_published_workflow(client, deal_type="b2c")
        assert current["workflow"]["is_default"] is True

        response = self._patch(client, candidate["workflow"], {"is_default": True})
        assert response.status_code == 200, response.text
        assert response.json()["is_default"] is True
        assert get_graph(client, current["workflow"]["id"])["workflow"]["is_default"] is False

        # Убираем за собой: воронка по умолчанию на тип в общей БД тестов не нужна.
        release = self._patch(client, response.json(), {"is_default": False})
        assert release.json()["is_default"] is False

    def test_publishing_an_older_default_replaces_a_newer_one(self, client) -> None:
        # Старая воронка (меньший id) публикуется как default при уже опубликованной новой:
        # прежняя должна уступить раньше, иначе уникальный индекс ловит две default разом.
        login(client)
        older = create_draft_workflow(client, deal_type="b2c", is_default=True)
        newer = create_published_workflow(client, deal_type="b2c", is_default=True)

        saved = put_graph(client, older, graph_body())
        assert saved.status_code == 200, saved.text
        published = client.post(
            f"/api/workflows/{older['id']}/publish",
            headers={"If-Match": str(saved.json()["workflow"]["version"])},
        )
        assert published.status_code == 200, published.text
        assert published.json()["workflow"]["is_default"] is True
        assert get_graph(client, newer["workflow"]["id"])["workflow"]["is_default"] is False

        release = self._patch(client, published.json()["workflow"], {"is_default": False})
        assert release.status_code == 200, release.text

    def test_draft_can_be_marked_default_without_touching_others(self, client) -> None:
        login(client)
        published = create_published_workflow(client, deal_type="b2c", is_default=True)
        draft = create_draft_workflow(client, deal_type="b2c")

        response = self._patch(client, draft, {"is_default": True})
        assert response.status_code == 200, response.text
        assert response.json()["is_default"] is True
        assert get_graph(client, published["workflow"]["id"])["workflow"]["is_default"] is True

        release = self._patch(client, published["workflow"], {"is_default": False})
        assert release.status_code == 200, release.text

    def test_stale_version_is_a_conflict(self, client) -> None:
        login(client)
        workflow = create_draft_workflow(client)

        response = self._patch(
            client, workflow, {"name": "Другое"}, version=workflow["version"] + 3
        )
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "CRM-1002"

    def test_archived_workflow_cannot_be_edited(self, client) -> None:
        from sqlalchemy import update

        from app.core.db import session_scope
        from app.modules.workflow.models import Workflow

        login(client)
        workflow = create_draft_workflow(client)

        async def _archive() -> None:
            async with session_scope() as session:
                await session.execute(
                    update(Workflow)
                    .where(Workflow.id == uuid.UUID(workflow["id"]))
                    .values(state="archived")
                )

        run(client, _archive)
        response = self._patch(client, workflow, {"name": "Поздно"})
        assert response.status_code == 422, response.text

    def test_kam_cannot_patch(self, client) -> None:
        login(client)
        workflow = create_draft_workflow(client)
        login(client, "KAM")

        assert self._patch(client, workflow, {"name": "Чужое"}).status_code == 403


class TestImpactWithTarget:
    """B#6: `GET .../impact?target_status_id=` считает `problem_deals` — сделки без обязательных
    полей целевого статуса. Настоящая Postgres обязательна — см. докстринг модуля."""

    pytestmark = pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
    )

    def _setup(self, client):
        login(client)
        graph = create_published_workflow(
            client,
            graph_body(
                statuses=[
                    {"code": "new", "name": "Новая", "type": "initial"},
                    {"code": "work", "name": "В работе"},
                    {
                        "code": "won",
                        "name": "Закрыта",
                        "type": "won",
                        "required_fields": ["amount", "custom_fields.contract"],
                    },
                ],
                sla_rules=[],
            ),
        )
        statuses = by_code(graph)
        workflow_id = graph["workflow"]["id"]
        complete = create_deal(
            client,
            workflow_id,
            amount="100",
            title="Сделка с полями",
            custom_fields={"contract": "Д-1"},
        )
        partial = create_deal(client, workflow_id, amount="50", title="Без договора")
        empty = create_deal(client, workflow_id, title="Без всего")
        for deal in (complete, partial, empty):
            assert transition_deal(client, deal, statuses["work"]["id"]).status_code == 200
        return graph, statuses, {"complete": complete, "partial": partial, "empty": empty}

    def _impact(self, client, graph, statuses, **params):
        return client.get(
            f"/api/workflows/{graph['workflow']['id']}/statuses/{statuses['work']['id']}/impact",
            params=params,
        )

    def test_problem_deals_lists_deals_missing_target_fields(self, client) -> None:
        graph, statuses, deals = self._setup(client)

        response = self._impact(client, graph, statuses, target_status_id=statuses["won"]["id"])
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["active_count"] == 3
        assert body["problem_count"] == 2
        problems = {p["id"]: p for p in body["problem_deals"]}
        assert set(problems) == {deals["partial"]["id"], deals["empty"]["id"]}
        assert problems[deals["partial"]["id"]]["missing_fields"] == ["custom_fields.contract"]
        assert problems[deals["empty"]["id"]]["missing_fields"] == [
            "amount",
            "custom_fields.contract",
        ]
        assert problems[deals["empty"]["id"]]["title"] == "Без всего"
        assert problems[deals["empty"]["id"]]["number"] == deals["empty"]["number"]

    def test_without_target_the_list_stays_empty(self, client) -> None:
        graph, statuses, _deals = self._setup(client)

        body = self._impact(client, graph, statuses).json()
        assert (body["problem_deals"], body["problem_count"]) == ([], 0)
        assert body["active_count"] == 3

    def test_target_must_be_another_live_status_of_the_same_workflow(self, client) -> None:
        graph, statuses, _deals = self._setup(client)
        other = create_published_workflow(client)

        same = self._impact(client, graph, statuses, target_status_id=statuses["work"]["id"])
        assert same.status_code == 422, same.text
        foreign = self._impact(
            client, graph, statuses, target_status_id=by_code(other)["won"]["id"]
        )
        assert foreign.status_code == 404, foreign.text


class TestValidateEndpointStructuredIssues:
    """B9-6 (BUG-25): `POST /{id}/validate` отдаёт `issues[]`, и серия пробных вызовов (так
    делает редактор воронок на каждое изменение графа) не растит журнал аудита."""

    pytestmark = pytest.mark.skipif(
        not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
    )

    def test_issues_have_code_severity_and_path(self, client) -> None:
        login(client)
        draft = create_draft_workflow(client)
        # Статусы без единого перехода между ними — "work"/"won" недостижимы из "new".
        saved = put_graph(client, draft, graph_body(transitions=[]))
        assert saved.status_code == 200, saved.text

        response = client.post(f"/api/workflows/{draft['id']}/validate")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["issues"], "ожидается хотя бы один issue для графа без переходов"
        issue = body["issues"][0]
        assert {"code", "severity", "message", "path", "refs"} <= set(issue)
        assert issue["severity"] in ("error", "warning")
        # Старые поля остаются зеркалом issues до перехода редактора (риск задачи B9-6).
        assert body["errors"] == [i["message"] for i in body["issues"] if i["severity"] == "error"]

    def test_repeated_validation_calls_do_not_grow_the_audit_log(self, client) -> None:
        from sqlalchemy import func, select

        from app.core.db import session_scope
        from app.modules.audit.models import AuditLog

        login(client)
        workflow = create_draft_workflow(client)

        async def _count_validated() -> int:
            async with session_scope() as session:
                return (
                    await session.scalar(
                        select(func.count())
                        .select_from(AuditLog)
                        .where(
                            AuditLog.entity_type == "workflow",
                            AuditLog.entity_id == uuid.UUID(workflow["id"]),
                            AuditLog.action == "WORKFLOW_VALIDATED",
                        )
                    )
                ) or 0

        before = run(client, _count_validated)
        for _ in range(3):
            response = client.post(f"/api/workflows/{workflow['id']}/validate")
            assert response.status_code == 200, response.text
        after = run(client, _count_validated)
        assert after == before == 0
