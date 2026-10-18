"""B5-12: контрактные тесты на моках сайта (CMS) и LMS.

Копии спецификаций — `tests/contracts/cms.openapi.yaml`/`lms.openapi.yaml` — РЕКОНСТРУИРОВАНЫ
по коду клиента (`integration/cms_client.py`, `integration/cms_pull.py`, `integration/lms.py`),
`tests/lms_helpers.py` и тексту задач B5-2/B5-6/B5-7/B5-8 документа спринтов: настоящих моков
(`deploy/mocks/{cms,lms}/openapi.yaml`) в этом репозитории нет (отдельный репозиторий `deploy`).
Источник истины — эти настоящие моки; копии здесь и клиентский код меняются ВМЕСТЕ.

Тест «от обратного» (`TestContractGuardsAgainstDrift`) показывает, что без синхронизации копий
с кодом клиента тест обязан покраснеть — иначе сами контрактные тесты ничего не гарантируют.

Сквозные тесты (приём лида/прогресса в БД) требуют `TEST_DATABASE_URL`.
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tests.conftest import TEST_DATABASE_URL, run
from tests.lms_helpers import LMS_URL, Network, make_scene

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

_CONTRACTS_DIR = Path(__file__).parent / "contracts"


def _load(name: str) -> dict[str, Any]:
    with (_CONTRACTS_DIR / name).open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _assert_request_path_is_contracted(contract: dict[str, Any], requested_path: str) -> None:
    """Путь реального запроса клиента должен быть одним из путей контракта (без схемы/хоста,
    `requested_path` — остаток после `base_url`). Используется и «хорошим» тестом (путь
    совпадает), и тестом «от обратного» (`TestContractGuardsAgainstDrift`, на испорченной копии
    контракта)."""
    assert (
        requested_path in contract["paths"]
    ), f"{requested_path!r} не объявлен в контракте: {sorted(contract['paths'])}"


def _examples(contract: dict[str, Any], schema_name: str) -> dict[str, Any]:
    return contract["components"]["schemas"][schema_name].get("examples", {})


def _required_fields(contract: dict[str, Any], schema_name: str) -> list[str]:
    return contract["components"]["schemas"][schema_name].get("required", [])


def _check_record_has_required_fields(
    contract: dict[str, Any], schema_name: str, record: dict[str, Any]
) -> list[str]:
    """Простой обход схемы без JSON Schema-валидатора (B5-12: «новых библиотек для валидации не
    вводится»): для каждого `required`-поля (с поддержкой одного уровня вложенности через
    `родитель.ребёнок`, как у `student.email` EnrollmentRequest) проверяет, что оно есть и не
    `None`. Возвращает список отсутствующих полей."""
    missing: list[str] = []
    for field in _required_fields(contract, schema_name):
        if "." in field:
            parent, child = field.split(".", 1)
            value = (record.get(parent) or {}).get(child)
        else:
            value = record.get(field)
        if value is None:
            missing.append(field)
    return missing


class TestCmsContract:
    def test_client_requests_the_contracted_path_and_params(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.modules.integration.cms_client import CmsClient

        contract = _load("cms.openapi.yaml")
        spec = contract["paths"]["/api/v1/leads"]["get"]
        allowed_params = {p["name"] for p in spec["parameters"]}
        assert allowed_params >= {"updated_since", "cursor", "limit"}

        network = Network()
        network.install(monkeypatch)

        async def _call() -> None:
            client = CmsClient("https://cms-contract.invalid")
            await client.list_leads(updated_since="2026-09-01T00:00:00Z", cursor="c1", limit=50)

        asyncio.run(_call())

        assert len(network.calls) == 1
        call = network.calls[0]
        requested_path = httpx.URL(call["url"]).path
        _assert_request_path_is_contracted(contract, requested_path)
        assert set(call["params"]) <= allowed_params
        limit_schema = next(p["schema"] for p in spec["parameters"] if p["name"] == "limit")
        assert limit_schema["minimum"] <= 50 <= limit_schema["maximum"]

    def test_each_lead_example_is_ingested_and_mapped_to_a_catalog_product(self, client) -> None:
        """Каждый пример `Lead` из контракта проходит интерим-маппинг сайта (B5-2) и
        демо-маппинг B5-7 (`python-basics`/`data-analysis` → `py-base`/`ds-pro`), затем
        `CmsLeadService.ingest_pulled` (B5-2) — и создаёт либо находит сделку с этим продуктом."""
        from app.modules.integration.field_mapping import apply_mapping
        from app.modules.integration.lead_payload import parse_lead_payload
        from app.modules.integration.seed import _demo_cms_mapping

        contract = _load("cms.openapi.yaml")
        examples = _examples(contract, "Lead")
        assert examples, "в контракте нет примеров Lead — тест ничего не проверяет"
        mapping = _demo_cms_mapping()

        async def _run_example(record: dict[str, Any]) -> dict[str, Any]:
            from sqlalchemy import select

            from app.core.db import session_scope
            from app.modules.catalog.models import Product
            from app.modules.integration.cms import CmsLeadService
            from app.modules.integration.cms_pull import apply_interim_mapping

            mapped_record = apply_interim_mapping(record)
            mapped = apply_mapping(mapped_record, mapping, "cms")
            lead = parse_lead_payload(mapped.payload)

            product_code = {"python-basics": "py-base", "data-analysis": "ds-pro"}.get(
                lead.course, f"other-{uuid.uuid4().hex[:6]}"
            )
            async with session_scope() as session:
                existing = await session.scalar(select(Product).where(Product.code == product_code))
                if existing is None:
                    session.add(Product(code=product_code, name=product_code))
                    await session.flush()

                key = f"{record.get('external_id', 'lead')}-{uuid.uuid4().hex[:8]}"
                result = await CmsLeadService(session).ingest_pulled(mapped_record, key)
                return {"status": result.message.status, "course": lead.course}

        for name, record in examples.items():
            outcome = run(client, _run_example, record)
            assert outcome["status"] in {"processed", "duplicate"}, name


class TestLmsContract:
    def test_pull_requests_the_contracted_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.modules.integration.lms import LmsClient

        contract = _load("lms.openapi.yaml")

        network = Network()
        network.install(monkeypatch)

        async def _call() -> None:
            client = LmsClient(LMS_URL)
            await client.pull_progress(updated_since=None, max_pages=1, page_size=10)

        asyncio.run(_call())

        assert len(network.calls) == 1
        requested_path = httpx.URL(network.calls[0]["url"]).path
        _assert_request_path_is_contracted(contract, requested_path)

    def test_push_requests_the_contracted_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.modules.integration.lms import LmsClient

        contract = _load("lms.openapi.yaml")

        network = Network()
        network.install(monkeypatch)

        async def _call() -> None:
            client = LmsClient(LMS_URL)
            await client.push_enrollment({"external_course_id": "c-1", "deal_ref": "D-1"})

        asyncio.run(_call())

        assert len(network.calls) == 1
        requested_path = httpx.URL(network.calls[0]["url"]).path
        _assert_request_path_is_contracted(contract, requested_path)

    def test_progress_examples_normalize_and_apply(self, client) -> None:
        from app.modules.integration.lms import normalize_progress_row, upsert_progress

        contract = _load("lms.openapi.yaml")
        examples = _examples(contract, "ProgressRecord")
        assert examples

        scene = run(client, make_scene)

        async def _apply(record: dict[str, Any]) -> Any:
            from app.core.db import session_scope

            normalized = normalize_progress_row(record)
            assert "course_external_id" in normalized
            row = dict(normalized)
            row["deal_id"] = str(scene["deal_id"])
            async with session_scope() as session:
                return await upsert_progress(session, row)

        for name, record in examples.items():
            applied = run(client, _apply, record)
            assert applied is not None, name

    def test_enrollment_payload_has_all_required_fields(self, client) -> None:
        from app.modules.integration.lms import build_enrollment_details

        contract = _load("lms.openapi.yaml")
        scene = run(client, make_scene)

        async def _build() -> dict[str, Any]:
            from app.core.db import session_scope

            async with session_scope() as session:
                return await build_enrollment_details(session, scene["deal_id"])

        details = run(client, _build)
        missing = _check_record_has_required_fields(contract, "EnrollmentRequest", details)
        assert missing == [], f"нагрузка зачисления не несёт обязательных полей: {missing}"

    def test_already_enrolled_response_is_treated_as_success(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.modules.integration.lms import LmsClient

        network = Network()
        network.respond = lambda request: httpx.Response(200, json={"status": "already_enrolled"})
        network.install(monkeypatch)

        async def _call() -> dict[str, Any]:
            client = LmsClient(LMS_URL)
            return await client.push_enrollment({"external_course_id": "c-1", "deal_ref": "D-1"})

        import asyncio

        result = asyncio.run(_call())
        assert result["status"] == "already_enrolled"


class TestContractGuardsAgainstDrift:
    """Контроль «от обратного»: если скопированный контракт расходится с реальным путём
    клиента, ТА ЖЕ проверка, что используют «хорошие» тесты выше (`_assert_request_path_is_
    contracted`), обязана упасть — иначе сами контрактные тесты не защищают ни от чего."""

    def test_renamed_path_in_the_copy_fails_the_same_check_the_real_tests_use(self) -> None:
        contract = _load("cms.openapi.yaml")
        broken = copy.deepcopy(contract)
        broken["paths"]["/v1/leads"] = broken["paths"].pop("/api/v1/leads")

        with pytest.raises(AssertionError):
            _assert_request_path_is_contracted(broken, "/api/v1/leads")

        # Непорченный контракт (тот, что используют настоящие тесты) эту же проверку проходит —
        # падение выше вызвано порчей пути, а не самой функцией проверки.
        _assert_request_path_is_contracted(contract, "/api/v1/leads")
