"""B6-11: `_client_for(bucket)` — бакет `signatures` через отдельный клиент на выделенных
ключах (identity `crm-signatures-writer`), если они заданы; иначе общий клиент, как раньше.
Также `ensure_bucket` при `S3_AUTO_CREATE_BUCKETS=false`.

`client` — не сам HTTP-клиент (здесь без него), а фикстура `tests/conftest.py`: она готовит
окружение (`APP_PROFILE`, пароли, `get_settings.cache_clear()`), без которого `get_settings()`
упал бы валидацией недостающих полей. Проверяется сам boto3-клиент (его
`_request_signer._credentials`), а не сетевой вызов — SeaweedFS в тестах нет.
"""

from __future__ import annotations

import asyncio

import pytest

from app.core import storage
from app.core.config import Settings, get_settings
from app.core.errors import AppError
from tests.conftest import TEST_DATABASE_URL

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _access_key(boto_client) -> str:
    return boto_client._request_signer._credentials.access_key  # noqa: SLF001 — только тест


@pytest.fixture(autouse=True)
def _clear_client_caches(client):  # noqa: ARG001 — готовит окружение для get_settings()
    """`_client`/`_signatures_client` — `lru_cache(maxsize=1)`: без сброса между тестами
    первый вызов в процессе навсегда зафиксировал бы учётные данные того теста."""
    storage._client.cache_clear()
    storage._signatures_client.cache_clear()
    yield
    storage._client.cache_clear()
    storage._signatures_client.cache_clear()


class TestClientFor:
    def test_without_separate_keys_everything_uses_the_general_client(self, monkeypatch) -> None:
        settings = get_settings()
        monkeypatch.setattr(settings, "s3_signatures_access_key", None)
        monkeypatch.setattr(settings, "s3_signatures_secret_key", None)

        general = storage._client_for(settings.s3_bucket_files)
        signatures = storage._client_for(settings.s3_bucket_signatures)

        assert _access_key(general) == settings.s3_access_key.get_secret_value()
        assert _access_key(signatures) == settings.s3_access_key.get_secret_value()

    def test_with_separate_keys_only_the_signatures_bucket_uses_them(self, monkeypatch) -> None:
        from pydantic import SecretStr

        settings = get_settings()
        monkeypatch.setattr(settings, "s3_signatures_access_key", SecretStr("sig-access"))
        monkeypatch.setattr(settings, "s3_signatures_secret_key", SecretStr("sig-secret"))

        files_client = storage._client_for(settings.s3_bucket_files)
        signatures_client = storage._client_for(settings.s3_bucket_signatures)

        assert _access_key(files_client) == settings.s3_access_key.get_secret_value()
        assert _access_key(signatures_client) == "sig-access"

    def test_a_bucket_named_the_same_as_signatures_by_coincidence_is_unaffected_when_unset(
        self, monkeypatch
    ) -> None:
        settings = get_settings()
        monkeypatch.setattr(settings, "s3_signatures_access_key", None)
        monkeypatch.setattr(settings, "s3_signatures_secret_key", None)

        result = storage._client_for("totally-unrelated-bucket")

        assert _access_key(result) == settings.s3_access_key.get_secret_value()


class TestBlankSignaturesKeysMeanUnset:
    """`.env.example`: «пусто — как раньше, общий клиент на все бакеты». Пустая строка (строка в
    `.env` без значения, `${VAR:-}` в compose) без сведения к `None` стала бы `SecretStr('')`,
    и `_client_for` выбрал бы клиент `signatures` с пустыми ключами."""

    def test_blank_values_become_none(self) -> None:
        base = get_settings().model_dump()

        settings = Settings.model_validate(
            {**base, "s3_signatures_access_key": "", "s3_signatures_secret_key": "  "}
        )

        assert settings.s3_signatures_access_key is None
        assert settings.s3_signatures_secret_key is None

    def test_a_blank_pair_falls_back_to_the_shared_client(self, monkeypatch) -> None:
        settings = get_settings()
        blank = Settings.model_validate(
            {
                **settings.model_dump(),
                "s3_signatures_access_key": "",
                "s3_signatures_secret_key": "",
            }
        )
        monkeypatch.setattr(storage, "get_settings", lambda: blank)

        result = storage._client_for(blank.s3_bucket_signatures)

        assert _access_key(result) == blank.s3_access_key.get_secret_value()


class TestEnsureBucketAutoCreate:
    def test_auto_create_false_raises_a_clear_error_when_the_bucket_is_missing(
        self, monkeypatch
    ) -> None:
        settings = get_settings()
        monkeypatch.setattr(settings, "s3_auto_create_buckets", False)

        class _FakeClient:
            def head_bucket(self, Bucket: str) -> None:  # noqa: N803
                from botocore.exceptions import ClientError

                raise ClientError({"Error": {"Code": "404"}}, "HeadBucket")

            def create_bucket(self, Bucket: str) -> None:  # noqa: N803
                raise AssertionError("не должен создавать бакет при s3_auto_create_buckets=false")

        monkeypatch.setattr(storage, "_client_for", lambda _bucket: _FakeClient())

        with pytest.raises(AppError):
            asyncio.run(storage.ensure_bucket("some-bucket"))

    def test_a_forbidden_head_is_not_reported_as_a_missing_bucket(self, monkeypatch) -> None:
        settings = get_settings()
        monkeypatch.setattr(settings, "s3_auto_create_buckets", False)

        class _FakeClient:
            def head_bucket(self, Bucket: str) -> None:  # noqa: N803
                from botocore.exceptions import ClientError

                raise ClientError({"Error": {"Code": "403"}}, "HeadBucket")

            def create_bucket(self, Bucket: str) -> None:  # noqa: N803
                raise AssertionError("не должен создавать бакет при s3_auto_create_buckets=false")

        monkeypatch.setattr(storage, "_client_for", lambda _bucket: _FakeClient())

        with pytest.raises(AppError) as raised:
            asyncio.run(storage.ensure_bucket("some-bucket"))

        assert "не существует" not in raised.value.detail
        assert "403" in raised.value.detail

    def test_auto_create_true_creates_the_missing_bucket(self, monkeypatch) -> None:
        settings = get_settings()
        monkeypatch.setattr(settings, "s3_auto_create_buckets", True)
        created: list[str] = []

        class _FakeClient:
            def head_bucket(self, Bucket: str) -> None:  # noqa: N803
                from botocore.exceptions import ClientError

                raise ClientError({"Error": {"Code": "404"}}, "HeadBucket")

            def create_bucket(self, Bucket: str) -> None:  # noqa: N803
                created.append(Bucket)

        monkeypatch.setattr(storage, "_client_for", lambda _bucket: _FakeClient())

        asyncio.run(storage.ensure_bucket("some-bucket"))

        assert created == ["some-bucket"]
