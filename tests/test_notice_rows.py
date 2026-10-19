"""N-7 (QA 20.10): в письме по сделке строка об организации подписана по её типу —
«Вуз» у вуза и колледжа, «Организация» у компании. Чистая проверка без БД."""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from types import SimpleNamespace

import pytest

from app.modules.notification.notice import _deal_rows


class _Session:
    def __init__(self, organization) -> None:
        self._organization = organization

    async def get(self, model, key):
        if model.__name__ == "Deal":
            return SimpleNamespace(
                number="D-2026-000041",
                title="Курсы",
                organization_id=uuid.uuid4(),
                status_id=uuid.uuid4(),
                sla_due_at=dt.datetime(2026, 10, 20, tzinfo=dt.UTC),
            )
        if model.__name__ == "Organization":
            return self._organization
        return SimpleNamespace(name="Квалификация")


def _label_of_organization(org_type: str) -> str:
    organization = SimpleNamespace(org_type=org_type, short_name=None, name="Цифровые кадры")
    rows = asyncio.run(_deal_rows(_Session(organization), uuid.uuid4(), "DEAL_SLA_BREACHED"))
    labels = [label for label, value in rows if value == "Цифровые кадры"]
    assert len(labels) == 1
    return labels[0]


@pytest.mark.parametrize(
    ("org_type", "label"),
    [
        ("university", "Вуз"),
        ("college", "Вуз"),
        ("company", "Организация"),
        ("individual_entrepreneur", "Организация"),
    ],
)
def test_organization_label_follows_the_type(org_type: str, label: str) -> None:
    assert _label_of_organization(org_type) == label
