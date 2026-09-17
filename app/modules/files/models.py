"""Модели файлов и вложений (раздел 5.6, 9).

Полный цикл: `upload-intent` создаёт `files` в `pending` и выдаёт presigned
PUT на SeaweedFS, клиент грузит объект напрямую (не через API), `commit`
проверяет реальный объект и переводит в `ready`/`infected`. `attachments` —
полиморфная связь файла с сущностью (`entity_type`/`entity_id`), без FK на
конкретные таблицы: сущности слишком разные (deal, organization, contact,
report, erasure_request), а проверка существования и прав — на сервисном
уровне (раздел 5.6), тем же приёмом, что `deal_events.payload` не типизирует
полиморфные данные через FK.

Дедупликация по `sha256`: `refcount` считает активные ссылки из
`attachments`, физическое удаление объекта в S3 — только при `refcount = 0`
(раздел 3.7). Здесь это поле, инкремент/декремент — в `files.service`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UuidPkMixin


class FileStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    INFECTED = "infected"
    QUARANTINED = "quarantined"
    DELETED = "deleted"


class AttachmentCategory(StrEnum):
    CONTRACT = "contract"
    PRESENTATION = "presentation"
    ACT = "act"
    LICENSE = "license"
    REPORT = "report"
    SIGNATURE_CONTAINER = "signature_container"
    OTHER = "other"


class File(UuidPkMixin, Base):
    __tablename__ = "files"
    __table_args__ = (
        Index("ix_files_sha256", "sha256"),
        Index("ix_files_uploaded_by", "uploaded_by"),
        CheckConstraint(
            "status IN ('pending','ready','infected','quarantined','deleted')",
            name="files_status_valid",
        ),
    )

    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    bucket: Mapped[str] = mapped_column(String(128), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    refcount: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    scan_result: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scanned_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    contains_pd: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        server_default=text("now()"), nullable=False, index=True
    )
    deleted_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)


class Attachment(UuidPkMixin, Base):
    __tablename__ = "attachments"
    __table_args__ = (
        Index("ix_attachments_entity", "entity_type", "entity_id"),
        Index("ix_attachments_file", "file_id"),
        CheckConstraint(
            "category IN ('contract','presentation','act','license','report',"
            "'signature_container','other')",
            name="attachments_category_valid",
        ),
    )

    file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("files.id", ondelete="RESTRICT"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    category: Mapped[str] = mapped_column(String(24), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        server_default=text("now()"), nullable=False, index=True
    )
    deleted_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
