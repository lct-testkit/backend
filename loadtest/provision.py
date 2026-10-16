"""Провижининг тестовых данных для нагрузочного тестирования (new_spec §0,
§9 шаг 10: «p95 ... переход по статусу и добавление комментария ≤ 300 мс
при 50 RPS»).

Отдельный процесс с хоста, не через докер-контейнер — тот же приём, что
[[live-verification-standalone-script]]: коротким скриптам не нужен
пересобранный образ, если они просто пишут в Postgres напрямую через
опубликованный порт. `import app.main` в начале — обязателен: без полной
регистрации моделей `Organization.registry_version_id`'s FK на
`registry_versions` роняет `NoReferencedTableError` (тот же грабли, что
sprint10-admin-erasure-implementation.md уже описывает для Organization).

Создаёт:
* один организацию-фикстуру (переиспользуется при повторном запуске);
* `--count` сделок (по умолчанию 4000) в статусе `--from-code` воронки
  `--workflow-code` (по умолчанию `b2b_university_v2`, B2-4: пара сменилась
  с `identification→first_contact` воронки v1 на `meeting→documents_exchange`
  воронки v2 — после B2-1 это снова переход без условий, действий и
  обязательного комментария), владелец — демо-КАМ `kam.ivanov`
  (deploy/keycloak/realm-crm.json). Перед вставкой сделок скрипт сам
  проверяет, что у пары `--from-code`→`--to-code` нет `conditions`,
  `actions` и `requires_comment` (НФР 1: нагрузочный сценарий `transition`
  не должен тратить лишние запросы на условия или действия) — иначе выходит
  с понятным сообщением, а не роняет прогон локаста на первом же переходе;
* отдельный пул `--comment-pool-size` сделок (по умолчанию 100) для сценария
  «добавление комментария» — комментарии не меняют версию сделки, но
  циклический пул из 100, а не одна сделка, не создаёт горячую строку.

Пишет `loadtest/fixtures.json` — манифест для locustfile.py.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.main  # noqa: F401 — регистрирует все модели перед обращением к Organization/Deal
from app.modules.catalog.models import Organization
from app.modules.crm.models import Deal
from app.modules.identity.models import User
from app.modules.workflow.models import Workflow, WorkflowStatus, WorkflowTransition

DEFAULT_DATABASE_URL = "postgresql+asyncpg://crm_app:crm_app@localhost:5433/crm"
FIXTURE_ORG_NAME = "Нагрузочное тестирование — фикстура"
KAM_EMAIL = "ivanov@rt-it-school.ru"
#: B2-4: после B2-1 воронка по умолчанию — v2 (14 шагов задания); пара без условий, действий и
#: обязательного комментария — meeting→documents_exchange (3→4).
DEFAULT_WORKFLOW_CODE = "b2b_university_v2"
DEFAULT_FROM_CODE = "meeting"
DEFAULT_TO_CODE = "documents_exchange"


async def _get_or_create_org(session) -> Organization:
    org = await session.scalar(select(Organization).where(Organization.name == FIXTURE_ORG_NAME))
    if org is not None:
        return org
    org = Organization(name=FIXTURE_ORG_NAME, org_type="university")
    session.add(org)
    await session.flush()
    return org


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=4000, help="сделок для сценария transition")
    parser.add_argument("--comment-pool-size", type=int, default=100)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--database-url",
        default=os.environ.get("LOADTEST_DATABASE_URL", DEFAULT_DATABASE_URL),
    )
    parser.add_argument("--workflow-code", default=DEFAULT_WORKFLOW_CODE)
    parser.add_argument("--from-code", default=DEFAULT_FROM_CODE)
    parser.add_argument("--to-code", default=DEFAULT_TO_CODE)
    args = parser.parse_args()

    engine = create_async_engine(args.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        kam = await session.scalar(select(User).where(User.email == KAM_EMAIL))
        if kam is None:
            raise SystemExit(
                f"Демо-пользователь {KAM_EMAIL!r} не найден — realm-crm.json применён и "
                "just-in-time provisioning отработал (первый вход kam.ivanov в систему)?"
            )
        workflow = await session.scalar(
            select(Workflow).where(Workflow.code == args.workflow_code)
        )
        if workflow is None:
            raise SystemExit(f"Воронка {args.workflow_code!r} не найдена — сиды применены?")
        statuses = (
            (
                await session.execute(
                    select(WorkflowStatus).where(WorkflowStatus.workflow_id == workflow.id)
                )
            )
            .scalars()
            .all()
        )
        by_code = {s.code: s for s in statuses}
        if args.from_code not in by_code or args.to_code not in by_code:
            raise SystemExit(
                f"Статус {args.from_code!r} или {args.to_code!r} не найден в воронке "
                f"{args.workflow_code!r}"
            )
        from_status = by_code[args.from_code]
        to_status = by_code[args.to_code]

        # НФР 1: нагрузочный сценарий `transition` шлёт переход без комментария и файлов
        # (`locustfile_transition.py`), поэтому пара обязана быть без условий, действий и
        # обязательного комментария — иначе каждый переход в прогоне даёт 422, а не 200.
        transition = await session.scalar(
            select(WorkflowTransition).where(
                WorkflowTransition.workflow_id == workflow.id,
                WorkflowTransition.from_status_id == from_status.id,
                WorkflowTransition.to_status_id == to_status.id,
            )
        )
        if transition is None:
            raise SystemExit(
                f"Переход {args.from_code!r} → {args.to_code!r} не найден в воронке "
                f"{args.workflow_code!r}"
            )
        if transition.conditions or transition.actions or transition.requires_comment:
            raise SystemExit(
                f"Переход {args.from_code!r} → {args.to_code!r} несёт условия, действия или "
                "обязательный комментарий — нагрузочный сценарий transition шлёт переход без "
                "файлов и комментария и получит 422 на каждом запросе; выберите другую пару"
            )

        org = await _get_or_create_org(session)
        await session.commit()

        run_tag = uuid.uuid4().hex[:8]
        total = args.count + args.comment_pool_size
        deal_ids: list[str] = []
        comment_deal_ids: list[str] = []
        for start in range(0, total, args.batch_size):
            batch = []
            for i in range(start, min(start + args.batch_size, total)):
                deal = Deal(
                    number=f"LOADTEST-{run_tag}-{i:06d}",
                    title=f"Нагрузочный тест #{i}",
                    deal_type="b2b",
                    workflow_id=workflow.id,
                    status_id=from_status.id,
                    organization_id=org.id,
                    owner_id=kam.id,
                    created_by=kam.id,
                )
                batch.append(deal)
            session.add_all(batch)
            await session.flush()
            batch_range = range(start, min(start + args.batch_size, total))
            for i, deal in zip(batch_range, batch, strict=True):
                (comment_deal_ids if i >= args.count else deal_ids).append(str(deal.id))
            await session.commit()
            print(f"  провижининг: {min(start + args.batch_size, total)}/{total}")

    await engine.dispose()

    fixtures = {
        "workflow_id": str(workflow.id),
        "from_status_id": str(from_status.id),
        "to_status_id": str(to_status.id),
        "deal_ids": deal_ids,
        "comment_deal_ids": comment_deal_ids,
    }
    out_path = Path(__file__).parent / "fixtures.json"
    out_path.write_text(json.dumps(fixtures, ensure_ascii=False, indent=2))
    print(f"готово: {len(deal_ids)} сделок для transition, {len(comment_deal_ids)} для comment")
    print(f"манифест: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
