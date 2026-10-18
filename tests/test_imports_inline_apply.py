"""B3-12: небольшое задание (`IMPORT_INLINE_MAX_ROWS` строк, по умолчанию 500) применяется сразу
в `POST /api/imports/{id}/apply`, большее — уходит в фоновую обработку как раньше."""

from __future__ import annotations

import pytest

from tests.conftest import TEST_DATABASE_URL
from tests.crm_helpers import login
from tests.import_helpers import make_inn, start_import, token, xlsx_bytes

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

ORG_HEADERS = ["Наименование", "ИНН"]


def _org_rows(count: int, tok: str) -> list[list[str]]:
    return [[f"Организация {tok}-{i}", make_inn()] for i in range(count)]


class TestInlineApply:
    def test_small_job_completes_within_the_apply_request(self, client, monkeypatch) -> None:
        tok = token()
        head = login(client, "HEAD")
        content = xlsx_bytes(ORG_HEADERS, _org_rows(10, tok))
        started = start_import(
            client,
            monkeypatch,
            head,
            entity_type="organization",
            content=content,
            source_format="xlsx",
        )
        assert started["dry"]["error_rows"] == 0

        response = client.post(f"/api/imports/{started['id']}/apply")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] in ("completed", "completed_with_errors")
        assert body["processed_rows"] == 10
        assert body["finished_at"] is not None

    def test_large_job_stays_applying_for_the_background_task(self, client, monkeypatch) -> None:
        tok = token()
        head = login(client, "HEAD")
        content = xlsx_bytes(ORG_HEADERS, _org_rows(600, tok))
        started = start_import(
            client,
            monkeypatch,
            head,
            entity_type="organization",
            content=content,
            source_format="xlsx",
        )
        assert started["dry"]["error_rows"] == 0

        response = client.post(f"/api/imports/{started['id']}/apply")
        assert response.status_code == 200, response.text
        body = response.json()
        # Без фоновой задачи (не запущена в тесте) задание остаётся в `applying`, не завершается
        # внутри самого запроса — выше лимита инлайн-применения.
        assert body["status"] == "applying"
        assert body["finished_at"] is None
