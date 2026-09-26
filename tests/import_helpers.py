"""Заготовки сквозных тестов импорта: файл в памяти вместо S3, мастер «профиль -> маппинг ->
проверка», фоновое применение и откат без воркера.

Тестовая БД переживает прогоны и ничего не чистит, поэтому данные каждого теста уникальны
(`token()`): иначе второй прогон находил бы людей и компании первого и считал их «уже
существующими».
"""

from __future__ import annotations

import hashlib
import io
import random
import uuid
from typing import Any

import openpyxl

from tests.conftest import run

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def token() -> str:
    return uuid.uuid4().hex[:8]


def phone_parts() -> tuple[str, str]:
    """Уникальный мобильный номер: (`+7 (9XX) XXX-XX-XX` для файла, `+79XXXXXXXXX` — как он будет
    храниться после нормализации)."""
    digits = "9" + "".join(random.choice("0123456789") for _ in range(9))
    pretty = f"+7 ({digits[:3]}) {digits[3:6]}-{digits[6:8]}-{digits[8:]}"
    return pretty, f"+7{digits}"


def make_inn() -> str:
    """Уникальный ИНН юрлица (10 цифр) с верной контрольной суммой."""
    weights = (2, 4, 10, 3, 5, 9, 4, 6, 8)
    body = [random.randint(1, 9), random.randint(1, 9)] + [random.randint(0, 9) for _ in range(7)]
    check = sum(d * w for d, w in zip(body, weights, strict=True)) % 11 % 10
    return "".join(map(str, [*body, check]))


def xlsx_bytes(headers: list[str], rows: list[list[Any]], *, lookup: bool = False) -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Лист1"
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    if lookup:  # как в шаблоне LMS: справочники на втором листе, читается только первый
        second = workbook.create_sheet("Лист2")
        second.append(["М", "Без образования"])
        second.append(["Ж", "Основное общее образование - 9 классов"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def stub_storage(monkeypatch, content: bytes) -> None:
    """S3 в памяти: `imports.service` читает и пишет объекты только через эти четыре функции."""
    from app.core.storage import ObjectInspection
    from app.modules.imports import service as imports_service

    async def fake_inspect(*, bucket: str, key: str) -> ObjectInspection:
        return ObjectInspection(
            exists=True,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            magic_bytes=content[:8],
        )

    async def fake_download(*, bucket: str, key: str) -> bytes:
        return content

    async def fake_ensure_bucket(bucket: str) -> None:
        return None

    async def fake_upload(*, bucket: str, key: str, body: bytes, content_type: str) -> None:
        return None

    monkeypatch.setattr(imports_service, "inspect_object", fake_inspect)
    monkeypatch.setattr(imports_service, "download_object_bytes", fake_download)
    monkeypatch.setattr(imports_service, "ensure_bucket", fake_ensure_bucket)
    monkeypatch.setattr(imports_service, "upload_object_bytes", fake_upload)


async def _seed_file(content: bytes, uploaded_by: uuid.UUID, filename: str, mime: str) -> uuid.UUID:
    from app.core.db import session_scope
    from app.core.ids import uuid7
    from app.modules.files.models import File, FileStatus

    async with session_scope() as session:
        file = File(
            id=uuid7(),
            storage_key=f"test/{filename}",
            bucket="imports",
            original_filename=filename,
            mime_type=mime,
            size_bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            status=FileStatus.READY.value,
            uploaded_by=uploaded_by,
        )
        session.add(file)
        await session.flush()
        return file.id


def start_import(
    client,
    monkeypatch,
    user,
    *,
    entity_type: str,
    content: bytes,
    source_format: str,
    mode: str = "upsert",
    mapping: dict[str, str] | None = None,
    filename: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Файл -> задание -> профиль -> маппинг (подсказанный, если не задан) -> проверка. Клиент уже
    должен быть авторизован пользователем `user` (`sign_in`)."""
    stub_storage(monkeypatch, content)
    name = filename or f"import-{token()}.{source_format}"
    mime = XLSX_MIME if source_format == "xlsx" else "application/octet-stream"
    file_id = run(client, _seed_file, content, user.id, name, mime)

    created = client.post(
        "/api/imports",
        json={
            "file_id": str(file_id),
            "entity_type": entity_type,
            "mode": mode,
            "source_format": source_format,
        },
    )
    assert created.status_code == 201, created.text
    job_id = created.json()["id"]

    profile = client.get(f"/api/imports/{job_id}/profile")
    assert profile.status_code == 200, profile.text
    used_mapping = mapping if mapping is not None else profile.json()["suggested_mapping"]
    saved = client.put(f"/api/imports/{job_id}/mapping", json={"mapping": used_mapping})
    assert saved.status_code == 200, saved.text

    result: dict[str, Any] = {
        "id": job_id,
        "profile": profile.json(),
        "mapping": used_mapping,
        "dry": None,
    }
    if dry_run:
        dry = client.post(f"/api/imports/{job_id}/dry-run")
        assert dry.status_code == 200, dry.text
        result["dry"] = dry.json()
    return result


async def _apply(job_id: uuid.UUID) -> None:
    """Роутер только переводит задание в `applying`; построчную работу делает фоновая задача.
    Здесь — те же вызовы, что делает `sweep_import_jobs`, без воркера."""
    from app.core.db import session_scope
    from app.modules.imports.service import ImportService

    async with session_scope() as session:
        service = ImportService(session)
        job = await service.get_or_404(job_id)
        await service.start_apply(job)
        while await service.apply_batch(job, batch_size=500):
            pass
        await service.finalize_apply_if_done(job)


async def _rollback(job_id: uuid.UUID) -> None:
    from app.core.db import session_scope
    from app.modules.imports.service import ImportService

    async with session_scope() as session:
        service = ImportService(session)
        job = await service.get_or_404(job_id)
        await service.start_rollback(job)
        while await service.rollback_batch(job, batch_size=500):
            pass
        await service.finalize_rollback_if_done(job)


def apply_job(client, job_id: str) -> dict[str, Any]:
    run(client, _apply, uuid.UUID(job_id))
    return get_job(client, job_id)


def rollback_job(client, job_id: str) -> dict[str, Any]:
    run(client, _rollback, uuid.UUID(job_id))
    return get_job(client, job_id)


def get_job(client, job_id: str) -> dict[str, Any]:
    response = client.get(f"/api/imports/{job_id}")
    assert response.status_code == 200, response.text
    return response.json()


def job_rows(client, job_id: str, **params: str) -> list[dict[str, Any]]:
    response = client.get(f"/api/imports/{job_id}/rows", params={"limit": 100, **params})
    assert response.status_code == 200, response.text
    return response.json()["items"]
