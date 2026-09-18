"""Нагрузочный тест: `POST /api/deals/{id}/transition` (new_spec §0: «p95
времени ответа API на операции «переход по статусу»... ≤ 300 мс при 50 RPS»).

Каждая сделка переводится ровно один раз — это не упрощение теста, а
реалистичное свойство самого перехода по статусу (нельзя повторно
перевести из identification в first_contact сделку, которая там больше не
находится). Пул сделок готовит `provision.py` (`--count`, по умолчанию
4000 — с запасом на 50 RPS × 60 с = 3000 запросов).

Аутентификация — прямой grant Keycloak (`grant_type=password`), тот же
способ, что использовался для живой проверки во всех прошлых спринтах:
реальный JWT в `Authorization: Bearer`, в обход SvelteKit BFF/cookie-сессии
(её в этом репозитории нет — фронтенда нет, см. project-overview), тем же
путём, каким `ALLOW_BEARER_AUTH` в .env.example явно это разрешает.

Запуск:
  python loadtest/provision.py
  locust -f loadtest/locustfile_transition.py --headless \
      -u 50 -r 10 -t 60s --host=http://localhost:8080 \
      --csv=loadtest/results/transition
"""

from __future__ import annotations

import json
import os
from collections import deque
from pathlib import Path

import requests
from locust import HttpUser, constant, task

KEYCLOAK_TOKEN_URL = os.environ.get(
    "LOADTEST_KEYCLOAK_TOKEN_URL",
    "http://localhost:8080/auth/realms/crm/protocol/openid-connect/token",
)
CLIENT_ID = os.environ.get("LOADTEST_CLIENT_ID", "crm-bff")
CLIENT_SECRET = os.environ.get("LOADTEST_CLIENT_SECRET", "crm-bff-secret")
USERNAME = os.environ.get("LOADTEST_USERNAME", "kam.ivanov")
# Пароль из realm-crm.json (Kam123456789!) на живом стенде этой сессии
# больше не подходил — реалистичный дрейф после множества сессий живой
# проверки identity-модуля за 11 спринтов (смена/сброс пароля и т.п.
# тестировались буквально), не баг этого спринта. Сброшен через
# сервис-аккаунт `crm-admin` (тот же путь, что `identity.service` уже
# использует для админского сброса) с явного разрешения пользователя.
PASSWORD = os.environ.get("LOADTEST_PASSWORD", "LoadTest123456!")

_FIXTURES = json.loads((Path(__file__).parent / "fixtures.json").read_text())
_TO_STATUS_ID = _FIXTURES["to_status_id"]
# deque.popleft() под gevent: одна C-операция без точки переключения —
# безопасно делить между Locust-пользователями без явного лока (тот же
# довод, что для itertools.cycle в locustfile_comment.py).
_POOL: deque[str] = deque(_FIXTURES["deal_ids"])


def _fetch_access_token() -> str:
    response = requests.post(
        KEYCLOAK_TOKEN_URL,
        data={
            "grant_type": "password",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "username": USERNAME,
            "password": PASSWORD,
        },
        timeout=10,
    )
    response.raise_for_status()
    return response.json()["access_token"]


class TransitionUser(HttpUser):
    # Без явного wait_time Locust ждёт между задачами (версии 2.x — не 0 по
    # умолчанию) — на практике это удержало реальный RPS втрое ниже целевых
    # 50 при -u 50 в первом прогоне этого файла. constant(0) — сразу
    # следующий запрос, как только пришёл ответ; -u 50 тогда действительно
    # означает «до 50 одновременных запросов», а не «50 медленных пользователей».
    wait_time = constant(0)

    def on_start(self) -> None:
        token = _fetch_access_token()
        self.client.headers.update({"Authorization": f"Bearer {token}"})

    @task
    def transition(self) -> None:
        try:
            deal_id = _POOL.popleft()
        except IndexError:
            return  # пул исчерпан раньше конца прогона — не ошибка запроса
        # Каждая сделка свежесоздана provision.py, version=1 — If-Match
        # обязателен (раздел 3.5), но здесь не нужно вычитывать актуальную
        # версию перед каждым запросом: пул одноразовый по конструкции.
        self.client.post(
            f"/api/deals/{deal_id}/transition",
            json={"to_status_id": _TO_STATUS_ID},
            headers={"If-Match": "1"},
            name="/api/deals/{id}/transition",
        )
