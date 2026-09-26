"""Сиды модуля интеграций: реестр источников + служебная учётка INTEGRATION.

Запускается один раз при подготовке демо-окружения:

    python -m app.modules.integration.seed

Идемпотентно — тот же принцип, что `workflow.seed`/`reporting.seed`: если
запись с данным ключом уже есть, сид её не трогает (в том числе не включает
источник, который администратор мог выключить сознательно).

Источники заводятся `is_active=False` — тот же осторожный дефолт, что
флаг `external_org_lookup` (внешний поиск по ИНН): включает их администратор
явно через `PATCH /api/admin/integrations/sources/{code}`, когда `base_url`/
`credentials_ref` реально настроены, а не сразу после разворачивания.

Единственное исключение — `cms`: у него секрет вебхука задаётся окружением
(`CMS_WEBHOOK_SECRET_REF` — имя переменной, в которой лежит секрет), и без
исключения настройка окружения не доходила бы до вебхука, пока админ не
пройдёт по нему руками (вебхук отвечал 503). Поэтому сид подставляет
`credentials_ref` из `CMS_WEBHOOK_SECRET_REF` и включает источник, только если
переменная с секретом реально есть в окружении и не пуста; иначе он выключен,
как и остальные. Значение секрета нигде не читается в лог и не сохраняется —
в БД только имя переменной.

Служебная учётка `role=INTEGRATION` нужна `integration.service.
get_integration_principal()` — без неё любой вебхук CMS падает с CRM-9503
(«запустите сид»), см. докстринг там же. `keycloak_id=None` — эта учётка не
проходит через Keycloak вообще: вебхуки аутентифицированы HMAC-подписью
(раздел 4.14), не сессией браузера.
"""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.logging import configure_logging
from app.modules.identity.models import Role, User
from app.modules.integration.models import IntegrationSource, IntegrationSourceCode
from app.modules.integration.security import resolve_secret

logger = structlog.get_logger(__name__)

_SOURCES: tuple[tuple[str, str], ...] = (
    (IntegrationSourceCode.CMS.value, "Сайт (Laravel CMS)"),
    (IntegrationSourceCode.LMS.value, "LMS"),
    (IntegrationSourceCode.BITRIX24.value, "Bitrix24"),
)

_INTEGRATION_EMAIL = "integration@system.local"


def _configure_cms_source(source: IntegrationSource) -> None:
    """`credentials_ref` — из `CMS_WEBHOOK_SECRET_REF`; включён, только если секрет есть в
    окружении. Пустая переменная — не секрет: подпись, посчитанная от пустого ключа, ничего не
    защищает (`verify_signature` такой секрет не принимает)."""
    ref = get_settings().cms_webhook_secret_ref
    if not ref:
        return
    source.credentials_ref = ref
    secret = resolve_secret(ref)
    source.is_active = bool(secret and secret.strip())
    if not source.is_active:
        logger.warning("cms_source_left_inactive", reason="secret_not_in_environment", ref=ref)


async def seed_integration_sources(session: AsyncSession) -> int:
    created = 0
    for code, name in _SOURCES:
        existing = (
            await session.execute(select(IntegrationSource).where(IntegrationSource.code == code))
        ).scalar_one_or_none()
        if existing is not None:
            continue
        source = IntegrationSource(code=code, name=name, is_active=False)
        if code == IntegrationSourceCode.CMS.value:
            _configure_cms_source(source)
        session.add(source)
        created += 1
    if created:
        await session.flush()
    logger.info("integration_sources_seeded", created=created)
    return created


async def seed_integration_account(session: AsyncSession) -> User:
    existing = (
        (await session.execute(select(User).where(User.role == Role.INTEGRATION.value)))
        .scalars()
        .first()
    )
    if existing is not None:
        logger.info("integration_account_seed_skip_existing", user_id=str(existing.id))
        return existing

    user = User(
        keycloak_id=None,
        email=_INTEGRATION_EMAIL,
        full_name="Служебная учётка интеграций",
        role=Role.INTEGRATION.value,
        status="active",
    )
    session.add(user)
    await session.flush()
    logger.info("integration_account_seeded", user_id=str(user.id))
    return user


async def main() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    async with session_scope() as session:
        await seed_integration_sources(session)
        await seed_integration_account(session)


if __name__ == "__main__":
    asyncio.run(main())
