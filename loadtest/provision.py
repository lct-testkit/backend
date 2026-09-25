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
* `--count` сделок (по умолчанию 4000) в статусе `identification`
  воронки `b2b_university_v1`, владелец — демо-КАМ `kam.ivanov`
  (deploy/keycloak/realm-crm.json) — переход `identification→first_contact`
  ничем не обусловлен (`app/modules/workflow/seed.py`: обычный шаг вперёд
  без `conditions`/`requires_comment`), поэтому не нужно готовить
  синтетические данные под guard-условия;
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
from app.modules.workflow.models import Workflow, WorkflowStatus

DEFAULT_DATABASE_URL = "postgresql+asyncpg://crm_app:crm_app@localhost:5433/crm"
FIXTURE_ORG_NAME = "Нагрузочное тестирование — фикстура"
KAM_EMAIL = "ivanov@rt-it-school.ru"


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
            select(Workflow).where(Workflow.code == "b2b_university_v1")
        )
        if workflow is None:
            raise SystemExit("Воронка b2b_university_v1 не найдена — сиды применены?")
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
        from_status = by_code["identification"]
        to_status = by_code["first_contact"]

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
