"""B4-6: ограничения видимости (B4-5, `user_access_rules`) как SQL-условие.

Третий уровень проверки прав (раздел 4, `core/permissions.py`) — то, что
`deal_scope_clause`/`organization_scope_clause` (`app/modules/crm/service.py`,
`app/modules/catalog/service.py`) добавляют к обычному скоупу роли через `AND`.
Ограничения только СУЖАЮТ видимость роли, никогда не расширяют её — поэтому
везде `and_(role_clause, restriction)`, а не `or_`.

`contact_scope_clause` этот модуль не трогает: видимость контакта и так строится
через организацию и сделку (`organization_scope_clause`/`deal_scope_clause`),
которые уже несут ограничение — повторно накладывать его незачем.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import ColumnElement, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import Principal
from app.modules.catalog.models import Organization, Product
from app.modules.crm.models import Deal, DealProduct
from app.modules.identity.models import User, UserAccessRule

AccessMode = Literal["all", "allow", "deny"]


@dataclass(slots=True, frozen=True)
class AccessRules:
    org_mode: AccessMode
    org_ids: frozenset[uuid.UUID]
    dir_mode: AccessMode
    #: Корни из `user_access_rules` уже развёрнуты до всех потомков
    #: (`catalog.service.direction_descendant_ids`) — «направление QA» включает
    #: дочернее «QA-автоматизация», которое администратор не указывал явно.
    dir_ids: frozenset[uuid.UUID]


def _session_memo(session: AsyncSession) -> dict[uuid.UUID, AccessRules]:
    return session.info.setdefault("b4_access_rules_memo", {})


async def load_access_rules(session: AsyncSession, principal: Principal) -> AccessRules | None:
    """`None` — пользователь не ограничен (обычный путь для 99% ролей/пользователей, без
    единого лишнего запроса). Иначе — один `SELECT`, мемоизированный на сессию запроса:
    список сделок делает несколько вызовов скоупа (count + страница), второй не должен
    повторно идти в БД."""
    if not principal.restricted:
        return None

    memo = _session_memo(session)
    cached = memo.get(principal.user_id)
    if cached is not None:
        return cached

    rows = (
        (
            await session.execute(
                select(UserAccessRule).where(UserAccessRule.user_id == principal.user_id)
            )
        )
        .scalars()
        .all()
    )
    org_rows = [r for r in rows if r.organization_id is not None]
    dir_rows = [r for r in rows if r.direction_id is not None]
    org_mode: AccessMode = org_rows[0].effect if org_rows else "all"  # type: ignore[assignment]
    dir_mode: AccessMode = dir_rows[0].effect if dir_rows else "all"  # type: ignore[assignment]
    org_ids = frozenset(r.organization_id for r in org_rows if r.organization_id is not None)

    dir_roots = frozenset(r.direction_id for r in dir_rows if r.direction_id is not None)
    if dir_roots:
        # Ленивый импорт: `catalog.service` — сервисный слой другого модуля, тот же
        # приём, что уже используют `deal_scope_clause`/`organization_scope_clause`.
        from app.modules.catalog.service import direction_descendant_ids

        dir_ids = frozenset(await direction_descendant_ids(session, dir_roots))
    else:
        dir_ids = frozenset()

    result = AccessRules(org_mode=org_mode, org_ids=org_ids, dir_mode=dir_mode, dir_ids=dir_ids)
    memo[principal.user_id] = result
    return result


def deal_restriction(rules: AccessRules) -> ColumnElement[bool] | None:
    """Условие на `Deal`: сделка без вуза/продуктов — «закрыто по умолчанию» в режиме
    `allow` (скрыта) и видна в режиме `deny` (B4-5, «Семантика правил»). Сделка с
    продуктами нескольких направлений: в `allow` достаточно одного разрешённого, в
    `deny` скрывается, если хотя бы один продукт запрещённого направления."""
    parts: list[ColumnElement[bool]] = []
    if rules.org_mode == "allow":
        parts.append(Deal.organization_id.in_(rules.org_ids))
    elif rules.org_mode == "deny":
        parts.append(
            or_(Deal.organization_id.is_(None), Deal.organization_id.not_in(rules.org_ids))
        )

    if rules.dir_mode != "all":
        in_restricted_dirs = (
            select(DealProduct.id)
            .join(Product, Product.id == DealProduct.product_id)
            .where(DealProduct.deal_id == Deal.id, Product.direction_id.in_(rules.dir_ids))
            .exists()
        )
        if rules.dir_mode == "allow":
            parts.append(in_restricted_dirs)
        else:
            parts.append(~in_restricted_dirs)

    return and_(*parts) if parts else None


async def user_organization_allowed(
    session: AsyncSession, user: User, organization_id: uuid.UUID
) -> bool:
    """B4-6: `ensure_assignable_user`/`bulk_set_owner` — вуз вне `allow` (или внутри
    `deny`) ограниченного пользователя не достаётся ему ответственным, иначе он бы
    стал «владельцем», которого сам не видит. Правило по направлению здесь не при
    делах: оно сужает сделки, не сам список вузов (см. `organization_restriction`)."""
    if not user.access_restricted:
        return True
    rows = (
        await session.execute(
            select(UserAccessRule.effect, UserAccessRule.organization_id).where(
                UserAccessRule.user_id == user.id, UserAccessRule.organization_id.is_not(None)
            )
        )
    ).all()
    if not rows:
        return True
    mode = rows[0].effect
    ids = {row.organization_id for row in rows}
    if mode == "allow":
        return organization_id in ids
    return organization_id not in ids


def organization_restriction(rules: AccessRules) -> ColumnElement[bool] | None:
    """Условие на `Organization`. Направление не сужает список вузов напрямую — вуз
    обычно ведёт сделки нескольких направлений сразу, ограничение по направлению
    действует только на сами сделки (`deal_restriction`)."""
    if rules.org_mode == "allow":
        return Organization.id.in_(rules.org_ids)
    if rules.org_mode == "deny":
        return Organization.id.not_in(rules.org_ids)
    return None
