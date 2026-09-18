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
# См. комментарий в locustfile_transition.py: пароль сброшен через
# сервис-аккаунт `crm-admin` с явного разрешения пользователя.
PASSWORD = os.environ.get("LOADTEST_PASSWORD", "LoadTest123456!")

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


class CommentUser(HttpUser):
    # См. locustfile_transition.py: без явного wait_time RPS держится
    # заметно ниже -u.
    wait_time = constant(0)

    def on_start(self) -> None:
        token = _fetch_access_token()
        self.client.headers.update({"Authorization": f"Bearer {token}"})

    @task
    def add_comment(self) -> None:
        deal_id = next(_POOL)
        self.client.post(
            f"/api/deals/{deal_id}/comments",
            json={"body": "Нагрузочный тест: автоматический комментарий."},
            name="/api/deals/{id}/comments",
        )
