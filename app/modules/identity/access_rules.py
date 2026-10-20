"""B4-5: ограничения видимости Администратора — модель и API (`user_access_rules`).

Применение в SQL-скоупах, кэше принципала и отчётах — B4-6 (`app/core/access_rules.py`).
Этот модуль — только хранение правил и их замена целиком; строить из них SQL-условие
не должен (это обязанность `app.core.access_rules`, которая читает ТЕ ЖЕ строки через
`load_access_rules`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import invalidate_principal_after_commit
from app.core.errors import FieldError, ValidationError, VersionConflictError
from app.core.optimistic import claim_version
from app.core.redis_client import get_redis, key_recent
from app.core.security import Principal
from app.modules.audit.actions import AuditAction
from app.modules.audit.service import AuditService
from app.modules.catalog.models import Direction, Organization
from app.modules.identity.models import Role, User, UserAccessRule
from app.modules.reporting.service import ReportJobService

AccessRuleMode = Literal["all", "allow", "deny"]
#: Роли, для которых ограничения вообще имеют смысл (раздел «Семантика правил» B4-5):
#: ADMIN должен видеть всё, чтобы снять ограничение; AUDITOR не видит данных сделок;
#: INTEGRATION читает по источнику, не по владельцу.
_RESTRICTABLE_ROLES = (Role.KAM.value, Role.HEAD.value)
#: Раздел «Что сделать» п.4: не более стольких элементов на размерность.
MAX_IDS_PER_DIMENSION = 500


@dataclass(slots=True)
class DimensionInput:
    mode: AccessRuleMode
    ids: list[uuid.UUID] = field(default_factory=list)


@dataclass(slots=True)
class DimensionView:
    """Текущие правила одной размерности с именами — для ответа API."""

    mode: AccessRuleMode
    items: list[tuple[uuid.UUID, str]] = field(default_factory=list)


@dataclass(slots=True)
class AccessRulesView:
    user_id: uuid.UUID
    version: int
    access_restricted: bool
    organizations: DimensionView
    directions: DimensionView


_DIMENSION_LABELS = {"organizations": "вузы", "directions": "направления"}


def _label(name: str) -> str:
    """Человеческое название измерения ограничения вместо кода поля."""
    return _DIMENSION_LABELS.get(name, name)


def _validate_dimension(name: str, dimension: DimensionInput) -> None:
    if dimension.mode == "allow" and not dimension.ids:
        raise ValidationError(
            f"Режим «разрешить» без элементов означает «не видит ничего» — для этого есть "
            f"блокировка пользователя, а не ограничение видимости ({_label(name)})",
            [FieldError(field=name, reason="Список разрешённых пуст")],
        )
    if dimension.mode == "all" and dimension.ids:
        raise ValidationError(
            f"Режим «все» не принимает список ({_label(name)})",
            [FieldError(field=name, reason="Для режима «все» список не нужен")],
        )
    if len(dimension.ids) > MAX_IDS_PER_DIMENSION:
        raise ValidationError(
            f"Слишком много элементов: {_label(name)} (максимум {MAX_IDS_PER_DIMENSION})",
            [FieldError(field=name, reason="Превышен лимит")],
        )


def _dimension_change(before: DimensionView, after: DimensionView) -> dict[str, Any]:
    return {
        "old": {"mode": before.mode, "ids": [str(i) for i, _ in before.items]},
        "new": {"mode": after.mode, "ids": [str(i) for i, _ in after.items]},
    }


class AccessRulesService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._audit = AuditService(session)

    async def get(self, user_id: uuid.UUID) -> list[UserAccessRule]:
        rows = (
            await self._session.execute(
                select(UserAccessRule).where(UserAccessRule.user_id == user_id)
            )
        ).scalars()
        return list(rows)

    async def view(self, user: User) -> AccessRulesView:
        rows = await self.get(user.id)
        org_ids = [r.organization_id for r in rows if r.organization_id is not None]
        dir_ids = [r.direction_id for r in rows if r.direction_id is not None]
        org_names = await self._names(Organization, org_ids)
        dir_names = await self._names(Direction, dir_ids)
        org_effect = cast(
            AccessRuleMode, next((r.effect for r in rows if r.organization_id is not None), "all")
        )
        dir_effect = cast(
            AccessRuleMode, next((r.effect for r in rows if r.direction_id is not None), "all")
        )
        return AccessRulesView(
            user_id=user.id,
            version=user.version,
            access_restricted=user.access_restricted,
            organizations=DimensionView(
                mode=org_effect, items=[(i, org_names.get(i, str(i))) for i in org_ids]
            ),
            directions=DimensionView(
                mode=dir_effect, items=[(i, dir_names.get(i, str(i))) for i in dir_ids]
            ),
        )

    async def _names(
        self, model: type[Organization] | type[Direction], ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, str]:
        if not ids:
            return {}
        rows = await self._session.execute(select(model.id, model.name).where(model.id.in_(ids)))
        return {row.id: row.name for row in rows}

    async def _ensure_exist(
        self, model: type[Organization] | type[Direction], ids: list[uuid.UUID], label: str
    ) -> None:
        if not ids:
            return
        found = set(
            (
                await self._session.execute(
                    select(model.id).where(model.id.in_(ids), model.deleted_at.is_(None))
                )
            )
            .scalars()
            .all()
        )
        missing = set(ids) - found
        if missing:
            raise ValidationError(
                f"{_label(label).capitalize()}: {len(missing)} не найдено или удалено",
                [FieldError(field=label, reason=str(mid)) for mid in sorted(missing, key=str)[:10]],
            )

    async def replace(
        self,
        user: User,
        principal: Principal,
        *,
        organizations: DimensionInput,
        directions: DimensionInput,
        reason: str,
        expected_version: int,
    ) -> AccessRulesView:
        if user.role not in _RESTRICTABLE_ROLES:
            raise ValidationError(
                "Ограничить видимость можно только менеджеру или руководителю",
                [
                    FieldError(
                        field="user_id", reason="Для этой роли ограничения видимости недоступны"
                    )
                ],
            )
        _validate_dimension("organizations", organizations)
        _validate_dimension("directions", directions)
        await self._ensure_exist(Organization, organizations.ids, "organizations")
        await self._ensure_exist(Direction, directions.ids, "directions")

        before = await self.view(user)
        requested_signature = (
            organizations.mode,
            tuple(sorted(str(i) for i in organizations.ids)),
            directions.mode,
            tuple(sorted(str(i) for i in directions.ids)),
        )
        if self._signature(before) == requested_signature:
            # Повторный PUT с тем же содержимым — не пишет аудит и не трогает версию
            # (идемпотентно): проверяем совпадение `If-Match` как обычно (устаревший —
            # 409), но строки не трогаем вовсе.
            if user.version != expected_version:
                raise VersionConflictError(
                    user.version, {"access_restricted": user.access_restricted}
                )
            return before

        conflicting: dict[str, object] = {"access_restricted": user.access_restricted}
        await claim_version(self._session, user, expected_version, conflicting=conflicting)

        existing = await self.get(user.id)
        for row in existing:
            await self._session.delete(row)
        await self._session.flush()

        new_rows: list[UserAccessRule] = []
        if organizations.mode != "all":
            new_rows.extend(
                UserAccessRule(
                    user_id=user.id,
                    effect=organizations.mode,
                    organization_id=org_id,
                    created_by=principal.user_id,
                )
                for org_id in organizations.ids
            )
        if directions.mode != "all":
            new_rows.extend(
                UserAccessRule(
                    user_id=user.id,
                    effect=directions.mode,
                    direction_id=direction_id,
                    created_by=principal.user_id,
                )
                for direction_id in directions.ids
            )
        self._session.add_all(new_rows)
        user.access_restricted = bool(new_rows)
        await self._session.flush()

        after = await self.view(user)
        # Сюда доходим только когда запрошенное отличается от сохранённого (ранний
        # возврат выше) — писать аудит и сбрасывать кэш можно безусловно.
        await self._audit.record(
            AuditAction.ACCESS_RULES_CHANGED,
            entity_type="user",
            entity_id=user.id,
            changes={
                "organizations": _dimension_change(before.organizations, after.organizations),
                "directions": _dimension_change(before.directions, after.directions),
                "reason": {"old": None, "new": reason},
            },
        )
        invalidate_principal_after_commit(self._session, user.id, keycloak_id=user.keycloak_id)
        await self._clear_recent_after_commit(user.id)
        await ReportJobService(self._session).expire_for_user(user.id)
        return after

    @staticmethod
    def _signature(view: AccessRulesView) -> tuple[Any, ...]:
        return (
            view.organizations.mode,
            tuple(sorted(str(i) for i, _ in view.organizations.items)),
            view.directions.mode,
            tuple(sorted(str(i) for i, _ in view.directions.items)),
        )

    async def _clear_recent_after_commit(self, user_id: uuid.UUID) -> None:
        from app.core.db import run_after_commit

        async def _clear() -> None:
            try:
                await get_redis().delete(key_recent(user_id))
            except Exception:  # noqa: BLE001 — кэш не источник истины
                pass

        run_after_commit(self._session, _clear)


@dataclass(slots=True)
class PreviewCounts:
    visible_before: int
    visible_after: int

    @property
    def hidden(self) -> int:
        return max(0, self.visible_before - self.visible_after)


@dataclass(slots=True)
class AccessRulesPreview:
    deals: PreviewCounts
    organizations: PreviewCounts
    contacts: PreviewCounts


def _preview_principal(user: User, *, restricted: bool) -> Principal:
    """`Principal` реконструированный из строки `users` — тот же приём, что
    `reporting.tasks._principal_for_worker` для фонового рендеринга: ни HTTP-запроса,
    ни JWT у предпросмотра нет, только карточка целевого пользователя."""
    from app.core.security import TokenClaims

    return Principal(
        user_id=user.id,
        keycloak_id=user.keycloak_id or "",
        role=user.role,
        status=user.status,
        email=user.email,
        full_name=user.effective_name,
        team_id=user.team_id,
        manager_id=user.manager_id,
        perm_epoch=user.perm_epoch,
        session_id=None,
        consent_version=user.consent_version,
        must_change_password=False,
        claims=TokenClaims(
            subject=user.keycloak_id or str(user.id), raw={"source": "access_rules_preview"}
        ),
        restricted=restricted,
    )


async def _count_scope(session: AsyncSession, model: type[Any], clause: Any) -> int:
    from sqlalchemy import func

    stmt = select(func.count()).select_from(model).where(model.deleted_at.is_(None))
    if clause is not None:
        stmt = stmt.where(clause)
    return int(await session.scalar(stmt) or 0)


async def _counts_for(session: AsyncSession, user: User, rules: Any) -> dict[str, int]:
    from app.modules.catalog.models import Contact
    from app.modules.catalog.models import Organization as OrganizationModel
    from app.modules.catalog.service import (
        contact_scope_clause_for_rules,
        organization_scope_clause_for_rules,
    )
    from app.modules.crm.models import Deal
    from app.modules.crm.service import deal_scope_clause_for_rules

    principal = _preview_principal(user, restricted=rules is not None)
    deal_clause = await deal_scope_clause_for_rules(session, principal, rules)
    org_clause = await organization_scope_clause_for_rules(session, principal, rules)
    contact_clause = await contact_scope_clause_for_rules(session, principal, rules)
    return {
        "deals": await _count_scope(session, Deal, deal_clause),
        "organizations": await _count_scope(session, OrganizationModel, org_clause),
        "contacts": await _count_scope(session, Contact, contact_clause),
    }


async def preview_access_rules(
    session: AsyncSession,
    user: User,
    *,
    organizations: DimensionInput,
    directions: DimensionInput,
) -> AccessRulesPreview:
    """B4-6: сухой прогон — считает сделки/вузы/контакты до и после применения
    `organizations`/`directions` из запроса, ничего не сохраняя. «До» — по текущим
    сохранённым правилам пользователя (`access_restricted`/`user_access_rules`), «после»
    — по тем, что переданы (ещё не записаны)."""
    from app.core.access_rules import AccessRules as CoreAccessRules
    from app.core.access_rules import load_access_rules

    _validate_dimension("organizations", organizations)
    _validate_dimension("directions", directions)

    before_principal = _preview_principal(user, restricted=user.access_restricted)
    before_rules = await load_access_rules(session, before_principal)
    before = await _counts_for(session, user, before_rules)

    after_rules: CoreAccessRules | None = None
    if organizations.mode != "all" or directions.mode != "all":
        dir_ids: frozenset[uuid.UUID] = frozenset()
        if directions.ids:
            from app.modules.catalog.service import direction_descendant_ids

            dir_ids = frozenset(await direction_descendant_ids(session, directions.ids))
        after_rules = CoreAccessRules(
            org_mode=organizations.mode,
            org_ids=frozenset(organizations.ids),
            dir_mode=directions.mode,
            dir_ids=dir_ids,
        )
    after = await _counts_for(session, user, after_rules)

    return AccessRulesPreview(
        deals=PreviewCounts(before["deals"], after["deals"]),
        organizations=PreviewCounts(before["organizations"], after["organizations"]),
        contacts=PreviewCounts(before["contacts"], after["contacts"]),
    )
