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

B5-7: `_configure_demo_sources` — отдельный, более смелый шаг того же сида. Только при
`APP_PROFILE=demo` и настоящих адресах моков (`CMS_BASE_URL`/`LMS_BASE_URL`) он включает
источники `cms`/`lms` сразу с забором по расписанию и готовым маппингом/маршрутом — цепочка
«сайт → CRM → LMS → CRM» для жюри работает сама после `docker compose --profile integrations
up`, без похода в «Настройка → Интеграции». Маркер `config.demo_seeded=true` делает это
идемпотентным и не включает источник обратно, если администратор его выключил — тот же приём,
что и у остального сида модуля.
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
from app.modules.integration.field_mapping import FieldMapping, MappingRule
from app.modules.integration.models import IntegrationSource, IntegrationSourceCode
from app.modules.integration.routing import ProgressRule, ProgressWhen, Routing
from app.modules.integration.security import resolve_secret

logger = structlog.get_logger(__name__)

# B5-7: мок сайта и каталог демо-стенда называют один и тот же курс разными кодами — маппинг
# переименовывает значение поля "course" заявки на код продукта каталога (B5-3).
_CMS_DEMO_COURSE_MAP = {"python-basics": "py-base", "data-analysis": "ds-pro"}
# Мок LMS называет курс иначе, чем мок сайта, — свой словарь, тот же результат.
_LMS_DEMO_COURSE_MAP = {"crs-python-basics": "py-base", "crs-data-analysis": "ds-pro"}
# B5-8: сделки B2C по умолчанию двигаются из «Зачисление в LMS» в «Завершение обучения», когда
# LMS отдаёт `completed_at`. Код статусов — из опубликованной воронки, не зашит на глобальном
# уровне: сид подставляет код воронки сида, а не литерал "b2c_individual_v1" напрямую, только
# если такая воронка опубликована (см. `_configure_demo_sources`).
_B2C_WORKFLOW_CODE = "b2c_individual_v1"
_LMS_ENROLLMENT_STATUS = "lms_enrollment"
_TRAINING_COMPLETED_STATUS = "training_completed"

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
        elif code == IntegrationSourceCode.LMS.value:
            # Раздел 4.14, дословно: «раз в 30 минут» — колонка общая для всех источников
            # (по умолчанию 300 с), у lms переопределяется сидом (B5-6).
            source.pull_interval_seconds = 1800
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


def _demo_cms_mapping() -> FieldMapping:
    """B5-7: `email`/`phone` явно — иначе `validate_mapping` отклонил бы маппинг (нужна хотя бы
    одна цель контакта), а режим `passthrough` всё равно переносит их без изменений под
    собственными именами. `course` — значение мока → код продукта каталога (B5-3)."""
    return FieldMapping(
        mode="passthrough",
        rules=[
            MappingRule(target="email", source="email", transform="none"),
            MappingRule(target="phone", source="phone", transform="none"),
            MappingRule(
                target="course",
                source="course",
                transform="value_map",
                value_map=dict(_CMS_DEMO_COURSE_MAP),
            ),
        ],
    )


async def _demo_b2c_workflow_code(session: AsyncSession) -> str | None:
    """Код опубликованной B2C-воронки для правила B5-8: сначала сид `b2c_individual_v1`
    (`workflow.seed._b2c_spec`), иначе — опубликованная воронка B2C по умолчанию, какой бы код
    она ни носила (локальная замена/демо другого эпика). `None` — такой воронки ещё нет, правило
    по прогрессу тогда не заводится (сеется отдельно, когда воронка появится)."""
    from app.modules.workflow.models import Workflow, WorkflowState

    workflow = await session.scalar(
        select(Workflow).where(
            Workflow.code == _B2C_WORKFLOW_CODE,
            Workflow.state == WorkflowState.PUBLISHED.value,
        )
    )
    if workflow is None:
        workflow = await session.scalar(
            select(Workflow).where(
                Workflow.deal_type == "b2c",
                Workflow.is_default.is_(True),
                Workflow.state == WorkflowState.PUBLISHED.value,
            )
        )
    return workflow.code if workflow is not None else None


async def _demo_lms_routing(session: AsyncSession) -> Routing:
    """B5-8: дефолтное правило «завершил курс в LMS → сделка в „Завершение обучения“»; без
    опубликованной B2C-воронки правило не заводится (пустой `progress_rules`, безопасный
    промах — см. риски B5-8)."""
    workflow_code = await _demo_b2c_workflow_code(session)
    if workflow_code is None:
        return Routing()
    return Routing(
        progress_rules=[
            ProgressRule(
                when=ProgressWhen(completed=True),
                workflow_code=workflow_code,
                from_status=_LMS_ENROLLMENT_STATUS,
                to_status=_TRAINING_COMPLETED_STATUS,
                comment="Обучение завершено в LMS ({progress_pct} %)",
            )
        ]
    )


async def _configure_demo_sources(session: AsyncSession) -> int:
    """B5-7: на демо-стенде включает источники `cms`/`lms` сразу с готовым маппингом/маршрутом —
    см. докстринг модуля. Возвращает число источников, которые этот вызов настроил впервые
    (`0` на повторных прогонах и вне профиля `demo`)."""
    settings = get_settings()
    if settings.app_profile != "demo":
        return 0
    if not settings.cms_base_url or not settings.lms_base_url:
        return 0

    configured = 0
    for code in (IntegrationSourceCode.CMS.value, IntegrationSourceCode.LMS.value):
        source = (
            await session.execute(select(IntegrationSource).where(IntegrationSource.code == code))
        ).scalar_one_or_none()
        if source is None:
            continue  # seed_integration_sources должен выполниться раньше в этом же прогоне
        config = dict(source.config or {})
        if config.get("demo_seeded"):
            continue  # уже настроено (или сознательно выключено администратором после этого)

        source.is_active = True
        source.pull_enabled = True
        source.pull_interval_seconds = 60
        if code == IntegrationSourceCode.CMS.value:
            source.base_url = settings.cms_base_url
            source.field_mapping = _demo_cms_mapping().model_dump(mode="json")
        else:
            source.base_url = settings.lms_base_url
            source.routing = (await _demo_lms_routing(session)).model_dump(mode="json")
            config["course_code_map"] = dict(_LMS_DEMO_COURSE_MAP)
        config["demo_seeded"] = True
        source.config = config
        configured += 1

    if configured:
        await session.flush()
    logger.info("integration_demo_sources_configured", configured=configured)
    return configured


async def main() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    async with session_scope() as session:
        await seed_integration_sources(session)
        await seed_integration_account(session)
        await _configure_demo_sources(session)


if __name__ == "__main__":
    asyncio.run(main())
