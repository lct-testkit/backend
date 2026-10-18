"""B1-5: результирующий json-файл отчёта (rtk_requiriments.md §6 п.4). Чистый рендер — без БД,
тем же приёмом, что `test_reporting_xls.py`. Сквозной тест через API — `TestJsonEndToEnd`
(настоящая Postgres обязательна, см. докстринг `tests/conftest.py`).
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from typing import Any

import pytest

from app.modules.reporting.builders import ReportDataset
from app.modules.reporting.rendering import CONTENT_TYPES, render_json, render_report
from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

pytestmark_db = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _dataset(**kwargs) -> ReportDataset:
    defaults: dict = {
        "title": "Причины отказов",
        "columns": ["Причина", "Сделок"],
        "rows": [["Дорого", 5], ["Не ответили", 3]],
        "keys": None,
        "kinds": ["text", "int"],
    }
    defaults.update(kwargs)
    return ReportDataset(**defaults)


class TestJsonEnvelope:
    def test_schema_and_top_level_keys_are_present(self) -> None:
        job_id = uuid.uuid4()
        content = render_json(
            _dataset(),
            template_code="loss_reasons",
            job_id=job_id,
            params={"date_from": "2026-01-01"},
            tz_name="Europe/Moscow",
        )
        payload = json.loads(content)
        assert payload["schema"] == "rtk-crm-report/1"
        assert payload["report"]["code"] == "loss_reasons"
        assert payload["report"]["job_id"] == str(job_id)
        assert payload["report"]["timezone"] == "Europe/Moscow"
        assert payload["params"] == {"date_from": "2026-01-01"}
        assert payload["columns"] == [
            {"key": None, "title": "Причина", "kind": "text"},
            {"key": None, "title": "Сделок", "kind": "int"},
        ]
        assert payload["rows"] == [["Дорого", 5], ["Не ответили", 3]]
        assert payload["totals"] == {"row_count": 2, "total_rows": 2, "truncated": False}

    def test_underscore_params_are_hidden_from_the_integrator(self) -> None:
        content = render_json(
            _dataset(),
            template_code="loss_reasons",
            job_id=None,
            params={"date_from": "2026-01-01", "_seed": "demo"},
            tz_name="UTC",
        )
        payload = json.loads(content)
        assert "_seed" not in payload["params"]
        assert payload["params"] == {"date_from": "2026-01-01"}

    def test_row_count_matches_number_of_items_per_row(self) -> None:
        content = render_json(
            _dataset(), template_code="loss_reasons", job_id=None, params={}, tz_name="UTC"
        )
        payload = json.loads(content)
        for row in payload["rows"]:
            assert len(row) == len(payload["columns"])

    def test_decimal_becomes_a_plain_number(self) -> None:
        content = render_json(
            _dataset(
                columns=["Сумма"],
                rows=[[Decimal("150000.00")]],
                kinds=["money"],
            ),
            template_code="deal_register",
            job_id=None,
            params={},
            tz_name="UTC",
        )
        payload = json.loads(content)
        assert payload["rows"] == [[150000.0]]
        assert isinstance(payload["rows"][0][0], float)

    def test_aware_datetime_becomes_an_iso_string_with_user_offset(self) -> None:
        aware = dt.datetime(2026, 1, 5, 10, 30, tzinfo=dt.UTC)
        content = render_json(
            _dataset(columns=["Создана"], rows=[[aware]], kinds=["datetime"]),
            template_code="deal_register",
            job_id=None,
            params={},
            tz_name="Europe/Moscow",
        )
        payload = json.loads(content)
        assert payload["rows"][0][0] == "2026-01-05T13:30:00+03:00"

    def test_naive_date_becomes_plain_iso_date(self) -> None:
        content = render_json(
            _dataset(columns=["Создана"], rows=[[dt.date(2026, 1, 5)]], kinds=["date"]),
            template_code="deal_register",
            job_id=None,
            params={},
            tz_name="UTC",
        )
        payload = json.loads(content)
        assert payload["rows"][0][0] == "2026-01-05"

    def test_nan_in_data_raises_a_clear_error_not_invalid_json(self) -> None:
        with pytest.raises(ValueError):
            render_json(
                _dataset(columns=["X"], rows=[[float("nan")]], kinds=["pct"]),
                template_code="loss_reasons",
                job_id=None,
                params={},
                tz_name="UTC",
            )

    def test_truncated_dataset_reflects_total_rows_and_truncated_flag(self) -> None:
        content = render_json(
            _dataset(total_rows=3412, truncated=True),
            template_code="deal_register",
            job_id=None,
            params={},
            tz_name="UTC",
        )
        payload = json.loads(content)
        assert payload["totals"]["total_rows"] == 3412
        assert payload["totals"]["truncated"] is True


class TestRenderReportDispatchesJson:
    def test_json_format_dispatches_to_render_json(self) -> None:
        content = render_report(_dataset(), format="json", template_code="loss_reasons")
        payload = json.loads(content)
        assert payload["schema"] == "rtk-crm-report/1"

    def test_content_type_is_registered(self) -> None:
        assert CONTENT_TYPES["json"] == "application/json"


@pytestmark_db
class TestJsonEndToEnd:
    """B1-5, приёмка: `POST /api/reports` с `format=json` даёт `completed`, файл в подменённом
    хранилище разбирается `json.loads` и совпадает по `columns`/`rows` с `GET /data`."""

    def _ensure_template(self, client) -> None:
        from sqlalchemy import select as sa_select

        from app.core.db import session_scope
        from app.modules.reporting.models import ReportTemplate

        async def _ensure() -> None:
            async with session_scope() as session:
                existing = await session.scalar(
                    sa_select(ReportTemplate.id).where(ReportTemplate.code == "sla_compliance")
                )
                if existing is not None:
                    return
                session.add(
                    ReportTemplate(
                        code="sla_compliance",
                        name="Соблюдение SLA",
                        description="Доля сделок в норме/под угрозой/с нарушением SLA.",
                        query_def={"kind": "sla_compliance"},
                        allowed_roles=[],
                        default_params={},
                        output_formats=["xlsx", "json"],
                        is_active=True,
                    )
                )

        run(client, _ensure)

    def _stub_s3(self, monkeypatch) -> dict[str, Any]:
        import app.modules.reporting.service as reporting_service

        captured: dict[str, Any] = {}

        async def fake_ensure_bucket(bucket: str) -> None:
            return None

        async def fake_upload(*, bucket: str, key: str, body: bytes, content_type: str) -> None:
            captured["body"] = body
            captured["content_type"] = content_type

        monkeypatch.setattr(reporting_service, "ensure_bucket", fake_ensure_bucket)
        monkeypatch.setattr(reporting_service, "upload_object_bytes", fake_upload)
        return captured

    def test_post_reports_with_json_format_matches_the_data_endpoint(
        self, client, monkeypatch
    ) -> None:
        self._ensure_template(client)
        captured = self._stub_s3(monkeypatch)
        kam = run(client, _make_user, "KAM")
        client.headers["X-CSRF-Token"] = authenticate(client, kam)

        response = client.post(
            "/api/reports", json={"template_code": "sla_compliance", "format": "json"}
        )
        assert response.status_code == 201, response.text
        job = response.json()
        assert job["status"] == "completed"
        assert captured["content_type"] == "application/json"

        file_payload = json.loads(captured["body"])
        assert file_payload["schema"] == "rtk-crm-report/1"
        assert file_payload["report"]["job_id"] == job["id"]

        data = client.get(f"/api/reports/{job['id']}/data")
        assert data.status_code == 200, data.text
        body = data.json()
        assert [c["title"] for c in file_payload["columns"]] == body["columns"]
        assert file_payload["rows"] == body["rows"]

        # B1-12: снимок момента генерации в `ReportJobOut.result_meta` — то же, что ушло в
        # конверт файла (`file_payload["report"]["timezone"]`/`columns`), не отдельная копия,
        # которая могла бы разойтись с реальным содержимым.
        assert job["result_meta"]["timezone"] == file_payload["report"]["timezone"]
        assert job["result_meta"]["columns"] == [c["title"] for c in file_payload["columns"]]
        assert job["result_meta"]["total_rows"] == job["row_count"]
        assert job["result_meta"]["truncated"] is False

    def test_queued_job_has_no_result_meta_yet(self, client) -> None:
        """`result_meta` заполняется только в `generate()` — у задания, которое ещё не
        забрал `sweep_report_jobs`, его структурно не может быть (то же самое для
        `processing`/`failed`, здесь — самый частый случай)."""
        from app.core.db import session_scope
        from app.modules.reporting.models import ReportJob

        kam = run(client, _make_user, "KAM")
        client.headers["X-CSRF-Token"] = authenticate(client, kam)

        async def _insert_queued() -> str:
            async with session_scope() as session:
                job = ReportJob(
                    template_code="sla_compliance",
                    format="json",
                    requested_by=kam.id,
                    status="queued",
                )
                session.add(job)
                await session.flush()
                return str(job.id)

        job_id = run(client, _insert_queued)
        response = client.get(f"/api/reports/{job_id}")
        assert response.status_code == 200, response.text
        assert response.json()["result_meta"] is None

    def test_audit_has_exactly_one_report_exported_event(self, client, monkeypatch) -> None:
        from sqlalchemy import func, select

        from app.core.db import session_scope
        from app.modules.audit.models import AuditLog

        self._ensure_template(client)
        self._stub_s3(monkeypatch)
        kam = run(client, _make_user, "KAM")
        client.headers["X-CSRF-Token"] = authenticate(client, kam)

        job_id = client.post(
            "/api/reports", json={"template_code": "sla_compliance", "format": "json"}
        ).json()["id"]

        async def _count() -> int:
            async with session_scope() as session:
                return int(
                    (
                        await session.execute(
                            select(func.count(AuditLog.id)).where(
                                AuditLog.action == "REPORT_EXPORTED",
                                AuditLog.entity_id == uuid.UUID(job_id),
                            )
                        )
                    ).scalar_one()
                )

        assert run(client, _count) == 1
