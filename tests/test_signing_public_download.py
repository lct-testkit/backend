"""B7-3: публичная выдача подписанного файла и протокола по отдельной ссылке.

Ссылка "только скачать" (`signature_download_links`) отдельна от токена подписания: не даёт
права подписывать, свой TTL и лимит скачиваний, отзывается при `void`. Хранилище подменено
(настоящего S3 в тестовом окружении нет) — проверяется маршрутизация, коды ошибок, заголовки
и счётчик, не HTTP-клиент S3."""

from __future__ import annotations

import datetime as dt
import functools
import hashlib
import secrets
import uuid
from types import SimpleNamespace

import pytest

from tests.conftest import TEST_DATABASE_URL, run
from tests.signing_helpers import _build

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


@pytest.fixture
def stub_download(monkeypatch):
    """Подменяет скачивание из S3 в signing.service: отдаёт байты по bucket/key из уже
    созданных тестовых File-записей, минуя настоящее хранилище."""
    from app.modules.signing import service as svc

    async def download(*, bucket: str, key: str) -> bytes:
        return b"%PDF-1.4 stub body for " + key.encode()

    monkeypatch.setattr(svc, "download_object_bytes", download)


async def _create_link(
    document_id: uuid.UUID,
    *,
    expires_in_days: int = 30,
    revoked: bool = False,
    downloads: int = 0,
) -> str:
    from app.core.db import session_scope
    from app.modules.signing.models import SignatureDownloadLink

    token = secrets.token_urlsafe(32)
    now = dt.datetime.now(dt.UTC)
    async with session_scope() as session:
        session.add(
            SignatureDownloadLink(
                document_id=document_id,
                token_hash=hashlib.sha256(token.encode()).hexdigest(),
                expires_at=now + dt.timedelta(days=expires_in_days),
                revoked_at=now if revoked else None,
                downloads=downloads,
            )
        )
    return token


class TestDownloadRoutes:
    def test_valid_token_downloads_signed_file_with_headers(self, client, stub_download) -> None:
        built = _build(client, requests=[{"status": "signed"}], with_results=True)
        token = run(client, _create_link, built.document_id)

        response = client.get(f"/public/signed/{token}")

        assert response.status_code == 200, response.text
        assert response.headers["content-type"] == "application/pdf"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-robots-tag"] == "noindex"
        assert "attachment" in response.headers["content-disposition"]
        assert str(built.signed_file_id) is not None

    def test_protocol_variant(self, client, stub_download) -> None:
        built = _build(client, requests=[{"status": "signed"}], with_results=True)
        token = run(client, _create_link, built.document_id)

        response = client.get(f"/public/signed/{token}/protocol")

        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"

    def test_unknown_token_is_404_with_the_shared_code(self, client) -> None:
        response = client.get(f"/public/signed/{secrets.token_urlsafe(32)}")
        assert response.status_code == 404
        assert response.json()["code"] == "CRM-1504"

    def test_expired_token_is_404_with_the_shared_code(self, client, stub_download) -> None:
        built = _build(client, requests=[{"status": "signed"}], with_results=True)
        token = run(client, functools.partial(_create_link, built.document_id, expires_in_days=-1))

        response = client.get(f"/public/signed/{token}")

        assert response.status_code == 404
        assert response.json()["code"] == "CRM-1504"

    def test_revoked_token_is_404_with_the_shared_code(self, client, stub_download) -> None:
        built = _build(client, requests=[{"status": "signed"}], with_results=True)
        token = run(client, functools.partial(_create_link, built.document_id, revoked=True))

        response = client.get(f"/public/signed/{token}")

        assert response.status_code == 404
        assert response.json()["code"] == "CRM-1504"

    def test_exceeding_the_download_limit_is_429(self, client, stub_download) -> None:
        built = _build(client, requests=[{"status": "signed"}], with_results=True)
        # Лимит по умолчанию 20 (SIGNATURE_DOWNLOAD_MAX_PER_LINK) — создаём ссылку уже «на
        # пределе», чтобы не делать 20 запросов.
        token = run(client, functools.partial(_create_link, built.document_id, downloads=20))

        response = client.get(f"/public/signed/{token}")

        assert response.status_code == 429
        assert response.json()["code"] == "CRM-8429"

    def test_each_download_increments_the_counter(self, client, stub_download) -> None:
        built = _build(client, requests=[{"status": "signed"}], with_results=True)
        token = run(client, _create_link, built.document_id)

        first = client.get(f"/public/signed/{token}")
        second = client.get(f"/public/signed/{token}")

        assert first.status_code == 200
        assert second.status_code == 200

        async def _downloads() -> int:
            from sqlalchemy import select

            from app.core.db import session_scope
            from app.modules.signing.models import SignatureDownloadLink

            async with session_scope() as session:
                return await session.scalar(
                    select(SignatureDownloadLink.downloads).where(
                        SignatureDownloadLink.token_hash
                        == hashlib.sha256(token.encode()).hexdigest()
                    )
                )

        assert run(client, _downloads) == 2

    def test_sign_token_does_not_work_on_the_download_route(self, client) -> None:
        sign_token = secrets.token_urlsafe(32)
        built = _build(client, requests=[{"status": "sent", "token": sign_token}])
        token = built.tokens[built.request_ids[0]]

        response = client.get(f"/public/signed/{token}")

        assert response.status_code == 404

    def test_download_token_does_not_work_on_the_sign_route(self, client, stub_download) -> None:
        built = _build(client, requests=[{"status": "signed"}], with_results=True)
        token = run(client, _create_link, built.document_id)

        response = client.get(f"/public/sign/{token}")

        assert response.status_code == 404

    def test_disabling_the_feature_hides_the_route(
        self, client, stub_download, monkeypatch
    ) -> None:
        from app.modules.signing import public_router as pr

        built = _build(client, requests=[{"status": "signed"}], with_results=True)
        token = run(client, _create_link, built.document_id)

        monkeypatch.setattr(
            pr, "get_settings", lambda: SimpleNamespace(signature_public_download_enabled=False)
        )
        response = client.get(f"/public/signed/{token}")

        assert response.status_code == 404


class TestVoidRevokesLinks:
    def test_void_revokes_active_links(self, client, stub_download) -> None:
        built = _build(client, requests=[{"status": "signed"}], with_results=True)
        token = run(client, _create_link, built.document_id)

        async def _void():
            from app.core.db import session_scope
            from app.modules.signing.models import SignatureDocument
            from app.modules.signing.service import SignatureDocumentService

            async with session_scope() as session:
                document = await session.get(SignatureDocument, built.document_id)
                principal = SimpleNamespace(user_id=uuid.uuid4())
                await SignatureDocumentService(session).void(
                    document, principal=principal, reason="спор по документу"
                )

        run(client, _void)

        response = client.get(f"/public/signed/{token}")
        assert response.status_code == 404
        assert response.json()["code"] == "CRM-1504"
