"""PDF на странице подписи: просмотр без скачивания и без CORS (`GET .../file`).

Presigned-ссылка ведёт на другой origin (S3), у которого нет CORS, и заставляет
браузер скачивать файл (`attachment`). Тот же PDF отдаётся с origin приложения
(`file_url`), а presigned-ссылка — с `inline`. Сквозные тесты на настоящей
PostgreSQL (`TEST_DATABASE_URL`) — см. докстринг `tests/conftest.py`; S3
подменён: байты документа отдаёт заглушка.
"""

from __future__ import annotations

import datetime as dt
import uuid
from urllib.parse import parse_qs, urlparse

import pytest

from tests.conftest import TEST_DATABASE_URL, _make_user, run
from tests.signing_helpers import _build, _login

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)

PDF = b"%PDF-1.4 the contract to be signed"


@pytest.fixture(autouse=True)
def s3_returns_the_document(monkeypatch):
    async def fake_download(*, bucket: str, key: str) -> bytes:
        return PDF

    monkeypatch.setattr("app.modules.signing.service.download_object_bytes", fake_download)


def _external(client, **spec):
    token = f"tok-{uuid.uuid4().hex}"
    built = _build(client, requests=[{"user": None, "status": "sent", "token": token, **spec}])
    return token, built


class TestPublicFile:
    def test_signer_gets_the_pdf_inline_from_the_app_origin(self, client) -> None:
        token, _ = _external(client)

        response = client.get(f"/public/sign/{token}/file")

        assert response.status_code == 200, response.text
        assert response.content == PDF
        assert response.headers["content-type"] == "application/pdf"
        assert response.headers["content-disposition"].startswith("inline")
        assert "no-store" in response.headers["cache-control"]
        assert response.headers["x-content-type-options"] == "nosniff"

    def test_unknown_token_is_the_usual_404(self, client) -> None:
        response = client.get(f"/public/sign/tok-{uuid.uuid4().hex}/file")

        assert response.status_code == 404
        assert response.json()["code"] == "CRM-1504"

    def test_expired_link_gives_nothing(self, client) -> None:
        from app.core.db import session_scope
        from app.modules.signing.models import SignatureRequest

        token, built = _external(client)

        async def _expire() -> None:
            async with session_scope() as session:
                request = await session.get(SignatureRequest, built.request_ids[0])
                request.token_expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)

        run(client, _expire)

        assert client.get(f"/public/sign/{token}/file").status_code == 404

    def test_the_file_shares_the_token_rate_limit(self, client) -> None:
        from app.core.config import get_settings

        token, _ = _external(client)
        limit = get_settings().public_sign_rate_limit_per_min
        for _ in range(limit):
            client.get(f"/public/sign/{token}/file")

        assert client.get(f"/public/sign/{token}/file").status_code == 429

    def test_page_points_at_the_same_origin_file(self, client) -> None:
        token, _ = _external(client)

        page = client.get(f"/public/sign/{token}")

        assert page.status_code == 200, page.text
        assert page.json()["document"]["file_url"] == f"/public/sign/{token}/file"

    def test_presigned_preview_no_longer_forces_a_download(self, client) -> None:
        token, _ = _external(client)

        preview_url = client.get(f"/public/sign/{token}").json()["document"]["preview_url"]

        query = parse_qs(urlparse(preview_url).query)
        assert query["response-content-disposition"] == ["inline"]


class TestInternalFile:
    def _scene(self, client):
        signer = run(client, _make_user, "KAM")
        built = _build(client, requests=[{"user": signer, "status": "sent"}])
        return signer, built

    def test_the_signer_gets_the_pdf(self, client) -> None:
        signer, built = self._scene(client)
        _login(client, signer)

        response = client.get(f"/api/signature-requests/{built.request_ids[0]}/file")

        assert response.status_code == 200, response.text
        assert response.content == PDF
        assert response.headers["content-disposition"].startswith("inline")

    def test_someone_elses_request_is_not_found(self, client) -> None:
        _, built = self._scene(client)
        _login(client, run(client, _make_user, "KAM"))

        response = client.get(f"/api/signature-requests/{built.request_ids[0]}/file")

        assert response.status_code == 404, response.text

    def test_login_is_required(self, client) -> None:
        _, built = self._scene(client)

        assert client.get(f"/api/signature-requests/{built.request_ids[0]}/file").status_code == 401

    def test_view_returns_the_same_origin_url(self, client) -> None:
        signer, built = self._scene(client)
        _login(client, signer)

        response = client.post(f"/api/signature-requests/{built.request_ids[0]}/view")

        assert response.status_code == 200, response.text
        document = response.json()["document"]
        assert document["file_url"] == f"/api/signature-requests/{built.request_ids[0]}/file"
        assert parse_qs(urlparse(document["preview_url"]).query)[
            "response-content-disposition"
        ] == ["inline"]


def test_filename_with_cyrillic_is_encoded_for_the_header() -> None:
    from app.modules.signing.service import pdf_inline_headers

    headers = pdf_inline_headers("Договор №1.pdf")

    assert headers["Content-Disposition"].startswith("inline; filename=")
    assert "filename*=UTF-8''" in headers["Content-Disposition"]
    assert headers["Content-Disposition"].isascii()
    assert "\n" not in headers["Content-Disposition"]
