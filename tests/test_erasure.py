"""Тесты исполнения удаления/обезличивания (спринт 10, new_spec §4.8.4-4.8.5).

Как и в остальных тестах этого репозитория (см. `tests/test_signing.py`),
здесь нет поднятых PostgreSQL/Redis: покрываются чистые функции — рендеринг
акта (`signing.rendering.render_erasure_act_pdf`, реально запускает
reportlab, но без БД и сети), трансформация ORM-строки в ответ API
(`ErasureRequestDetail.from_model`, на облегчённой подставной записи вместо
настоящей `DataErasureRequest`) и схемы-валидаторы. Реальный доступ к
`data_erasure_requests`/исполнению (`ErasureExecutionService.execute`,
Keycloak-удаление, сборщик `identity.tasks.sweep_erasure_requests`) проверен
вживую против настоящего Postgres в этой же сессии — того же типа
верификация, что и для остального DB-слоя репозитория, просто не как
pytest-тест.
"""

from __future__ import annotations

import datetime as dt
import io
import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from pypdf import PdfReader

from app.core.permissions import Permission, has_permission
from app.modules.identity.models import Role
from app.modules.identity.schemas import (
    ErasureRejectRequest,
    ErasureRequestBody,
    ErasureRequestDetail,
)
from app.modules.signing.rendering import render_erasure_act_pdf


def _fake_request(**overrides: object) -> SimpleNamespace:
    base = dict(
        id=uuid.uuid4(),
        subject_type="user",
        subject_id=uuid.uuid4(),
        status="blocked",
        reason="увольнение",
        legal_basis="ст. 21 152-ФЗ",
        requested_by=uuid.uuid4(),
        requested_at=dt.datetime(2026, 9, 18, tzinfo=dt.UTC),
        deadline_at=None,
        grace_until=None,
        blockers={
            "mode": "anonymize",
            "comment": None,
            "items": [{"code": "active_deals", "detail": "Есть активные сделки", "count": 2}],
        },
        rejection_reason=None,
        executed_at=None,
        act_file_id=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestErasureActRendering:
    def test_produces_valid_pdf(self) -> None:
        pdf_bytes = render_erasure_act_pdf(
            subject_type="user",
            subject_display="Иванов И. И.",
            request_id=str(uuid.uuid4()),
            legal_basis="ст. 21 152-ФЗ",
            executed_at_iso="2026-09-18T12:00:00+03:00",
            responsible_display="система (плановое исполнение)",
            categories_erased=["ФИО", "email", "телефон"],
            categories_retained=[
                {"category": "Подписи", "legal_basis": "ст. 6 ч. 1 п. 5, 7 152-ФЗ"}
            ],
        )
        assert pdf_bytes.startswith(b"%PDF")
        assert len(PdfReader(io.BytesIO(pdf_bytes)).pages) >= 1

    def test_no_retained_categories_still_renders(self) -> None:
        # Жёсткое удаление: retained пуст — акт обязан честно сказать «нет»,
        # а не промолчать (dop.md §10.7).
        pdf_bytes = render_erasure_act_pdf(
            subject_type="contact",
            subject_display="Контакт #abcd1234",
            request_id=str(uuid.uuid4()),
            legal_basis="отзыв согласия",
            executed_at_iso="2026-09-18T12:00:00+03:00",
            responsible_display="система",
            categories_erased=["ФИО", "email"],
            categories_retained=[],
        )
        assert pdf_bytes.startswith(b"%PDF")

    def test_long_category_list_paginates_without_error(self) -> None:
        # `_ensure_space` должен переносить на новую страницу, а не падать,
        # когда категорий больше, чем помещается на A4.
        pdf_bytes = render_erasure_act_pdf(
            subject_type="user",
            subject_display="Тест Тестов",
            request_id=str(uuid.uuid4()),
            legal_basis="ст. 21 152-ФЗ",
            executed_at_iso="2026-09-18T12:00:00+03:00",
            responsible_display="система",
            categories_erased=[f"Поле #{i}" for i in range(80)],
            categories_retained=[
                {"category": f"Категория #{i}", "legal_basis": "x"} for i in range(80)
            ],
        )
        assert pdf_bytes.startswith(b"%PDF")
        assert len(PdfReader(io.BytesIO(pdf_bytes)).pages) > 1


class TestErasureRequestDetailFromModel:
    def test_unpacks_blockers_jsonb_into_flat_list(self) -> None:
        detail = ErasureRequestDetail.from_model(_fake_request())
        assert detail.mode == "anonymize"
        assert len(detail.blockers) == 1
        assert detail.blockers[0].code == "active_deals"

    def test_empty_items_list_when_no_blockers(self) -> None:
        request = _fake_request(status="pending", blockers={"mode": "hard_delete", "items": []})
        detail = ErasureRequestDetail.from_model(request)
        assert detail.blockers == []
        assert detail.mode == "hard_delete"

    def test_missing_blockers_dict_does_not_crash(self) -> None:
        # Контактный запрос до первого пересчёта хранит тот же `blockers`
        # контракт, но лучше не полагаться на то, что поле всегда заполнено.
        request = _fake_request(blockers=None)
        detail = ErasureRequestDetail.from_model(request)
        assert detail.blockers == []
        assert detail.mode is None

    def test_surfaces_post_execution_fields(self) -> None:
        act_id = uuid.uuid4()
        request = _fake_request(
            status="completed",
            executed_at=dt.datetime(2026, 10, 20, tzinfo=dt.UTC),
            act_file_id=act_id,
            blockers={"mode": "anonymize", "items": []},
        )
        detail = ErasureRequestDetail.from_model(request)
        assert detail.status == "completed"
        assert detail.act_file_id == act_id
        assert detail.executed_at is not None


class TestErasureRequestBodyValidation:
    def test_valid_payload_accepted(self) -> None:
        body = ErasureRequestBody(
            mode="anonymize", reason="увольнение", legal_basis="ст. 21 152-ФЗ"
        )
        assert body.mode == "anonymize"

    def test_unknown_mode_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ErasureRequestBody(mode="delete_everything", reason="x" * 5, legal_basis="y")

    def test_too_short_reason_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ErasureRequestBody(reason="ab", legal_basis="ст. 21 152-ФЗ")


class TestErasureRejectRequestValidation:
    def test_valid_reason_accepted(self) -> None:
        payload = ErasureRejectRequest(reason="Действующий договор до конца учебного года")
        assert "договор" in payload.reason

    def test_empty_reason_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ErasureRejectRequest(reason="")


class TestErasurePermissionMatrix:
    """dop.md §13 / new_spec §5: управление удалением — только ADMIN."""

    def test_erasure_manage_granted_to_admin(self) -> None:
        assert has_permission(Role.ADMIN.value, Permission.ERASURE_MANAGE)

    @pytest.mark.parametrize("role", [Role.KAM, Role.HEAD, Role.AUDITOR, Role.INTEGRATION])
    def test_erasure_manage_denied_to_everyone_else(self, role: Role) -> None:
        assert not has_permission(role.value, Permission.ERASURE_MANAGE)
