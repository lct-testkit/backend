"""Нагрузочный тест: `POST /api/deals/{id}/comments` (new_spec §0: «p95...
«добавление комментария» ≤ 300 мс при 50 RPS»).

Комментарии не меняют версию сделки (`create_comment` не принимает
`If-Match` — см. `app/modules/crm/router.py`), поэтому, в отличие от
transition, сделки можно переиспользовать. Циклический пул из
`--comment-pool-size` сделок (`provision.py`, по умолчанию 100), а не одна
сделка — чтобы не бить в одну и ту же строку/её индексы всем 50 RPS сразу.

Запуск:
  locust -f loadtest/locustfile_comment.py --headless \
      -u 50 -r 10 -t 60s --host=http://localhost:8080 \
      --csv=loadtest/results/comment
"""

from __future__ import annotations

import itertools
import json
import os
import time
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
_POOL = itertools.cycle(_FIXTURES["comment_deal_ids"])


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


class CommentUser(HttpUser):
    # См. locustfile_transition.py: без явного wait_time RPS держится
    # заметно ниже -u.
    wait_time = constant_throughput(_RPS_PER_USER) if _RPS_PER_USER > 0 else constant(0)

    def on_start(self) -> None:
        # Вход Bearer-токеном, как в locustfile_transition.py; про prod см. его шапку.
        token = _shared_token()
        self.client.headers.update({"Authorization": f"Bearer {token}"})

    @task
    def add_comment(self) -> None:
        deal_id = next(_POOL)
        self.client.post(
            f"/api/deals/{deal_id}/comments",
            json={"body": "Нагрузочный тест: автоматический комментарий."},
            name="/api/deals/{id}/comments",
        )
