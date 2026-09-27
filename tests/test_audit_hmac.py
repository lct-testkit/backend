"""HMAC хэша аудита (версия 3) и предел простоя транзакции.

Голый SHA-256 пересчитает любой, у кого есть запись в БД: он правит запись и переписывает
хэши всех последующих. С ключом `AUDIT_HMAC_KEY` (вне БД) новые записи пишутся версией 3 —
тот же состав полей, но HMAC. Старые записи неизменяемы, поэтому цепочка обязана проверяться
смешанной: версия 1, 2 и 3 подряд.

Записи, которые пишут эти тесты, в БД не остаются: цепочка проверяется внутри открытой
транзакции, и она откатывается. Иначе запись версии 3 осталась бы в общей БД, и любой другой
тест, проверяющий хвост цепочки без ключа, увидел бы её как «непроверяемую».
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid

import pytest
from pydantic import SecretStr

from app.modules.audit.service import GENESIS_HASH, compute_hash
from tests.conftest import TEST_DATABASE_URL, run

_KEY = b"audit-test-key-0123456789"
_OTHER_KEY = b"another-key-9876543210"

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
    "actor_role": "KAM",
    "ip": "10.0.0.1",
    "user_agent": "pytest",
}


class TestVersion3Hash:
    def test_it_is_hmac_sha256_over_the_canonical_version_2_payload(self) -> None:
        payload = {
            k: v for k, v in _BASE.items() if k not in {"actor_role", "ip", "user_agent"}
        } | {
            "v": 3,
            "actor_role": "KAM",
            "impersonated_by": None,
            "ip": "10.0.0.1",
            "user_agent": "pytest",
        }
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
        )
        expected = hmac.new(_KEY, canonical.encode("utf-8"), hashlib.sha256).hexdigest()

        assert compute_hash(**_BASE, version=3, hmac_key=_KEY) == expected

    def test_it_is_not_the_plain_sha256_of_the_same_payload(self) -> None:
        assert compute_hash(**_BASE, version=3, hmac_key=_KEY) != compute_hash(**_BASE, version=2)

    def test_the_key_changes_the_hash(self) -> None:
        assert compute_hash(**_BASE, version=3, hmac_key=_KEY) != compute_hash(
            **_BASE, version=3, hmac_key=_OTHER_KEY
        )

    def test_versions_1_and_2_ignore_a_key(self) -> None:
        for version in (1, 2):
            assert compute_hash(**_BASE, version=version, hmac_key=_KEY) == compute_hash(
                **_BASE, version=version
            )

    def test_version_3_without_a_key_is_an_error_not_a_silent_sha256(self) -> None:
        with pytest.raises(ValueError, match="ключ"):
            compute_hash(**_BASE, version=3)

    def test_an_unknown_version_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="версия"):
            compute_hash(**_BASE, version=4, hmac_key=_KEY)


pytestmark_db = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _set_key(monkeypatch, key: bytes | None) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(
        get_settings(), "audit_hmac_key", SecretStr(key.decode()) if key is not None else None
    )


async def _add_raw(session, *, version: int, key: bytes | None, hashed_ip: str = "10.1.2.3") -> str:
    """Запись с явно заданной версией — так, как её писал бы код соответствующей эпохи.
    `hashed_ip` — адрес, с которым посчитан хэш; в строке всегда лежит 10.1.2.3."""
    from sqlalchemy import text, update

    from app.modules.audit.models import AuditChainHead, AuditLog
    from app.modules.audit.service import AuditService

    prev = await AuditService(session)._chain_head()
    now = await session.scalar(text("SELECT clock_timestamp()"))
    entry = AuditLog(
        action=f"HMAC_V{version}_{uuid.uuid4().hex[:8]}",
        entity_type="hash_test",
        result="success",
        prev_hash=prev,
        hash_version=version,
        ip="10.1.2.3",
        user_agent="pytest",
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
        version=version,
        actor_role="KAM",
        ip=hashed_ip,
        user_agent="pytest",
        hmac_key=key,
    )
    session.add(entry)
    await session.flush()
    # `_add_raw` заменяет собой `AuditService.record()` (пишет «как код той эпохи») — значит,
    # и указатель головы цепочки должен продвигаться так же, иначе следующий вызов в этом же
    # цикле (`_chain_with` строит цепочку из нескольких версий подряд) увидит старую голову.
    await session.execute(update(AuditChainHead).values(hash=entry.hash))
    return str(entry.id)


async def _in_rolled_back_transaction(scenario) -> dict:
    from app.core.db import get_session_factory

    async with get_session_factory()() as session:
        try:
            return await scenario(session)
        finally:
            await session.rollback()


async def _new_records_are_v3() -> dict:
    from sqlalchemy import select

    from app.modules.audit.models import AuditLog
    from app.modules.audit.service import AuditService

    async def scenario(session) -> dict:
        marker = f"HMAC_NEW_{uuid.uuid4().hex[:8]}"
        await AuditService(session).record(marker, entity_type="hash_test")
        version = await session.scalar(
            select(AuditLog.hash_version).where(AuditLog.action == marker)
        )
        report = await AuditService(session).verify_chain(limit=3)
        return {"version": version, "report": report}

    return await _in_rolled_back_transaction(scenario)


async def _mixed_chain(monkeypatch, with_key: bool) -> dict:
    """v1 → v2 → v3 подряд, проверка — с ключом или без."""
    from app.modules.audit.service import AuditService

    async def scenario(session) -> dict:
        await _add_raw(session, version=1, key=None)
        await _add_raw(session, version=2, key=None)
        await _add_raw(session, version=3, key=_KEY)
        _set_key(monkeypatch, _KEY if with_key else None)
        return await AuditService(session).verify_chain(limit=3)

    return await _in_rolled_back_transaction(scenario)


async def _chain_with(monkeypatch, versions: list[tuple[int, bytes | None]], verify_key) -> dict:
    from app.modules.audit.service import AuditService

    async def scenario(session) -> dict:
        for version, key in versions:
            await _add_raw(session, version=version, key=key)
        _set_key(monkeypatch, verify_key)
        return await AuditService(session).verify_chain(limit=len(versions))

    return await _in_rolled_back_transaction(scenario)


async def _forged_ip(monkeypatch) -> dict:
    from app.modules.audit.service import AuditService

    async def scenario(session) -> dict:
        # В строке адрес другой, чем в хэше: как если бы его поправили в БД.
        await _add_raw(session, version=3, key=_KEY, hashed_ip="10.9.9.9")
        _set_key(monkeypatch, _KEY)
        return await AuditService(session).verify_chain(limit=1)

    return await _in_rolled_back_transaction(scenario)


@pytestmark_db
class TestVerifyChainWithHmac:
    def test_with_a_key_new_records_are_version_3_and_verify(self, client, monkeypatch) -> None:
        _set_key(monkeypatch, _KEY)
        result = run(client, _new_records_are_v3)
        assert result["version"] == 3
        assert result["report"]["ok"], result["report"]["problems"]

    def test_without_a_key_new_records_stay_version_2(self, client, monkeypatch) -> None:
        _set_key(monkeypatch, None)
        result = run(client, _new_records_are_v3)
        assert result["version"] == 2
        assert result["report"]["ok"], result["report"]["problems"]

    def test_a_mixed_v1_v2_v3_chain_verifies_with_the_key(self, client, monkeypatch) -> None:
        report = run(client, _mixed_chain, monkeypatch, True)
        assert report["ok"], report["problems"]
        assert report["checked"] == 3

    def test_a_wrong_key_is_reported_as_a_hash_mismatch_naming_the_key(
        self, client, monkeypatch
    ) -> None:
        report = run(client, _chain_with, monkeypatch, [(3, _KEY)], _OTHER_KEY)
        assert not report["ok"]
        assert len(report["problems"]) == 1
        assert "не совпадает" in report["problems"][0]
        assert "AUDIT_HMAC_KEY" in report["problems"][0]

    def test_a_missing_key_says_so_instead_of_reporting_forgery(self, client, monkeypatch) -> None:
        report = run(client, _mixed_chain, monkeypatch, False)
        assert not report["ok"]
        assert len(report["problems"]) == 1
        problem = report["problems"][0]
        assert "не задан AUDIT_HMAC_KEY" in problem
        assert "не совпадает" not in problem
        # Записи версий 1 и 2 при этом проверены обычным путём, цепочка не порвана.
        assert "разрыв" not in problem

    def test_a_field_changed_under_a_valid_looking_hash_is_caught(
        self, client, monkeypatch
    ) -> None:
        report = run(client, _forged_ip, monkeypatch)
        assert not report["ok"]
        assert "не совпадает" in report["problems"][0]

    def test_a_downgrade_after_an_hmac_record_is_flagged_when_a_key_is_set(
        self, client, monkeypatch
    ) -> None:
        # Записи, переписанные голым SHA-256 «под версию 2», иначе выглядели бы штатно.
        report = run(client, _chain_with, monkeypatch, [(3, _KEY), (2, None)], _KEY)
        assert not report["ok"]
        assert any("понижение" in problem for problem in report["problems"])

    def test_a_downgrade_is_not_flagged_for_a_plain_upgrade_path(self, client, monkeypatch) -> None:
        report = run(client, _chain_with, monkeypatch, [(1, None), (2, None), (3, _KEY)], _KEY)
        assert report["ok"], report["problems"]

    def test_an_unknown_version_is_reported(self, client, monkeypatch) -> None:
        async def scenario(session) -> dict:
            from sqlalchemy import text

            from app.modules.audit.models import AuditLog
            from app.modules.audit.service import AuditService

            # Запись «из будущего»: версия, которую этот код не умеет проверять.
            future = AuditLog(
                action="HMAC_FUTURE",
                result="success",
                prev_hash=await AuditService(session)._chain_head(),
                hash="0" * 64,
                hash_version=9,
            )
            future.created_at = await session.scalar(text("SELECT clock_timestamp()"))
            session.add(future)
            await session.flush()
            return await AuditService(session).verify_chain(limit=1)

        report = run(client, _in_rolled_back_transaction, scenario)
        assert not report["ok"]
        assert "неизвестная версия" in report["problems"][0]


class TestIdleInTransactionTimeout:
    def test_the_setting_is_passed_to_new_connections_in_milliseconds(self, monkeypatch) -> None:
        from app.core import db
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "db_idle_in_transaction_timeout_seconds", 45)
        assert db._server_settings()["idle_in_transaction_session_timeout"] == "45000"
        assert db._server_settings()["jit"] == "off"

    def test_zero_disables_the_limit_and_a_negative_value_does_not_break_startup(
        self, monkeypatch
    ) -> None:
        from app.core import db
        from app.core.config import get_settings

        monkeypatch.setattr(get_settings(), "db_idle_in_transaction_timeout_seconds", 0)
        assert db._server_settings()["idle_in_transaction_session_timeout"] == "0"
        monkeypatch.setattr(get_settings(), "db_idle_in_transaction_timeout_seconds", -5)
        assert db._server_settings()["idle_in_transaction_session_timeout"] == "0"

    @pytestmark_db
    def test_a_live_connection_carries_the_configured_timeout(self, client) -> None:
        async def read() -> str | None:
            from sqlalchemy import text

            from app.core.db import get_engine

            async with get_engine().connect() as conn:
                return await conn.scalar(
                    text(
                        "SELECT setting FROM pg_settings "
                        "WHERE name = 'idle_in_transaction_session_timeout'"
                    )
                )

        from app.core.config import get_settings

        expected = get_settings().db_idle_in_transaction_timeout_seconds * 1000
        assert int(run(client, read)) == expected
