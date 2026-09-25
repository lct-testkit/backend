#!/usr/bin/env bash
# Пересобирает requirements.lock / requirements-dev.lock (с хэшами) из pyproject.toml.
#
#   bash tools/lock.sh                 # только добавить новые зависимости, версии не трогать
#   bash tools/lock.sh --upgrade       # обновить все транзитивные версии
#
# Цель — Linux/CPython 3.12 (то, что ставится в образ и в CI). Нужен `uv`
# (pip install uv). CI (job `lock`) запускает этот же скрипт и требует пустой
# `git diff`: pyproject.toml и lock-файлы не должны расходиться.
set -euo pipefail
cd "$(dirname "$0")/.."

common=(--python-version 3.12 --python-platform linux --generate-hashes --no-header -q)

uv pip compile pyproject.toml "${common[@]}" "$@" -o requirements.lock
uv pip compile pyproject.toml --extra dev "${common[@]}" "$@" -o requirements-dev.lock
echo "lock-файлы обновлены"
