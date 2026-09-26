"""Версионирование хэша аудита и предел ожидания лока цепочки.

Хэш записи раньше не покрывал роль актора, подмену личности, IP и User-Agent: их можно было
поправить в БД, а цепочка не заметила бы. Состав расширен как версия 2; записи версии 1 (всё,
что было до расширения) обязаны проверяться по-старому, иначе `verify_chain` сломался бы на
существующих данных. Лок цепочки один на систему — без предела ожидания зависшая транзакция
останавливала бы аудируемые изменения у всех.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid

import pytest

from app.modules.audit.service import GENESIS_HASH, compute_hash
from tests.conftest import TEST_DATABASE_URL, run

_BASE = {
    "prev_hash": GENESIS_HASH,
    "created_at": "2026-06-01T12:30:00+00:00",
    "actor_id": "11111111-1111-1111-1111-111111111111",
    "action": "DEAL_CREATED",
    "entity_type": "deal",
    "entity_id": "22222222-2222-2222-2222-222222222222",
    "changes": {"title": {"old": None, "new": "Сделка"}},
    "result": "success",
    "request_id": "req-1",
}


def _legacy_digest() -> str:
    """Хэш ровно так, как его считали до расширения: девять полей, канонический JSON."""
    canonical = json.dumps(_BASE, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TestHashVersions:
    def test_version_1_is_byte_for_byte_the_legacy_hash(self) -> None:
        assert compute_hash(**_BASE, version=1) == _legacy_digest()
        # Без указания версии — тоже прежняя (существующие вызовы и тесты).
        assert compute_hash(**_BASE) == _legacy_digest()

    def test_version_1_ignores_the_new_fields(self) -> None:
        with_extras = compute_hash(**_BASE, version=1, ip="10.0.0.1", actor_role="ADMIN")
        assert with_extras == _legacy_digest()

    @pytest.mark.parametrize(
        "override",
        [
            {"actor_role": "ADMIN"},
            {"impersonated_by": "33333333-3333-3333-3333-333333333333"},
            {"ip": "10.0.0.1"},
            {"user_agent": "curl/8"},
        ],
    )
    def test_version_2_covers_role_impersonation_ip_and_user_agent(self, override) -> None:
        plain = compute_hash(**_BASE, version=2)
        assert compute_hash(**_BASE, version=2, **override) != plain

    def test_version_2_is_not_confusable_with_version_1(self) -> None:
        assert compute_hash(**_BASE, version=2) != compute_hash(**_BASE, version=1)


pytestmark_db = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


async def _record_v2_and_verify() -> tuple[int, dict]:
    from sqlalchemy import select

    from app.core.db import get_session_factory
    from app.modules.audit.models import AuditLog
    from app.modules.audit.service import AuditService

    factory = get_session_factory()
    marker = f"HASH_V2_{uuid.uuid4().hex[:8]}"
    async with factory() as session:
        await AuditService(session).record(marker, entity_type="hash_test")
        await session.commit()
    async with factory() as session:
        version = await session.scalar(
            select(AuditLog.hash_version).where(AuditLog.action == marker)
        )
        report = await AuditService(session).verify_chain(limit=5)
    return int(version), report


async def _legacy_head_then_new_record() -> dict:
    """Запись версии 1 (как в БД до расширения) становится головой, за ней — обычная запись."""
    from sqlalchemy import text

    from app.core.db import get_session_factory
    from app.modules.audit.models import AuditLog
    from app.modules.audit.service import AuditService

    factory = get_session_factory()
    async with factory() as session:
        service = AuditService(session)
        prev = await service._chain_head()
        now = await session.scalar(text("SELECT clock_timestamp()"))
        entry = AuditLog(
            action=f"HASH_V1_{uuid.uuid4().hex[:8]}",
            entity_type="hash_test",
            result="success",
            prev_hash=prev,
            hash_version=1,
            ip="10.1.2.3",
            user_agent="legacy",
            actor_role="KAM",
        )
        entry.created_at = now
        entry.hash = compute_hash(
            prev_hash=prev,
            created_at=now.isoformat(),
            actor_id=None,
            action=entry.action,
            entity_type="hash_test",
            entity_id=None,
            changes=None,
            result="success",
            request_id=None,
            version=1,
        )
        session.add(entry)
        await session.flush()
        await session.commit()
    async with factory() as session:
        await AuditService(session).record("HASH_AFTER_LEGACY", entity_type="hash_test")
        await session.commit()
    async with factory() as session:
        return await AuditService(session).verify_chain(limit=3)


@pytestmark_db
class TestVerifyChainWithMixedVersions:
    def test_new_records_are_version_2_and_verify(self, client) -> None:
        version, report = run(client, _record_v2_and_verify)
        assert version == 2
        assert report["ok"], report["problems"]

    def test_a_legacy_version_1_record_still_verifies(self, client) -> None:
        report = run(client, _legacy_head_then_new_record)
        assert report["ok"], report["problems"]
        assert report["checked"] == 3


async def _hold_lock_and_try_to_record(timeout_ms: int) -> dict:
    from sqlalchemy import text

    from app.core.db import get_session_factory
    from app.core.errors import AppError, ErrorCode
    from app.modules.audit import service as audit_service
    from app.modules.audit.service import AuditService

    factory = get_session_factory()
    holder = factory()
    waiter = factory()
    original = audit_service._AUDIT_LOCK_TIMEOUT_MS
    audit_service._AUDIT_LOCK_TIMEOUT_MS = timeout_ms
    result: dict = {}
    try:
        await holder.execute(
            text("SELECT pg_advisory_xact_lock(:id)"), {"id": audit_service._AUDIT_CHAIN_LOCK_ID}
        )
        started = time.monotonic()
        try:
            await AuditService(waiter).record("LOCK_TIMEOUT_TEST", entity_type="hash_test")
            result["error"] = None
        except AppError as exc:
            result["error"] = exc.code
            result["is_503"] = exc.status == 503 and exc.code is ErrorCode.DEPENDENCY_UNAVAILABLE
        result["waited"] = time.monotonic() - started
        await waiter.rollback()
        result["holder_setting"] = await holder.scalar(
            text("SELECT current_setting('lock_timeout')")
        )
        await holder.commit()  # лок свободен

        async with factory() as fresh:
            await AuditService(fresh).record("LOCK_TIMEOUT_AFTER", entity_type="hash_test")
            result["setting_after_record"] = await fresh.scalar(
                text("SELECT current_setting('lock_timeout')")
            )
            await fresh.commit()
    finally:
        audit_service._AUDIT_LOCK_TIMEOUT_MS = original
        await holder.close()
        await waiter.close()
    return result


@pytestmark_db
class TestChainLockTimeout:
    def test_waiting_for_the_chain_lock_is_bounded(self, client) -> None:
        result = run(client, _hold_lock_and_try_to_record, 300)

        assert result["is_503"], result
        assert result["waited"] < 5, result
        # `lock_timeout` действует только на время захвата и возвращается прежним.
        assert result["holder_setting"] == "0"
        assert result["setting_after_record"] == "0"
