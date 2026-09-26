"""Нагрузочный тест: `POST /api/deals/{id}/transition` (new_spec §0: «p95
времени ответа API на операции «переход по статусу»... ≤ 300 мс при 50 RPS»).

Каждая сделка переводится ровно один раз — это не упрощение теста, а
реалистичное свойство самого перехода по статусу (нельзя повторно
перевести из identification в first_contact сделку, которая там больше не
находится). Пул сделок готовит `provision.py` (`--count`, по умолчанию
4000 — с запасом на 50 RPS × 60 с = 3000 запросов).

Аутентификация — прямой grant Keycloak (`grant_type=password`): реальный JWT в
`Authorization: Bearer`, в обход BFF/cookie-сессии и CSRF-токена, тем же путём, каким
`ALLOW_BEARER_AUTH` в .env.example явно это разрешает.

Для стенда с профилем `demo`/`dev` этого достаточно: Bearer там принимается от любой роли
(`Settings.bearer_auth_mode == "all"`). В `prod` Bearer разрешён только роли INTEGRATION
(`bearer_auth_mode == "integration_only"`), и этот сценарий под KAM получит 401: браузерный
клиент ходит через `/api/auth/login` → Keycloak → `/api/auth/callback` (cookie `crm_sid`) и
шлёт `X-CSRF-Token` на каждый мутирующий запрос. Поэтому гейт гоняют на стенде с
`APP_PROFILE=demo`. Bearer обходит чтение сессии из Redis (`session_store.get`) и сверку
CSRF, то есть немного оптимистичнее браузерного пути; замера с настоящими cookie-сессиями
на prod-профиле пока нет.

Запуск:
  python loadtest/provision.py
  locust -f loadtest/locustfile_transition.py --headless \
      -u 50 -r 10 -t 60s --host=http://localhost:8080 \
      --csv=loadtest/results/transition
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from pathlib import Path

import requests
from gevent.lock import Semaphore
from locust import HttpUser, constant, constant_throughput, task

# Фиксированная нагрузка: LOADTEST_RPS_PER_USER=1 при -u 50 даёт ровно 50 RPS
# (критерий спеки — «p95 ≤ 300 мс при 50 RPS»). Без переменной — constant(0):
# каждый пользователь шлёт следующий запрос сразу после ответа (максимум).
_RPS_PER_USER = float(os.environ.get("LOADTEST_RPS_PER_USER", "0"))

KEYCLOAK_TOKEN_URL = os.environ.get(
    "LOADTEST_KEYCLOAK_TOKEN_URL",
    "http://localhost:8080/auth/realms/crm/protocol/openid-connect/token",
)
CLIENT_ID = os.environ.get("LOADTEST_CLIENT_ID", "crm-bff")
CLIENT_SECRET = os.environ.get("LOADTEST_CLIENT_SECRET", "crm-bff-secret")
USERNAME = os.environ.get("LOADTEST_USERNAME", "kam.ivanov")
# Пароль демо-КАМа `kam.ivanov` из deploy/keycloak/realm-crm.json: им же realm импортируется
# при первом старте стека, его же подставляет loadtest/run_ci.sh. Прежний дефолт
# (`LoadTest123456!`) годился только на одном давно пересозданном стенде и на чистом стеке
# давал 401 у password-grant. Другой стенд или пароль — переменная LOADTEST_PASSWORD.
PASSWORD = os.environ.get("LOADTEST_PASSWORD", "Kam123456789!")

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


# Один токен на всех виртуальных пользователей. 50 одновременных password-grant'ов от одного
# демо-КАМа срабатывают как брутфорс: Keycloak (brute-force detection, спека §4.5) временно
# блокирует пользователя (`user_temporarily_disabled`, 401), и прогон измеряет не API, а защиту
# IdP. Токен живёт 5 минут (`ACCESS_TOKEN_TTL`) — кэшируем на 240 с, этого хватает на любой
# сценарий из README.
_TOKEN_CACHE: dict[str, float | str] = {"value": "", "fetched_at": 0.0}
_TOKEN_MAX_AGE_S = 240.0


# Без блокировки все пользователи при старте одновременно видят пустой кэш и снова шлют
# N параллельных password-grant'ов (gevent переключается на сетевом вводе-выводе прямо
# внутри _fetch_access_token).
_TOKEN_LOCK = Semaphore()


def _shared_token() -> str:
    with _TOKEN_LOCK:
        age = time.monotonic() - float(_TOKEN_CACHE["fetched_at"])
        if not _TOKEN_CACHE["value"] or age > _TOKEN_MAX_AGE_S:
            _TOKEN_CACHE["value"] = _fetch_access_token()
            _TOKEN_CACHE["fetched_at"] = time.monotonic()
        return str(_TOKEN_CACHE["value"])


class TransitionUser(HttpUser):
    # Без явного wait_time Locust ждёт между задачами (версии 2.x — не 0 по
    # умолчанию) — на практике это удержало реальный RPS втрое ниже целевых
    # 50 при -u 50 в первом прогоне этого файла. constant(0) — сразу
    # следующий запрос, как только пришёл ответ; -u 50 тогда действительно
    # означает «до 50 одновременных запросов», а не «50 медленных пользователей».
    wait_time = constant_throughput(_RPS_PER_USER) if _RPS_PER_USER > 0 else constant(0)

    def on_start(self) -> None:
        token = _shared_token()
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
