"""Модель импорта каталогов (раздел 5.10/§4.12): задания, строки, пресеты.

`ImportRowResult.status` несёт больше состояний, чем перечисляет раздел 5.10
буквально (`ok`/`warn`/`error`): `rolled_back`/`rollback_blocked` добавлены,
чтобы `imports.tasks.sweep_import_jobs` могло возобновляться после падения
воркера без дублирования отката — та же идея, что уже применена к
`status_mapping_jobs`/`published_graph` в `workflow.models`: явное состояние
вместо вычисления «что уже сделано» по побочным признакам.
"""

from __future__ import annotations

import datetime as dt
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UuidPkMixin


class ImportEntityType(StrEnum):
    ORGANIZATION = "organization"
    PRODUCT = "product"
    # П3 (rtk_requiriments.md разд. 4, Треб.1): лицензии/договоры
    # вуз↔вендор↔ПО, см. `catalog.models.OrganizationLicense`.
    LICENSE = "license"
    # Три файла заказчика о людях. Строка такого файла порождает несколько записей сразу
    # (организация + продукты + контакт + связи; контакт + сделка + продукт сделки), поэтому эти
    # типы применяются обработчиками из `imports.handlers` и откатываются по `effects`.
    VENDOR_CONTACT = "vendor_contact"  # «Вендоры»: компания, продукты, ответственный
    PAYMENT = "payment"  # «Данные оплат»: заказ физлица на курс
    LEARNER = "learner"  # «Загрузка пользователей» — шаблон учащихся LMS


class ImportMode(StrEnum):
    INSERT = "insert"
    UPSERT = "upsert"
    UPDATE = "update"


class ImportJobStatus(StrEnum):
    UPLOADED = "uploaded"
    MAPPED = "mapped"
    VALIDATED = "validated"
    APPLYING = "applying"
    COMPLETED = "completed"
    COMPLETED_WITH_ERRORS = "completed_with_errors"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


class ImportRowStatus(StrEnum):
    OK = "ok"
    WARN = "warn"
    ERROR = "error"
    # Терминальные состояния, которые `imports.service.apply_batch`/
    # `rollback_batch` присваивают строкам, чтобы периодический скан не
    # подбирал их снова: без отдельного терминального статуса строка с
    # `entity_id IS NULL`, оставленная в `ok`/`warn`, обрабатывалась бы на
    # каждом следующем тике бесконечно.
    SKIPPED = "skipped"
    ROLLED_BACK = "rolled_back"
    ROLLBACK_BLOCKED = "rollback_blocked"


_JOB_STATUSES = tuple(s.value for s in ImportJobStatus)
_ROW_STATUSES = tuple(s.value for s in ImportRowStatus)
_ENTITY_TYPES = tuple(e.value for e in ImportEntityType)


class ImportJob(UuidPkMixin, TimestampMixin, Base):
    __tablename__ = "import_jobs"
    __table_args__ = (
        Index("ix_import_jobs_status", "status"),
        Index("ix_import_jobs_initiated_by", "initiated_by"),
        CheckConstraint(
            f"entity_type IN {_ENTITY_TYPES!r}",
            name="import_jobs_entity_type_valid",
        ),
        CheckConstraint("mode IN ('insert','upsert','update')", name="import_jobs_mode_valid"),
        CheckConstraint(f"status IN {_JOB_STATUSES!r}", name="import_jobs_status_valid"),
    )

    file_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    entity_type: Mapped[str] = mapped_column(String(16), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    mapping: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default=text("'uploaded'")
    )
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    # Прогресс применения: сколько строк уже обработано фоновой задачей (`ok_rows`/`warn_rows`/
    # `error_rows` — итог проверки, они при применении не двигаются).
    processed_rows: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    ok_rows: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    warn_rows: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    error_rows: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    result_file_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    initiated_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    source_format: Mapped[str] = mapped_column(String(8), nullable=False)
    started_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    rollback_available: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))
    rolled_back_at: Mapped[dt.datetime | None] = mapped_column(nullable=True)
    # Причина ухода в `FAILED` — партия упала вне построчного try/except (см. `imports.tasks.
    # _run_one_batch`): баг обработчика, обрыв соединения с БД и т.п. Название и тип — как у
    # `last_error` в `integration.models` (тот же приём для фоновых заданий).
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class ImportRowResult(UuidPkMixin, Base):
    __tablename__ = "import_row_results"
    __table_args__ = (
        Index("ix_import_row_results_job", "import_job_id"),
        Index(
            "ix_import_row_results_job_pending",
            "import_job_id",
            postgresql_where=text("entity_id IS NULL AND status IN ('ok','warn')"),
        ),
        Index(
            "ix_import_row_results_job_applied",
            "import_job_id",
            postgresql_where=text("entity_id IS NOT NULL"),
        ),
        CheckConstraint(f"status IN {_ROW_STATUSES!r}", name="import_row_results_status_valid"),
    )

    import_job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("import_jobs.id", ondelete="CASCADE"), nullable=False
    )
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    before_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # Сверх минимума раздела 5.10: уже провалидированные и смаппленные
    # значения строки. Без этого `apply()` пришлось бы заново парсить и
    # валидировать весь файл на каждом батче периодической задачи — здесь
    # это чекпоинт по данным, не только по номеру строки.
    row_data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    # Что строка создала или изменила: `[{"kind": "contact", "op": "create", "id": "…"}, …]`,
    # `op="update"` несёт `before` — прежние значения. Нужен для отката строк, порождающих
    # несколько записей (`before_snapshot` умеет только одну запись типа задания).
    effects: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    errors: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    created_at: Mapped[dt.datetime] = mapped_column(server_default=text("now()"), nullable=False)


class ImportPreset(UuidPkMixin, TimestampMixin, Base):
    __tablename__ = "import_presets"
    __table_args__ = (Index("ix_import_presets_entity_type", "entity_type"),)

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(16), nullable=False)
    mapping: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
