"""B7-9: отчёт «Подписание по периодам» (`build_signing_periods`,
`app/modules/reporting/builders.py`) — число документов на подпись по месяцам, доля
подписанных, истёкшие/отклонённые, медиана часов от первой отправки подписанту до
завершения документа. Прямой вызов builder'а, тот же приём, что у `build_monthly_dynamics`
в `tests/test_reporting_period.py` (настоящая Postgres обязательна — `date_trunc`/
`percentile_cont` считаются на стороне БД).
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.modules.reporting.builders import build_signing_periods
from tests.conftest import TEST_DATABASE_URL, run
from tests.test_reporting_register import _principal

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


async def _make_document(
    status: str,
    created_at: dt.datetime,
    sent_at: dt.datetime | None,
    completed_at: dt.datetime | None,
) -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.catalog.models import Contact
    from app.modules.files.models import File
    from app.modules.signing.models import SignatureDocument, SignatureRequest

    token = uuid.uuid4().hex[:10]
    async with session_scope() as session:
        contact = Contact(first_name="Тест", last_name="Подписант")
        session.add(contact)
        await session.flush()

        file = File(
            storage_key=f"test/{token}.pdf",
            bucket="files",
            original_filename="doc.pdf",
            mime_type="application/pdf",
            size_bytes=10,
            sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            status="ready",
        )
        session.add(file)
        await session.flush()

        document = SignatureDocument(
            doc_type="kp",
            title=f"Документ {token}",
            entity_type="deal",
            entity_id=uuid.uuid4(),
            file_id=file.id,
            content_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            status=status,
            created_at=created_at,
            completed_at=completed_at,
        )
        session.add(document)
        await session.flush()

        if sent_at is not None:
            session.add(
                SignatureRequest(
                    document_id=document.id,
                    signer_type="external",
                    signer_contact_id=contact.id,
                    signer_role_code="contact",
                    signer_name_snapshot="Тестовый подписант",
                    access_token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                    token_expires_at=sent_at + dt.timedelta(days=7),
                    status="signed" if status == "signed" else "pending",
                    sent_at=sent_at,
                )
            )
            await session.flush()

        return document.id


def _fresh_year() -> int:
    """Builder не скопирован по сделке/пользователю (глобальный агрегат по всей подсистеме
    подписания, см. докстринг `build_signing_periods`) — изоляция теста только датой. Год из
    далёкого будущего, случайный на каждый вызов: ни другие тесты (у них даты около 2026),
    ни повторный прогон этого же теста на не очищаемой между прогонами `crm_test` не попадают
    в то же окно и не завышают счётчики."""
    return 2200 + uuid.uuid4().int % 6800


class TestSigningPeriods:
    def test_counts_shares_and_median_duration_per_month(self, client) -> None:
        principal = _principal(uuid.uuid4(), "ADMIN")
        year = _fresh_year()

        run(
            client,
            _make_document,
            "signed",
            dt.datetime(year, 1, 5, tzinfo=dt.UTC),
            dt.datetime(year, 1, 5, 10, 0, tzinfo=dt.UTC),
            dt.datetime(year, 1, 7, 10, 0, tzinfo=dt.UTC),  # 48 часов
        )
        run(
            client,
            _make_document,
            "signed",
            dt.datetime(year, 1, 10, tzinfo=dt.UTC),
            dt.datetime(year, 1, 10, 0, 0, tzinfo=dt.UTC),
            dt.datetime(year, 1, 10, 12, 0, tzinfo=dt.UTC),  # 12 часов
        )
        run(
            client,
            _make_document,
            "expired",
            dt.datetime(year, 1, 15, tzinfo=dt.UTC),
            dt.datetime(year, 1, 15, 0, 0, tzinfo=dt.UTC),
            None,
        )
        run(
            client,
            _make_document,
            "rejected",
            dt.datetime(year, 2, 1, tzinfo=dt.UTC),
            dt.datetime(year, 2, 1, 0, 0, tzinfo=dt.UTC),
            None,
        )

        async def _build():
            from app.core.db import session_scope

            async with session_scope() as session:
                return await build_signing_periods(
                    session,
                    principal,
                    {"date_from": f"{year}-01-01", "date_to": f"{year}-02-28"},
                )

        dataset = run(client, _build)
        by_month = {row[0]: row for row in dataset.rows}

        jan = by_month[f"{year}-01"]
        # [месяц, всего, подписано, доля%, истекло, отклонено, медиана часов]
        assert jan[1] == 3
        assert jan[2] == 2
        assert jan[3] == pytest.approx(66.7, abs=0.1)
        assert jan[4] == 1
        assert jan[5] == 0
        assert jan[6] == pytest.approx(30.0, abs=0.01)  # медиана(48, 12) = 30

        feb = by_month[f"{year}-02"]
        assert feb[1] == 1
        assert feb[2] == 0
        assert feb[3] == 0.0
        assert feb[5] == 1
        assert feb[6] is None

    def test_a_month_without_any_document_is_zero_not_missing(self, client) -> None:
        principal = _principal(uuid.uuid4(), "ADMIN")
        year = _fresh_year()

        run(
            client,
            _make_document,
            "signed",
            dt.datetime(year, 3, 5, tzinfo=dt.UTC),
            dt.datetime(year, 3, 5, tzinfo=dt.UTC),
            dt.datetime(year, 3, 5, 1, tzinfo=dt.UTC),
        )

        async def _build():
            from app.core.db import session_scope

            async with session_scope() as session:
                return await build_signing_periods(
                    session,
                    principal,
                    {"date_from": f"{year}-01-01", "date_to": f"{year}-03-31"},
                )

        dataset = run(client, _build)
        by_month = {row[0]: row for row in dataset.rows}

        assert set(by_month) == {f"{year}-01", f"{year}-02", f"{year}-03"}
        assert by_month[f"{year}-02"][1] == 0
