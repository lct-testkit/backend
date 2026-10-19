"""D-10 (QA 20.10): сумма сделки не может быть отрицательной или выходить за колонку
`Numeric(14, 2)` — ни при создании, ни в PATCH, ни полем перехода."""

from __future__ import annotations

import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.crm_helpers import create_deal, login
from tests.test_deal_stages import _v2_id

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


@pytest.mark.parametrize("amount", ["-1000", "-0.01", "1000000000000"])
def test_create_rejects_out_of_range_amount(client, amount: str) -> None:
    login(client, "ADMIN")
    workflow_id = run(client, _v2_id)
    from tests.crm_helpers import create_organization

    response = client.post(
        "/api/deals",
        json={
            "title": "Сумма вне границ",
            "deal_type": "b2b",
            "workflow_id": str(workflow_id),
            "organization_id": create_organization(client),
            "amount": amount,
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["errors"][0]["field"] == "amount"


def test_create_accepts_zero_and_a_normal_amount(client) -> None:
    login(client, "ADMIN")
    workflow_id = run(client, _v2_id)
    zero = create_deal(client, str(workflow_id), amount="0")
    assert float(zero["amount"]) == 0
    normal = create_deal(client, str(workflow_id), amount="2100000.50")
    assert float(normal["amount"]) == 2100000.5


def test_patch_rejects_a_negative_amount(client) -> None:
    login(client, "ADMIN")
    workflow_id = run(client, _v2_id)
    deal = create_deal(client, str(workflow_id), amount="100")
    response = client.patch(
        f"/api/deals/{deal['id']}",
        json={"amount": "-1000"},
        headers={"If-Match": str(deal["version"])},
    )
    assert response.status_code == 422, response.text
    assert response.json()["errors"][0]["field"] == "amount"
