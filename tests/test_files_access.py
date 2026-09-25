"""Права на файлы и вложения (`files/router.py`, `files/service.py`) на настоящей
PostgreSQL (`TEST_DATABASE_URL`) — см. докстринг `tests/conftest.py`. Файлы и
вложения заводятся в БД напрямую: S3 в тестах недоступен, а ссылки на скачивание
(presigned URL) подписываются локально и сети не требуют.
"""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import TEST_DATABASE_URL, _make_user, authenticate, run

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL, reason="нужен TEST_DATABASE_URL с применёнными миграциями"
)


def _login(client, user) -> None:
    csrf = authenticate(client, user)
    client.headers["X-CSRF-Token"] = csrf


async def _file(uploaded_by: uuid.UUID | None, status: str = "ready") -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.files.models import File

    async with session_scope() as session:
        file = File(
            storage_key=f"{uuid.uuid4()}/scan.pdf",
            bucket="files",
            original_filename="scan.pdf",
            mime_type="application/pdf",
            size_bytes=10,
            sha256=uuid.uuid4().hex + uuid.uuid4().hex,
            status=status,
            uploaded_by=uploaded_by,
        )
        session.add(file)
        await session.flush()
        return file.id


async def _agreement(file_id: uuid.UUID | None) -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.signing.models import EdmAgreement

    async with session_scope() as session:
        agreement = EdmAgreement(
            party_type="organization",
            party_id=uuid.uuid4(),
            conclusion_method="paper",
            agreement_file_id=file_id,
        )
        session.add(agreement)
        await session.flush()
        return agreement.id


async def _deal(owner_id: uuid.UUID) -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.catalog.models import Organization
    from app.modules.crm.models import Deal
    from app.modules.workflow.models import Workflow, WorkflowStatus

    async with session_scope() as session:
        workflow = Workflow(
            code=f"wf-{uuid.uuid4().hex[:10]}",
            name="Воронка файлов",
            deal_type="b2b",
            state="draft",
        )
        session.add(workflow)
        await session.flush()
        status = WorkflowStatus(workflow_id=workflow.id, code="new", name="Новая")
        org = Organization(name=f"Вуз {uuid.uuid4().hex[:6]}", org_type="university")
        session.add_all([status, org])
        await session.flush()
        deal = Deal(
            number=f"D-{uuid.uuid4().hex[:10]}",
            title="Сделка с файлами",
            deal_type="b2b",
            workflow_id=workflow.id,
            status_id=status.id,
            organization_id=org.id,
            owner_id=owner_id,
        )
        session.add(deal)
        await session.flush()
        return deal.id


async def _attach(file_id: uuid.UUID, deal_id: uuid.UUID, uploaded_by: uuid.UUID) -> uuid.UUID:
    from app.core.db import session_scope
    from app.modules.files.models import Attachment, File

    async with session_scope() as session:
        attachment = Attachment(
            file_id=file_id,
            entity_type="deal",
            entity_id=deal_id,
            category="contract",
            uploaded_by=uploaded_by,
        )
        session.add(attachment)
        (await session.get(File, file_id)).refcount += 1
        await session.flush()
        return attachment.id


async def _file_state(file_id: uuid.UUID) -> dict[str, object]:
    from app.core.db import session_scope
    from app.modules.files.models import File

    async with session_scope() as session:
        file = await session.get(File, file_id)
        return {"refcount": file.refcount, "status": file.status}


async def _attachment_deleted(attachment_id: uuid.UUID) -> bool:
    from app.core.db import session_scope
    from app.modules.files.models import Attachment

    async with session_scope() as session:
        return (await session.get(Attachment, attachment_id)).deleted_at is not None


class TestEdmAgreementScanDownload:
    """Скан соглашения об ЭДО — `edm_agreements.agreement_file_id`, вложения у него
    нет. Скачивает администратор и тот, у кого `edm:read` (AUDITOR)."""

    def _url(self, file_id, agreement_id) -> str:
        return (
            f"/api/files/{file_id}/download-url"
            f"?entity_type=edm_agreement&entity_id={agreement_id}"
        )

    def _scene(self, client):
        admin = run(client, _make_user, "ADMIN")
        file_id = run(client, _file, admin.id)
        return file_id, run(client, _agreement, file_id)

    def test_auditor_reads_the_scan(self, client) -> None:
        file_id, agreement_id = self._scene(client)
        _login(client, run(client, _make_user, "AUDITOR"))

        response = client.get(self._url(file_id, agreement_id))

        assert response.status_code == 200, response.text
        assert response.json()["download_url"].startswith("http")

    def test_admin_reads_the_scan(self, client) -> None:
        file_id, agreement_id = self._scene(client)
        _login(client, run(client, _make_user, "ADMIN"))

        assert client.get(self._url(file_id, agreement_id)).status_code == 200

    @pytest.mark.parametrize("role", ["KAM", "HEAD"])
    def test_roles_without_edm_read_are_refused(self, client, role: str) -> None:
        file_id, agreement_id = self._scene(client)
        _login(client, run(client, _make_user, role))

        response = client.get(self._url(file_id, agreement_id))

        assert response.status_code == 403, response.text

    def test_another_file_is_not_the_agreement_scan(self, client) -> None:
        _, agreement_id = self._scene(client)
        stranger = run(client, _file, run(client, _make_user, "ADMIN").id)
        _login(client, run(client, _make_user, "AUDITOR"))

        response = client.get(self._url(stranger, agreement_id))

        assert response.status_code == 403, response.text
        assert response.json()["code"] == "CRM-1404"

    def test_auditor_gets_nothing_else_through_the_same_route(self, client) -> None:
        owner = run(client, _make_user, "KAM")
        deal = run(client, _deal, owner.id)
        file_id = run(client, _file, owner.id)
        run(client, _attach, file_id, deal, owner.id)
        _login(client, run(client, _make_user, "AUDITOR"))

        response = client.get(
            f"/api/files/{file_id}/download-url?entity_type=deal&entity_id={deal}"
        )

        assert response.status_code == 403, response.text


class TestDeleteAttachmentAndFile:
    """Автор убирает своё ошибочное вложение и файл без ссылок; чужое —
    только с правом `file:delete` (HEAD, ADMIN)."""

    def _scene(self, client, *, attached_by: str = "KAM"):
        owner = run(client, _make_user, "KAM")
        author = owner if attached_by == "KAM" else run(client, _make_user, attached_by)
        deal = run(client, _deal, owner.id)
        file_id = run(client, _file, author.id)
        attachment = run(client, _attach, file_id, deal, author.id)
        return owner, file_id, attachment

    def test_author_unlinks_his_own_attachment_and_then_the_file(self, client) -> None:
        owner, file_id, attachment = self._scene(client)
        _login(client, owner)

        unlink = client.delete(f"/api/attachments/{attachment}")
        assert unlink.status_code == 200, unlink.text
        assert run(client, _attachment_deleted, attachment)
        assert run(client, _file_state, file_id)["refcount"] == 0

        delete = client.delete(f"/api/files/{file_id}")
        assert delete.status_code == 200, delete.text
        assert run(client, _file_state, file_id)["status"] == "deleted"

    def test_someone_elses_attachment_stays_for_a_kam(self, client) -> None:
        owner, file_id, attachment = self._scene(client, attached_by="HEAD")
        _login(client, owner)

        response = client.delete(f"/api/attachments/{attachment}")

        assert response.status_code == 403, response.text
        assert not run(client, _attachment_deleted, attachment)
        assert run(client, _file_state, file_id)["refcount"] == 1

    def test_admin_still_unlinks_anyone_s_attachment(self, client) -> None:
        _, file_id, attachment = self._scene(client)
        _login(client, run(client, _make_user, "ADMIN"))

        assert client.delete(f"/api/attachments/{attachment}").status_code == 200
        assert run(client, _file_state, file_id)["refcount"] == 0

    def test_author_cannot_delete_a_file_that_is_still_attached(self, client) -> None:
        owner, file_id, _ = self._scene(client)
        _login(client, owner)

        response = client.delete(f"/api/files/{file_id}")

        assert response.status_code == 422, response.text
        assert run(client, _file_state, file_id)["status"] == "ready"

    def test_someone_elses_file_is_not_deletable_for_a_kam(self, client) -> None:
        stranger = run(client, _make_user, "KAM")
        file_id = run(client, _file, stranger.id)
        _login(client, run(client, _make_user, "KAM"))

        response = client.delete(f"/api/files/{file_id}")

        assert response.status_code == 403, response.text
        assert run(client, _file_state, file_id)["status"] == "ready"

    def test_head_deletes_a_file_nobody_uses(self, client) -> None:
        file_id = run(client, _file, run(client, _make_user, "KAM").id)
        _login(client, run(client, _make_user, "HEAD"))

        assert client.delete(f"/api/files/{file_id}").status_code == 200

    def test_auditor_cannot_delete_anything(self, client) -> None:
        file_id = run(client, _file, None)
        _login(client, run(client, _make_user, "AUDITOR"))

        assert client.delete(f"/api/files/{file_id}").status_code == 403

    def test_author_without_access_to_the_deal_cannot_unlink(self, client) -> None:
        # Объектная проверка родительской сущности остаётся: вложение чужой сделки не отвязать.
        owner, _, attachment = self._scene(client)
        outsider = run(client, _make_user, "KAM")
        _login(client, outsider)

        response = client.delete(f"/api/attachments/{attachment}")

        assert response.status_code in (403, 404), response.text
        assert not run(client, _attachment_deleted, attachment)
        assert owner.id != outsider.id
