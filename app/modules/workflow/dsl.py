"""DSL условий и действий переходов (раздел 8).

Условия — данные, а не код. Здесь живёт декларативный интерпретатор: он
валидирует выражение при сохранении графа и вычисляет его при переходе.

Два требования безопасности, вокруг которых построен модуль:

* **белый список полей**. Произвольное имя поля превратило бы конструктор в
  дыру: администратор описал бы условие по чужому полю, а интерпретатор
  прочитал бы его из проекции сделки. Поэтому поле обязано быть либо в
  точном списке, либо в разрешённом префиксе (`custom_fields.*`,
  `attachments.*`);
* **отсутствующее значение не ломает вычисление**. Условие `amount > 0` при
  пустой сумме должно дать «не выполнено», а не 500 из-за сравнения `None`
  с числом.

Ограничение вложенности и числа листьев тоже осознанное: выражение приходит
из внешнего JSON, и его вычисление не должно зависеть от глубины дерева.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any

# --- Белый список полей ---------------------------------------------------

#: Поля проекции сделки, доступные условиям (раздел 8).
ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "amount",
        "currency",
        "owner_id",
        "organization_id",
        "contact_id",
        "priority",
        "loss_reason_id",
        "expected_close_date",
        "signature_status",
        "students_planned",
        "tasks.open_count",
        "products.count",
    }
)

#: Префиксы, внутри которых имя задаёт администратор: пользовательские поля и
#: категории вложений. Дальше одного сегмента вложенность не разрешена.
ALLOWED_FIELD_PREFIXES: tuple[str, ...] = ("custom_fields.", "attachments.")

#: Категории вложений из раздела 1 (contract, act, license, presentation, other).
ATTACHMENT_CATEGORIES: frozenset[str] = frozenset(
    {"contract", "act", "license", "presentation", "other"}
)

MAX_CONDITION_DEPTH = 5
MAX_CONDITION_LEAVES = 50


class Operator(StrEnum):
    EQ = "eq"
    NEQ = "neq"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    NOT_NULL = "not_null"
    IS_NULL = "is_null"
    IN = "in"
    NOT_IN = "not_in"
    CONTAINS = "contains"
    EXISTS = "exists"
    DATE_BEFORE = "date_before"
    DATE_AFTER = "date_after"


#: Операторы, которым значение не нужно (и запрещено: лишний `value` почти
#: всегда означает, что автор имел в виду другой оператор).
_VALUELESS = frozenset({Operator.NOT_NULL, Operator.IS_NULL, Operator.EXISTS})
_LIST_VALUED = frozenset({Operator.IN, Operator.NOT_IN})
_NUMERIC = frozenset({Operator.GT, Operator.GTE, Operator.LT, Operator.LTE})
_DATE = frozenset({Operator.DATE_BEFORE, Operator.DATE_AFTER})


# --- Действия -------------------------------------------------------------


class ActionType(StrEnum):
    CREATE_TASK = "create_task"
    NOTIFY = "notify"
    REQUEST_SIGNATURE = "request_signature"
    # Раздел 7: шаги lms_transfer и training_launch публикуют события в LMS.
    INTEGRATION_EVENT = "integration_event"


NOTIFY_CHANNELS: frozenset[str] = frozenset({"in_app", "email", "telegram"})
NOTIFY_RECIPIENTS: frozenset[str] = frozenset(
    {"owner", "manager", "participants", "team_head", "initiator", "contact"}
)
TASK_PRIORITIES: frozenset[str] = frozenset({"low", "normal", "high", "critical"})
SIGNATURE_ORDERS: frozenset[str] = frozenset({"sequential", "parallel"})
ON_EXPIRED: frozenset[str] = frozenset({"notify_initiator", "void", "previous_status"})
INTEGRATION_EVENTS: frozenset[str] = frozenset(
    {
        "LEARNING_TRANSFER_REQUESTED",
        "LEARNING_ENROLLMENT_SENT",
        "LEARNING_PROGRESS_REQUESTED",
        "CMS_LEAD_ACCEPTED",
    }
)
ROLES: frozenset[str] = frozenset({"KAM", "HEAD", "ADMIN", "AUDITOR", "INTEGRATION"})


@dataclass(slots=True)
class Unmet:
    """Невыполненный лист условия — уходит в тело ошибки CRM-1201."""

    field: str
    op: str
    expected: Any = None
    actual: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "op": self.op,
            "expected": self.expected,
            "actual": self.actual,
        }


@dataclass(slots=True)
class Evaluation:
    ok: bool
    unmet: list[Unmet] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "unmet": [item.as_dict() for item in self.unmet]}


# --- Валидация ------------------------------------------------------------


def validate_condition(
    node: Any,
    *,
    path: str = "conditions",
    known_custom_fields: frozenset[str] | None = None,
    _depth: int = 1,
    _counter: list[int] | None = None,
) -> list[str]:
    """Проверяет выражение и возвращает список человекочитаемых ошибок.

    `known_custom_fields` — коды из `custom_field_defs`. Если справочник
    передан, условие по несуществующему полю считается ошибкой: раздел 6.5
    требует проверять существование полей при валидации графа.
    """
    errors: list[str] = []
    counter = _counter if _counter is not None else [0]

    if node in (None, {}):
        return errors
    if not isinstance(node, Mapping):
        return [f"{path}: ожидается объект условия"]
    if _depth > MAX_CONDITION_DEPTH:
        return [f"{path}: превышена допустимая вложенность условий ({MAX_CONDITION_DEPTH})"]

    keys = set(node.keys())
    if keys & {"all", "any"}:
        if len(keys) > 1:
            errors.append(f"{path}: узел должен содержать только один из ключей all или any")
        for group in ("all", "any"):
            if group not in node:
                continue
            branches = node[group]
            if not isinstance(branches, Sequence) or isinstance(branches, str | bytes):
                errors.append(f"{path}.{group}: ожидается массив условий")
                continue
            if not branches:
                errors.append(f"{path}.{group}: пустой массив условий")
            for index, branch in enumerate(branches):
                errors.extend(
                    validate_condition(
                        branch,
                        path=f"{path}.{group}[{index}]",
                        known_custom_fields=known_custom_fields,
                        _depth=_depth + 1,
                        _counter=counter,
                    )
                )
        return errors

    # Лист условия.
    counter[0] += 1
    if counter[0] > MAX_CONDITION_LEAVES:
        return [f"{path}: слишком много условий в одном переходе (>{MAX_CONDITION_LEAVES})"]

    unexpected = keys - {"field", "op", "value"}
    if unexpected:
        errors.append(f"{path}: неизвестные ключи условия: {', '.join(sorted(unexpected))}")

    field_name = node.get("field")
    if not isinstance(field_name, str) or not field_name:
        errors.append(f"{path}.field: обязательное строковое имя поля")
    else:
        errors.extend(_validate_field_name(field_name, path, known_custom_fields))

    raw_op = node.get("op")
    try:
        operator = Operator(raw_op)
    except ValueError:
        errors.append(
            f"{path}.op: неизвестный оператор {raw_op!r}; "
            f"допустимы {', '.join(sorted(o.value for o in Operator))}"
        )
        return errors

    has_value = "value" in node
    if operator in _VALUELESS and has_value:
        errors.append(f"{path}: оператор {operator.value} не принимает value")
    if operator not in _VALUELESS and not has_value:
        errors.append(f"{path}: оператор {operator.value} требует value")

    value = node.get("value")
    if operator in _LIST_VALUED and not isinstance(value, list):
        errors.append(f"{path}.value: оператор {operator.value} требует массив значений")
    if operator in _NUMERIC and has_value and _to_decimal(value) is None:
        errors.append(f"{path}.value: оператор {operator.value} требует число")
    if operator in _DATE and has_value and _to_date(value) is None:
        errors.append(f"{path}.value: оператор {operator.value} требует дату в формате ISO 8601")

    return errors


def _validate_field_name(
    field_name: str, path: str, known_custom_fields: frozenset[str] | None
) -> list[str]:
    if field_name in ALLOWED_FIELDS:
        return []

    for prefix in ALLOWED_FIELD_PREFIXES:
        if not field_name.startswith(prefix):
            continue
        tail = field_name[len(prefix) :]
        if not tail or "." in tail:
            return [f"{path}.field: после {prefix} ожидается одно имя без точек"]
        if prefix == "attachments." and tail not in ATTACHMENT_CATEGORIES:
            return [
                f"{path}.field: неизвестная категория вложения {tail!r}; "
                f"допустимы {', '.join(sorted(ATTACHMENT_CATEGORIES))}"
            ]
        if (
            prefix == "custom_fields."
            and known_custom_fields is not None
            and tail not in known_custom_fields
        ):
            return [f"{path}.field: пользовательское поле {tail!r} не определено"]
        return []

    return [
        f"{path}.field: поле {field_name!r} не разрешено в условиях; "
        "список полей закрыт (раздел 8)"
    ]


def validate_actions(actions: Any, *, path: str = "actions") -> list[str]:
    """Проверяет массив действий перехода."""
    if actions in (None, []):
        return []
    if not isinstance(actions, Sequence) or isinstance(actions, str | bytes):
        return [f"{path}: ожидается массив действий"]

    errors: list[str] = []
    for index, action in enumerate(actions):
        item_path = f"{path}[{index}]"
        if not isinstance(action, Mapping):
            errors.append(f"{item_path}: ожидается объект действия")
            continue
        try:
            action_type = ActionType(action.get("type"))
        except ValueError:
            errors.append(
                f"{item_path}.type: неизвестное действие {action.get('type')!r}; "
                f"допустимы {', '.join(sorted(a.value for a in ActionType))}"
            )
            continue
        errors.extend(_VALIDATORS[action_type](action, item_path))
    return errors


def _validate_create_task(action: Mapping[str, Any], path: str) -> list[str]:
    allowed = {"type", "title", "assignee_role", "assignee", "due_days", "priority"}
    errors = _reject_unknown(action, allowed, path)
    if not _non_empty_str(action.get("title")):
        errors.append(f"{path}.title: обязательный непустой заголовок задачи")
    role = action.get("assignee_role")
    if role is not None and role not in ROLES:
        errors.append(f"{path}.assignee_role: неизвестная роль {role!r}")
    assignee = action.get("assignee")
    if assignee is not None and assignee not in {"owner", "manager", "initiator"}:
        errors.append(f"{path}.assignee: допустимы owner, manager, initiator")
    if role is None and assignee is None:
        errors.append(f"{path}: нужно указать assignee_role или assignee")
    due_days = action.get("due_days")
    if due_days is not None and not (isinstance(due_days, int) and 0 < due_days <= 365):
        errors.append(f"{path}.due_days: целое число от 1 до 365")
    priority = action.get("priority")
    if priority is not None and priority not in TASK_PRIORITIES:
        errors.append(f"{path}.priority: допустимы {', '.join(sorted(TASK_PRIORITIES))}")
    return errors


def _validate_notify(action: Mapping[str, Any], path: str) -> list[str]:
    errors = _reject_unknown(action, {"type", "event_code", "channels", "recipients"}, path)
    if not _non_empty_str(action.get("event_code")):
        errors.append(f"{path}.event_code: обязательный код события уведомления")
    errors.extend(_validate_enum_list(action.get("channels"), NOTIFY_CHANNELS, f"{path}.channels"))
    errors.extend(
        _validate_enum_list(action.get("recipients"), NOTIFY_RECIPIENTS, f"{path}.recipients")
    )
    return errors


def _validate_request_signature(action: Mapping[str, Any], path: str) -> list[str]:
    errors = _reject_unknown(
        action,
        {"type", "template", "signers", "order", "deadline_days", "on_rejected", "on_expired"},
        path,
    )
    if not _non_empty_str(action.get("template")):
        errors.append(f"{path}.template: обязательный код шаблона документа")

    signers = action.get("signers")
    if not isinstance(signers, list) or not signers:
        errors.append(f"{path}.signers: требуется непустой массив подписантов")
    else:
        for index, signer in enumerate(signers):
            signer_path = f"{path}.signers[{index}]"
            if not isinstance(signer, Mapping):
                errors.append(f"{signer_path}: ожидается объект подписанта")
                continue
            keys = set(signer.keys())
            if keys - {"role", "contact_role", "user_id"}:
                errors.append(f"{signer_path}: допустимы role, contact_role, user_id")
            if not keys & {"role", "contact_role", "user_id"}:
                errors.append(f"{signer_path}: нужно указать role, contact_role или user_id")
            if "role" in signer and signer["role"] not in ROLES:
                errors.append(f"{signer_path}.role: неизвестная роль {signer['role']!r}")

    order = action.get("order")
    if order is not None and order not in SIGNATURE_ORDERS:
        errors.append(f"{path}.order: допустимы {', '.join(sorted(SIGNATURE_ORDERS))}")
    deadline = action.get("deadline_days")
    if deadline is not None and not (isinstance(deadline, int) and 0 < deadline <= 365):
        errors.append(f"{path}.deadline_days: целое число от 1 до 365")
    on_expired = action.get("on_expired")
    if on_expired is not None and on_expired not in ON_EXPIRED:
        errors.append(f"{path}.on_expired: допустимы {', '.join(sorted(ON_EXPIRED))}")
    # `on_rejected` — либо `previous_status`, либо код статуса этой воронки.
    # Существование кода проверяет валидатор графа, здесь только тип.
    on_rejected = action.get("on_rejected")
    if on_rejected is not None and not _non_empty_str(on_rejected):
        errors.append(f"{path}.on_rejected: previous_status или код статуса воронки")
    return errors


def _validate_integration_event(action: Mapping[str, Any], path: str) -> list[str]:
    errors = _reject_unknown(action, {"type", "event_code", "payload"}, path)
    event_code = action.get("event_code")
    if event_code not in INTEGRATION_EVENTS:
        errors.append(
            f"{path}.event_code: неизвестное событие {event_code!r}; "
            f"допустимы {', '.join(sorted(INTEGRATION_EVENTS))}"
        )
    payload = action.get("payload")
    if payload is not None and not isinstance(payload, Mapping):
        errors.append(f"{path}.payload: ожидается объект")
    return errors


_VALIDATORS = {
    ActionType.CREATE_TASK: _validate_create_task,
    ActionType.NOTIFY: _validate_notify,
    ActionType.REQUEST_SIGNATURE: _validate_request_signature,
    ActionType.INTEGRATION_EVENT: _validate_integration_event,
}


def _reject_unknown(action: Mapping[str, Any], allowed: set[str], path: str) -> list[str]:
    """Опечатка в имени ключа не должна молча превращать действие в пустышку."""
    unexpected = set(action.keys()) - allowed
    if unexpected:
        return [f"{path}: неизвестные ключи: {', '.join(sorted(unexpected))}"]
    return []


def _validate_enum_list(value: Any, allowed: frozenset[str], path: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not value:
        return [f"{path}: требуется непустой массив"]
    unknown = [item for item in value if item not in allowed]
    if unknown:
        return [f"{path}: недопустимые значения {unknown}; допустимы {', '.join(sorted(allowed))}"]
    return []


def _non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def signature_actions(actions: Any) -> list[Mapping[str, Any]]:
    """Действия `request_signature` перехода — нужны валидатору графа."""
    if not isinstance(actions, Sequence) or isinstance(actions, str | bytes):
        return []
    return [
        action
        for action in actions
        if isinstance(action, Mapping)
        and action.get("type") == ActionType.REQUEST_SIGNATURE.value
    ]


# --- Вычисление -----------------------------------------------------------


def evaluate(node: Any, context: Mapping[str, Any]) -> Evaluation:
    """Вычисляет выражение на проекции сделки.

    Пустое условие истинно. Отсутствующее поле не бросает исключение: лист
    просто не выполняется, и его описание уходит в `unmet`, чтобы фронтенд
    показал, чего не хватает для перехода.
    """
    if node in (None, {}):
        return Evaluation(ok=True)
    if not isinstance(node, Mapping):
        return Evaluation(ok=False, unmet=[Unmet(field="conditions", op="malformed")])

    if "all" in node:
        unmet: list[Unmet] = []
        for branch in node["all"]:
            result = evaluate(branch, context)
            unmet.extend(result.unmet)
        return Evaluation(ok=not unmet, unmet=unmet)

    if "any" in node:
        branches = list(node["any"])
        collected: list[Unmet] = []
        for branch in branches:
            result = evaluate(branch, context)
            if result.ok:
                return Evaluation(ok=True)
            collected.extend(result.unmet)
        # Дизъюнкция пустого множества ложна: нет варианта, который мог бы
        # быть истинным. (`validate_condition` в любом случае запрещает
        # пустой `any` при сохранении графа — здесь просто защита.)
        return Evaluation(ok=False, unmet=collected)

    return _evaluate_leaf(node, context)


def _evaluate_leaf(node: Mapping[str, Any], context: Mapping[str, Any]) -> Evaluation:
    field_name = str(node.get("field", ""))
    try:
        operator = Operator(node.get("op"))
    except ValueError:
        return Evaluation(ok=False, unmet=[Unmet(field=field_name, op=str(node.get("op")))])

    expected = node.get("value")
    actual = resolve_field(field_name, context)
    ok = _apply(operator, actual, expected)
    if ok:
        return Evaluation(ok=True)
    return Evaluation(
        ok=False,
        unmet=[Unmet(field=field_name, op=operator.value, expected=expected, actual=actual)],
    )


def resolve_field(field_name: str, context: Mapping[str, Any]) -> Any:
    """Достаёт значение по точечному пути, не падая на отсутствующих ветках."""
    current: Any = context
    for part in field_name.split("."):
        if isinstance(current, Mapping):
            current = current.get(part)
        else:
            return None
    return current


def _apply(operator: Operator, actual: Any, expected: Any) -> bool:  # noqa: PLR0911
    match operator:
        case Operator.IS_NULL:
            return _is_empty(actual)
        case Operator.NOT_NULL:
            return not _is_empty(actual)
        case Operator.EXISTS:
            # Для `attachments.contract` и коллекций «существует» означает
            # непустое значение, а не «ключ присутствует».
            return _exists(actual)
        case Operator.EQ:
            return _equals(actual, expected)
        case Operator.NEQ:
            return not _equals(actual, expected)
        case Operator.IN:
            return isinstance(expected, list) and any(_equals(actual, item) for item in expected)
        case Operator.NOT_IN:
            return isinstance(expected, list) and not any(
                _equals(actual, item) for item in expected
            )
        case Operator.CONTAINS:
            return _contains(actual, expected)
        case Operator.GT | Operator.GTE | Operator.LT | Operator.LTE:
            return _compare_numbers(operator, actual, expected)
        case Operator.DATE_BEFORE | Operator.DATE_AFTER:
            return _compare_dates(operator, actual, expected)
    return False


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _exists(value: Any) -> bool:
    if _is_empty(value):
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float | Decimal):
        return value != 0
    if isinstance(value, Sequence | Mapping | set):
        return len(value) > 0
    return True


def _equals(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return bool(actual) is bool(expected)
    left, right = _to_decimal(actual), _to_decimal(expected)
    if left is not None and right is not None:
        return left == right
    if actual is None or expected is None:
        return actual is expected
    return str(actual) == str(expected)


def _contains(actual: Any, expected: Any) -> bool:
    if actual is None or expected is None:
        return False
    if isinstance(actual, Mapping):
        return str(expected) in {str(key) for key in actual}
    if isinstance(actual, Sequence) and not isinstance(actual, str | bytes):
        return any(_equals(item, expected) for item in actual)
    return str(expected).lower() in str(actual).lower()


def _compare_numbers(operator: Operator, actual: Any, expected: Any) -> bool:
    left, right = _to_decimal(actual), _to_decimal(expected)
    if left is None or right is None:
        # Незаполненная сумма — это «условие не выполнено», а не ошибка.
        return False
    match operator:
        case Operator.GT:
            return left > right
        case Operator.GTE:
            return left >= right
        case Operator.LT:
            return left < right
        case Operator.LTE:
            return left <= right
    return False


def _compare_dates(operator: Operator, actual: Any, expected: Any) -> bool:
    left, right = _to_date(actual), _to_date(expected)
    if left is None or right is None:
        return False
    if operator is Operator.DATE_BEFORE:
        return left < right
    return left > right


def _to_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | float):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except (InvalidOperation, ValueError):
            return None
    return None


def _to_date(value: Any) -> dt.datetime | None:
    """Приводит значение к datetime в UTC.

    Даты в условиях приходят строками (`"2026-09-01"` или ISO 8601 с
    таймзоной), а из проекции сделки — уже объектами. Сравнивать naive и
    aware datetime нельзя: это TypeError, поэтому naive считаем UTC.
    """
    parsed: dt.datetime | None = None
    if isinstance(value, dt.datetime):
        parsed = value
    elif isinstance(value, dt.date):
        parsed = dt.datetime.combine(value, dt.time.min)
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw in {"today", "now"}:
            return dt.datetime.now(dt.UTC)
        try:
            parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
