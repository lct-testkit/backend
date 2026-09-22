"""Расхождения реквизитов организации с ЕГРЮЛ (`Organization.requisites_drift`).

Чистые функции без зависимостей от БД и Redis — их проверяют тесты без стенда."""

from __future__ import annotations

from typing import Any


def drift_new_value(entry: Any) -> Any:
    """Новое значение поля из `requisites_drift`.

    Сверка с ЕГРЮЛ (`registry/tasks.py::_compute_drift`) пишет расхождение как
    `{"old": …, "new": …}`; раньше `apply_drift` присваивал колонке весь этот
    словарь и падал `DBAPIError` (dict в текстовой колонке → 500). Принимаем и
    эту форму, и «голое» значение (так расхождение мог выставить оператор)."""
    if isinstance(entry, dict) and "new" in entry:
        return entry["new"]
    return entry
