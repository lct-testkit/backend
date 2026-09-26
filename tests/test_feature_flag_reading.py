"""Чтение флагов функциональности: процент раскатки действительно применяется."""

from __future__ import annotations

import uuid

import pytest

from app.modules.admin.flags import is_feature_enabled, rollout_bucket
from tests.conftest import TEST_DATABASE_URL, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _flag(client, *, enabled: bool, rollout: int) -> str:
    from app.core.db import session_scope
    from app.modules.admin.models import FeatureFlag

    code = f"flag_{uuid.uuid4().hex[:10]}"

    async def _create() -> None:
        async with session_scope() as session:
            session.add(FeatureFlag(code=code, is_enabled=enabled, rollout=rollout))

    run(client, _create)
    return code


def _enabled(client, code: str, subject=None, **kwargs) -> bool:
    from app.core.db import session_scope

    async def _read() -> bool:
        async with session_scope() as session:
            return await is_feature_enabled(session, code, subject_id=subject, **kwargs)

    return run(client, _read)


class TestFeatureFlagReading:
    def test_unknown_flag_falls_back_to_the_default(self, client) -> None:
        assert _enabled(client, "no_such_flag_" + uuid.uuid4().hex) is True
        assert _enabled(client, "no_such_flag_" + uuid.uuid4().hex, default=False) is False

    def test_disabled_flag_is_off_for_everyone(self, client) -> None:
        code = _flag(client, enabled=False, rollout=100)
        assert _enabled(client, code, uuid.uuid4()) is False

    def test_full_and_zero_rollout(self, client) -> None:
        assert _enabled(client, _flag(client, enabled=True, rollout=100)) is True
        assert _enabled(client, _flag(client, enabled=True, rollout=0), uuid.uuid4()) is False

    def test_partial_rollout_is_stable_per_subject_and_grows_monotonically(self, client) -> None:
        code = _flag(client, enabled=True, rollout=40)
        users = [uuid.uuid4() for _ in range(200)]

        first = [_enabled(client, code, user) for user in users[:20]]
        again = [_enabled(client, code, user) for user in users[:20]]
        assert first == again  # тот же субъект — тот же ответ

        on = sum(rollout_bucket(code, user) < 40 for user in users)
        assert 40 < on < 120  # около 40% из 200, с большим запасом на разброс

        wider = _flag(client, enabled=True, rollout=70)
        # Корзина не зависит от процента: кто включён при 40%, включён и при 70% (тот же код).
        for user in users:
            if rollout_bucket(code, user) < 40:
                assert rollout_bucket(code, user) < 70
        assert wider

    def test_partial_rollout_without_a_subject_is_off(self, client) -> None:
        assert _enabled(client, _flag(client, enabled=True, rollout=50)) is False
