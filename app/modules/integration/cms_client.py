"""Клиент сайта (CMS) для забора лидов по API (B5-2, Функц.5 ТЗ).

По образцу `integration.lms.LmsClient`: `CmsClient.list_leads` опрашивает
`GET {base_url}/api/v1/leads?updated_since=&cursor=&limit=` — контракт мока
сайта (`deploy/mocks/cms/openapi.yaml` в этом репозитории отсутствует, адрес
и форма ответа взяты из текста задачи B5-2 документа спринтов; закреплены
фикстурами в `tests/test_cms_pull.py`). Ответ — объект `{"items": [...],
"next_cursor": "..."}`; тело-массив тоже принимается (сайт мог бы и не
заворачивать список в обёртку).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from app.modules.integration.http import TIMEOUT, http_errors_without_url

_SOURCE = "cms"


@dataclass(slots=True)
class LeadPage:
    items: list[dict[str, Any]] = field(default_factory=list)
    next_cursor: str | None = None


class CmsClient:
    def __init__(self, base_url: str, auth_token: str | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth_token = auth_token

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        return headers

    async def list_leads(
        self, *, updated_since: str | None, cursor: str | None, limit: int
    ) -> LeadPage:
        params: dict[str, Any] = {"limit": limit}
        if updated_since:
            params["updated_since"] = updated_since
        if cursor:
            params["cursor"] = cursor
        async with (
            http_errors_without_url(_SOURCE, "leads"),
            httpx.AsyncClient(timeout=TIMEOUT) as client,
        ):
            response = await client.get(
                f"{self._base_url}/api/v1/leads", params=params, headers=self._headers()
            )
            response.raise_for_status()
            body = response.json()
            if isinstance(body, list):
                return LeadPage(items=body, next_cursor=None)
            return LeadPage(items=body.get("items", []), next_cursor=body.get("next_cursor"))

    async def get_lead(self, lead_id: str) -> dict[str, Any]:
        """Один лид по id — не используется этой задачей, нужен контрактному тесту (B5-12)."""
        async with (
            http_errors_without_url(_SOURCE, "lead"),
            httpx.AsyncClient(timeout=TIMEOUT) as client,
        ):
            response = await client.get(
                f"{self._base_url}/api/v1/leads/{lead_id}", headers=self._headers()
            )
            response.raise_for_status()
            return response.json()
