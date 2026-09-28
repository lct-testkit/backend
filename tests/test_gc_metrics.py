"""`crm_gc_pause_seconds`: пауза сборщика мусора видна в метриках (перф-диагностика 28.09)."""

from __future__ import annotations

import gc

from prometheus_client import generate_latest

from app.core import metrics


def test_gc_pause_is_recorded_per_generation() -> None:
    metrics.install_gc_metrics()
    metrics.install_gc_metrics()  # повторный вызов не удваивает подписку
    assert gc.callbacks.count(metrics._observe_gc) == 1

    gc.collect()

    text = generate_latest().decode()
    assert 'crm_gc_pause_seconds_count{generation="2"}' in text
    count = next(
        float(line.rsplit(" ", 1)[1])
        for line in text.splitlines()
        if line.startswith('crm_gc_pause_seconds_count{generation="2"}')
    )
    assert count >= 1


def test_gc_callback_ignores_stop_without_start() -> None:
    # Сборщик отключён на время проверки: иначе сама `generate_latest()` могла бы запустить сборку и
    # изменить счётчики между двумя снимками.
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        metrics._gc_started.clear()
        before = generate_latest().decode()
        metrics._observe_gc("stop", {"generation": 1})  # старт не видели — не падаем и не считаем
        assert generate_latest().decode() == before
    finally:
        if was_enabled:
            gc.enable()
