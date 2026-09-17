"""Тесты конструктора воронок (спринт 2, раздел 6.5).

Проверяется то, что ломается молча: пропуск обязательного статуса в белом
списке DSL превращает конструктор в дыру, забытая проверка достижимости
пропускает статус-ловушку в продакшн, а собственные сиды воронок обязаны
проходить тот же валидатор, что и ручные графы администратора.

Тесты не требуют поднятых PostgreSQL, Redis и Keycloak: `_validate_graph_data`
и DSL — чистые функции, а ORM-объекты здесь используются как обычные Python
инстансы, без сессии и без похода в БД.
"""

from __future__ import annotations

import uuid

from app.core.permissions import Permission, has_permission
from app.modules.workflow import dsl
from app.modules.workflow.models import (
    StatusType,
    WorkflowState,
    WorkflowStatus,
    WorkflowTransition,
)
from app.modules.workflow.seed import _b2b_spec, _b2c_spec
from app.modules.workflow.service import _validate_graph_data


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


class TestGraphValidation:
    def test_requires_exactly_one_initial(self) -> None:
        a = _status("a", StatusType.INITIAL.value)
        b = _status("b", StatusType.INITIAL.value)
        won = _status("won", StatusType.WON.value)
        transitions = [_transition(a, won), _transition(b, won)]
        errors, _ = _validate_graph_data([a, b, won], transitions)
        assert any("initial" in e for e in errors)

    def test_requires_at_least_one_terminal(self) -> None:
        a = _status("a", StatusType.INITIAL.value)
        b = _status("b", StatusType.INTERMEDIATE.value)
        errors, _ = _validate_graph_data([a, b], [_transition(a, b)])
        assert any("терминальный" in e for e in errors)

    def test_detects_unreachable_status(self) -> None:
        a = _status("a", StatusType.INITIAL.value)
        b = _status("b", StatusType.INTERMEDIATE.value)
        orphan = _status("orphan", StatusType.INTERMEDIATE.value)
        won = _status("won", StatusType.WON.value)
        transitions = [_transition(a, b), _transition(b, won)]
        errors, _ = _validate_graph_data([a, b, orphan, won], transitions)
        assert any("Недостижимые" in e and "orphan" in e for e in errors)

    def test_detects_trap_status(self) -> None:
        # `b` достижим из initial, но из него нет пути ни в один терминал.
        a = _status("a", StatusType.INITIAL.value)
        b = _status("b", StatusType.INTERMEDIATE.value)
        won = _status("won", StatusType.WON.value)
        transitions = [_transition(a, b)]
        errors, _ = _validate_graph_data([a, b, won], transitions)
        assert any("ловушки" in e and "b" in e for e in errors)

    def test_archived_status_and_its_transitions_are_excluded(self) -> None:
        # Архивный статус не обязан быть достижимым и не создаёт ловушку —
        # он больше не часть действующего графа.
        a = _status("a", StatusType.INITIAL.value)
        won = _status("won", StatusType.WON.value)
        archived = _status("old", StatusType.INTERMEDIATE.value, archived=True)
        transitions = [_transition(a, won), _transition(a, archived)]
        errors, _ = _validate_graph_data([a, won, archived], transitions)
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
        errors, _ = _validate_graph_data([a, b, won], [bad_transition, _transition(b, won)])
        assert any("on_rejected" in e for e in errors)

    def test_lost_transition_without_reason_condition_warns_not_errors(self) -> None:
        a = _status("a", StatusType.INITIAL.value)
        lost = _status("lost", StatusType.LOST.value)
        errors, warnings = _validate_graph_data([a, lost], [_transition(a, lost)])
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
        _, warnings = _validate_graph_data([a, lost], [t])
        assert warnings == []

    def test_condition_errors_include_transition_name_for_debugging(self) -> None:
        a = _status("a", StatusType.INITIAL.value)
        won = _status("won", StatusType.WON.value)
        bad = _transition(
            a, won, name="bad", conditions={"field": "nope", "op": "eq", "value": 1}
        )
        errors, _ = _validate_graph_data([a, won], [bad])
        assert any("bad" in e for e in errors)


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
                workflow_id=workflow_id, code=s.code, name=s.name, type=s.type,
                sort_order=s.sort_order, required_fields=s.required_fields,
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
        errors, warnings = _validate_graph_data(statuses, transitions)
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
        errors, warnings = _validate_graph_data(statuses, transitions)
        assert errors == []
        assert warnings == []

    def test_no_duplicate_transition_pairs_in_seeds(self) -> None:
        for spec in (_b2b_spec(), _b2c_spec()):
            pairs = [(t.from_code, t.to_code) for t in spec.transitions]
            assert len(pairs) == len(set(pairs)), spec.code


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


class TestWorkflowState:
    def test_states_match_spec_enum(self) -> None:
        assert {s.value for s in WorkflowState} == {"draft", "published", "archived"}
