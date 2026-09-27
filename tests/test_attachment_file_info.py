"""Вложения отдают сведения о файле (A-31): имя, размер, тип, статус.

Список вложений раньше содержал только `file_id`, и интерфейс не мог показать, что за файл
приложен. Поле `file` добавлено рядом (аддитивно), а сведения берутся одним запросом на весь
список, чтобы страница вложений не превратилась в N+1."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import event

from tests.conftest import TEST_DATABASE_URL, _make_user, run
from tests.test_files_access import _attach, _deal, _file, _login

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


async def _rename(file_id: uuid.UUID, name: str, size: int, mime: str) -> None:
    from app.core.db import session_scope
    from app.modules.files.models import File

    async with session_scope() as session:
        file = await session.get(File, file_id)
        file.original_filename = name
        file.size_bytes = size
        file.mime_type = mime


def _url(deal: uuid.UUID) -> str:
    return f"/api/attachments?entity_type=deal&entity_id={deal}"


class TestAttachmentFileInfo:
    def test_list_carries_name_size_type_and_status(self, client) -> None:
        owner = run(client, _make_user, "KAM")
        deal = run(client, _deal, owner.id)
        file_id = run(client, _file, owner.id)
        run(client, _rename, file_id, "Договор №5.pdf", 2048, "application/pdf")
        run(client, _attach, file_id, deal, owner.id)
        _login(client, owner)

        response = client.get(_url(deal))

        assert response.status_code == 200, response.text
        (item,) = response.json()["items"]
        assert item["file_id"] == str(file_id)  # прежнее поле на месте
        assert item["file"] == {
            "original_filename": "Договор №5.pdf",
            "size_bytes": 2048,
            "mime_type": "application/pdf",
            "status": "ready",
        }

    def test_creating_an_attachment_returns_the_same_info(self, client) -> None:
        owner = run(client, _make_user, "KAM")
        deal = run(client, _deal, owner.id)
        file_id = run(client, _file, owner.id)
        run(client, _rename, file_id, "scan.pdf", 10, "application/pdf")
        _login(client, owner)

        response = client.post(
            "/api/attachments",
            json={
                "file_id": str(file_id),
                "entity_type": "deal",
                "entity_id": str(deal),
                "category": "contract",
            },
        )

        assert response.status_code == 201, response.text
        assert response.json()["file"]["original_filename"] == "scan.pdf"
        assert response.json()["file"]["status"] == "ready"

    def test_list_does_not_query_files_once_per_attachment(self, client) -> None:
        from app.core.db import get_engine

        owner = run(client, _make_user, "KAM")
        deal = run(client, _deal, owner.id)
        for _ in range(5):
            file_id = run(client, _file, owner.id)
            run(client, _attach, file_id, deal, owner.id)
        _login(client, owner)

        statements: list[str] = []

        def _count(_conn, _cursor, statement, *_args) -> None:
            statements.append(statement)

        engine = get_engine().sync_engine
        event.listen(engine, "before_cursor_execute", _count)
        try:
            response = client.get(_url(deal))
        finally:
            event.remove(engine, "before_cursor_execute", _count)

        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == 5
        assert all(item["file"] is not None for item in response.json()["items"])
        from_files = [s for s in statements if "FROM files" in s]
        assert len(from_files) == 1, from_files
