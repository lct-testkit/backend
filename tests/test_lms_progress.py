"""Приём прогресса из LMS: push-ручка `POST /api/v1/integrations/lms/progress` и pull-задача
`sweep_lms_progress_pull` (`lms.apply_progress_rows`, `lms.upsert_progress`).

Раньше одна «битая» строка (процент вне 0..100, не число в оценке, сбой БД) откатывала всю пачку:
push отвечал 500, pull терял тик. Теперь каждая строка идёт в своём SAVEPOINT — пропускается и
попадает в счётчики; неполная строка не затирает известные поля значением `null`; курсор pull не
уходит дальше строк, которые применить не удалось.

Сквозные тесты на настоящей PostgreSQL (`TEST_DATABASE_URL`); сеть подменяет `httpx.MockTransport`.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import uuid
from typing import Any

import httpx
import pytest

from app.modules.integration.security import compute_signature
from tests.conftest import TEST_DATABASE_URL, run
from tests.lms_helpers import LMS_URL, Network, make_scene

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

_SECRET_ENV = "LMS_PROGRESS_TEST_SECRET"
_SECRET = "lms-progress-test-secret"
_PUSH_URL = "/api/v1/integrations/lms/progress"


# --- БД --------------------------------------------------------------------------------------


async def _apply(rows: list[Any]) -> dict[str, Any]:
    from app.core.db import session_scope
    from app.modules.integration.lms import apply_progress_rows

    async with session_scope() as session:
        batch = await apply_progress_rows(session, rows)
        return {
            "outcomes": batch.outcomes,
            "applied": batch.applied,
            "skipped": batch.skipped,
            "failed": batch.failed,
        }


async def _progress(deal_id: uuid.UUID) -> dict[str, dict[str, Any]]:
    """Строки прогресса сделки по `external_course_id`."""
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.integration.models import LearningProgress

    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(LearningProgress).where(LearningProgress.deal_id == deal_id)
                )
            )
            .scalars()
            .all()
        )
        return {
            row.external_course_id: {
                "progress_pct": row.progress_pct,
                "score": row.score,
                "enrolled_at": row.enrolled_at,
                "completed_at": row.completed_at,
                "last_activity_at": row.last_activity_at,
                "contact_id": row.contact_id,
                "product_id": row.product_id,
                "raw": row.raw,
            }
            for row in rows
        }


async def _set_lms_source(*, active: bool = True) -> None:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.integration.models import IntegrationSource

    async with session_scope() as session:
        source = (
            await session.execute(select(IntegrationSource).where(IntegrationSource.code == "lms"))
        ).scalar_one_or_none()
        if source is None:
            source = IntegrationSource(code="lms", name="LMS")
            session.add(source)
        source.is_active = active
        source.credentials_ref = _SECRET_ENV
        source.base_url = LMS_URL


async def _inbound(key: str) -> dict[str, Any] | None:
    from sqlalchemy import select

    from app.core.db import session_scope
    from app.modules.integration.models import InboundMessage

    async with session_scope() as session:
        message = (
            await session.execute(
                select(InboundMessage).where(
                    InboundMessage.source_code == "lms", InboundMessage.external_id == key
                )
            )
        ).scalar_one_or_none()
        if message is None:
            return None
        return {"status": message.status, "error": message.error, "raw": message.raw_payload}


async def _reset_cursor(value: str | None = None) -> None:
    from sqlalchemy import delete

    from app.core.db import session_scope
    from app.modules.integration.lms import set_cursor
    from app.modules.integration.models import SyncCursor

    async with session_scope() as session:
        await session.execute(delete(SyncCursor).where(SyncCursor.source_code == "lms"))
        if value is not None:
            await set_cursor(session, source_code="lms", resource="students_progress", value=value)


async def _cursor() -> str | None:
    from app.core.db import session_scope
    from app.modules.integration.lms import get_cursor

    async with session_scope() as session:
        return await get_cursor(session, source_code="lms", resource="students_progress")


def _row(scene: dict[str, Any], course: str = "c-1", **fields: Any) -> dict[str, Any]:
    return {"deal_id": str(scene["deal_id"]), "course_external_id": course, **fields}


# =============================================================================================
# apply_progress_rows / upsert_progress
# =============================================================================================


class TestApplyRows:
    def test_bad_rows_are_skipped_and_good_ones_are_kept(self, client) -> None:
        scene = run(client, make_scene)
        rows = [
            _row(scene, "ok-1", progress_pct=10),
            _row(scene, "bad-pct", progress_pct=150),
            _row(scene, "ok-2", progress_pct=20, score=4.5),
            _row(scene, "bad-score", score="отлично"),
            _row(scene, "bad-type", progress_pct={"a": 1}),
            {"deal_id": str(uuid.uuid4()), "course_external_id": "alien", "progress_pct": 5},
            None,
            "строка",
            _row(scene, "ok-3", progress_pct=30),
        ]

        result = run(client, _apply, rows)

        assert result["outcomes"] == [
            "applied",
            "failed",
            "applied",
            "failed",
            "failed",
            "unmatched",
            "unmatched",
            "unmatched",
            "applied",
        ]
        assert (result["applied"], result["skipped"], result["failed"]) == (3, 6, 3)
        stored = run(client, _progress, scene["deal_id"])
        assert sorted(stored) == ["ok-1", "ok-2", "ok-3"]
        assert [stored[c]["progress_pct"] for c in ("ok-1", "ok-2", "ok-3")] == [10, 20, 30]
        assert str(stored["ok-2"]["score"]) == "4.50"

    def test_database_error_in_one_row_does_not_poison_the_rest(self, client) -> None:
        """`course_external_id` длиннее колонки — ошибка БД внутри вставки; транзакция при этом
        остаётся рабочей, строки до и после применяются."""
        scene = run(client, make_scene)
        rows = [
            _row(scene, "before", progress_pct=1),
            _row(scene, "x" * 300, progress_pct=2),
            _row(scene, "after", progress_pct=3),
        ]

        result = run(client, _apply, rows)

        assert result["outcomes"] == ["applied", "failed", "applied"]
        assert sorted(run(client, _progress, scene["deal_id"])) == ["after", "before"]

    def test_non_finite_numbers_are_a_failed_row_not_a_crash(self, client) -> None:
        scene = run(client, make_scene)
        rows = [
            _row(scene, "nan", progress_pct=float("nan")),
            _row(scene, "inf", score=float("inf")),
            _row(scene, "extra-nan", progress_pct=5, extra=float("nan")),  # NaN в raw — не в JSONB
            _row(scene, "fine", progress_pct=6),
        ]

        result = run(client, _apply, rows)

        assert result["outcomes"] == ["failed", "failed", "failed", "applied"]

    @pytest.mark.parametrize(
        ("value", "expected"),
        [(0, 0), (100, 100), (55.5, 56), ("70", 70), (99.4, 99), ("42.0", 42), (None, None)],
    )
    def test_percent_forms(self, client, value, expected) -> None:
        scene = run(client, make_scene)

        result = run(client, _apply, [_row(scene, progress_pct=value)])

        assert result["outcomes"] == ["applied"]
        assert run(client, _progress, scene["deal_id"])["c-1"]["progress_pct"] == expected

    @pytest.mark.parametrize("value", [-1, 101, 1000, True, "abc", [1], {"n": 1}, "nan", "inf"])
    def test_percent_out_of_range_or_of_a_wrong_type_rejects_the_row(self, client, value) -> None:
        scene = run(client, make_scene)

        result = run(client, _apply, [_row(scene, progress_pct=value)])

        assert result["outcomes"] == ["failed"]
        assert run(client, _progress, scene["deal_id"]) == {}

    @pytest.mark.parametrize("value", [1000, -1000, 10**6, True, "abc", "nan"])
    def test_score_out_of_range_rejects_the_row(self, client, value) -> None:
        scene = run(client, make_scene)

        assert run(client, _apply, [_row(scene, score=value)])["outcomes"] == ["failed"]

    def test_times_without_a_zone_are_taken_as_utc(self, client) -> None:
        scene = run(client, make_scene)

        run(
            client,
            _apply,
            [
                _row(
                    scene,
                    enrolled_at="2026-09-01T10:00:00",
                    completed_at="2026-09-20T12:30:00Z",
                    last_activity_at="2026-09-21T08:00:00+03:00",
                )
            ],
        )

        stored = run(client, _progress, scene["deal_id"])["c-1"]
        assert stored["enrolled_at"] == dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.UTC)
        assert stored["completed_at"] == dt.datetime(2026, 9, 20, 12, 30, tzinfo=dt.UTC)
        assert stored["last_activity_at"] == dt.datetime(2026, 9, 21, 5, 0, tzinfo=dt.UTC)

    def test_unreadable_time_is_ignored_not_an_error(self, client) -> None:
        scene = run(client, make_scene)

        result = run(client, _apply, [_row(scene, enrolled_at="вчера", progress_pct=5)])

        assert result["outcomes"] == ["applied"]
        assert run(client, _progress, scene["deal_id"])["c-1"]["enrolled_at"] is None

    def test_row_without_a_deal_or_a_course_is_not_ours(self, client) -> None:
        scene = run(client, make_scene)

        result = run(
            client,
            _apply,
            [
                {"course_external_id": "c-1"},
                {"deal_id": str(scene["deal_id"])},
                {"deal_id": "не-uuid", "course_external_id": "c-1"},
                {"deal_id": None, "course_external_id": None},
            ],
        )

        assert result["outcomes"] == ["unmatched"] * 4

    def test_deleted_deal_is_not_ours_either(self, client) -> None:
        from app.core.db import session_scope
        from app.modules.crm.models import Deal

        scene = run(client, make_scene)

        async def delete() -> None:
            async with session_scope() as session:
                deal = await session.get(Deal, scene["deal_id"])
                assert deal is not None
                deal.deleted_at = dt.datetime.now(dt.UTC)

        run(client, delete)

        assert run(client, _apply, [_row(scene)])["outcomes"] == ["unmatched"]

    def test_empty_batch(self, client) -> None:
        assert run(client, _apply, [])["outcomes"] == []


class TestIncompleteRowsDoNotEraseData:
    _FULL = {
        "enrolled_at": "2026-09-01T10:00:00Z",
        "progress_pct": 60,
        "score": 4.5,
        "completed_at": "2026-09-20T12:00:00Z",
        "last_activity_at": "2026-09-21T09:00:00Z",
    }

    def test_missing_fields_keep_their_values(self, client) -> None:
        scene = run(client, make_scene)
        run(client, _apply, [_row(scene, **self._FULL)])

        run(client, _apply, [_row(scene, progress_pct=80)])

        stored = run(client, _progress, scene["deal_id"])["c-1"]
        assert stored["progress_pct"] == 80
        assert str(stored["score"]) == "4.50"
        assert stored["enrolled_at"] == dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.UTC)
        assert stored["completed_at"] == dt.datetime(2026, 9, 20, 12, 0, tzinfo=dt.UTC)
        assert stored["last_activity_at"] == dt.datetime(2026, 9, 21, 9, 0, tzinfo=dt.UTC)

    def test_explicit_nulls_and_empty_strings_do_not_erase_either(self, client) -> None:
        scene = run(client, make_scene)
        run(client, _apply, [_row(scene, **self._FULL)])

        run(
            client,
            _apply,
            [_row(scene, progress_pct=None, score="", enrolled_at=None, completed_at="")],
        )

        stored = run(client, _progress, scene["deal_id"])["c-1"]
        assert stored["progress_pct"] == 60
        assert str(stored["score"]) == "4.50"
        assert stored["enrolled_at"] is not None and stored["completed_at"] is not None

    def test_a_reported_value_replaces_the_old_one_even_when_lower(self, client) -> None:
        scene = run(client, make_scene)
        run(client, _apply, [_row(scene, progress_pct=90, score=5)])

        run(client, _apply, [_row(scene, progress_pct=0, score=0)])

        stored = run(client, _progress, scene["deal_id"])["c-1"]
        assert stored["progress_pct"] == 0 and str(stored["score"]) == "0.00"

    def test_rejected_row_leaves_the_old_values_untouched(self, client) -> None:
        scene = run(client, make_scene)
        run(client, _apply, [_row(scene, progress_pct=50, score=3)])

        result = run(client, _apply, [_row(scene, progress_pct=999, score=5)])

        assert result["outcomes"] == ["failed"]
        stored = run(client, _progress, scene["deal_id"])["c-1"]
        assert stored["progress_pct"] == 50 and str(stored["score"]) == "3.00"

    def test_raw_is_the_last_delivered_row(self, client) -> None:
        scene = run(client, make_scene)
        run(client, _apply, [_row(scene, progress_pct=10, note="первая")])

        run(client, _apply, [_row(scene, progress_pct=20, note="вторая")])

        assert run(client, _progress, scene["deal_id"])["c-1"]["raw"]["note"] == "вторая"

    def test_contact_and_product_are_linked_from_the_deal(self, client) -> None:
        scene = run(client, make_scene)

        run(client, _apply, [_row(scene, progress_pct=10)])

        stored = run(client, _progress, scene["deal_id"])["c-1"]
        assert stored["contact_id"] == scene["contact_id"]
        assert stored["product_id"] == scene["products"][0]["id"]

    def test_courses_of_one_deal_are_separate_rows(self, client) -> None:
        scene = run(client, make_scene)

        run(client, _apply, [_row(scene, "a", progress_pct=10), _row(scene, "b", progress_pct=20)])
        run(client, _apply, [_row(scene, "a", progress_pct=11)])

        stored = run(client, _progress, scene["deal_id"])
        assert (stored["a"]["progress_pct"], stored["b"]["progress_pct"]) == (11, 20)


# =============================================================================================
# push
# =============================================================================================


class Push:
    def __init__(self, client) -> None:
        self.client = client

    def send(self, rows: list[Any], *, key: str | None = None, signature: Any = None, raw=None):
        body = (
            raw
            if raw is not None
            else json.dumps(
                {"external_id": key or f"push-{uuid.uuid4().hex}", "items": rows}
            ).encode()
        )
        headers = {
            "Content-Type": "application/json",
            "X-Signature": signature or f"sha256={compute_signature(_SECRET, body)}",
        }
        return self.client.post(_PUSH_URL, content=body, headers=headers)


@pytest.fixture
def push(client, monkeypatch: pytest.MonkeyPatch) -> Push:
    monkeypatch.setenv(_SECRET_ENV, _SECRET)
    run(client, _set_lms_source)
    return Push(client)


class TestPushEndpoint:
    def test_one_bad_row_does_not_roll_back_the_batch(self, client, push) -> None:
        scene = run(client, make_scene)
        key = f"push-{uuid.uuid4().hex}"
        rows = [
            _row(scene, "a", progress_pct=10),
            _row(scene, "bad", progress_pct=500),
            _row(scene, "b", progress_pct=20),
            {"deal_id": str(uuid.uuid4()), "course_external_id": "alien"},
        ]

        response = push.send(rows, key=key)

        assert response.status_code == 200, response.text
        assert response.json() == {"status": "processed", "applied": 2, "skipped": 2, "failed": 1}
        assert sorted(run(client, _progress, scene["deal_id"])) == ["a", "b"]
        stored = run(client, _inbound, key)
        assert stored is not None and stored["status"] == "processed"
        assert "Пропущено строк: 2 из 4" in stored["error"]

    def test_clean_batch_reports_no_skipped_rows(self, client, push) -> None:
        scene = run(client, make_scene)
        key = f"push-{uuid.uuid4().hex}"

        response = push.send([_row(scene, "a", progress_pct=10)], key=key)

        assert response.json() == {"status": "processed", "applied": 1, "skipped": 0, "failed": 0}
        stored = run(client, _inbound, key)
        assert stored is not None and stored["error"] is None

    def test_nan_in_the_body_is_a_skipped_row_not_a_500(self, client, push) -> None:
        scene = run(client, make_scene)
        key = f"push-{uuid.uuid4().hex}"
        deal = scene["deal_id"]
        raw = (
            f'{{"external_id": "{key}", "items": ['
            f'{{"deal_id": "{deal}", "course_external_id": "a", "progress_pct": NaN}}, '
            f'{{"deal_id": "{deal}", "course_external_id": "b", "progress_pct": 7}}]}}'
        ).encode()

        response = push.send([], raw=raw)

        assert response.status_code == 200, response.text
        assert response.json()["applied"] == 1 and response.json()["failed"] == 1
        assert sorted(run(client, _progress, scene["deal_id"])) == ["b"]

    def test_replayed_delivery_returns_the_stored_status(self, client, push) -> None:
        scene = run(client, make_scene)
        key = f"push-{uuid.uuid4().hex}"
        first = push.send([_row(scene, "a", progress_pct=10)], key=key)
        assert first.status_code == 200

        again = push.send([_row(scene, "a", progress_pct=99)], key=key)

        assert again.status_code == 200
        assert again.json()["status"] == "processed"
        assert run(client, _progress, scene["deal_id"])["a"]["progress_pct"] == 10

    def test_wrong_signature_is_still_refused_and_does_not_take_the_key(self, client, push) -> None:
        scene = run(client, make_scene)
        key = f"push-{uuid.uuid4().hex}"

        response = push.send([_row(scene, "a", progress_pct=10)], key=key, signature="sha256=00")

        assert response.status_code == 401
        assert response.json()["code"] == "CRM-1701"
        # Подпись проверяется раньше ключа: чужой запрос с угаданным `external_id` его не занимает
        # (запись-улика лежит под собственным `invalid:<uuid>`, см. `test_integration_hardening`),
        # и настоящая доставка с тем же ключом проходит.
        assert run(client, _inbound, key) is None
        assert run(client, _progress, scene["deal_id"]) == {}
        real = push.send([_row(scene, "a", progress_pct=10)], key=key)
        assert real.status_code == 200, real.text
        assert real.json()["applied"] == 1

    def test_malformed_body_is_still_a_validation_error(self, push) -> None:
        assert push.send([], raw=b'{"external_id": "x", "items": "not-a-list"}').status_code == 422

    def test_inactive_source_still_answers_503(self, client, push) -> None:
        run(client, functools.partial(_set_lms_source, active=False))

        assert push.send([]).status_code == 503


# =============================================================================================
# pull
# =============================================================================================


class TestPull:
    @pytest.fixture(autouse=True)
    def _setup(self, client, monkeypatch: pytest.MonkeyPatch) -> None:
        from app.core.config import get_settings

        self.client = client
        self.network = Network().install(monkeypatch)
        monkeypatch.setattr(get_settings(), "lms_base_url", LMS_URL)
        monkeypatch.setattr(get_settings(), "lms_auth_ref", None)
        run(client, _reset_cursor)

    def _serve(self, rows: Any) -> None:
        body = rows if isinstance(rows, dict) else {"items": rows}
        self.network.respond = lambda request: httpx.Response(200, json=body)

    def _pull(self) -> dict[str, int]:
        from app.modules.integration.tasks import sweep_lms_progress_pull

        return run(self.client, functools.partial(sweep_lms_progress_pull, {}))

    def test_cursor_moves_to_the_latest_row(self) -> None:
        scene = run(self.client, make_scene)
        self._serve(
            [
                _row(scene, "a", progress_pct=10, updated_at="2026-09-20T10:00:00Z"),
                _row(scene, "b", progress_pct=20, updated_at="2026-09-22T10:00:00Z"),
                _row(scene, "c", progress_pct=30, updated_at="2026-09-21T10:00:00Z"),
            ]
        )

        result = self._pull()

        assert result == {"pulled": 3, "skipped": 0, "failed": 0}
        assert run(self.client, _cursor) == "2026-09-22T10:00:00Z"

    def test_bad_row_is_skipped_and_the_cursor_stops_before_it(self) -> None:
        scene = run(self.client, make_scene)
        self._serve(
            [
                _row(scene, "a", progress_pct=10, updated_at="2026-09-20T10:00:00Z"),
                _row(scene, "bad", progress_pct=500, updated_at="2026-09-21T10:00:00Z"),
                _row(scene, "c", progress_pct=30, updated_at="2026-09-22T10:00:00Z"),
            ]
        )

        result = self._pull()

        assert result == {"pulled": 2, "skipped": 1, "failed": 1}
        assert sorted(run(self.client, _progress, scene["deal_id"])) == ["a", "c"]
        # Дальше сбойной строки курсор не уходит, хотя строка `c` применена.
        assert run(self.client, _cursor) == "2026-09-20T10:00:00Z"

    def test_failed_row_is_asked_for_again_on_the_next_tick(self) -> None:
        scene = run(self.client, make_scene)
        self._serve(
            [
                _row(scene, "a", progress_pct=10, updated_at="2026-09-20T10:00:00Z"),
                _row(scene, "bad", progress_pct=500, updated_at="2026-09-21T10:00:00Z"),
            ]
        )
        self._pull()

        self._pull()

        first, second = (c for c in self.network.calls if c["url"].endswith("/students/progress"))
        assert first["params"] == {}
        assert second["params"] == {"updated_since": "2026-09-20T10:00:00Z"}

    def test_cursor_does_not_move_when_the_first_row_fails(self) -> None:
        scene = run(self.client, make_scene)
        run(self.client, functools.partial(_reset_cursor, "2026-09-01T00:00:00Z"))
        self._serve(
            [
                _row(scene, "bad", progress_pct=500, updated_at="2026-09-21T10:00:00Z"),
                _row(scene, "c", progress_pct=30, updated_at="2026-09-22T10:00:00Z"),
            ]
        )

        result = self._pull()

        assert result["failed"] == 1 and result["pulled"] == 1
        assert run(self.client, _cursor) == "2026-09-01T00:00:00Z"

    def test_rows_with_the_same_time_as_the_failed_one_do_not_move_the_cursor(self) -> None:
        scene = run(self.client, make_scene)
        self._serve(
            [
                _row(scene, "a", progress_pct=10, updated_at="2026-09-20T10:00:00Z"),
                _row(scene, "bad", progress_pct=500, updated_at="2026-09-21T10:00:00Z"),
                _row(scene, "same", progress_pct=30, updated_at="2026-09-21T10:00:00Z"),
            ]
        )

        self._pull()

        assert run(self.client, _cursor) == "2026-09-20T10:00:00Z"

    def test_failed_row_without_a_time_freezes_the_cursor(self) -> None:
        scene = run(self.client, make_scene)
        self._serve(
            [
                _row(scene, "a", progress_pct=10, updated_at="2026-09-20T10:00:00Z"),
                _row(scene, "bad", progress_pct=500),
            ]
        )

        self._pull()

        assert run(self.client, _cursor) is None

    def test_rows_of_strangers_do_not_hold_the_cursor(self) -> None:
        scene = run(self.client, make_scene)
        self._serve(
            [
                {
                    "deal_id": str(uuid.uuid4()),
                    "course_external_id": "alien",
                    "updated_at": "2026-09-19T10:00:00Z",
                },
                _row(scene, "a", progress_pct=10, updated_at="2026-09-20T10:00:00Z"),
                {"deal_id": None, "course_external_id": "x", "updated_at": "2026-09-23T10:00:00Z"},
            ]
        )

        result = self._pull()

        assert result == {"pulled": 1, "skipped": 2, "failed": 0}
        assert run(self.client, _cursor) == "2026-09-23T10:00:00Z"

    def test_non_object_items_are_skipped_without_an_error(self) -> None:
        scene = run(self.client, make_scene)
        self._serve(
            [
                None,
                "строка",
                5,
                _row(scene, "a", progress_pct=10, updated_at="2026-09-20T10:00:00Z"),
            ]
        )

        result = self._pull()

        assert result == {"pulled": 1, "skipped": 3, "failed": 0}
        assert run(self.client, _cursor) == "2026-09-20T10:00:00Z"

    def test_body_that_is_not_a_list_of_rows_is_an_empty_pull(self) -> None:
        self._serve({"items": "oops"})

        assert self._pull() == {"pulled": 0, "skipped": 0, "failed": 0}
        assert run(self.client, _cursor) is None

    def test_plain_array_body_is_accepted(self) -> None:
        scene = run(self.client, make_scene)
        self.network.respond = lambda request: httpx.Response(
            200,
            json=[_row(scene, "a", progress_pct=10, updated_at="2026-09-20T10:00:00Z")],
        )

        assert self._pull()["pulled"] == 1

    def test_network_failure_leaves_the_cursor_alone(self) -> None:
        run(self.client, functools.partial(_reset_cursor, "2026-09-01T00:00:00Z"))
        self.network.respond = lambda request: httpx.Response(500, json={})

        assert self._pull() == {"pulled": 0, "error": 1}
        assert run(self.client, _cursor) == "2026-09-01T00:00:00Z"

    def test_without_a_base_url_nothing_is_pulled(self, monkeypatch) -> None:
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "lms_base_url", None)

        assert self._pull() == {"pulled": 0, "skipped": 1}
        assert self.network.calls == []
