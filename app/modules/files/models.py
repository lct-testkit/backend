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

B2-6: `Attachment.status_id`/`status_history_id` привязывают файл сделки к
шагу воронки. `status_id` — статус сделки на момент прикладывания (для
вложения перехода — статус, из которого уходят, см. `DealService.transition`
в `crm.service`); `status_history_id` — запись истории перехода, с которой
файл был передан (B2-7), выставляется один раз и больше не переписывается.
Для вложений не-сделок (организация, контакт, импорт) обе колонки `NULL`.
`ON DELETE RESTRICT` на оба FK: статус или запись истории с живыми
вложениями нельзя убрать мимо `WorkflowService.save_graph` (CRM-1208).
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
    # B6-4: 128, не 32 — имя сигнатуры настоящего антивируса (`ClamAvScanner`, в отличие от
    # заглушки) бывает длиннее 32 символов; без расширения колонки `commit` упал бы с 500
    # именно на заражённых файлах (миграция 0028).
    scan_result: Mapped[str | None] = mapped_column(String(128), nullable=True)
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
        # B2-6: вкладка «Файлы в статусе» и условия `step_attachments.*` читают по
        # (entity_id, status_id) только у живых вложений сделок.
        Index(
            "ix_attachments_deal_status",
            "entity_id",
            "status_id",
            postgresql_where=text("entity_type = 'deal' AND deleted_at IS NULL"),
        ),
        Index(
            "ix_attachments_status_history",
            "status_history_id",
            postgresql_where=text("status_history_id IS NOT NULL"),
        ),
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
    # B2-6/B2-7: см. докстринг модуля. `status_id` проставляет `AttachmentService.create`
    # (из `Deal.status_id` текущей сделки); `status_history_id` — только `DealService.transition`
    # при привязке файлов к переходу, один раз.
    status_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workflow_statuses.id", ondelete="RESTRICT"), nullable=True
    )
    status_history_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("deal_status_history.id", ondelete="RESTRICT"), nullable=True
    )
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        server_default=text("now()"), nullable=False, index=True
    )
    deleted_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
