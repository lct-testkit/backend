"""LMS: pull прогресса по расписанию + push зачисления (new_spec §4.14).

Два независимых потока делят один HTTP-клиент (`LmsClient`) и одну функцию
апсерта (`upsert_progress`): раздел 8 перечисляет и pull (наш `GET
{lms_base_url}/students/progress`, где `lms_base_url` сам уже несёт префикс
`/api` — итоговый абсолютный путь на сервере `/api/students/progress`,
инициируется нами по cron — `integration.tasks.sweep_lms_progress_pull`), и
push-приём (`POST /api/v1/integrations/lms/progress`, для LMS, которые сами
шлют обновления, не дожидаясь опроса) под одним контуром — обе ветки в
итоге приводят к одной и той же строке `learning_progress`.

**Контракт сопоставления.** Предпочтительно: прогресс сопоставляется со
сделкой по `deal_id` — **нашему собственному** UUID, а не отдельному
LMS-идентификатору. При отправке зачисления (`push_enrollment`) мы сообщаем
LMS `deal_id` сделки; вернувшиеся строки прогресса обязаны содержать то же
значение — тогда дополнительная таблица сопоставления не нужна
(`external_refs` уже занят Bitrix, где внешний id действительно чужой и
непредсказуемый). B5-6 (BUG-16): настоящий мок LMS `deal_id` не присылает
вовсе — для таких строк `_find_deal_by_contact_and_course` ищет сделку по
email учащегося (`student_email`/`email`) и коду курса
(`course_external_id`/`external_course_id`), с необязательной подстановкой
кода через `course_code_map` (B5-7), когда код курса мока и код продукта
каталога расходятся.

Push зачисления (`LEARNING_ENROLLMENT_SENT`/`LEARNING_TRANSFER_REQUESTED`,
воронка `crm.workflow.seed`) вызывается из `integration.tasks.
sweep_outbox_events`, не отсюда напрямую — outbox публикуется синхронно с
переходом сделки, доставка асинхронна (раздел 3.6). Что именно уходит в LMS,
собирает `build_enrollment_details`: действие DSL `integration_event`
публикует событие с пустой нагрузкой, и без обогащения при доставке
зачисление не несло бы ни одного учащегося.

Приём прогресса (`apply_progress_rows`) обрабатывает каждую строку в своём
SAVEPOINT: одна «битая» строка не откатывает остальные ни в push-ручке, ни в
pull-задаче. B5-8: после успешного апсерта строки — попытка движения
сделки по `routing.progress_rules` источника lms (`move_deal_by_progress`),
тоже в своём SAVEPOINT."""

from __future__ import annotations

import datetime as dt
import uuid
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.normalize import normalize_email
from app.modules.catalog.models import Contact, Organization, Product
from app.modules.crm.models import Deal, DealProduct
from app.modules.integration.http import TIMEOUT as _TIMEOUT
from app.modules.integration.http import http_errors_without_url as _http_errors_without_url_base
from app.modules.integration.models import LearningProgress, SyncCursor
from app.modules.integration.routing import ProgressRule, ProgressWhen

logger = structlog.get_logger(__name__)


def _http_errors_without_url(operation: str) -> AbstractAsyncContextManager[None]:
    """B5-2: обёртка вынесена в `integration.http` (общая с `CmsClient`) — тонкая прокладка здесь,
    чтобы не трогать остальные вызовы этого модуля."""
    return _http_errors_without_url_base("lms", operation)


#: События воронки, при доставке которых LMS получает сведения об учащемся.
ENROLLMENT_EVENTS = frozenset({"LEARNING_ENROLLMENT_SENT", "LEARNING_TRANSFER_REQUESTED"})


class LmsClient:
    def __init__(self, base_url: str, auth_token: str | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._auth_token = auth_token

    def _headers(self, idempotency_key: str | None = None) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._auth_token:
            headers["Authorization"] = f"Bearer {self._auth_token}"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    async def pull_progress(
        self, *, updated_since: str | None, max_pages: int = 20, page_size: int = 100
    ) -> list[dict[str, Any]]:
        """B5-6 (BUG-16): постранично — раньше читалась только первая страница ответа мока
        (`next_cursor` игнорировался), и строки сверх `page_size` терялись молча."""
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        for _page in range(max_pages):
            params: dict[str, Any] = {"limit": page_size}
            if updated_since:
                params["updated_since"] = updated_since
            if cursor:
                params["cursor"] = cursor
            async with (
                _http_errors_without_url("students/progress"),
                httpx.AsyncClient(timeout=_TIMEOUT) as client,
            ):
                response = await client.get(
                    f"{self._base_url}/students/progress", params=params, headers=self._headers()
                )
                response.raise_for_status()
                body = response.json()
            if isinstance(body, dict):
                page_items = body.get("items", [])
                cursor = body.get("next_cursor")
            else:
                page_items = body
                cursor = None
            if not isinstance(page_items, list) or not page_items:
                # Мусорный ответ (строка/число вместо списка) — тот же защитный приём, что
                # раньше жил в `sweep_lms_progress_pull` (`isinstance(rows, list)`): без него
                # `extend()` на строку развалил бы её на отдельные символы-«строки».
                break
            items.extend(page_items)
            if not cursor:
                break
        return items

    async def push_enrollment(
        self, payload: dict[str, Any], *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """`idempotency_key` — стабильный ключ доставки (id события outbox): повтор после сбоя
        или обрыва связи приходит к LMS с тем же ключом, и она не заводит зачисление второй раз."""
        async with (
            _http_errors_without_url("enrollments"),
            httpx.AsyncClient(timeout=_TIMEOUT) as client,
        ):
            response = await client.post(
                f"{self._base_url}/enrollments",
                json=payload,
                headers=self._headers(idempotency_key),
            )
            response.raise_for_status()
            return response.json() if response.content else {}


def _parse_dt(value: Any) -> dt.datetime | None:
    """Время из строки LMS. Без часового пояса — UTC: колонки `timestamptz`, а `asyncpg` не
    принимает для них наивное время (строка целиком уходила бы в ошибку)."""
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


class ProgressRowRejected(ValueError):
    """Строка прогресса непригодна к записи (значение неверного типа или вне диапазона): такая
    строка пропускается, а не роняет пачку. Текст — имя поля, без значения."""


_SCORE_LIMIT = Decimal(1000)  # numeric(5, 2)


def _parse_pct(value: Any) -> int | None:
    """Процент прохождения: `None` — LMS его не прислал (существующее значение не трогаем);
    иначе целое 0..100 (дробное округляется), всё остальное — строка отвергается."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ProgressRowRejected("progress_pct")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ProgressRowRejected("progress_pct") from exc
    if not number.is_finite():
        raise ProgressRowRejected("progress_pct")
    pct = int(number.to_integral_value(rounding=ROUND_HALF_UP))
    if not 0 <= pct <= 100:
        raise ProgressRowRejected("progress_pct")
    return pct


def _parse_score(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise ProgressRowRejected("score")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ProgressRowRejected("score") from exc
    if not number.is_finite() or abs(number) >= _SCORE_LIMIT:
        raise ProgressRowRejected("score")
    return number.quantize(Decimal("0.01"))


def normalize_progress_row(row: dict[str, Any]) -> dict[str, Any]:
    """B5-6 (BUG-16): мок отдаёт `external_course_id`/`email`/`external_student_id` — старые
    ключи (`course_external_id`, `deal_id`) остаются рабочими (push-приём и прежние тесты их
    используют уже сейчас): это дополнение алиасами, не замена. Если в строке УЖЕ есть старое
    имя, оно не перезатирается новым."""
    normalized = dict(row)
    if "course_external_id" not in normalized and "external_course_id" in normalized:
        normalized["course_external_id"] = normalized["external_course_id"]
    if "student_email" not in normalized and "email" in normalized:
        normalized["student_email"] = normalized["email"]
    if "student_ref" not in normalized and "external_student_id" in normalized:
        normalized["student_ref"] = normalized["external_student_id"]
    return normalized


async def _find_deal_by_contact_and_course(
    session: AsyncSession,
    *,
    student_email: str,
    course_external_id: str,
    course_code_map: dict[str, str] | None = None,
) -> Deal | None:
    """B5-6 (BUG-16): мок не присылает `deal_id` вовсе — привязка по email учащегося и курсу,
    когда строка не называет сделку напрямую. Продукт ищется по совпадению `course_external_id`
    с `Product.code` (значение мока — код курса; прямое совпадение, если коды каталога и мока
    совпадают). `course_code_map` (B5-7: `integration_sources.config.course_code_map`,
    сеется демо-профилем) — словарь «код курса мока → код продукта каталога» для случаев, когда
    они расходятся (демо-мок LMS и каталог ИТ-Школы называют один курс по-разному); пусто или нет
    совпадения — код мока используется как есть. Предпочитается открытая сделка контакта с этим
    продуктом, иначе — последняя по дате создания."""
    normalized_email = normalize_email(student_email)
    if not normalized_email:
        return None
    contact = await session.scalar(select(Contact).where(Contact.email == normalized_email))
    if contact is None or contact.deleted_at is not None:
        return None
    product_code = (course_code_map or {}).get(course_external_id, course_external_id)
    product = await session.scalar(select(Product).where(Product.code == product_code))
    if product is None:
        return None
    candidates = (
        (
            await session.execute(
                select(Deal)
                .join(DealProduct, DealProduct.deal_id == Deal.id)
                .where(
                    Deal.contact_id == contact.id,
                    Deal.deleted_at.is_(None),
                    DealProduct.product_id == product.id,
                )
                .order_by(Deal.closed_at.is_(None).desc(), Deal.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return candidates[0] if candidates else None


async def upsert_progress(
    session: AsyncSession, row: dict[str, Any], *, course_code_map: dict[str, str] | None = None
) -> LearningProgress | None:
    """Один элемент ответа LMS → строка `learning_progress`; `None` — строка ни к какой нашей сделке
    не относится (нет способа её привязать, чужой или удалённый `deal_id`/контакт/продукт): LMS
    знает и учащихся, которых в CRM нет, это не ошибка. Значения неверного типа или вне диапазона —
    `ProgressRowRejected` (вызывающий пропускает строку, см. `apply_progress_rows`).

    Неполная строка не затирает известное: поле, которого в строке нет (или оно `null`), у
    существующей записи остаётся как было — LMS присылает и «только процент», и «только
    завершение», и обнулять остальное они не просят. `raw` — последняя присланная строка.

    `course_code_map` — см. `_find_deal_by_contact_and_course`; используется только когда строка
    не называет сделку напрямую (мок без `deal_id`)."""
    row = normalize_progress_row(row)
    deal_id_raw = row.get("deal_id")
    course_external_id = row.get("course_external_id")
    if not course_external_id:
        logger.warning("lms_progress_row_unmatched", deal_id=str(deal_id_raw)[:64])
        return None

    deal: Deal | None = None
    if deal_id_raw:
        try:
            deal_id = uuid.UUID(str(deal_id_raw))
        except ValueError:
            logger.warning("lms_progress_row_bad_deal_id", deal_id_raw=str(deal_id_raw)[:64])
            return None
        deal = await session.get(Deal, deal_id)
    elif row.get("student_email"):
        # BUG-16: мок не называет сделку напрямую — привязка по email учащегося и коду курса.
        deal = await _find_deal_by_contact_and_course(
            session,
            student_email=str(row["student_email"]),
            course_external_id=course_external_id,
            course_code_map=course_code_map,
        )

    if deal is None or deal.deleted_at is not None:
        logger.warning("lms_progress_deal_not_found", deal_id=str(deal_id_raw))
        return None

    # Значения проверяются до записи: отвергнутая строка не оставляет полузаписанной записи.
    reported: dict[str, Any] = {
        "enrolled_at": _parse_dt(row.get("enrolled_at")),
        "progress_pct": _parse_pct(row.get("progress_pct")),
        "score": _parse_score(row.get("score")),
        "completed_at": _parse_dt(row.get("completed_at")),
        "last_activity_at": _parse_dt(row.get("last_activity_at")),
    }

    product_id = (
        (
            await session.execute(
                select(DealProduct.product_id).where(DealProduct.deal_id == deal.id).limit(1)
            )
        )
        .scalars()
        .first()
    )
    reported["contact_id"] = deal.contact_id
    reported["product_id"] = product_id
    values = {key: value for key, value in reported.items() if value is not None}
    values.update(
        deal_id=deal.id,
        external_course_id=str(course_external_id),
        raw=row,
        synced_at=dt.datetime.now(dt.UTC),
    )

    existing = (
        await session.execute(
            select(LearningProgress).where(
                LearningProgress.deal_id == deal.id,
                LearningProgress.external_course_id == str(course_external_id),
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        existing = LearningProgress(**values)
        session.add(existing)
    else:
        for key, value in values.items():
            setattr(existing, key, value)
    await session.flush()
    return existing


APPLIED = "applied"
UNMATCHED = "unmatched"
FAILED = "failed"


@dataclass(slots=True)
class ProgressBatchResult:
    """Итог пачки строк прогресса: по одному исходу на каждую входную строку, в том же порядке.

    B5-8: `moved`/`move_failed` — отдельные счётчики попыток движения сделки по `progress_rules`
    (не по одному на строку прогресса: строка без подходящего правила не считается ни тем, ни
    другим)."""

    outcomes: list[str] = field(default_factory=list)
    moved: int = 0
    move_failed: int = 0

    @property
    def applied(self) -> int:
        return self.outcomes.count(APPLIED)

    @property
    def failed(self) -> int:
        return self.outcomes.count(FAILED)

    @property
    def skipped(self) -> int:
        """Не применено: чужие строки (`unmatched`) и строки с ошибкой (`failed`)."""
        return len(self.outcomes) - self.applied


def _progress_matches(when: ProgressWhen, progress: LearningProgress) -> bool:
    if when.completed and progress.completed_at is None:
        return False
    return not (
        when.progress_pct_gte is not None
        and (progress.progress_pct is None or progress.progress_pct < when.progress_pct_gte)
    )


#: Исход попытки движения сделки по прогрессу: нет подходящего правила (строка не в счётчиках),
#: переход выполнен (`moved`) или правило подошло, но переход не удался (`move_failed`).
NOT_MATCHED = "not_matched"
MOVED = "moved"
MOVE_FAILED = "move_failed"


async def move_deal_by_progress(
    session: AsyncSession, progress: LearningProgress, rules: list[ProgressRule]
) -> str:
    """B5-8: двигает сделку по подходящему правилу `progress_rules` (маршрутизация источника
    `lms`, B5-4). Возвращает `NOT_MATCHED` (ни одно правило не подошло — не ошибка, большинство
    строк прогресса), `MOVED` (переход выполнен) или `MOVE_FAILED` (правило подошло, но сделку
    нельзя двигать, не трогая её: удалена, статус правила пропал из графа). Правило не
    срабатывает дважды: после перехода текущий статус сделки уже другой и не совпадёт с
    `from_status` на следующем прогоне.

    Вызывается внутри ОТДЕЛЬНОГО SAVEPOINT вызывающим (`apply_progress_rows`): отказ перехода не
    должен откатывать саму запись прогресса, применённую непосредственно перед этим. Отказ самого
    перехода (`AppError`: сделка закрыта, конфликт версии, нет перехода в графе) здесь НЕ
    превращается в `MOVE_FAILED` — он пробрасывается наружу, чтобы SAVEPOINT откатил всё, что
    `DealService.transition` успел записать до отказа (`claim_version` бьёт UPDATE сразу, ещё до
    проверки условий: иначе версия сделки росла бы на каждом опросе LMS)."""
    if not rules or progress.deal_id is None:
        return NOT_MATCHED

    # Ленивые импорты — тот же приём, что `catalog.service`/`identity.admin_service` уже
    # применяют к `crm.service`: `crm` не ссылается на `integration`, цикл не создаётся.
    from app.modules.workflow.models import Workflow, WorkflowState
    from app.modules.workflow.service import get_cached_published_graph

    deal = await session.get(Deal, progress.deal_id)
    if deal is None or deal.workflow_id is None:
        return NOT_MATCHED
    workflow = await session.get(Workflow, deal.workflow_id)
    if workflow is None or workflow.state != WorkflowState.PUBLISHED.value:
        return NOT_MATCHED

    graph = await get_cached_published_graph(workflow)
    statuses_by_id = {s["id"]: s for s in graph["statuses"]}
    current = statuses_by_id.get(str(deal.status_id))
    if current is None:
        return NOT_MATCHED

    matched = next(
        (
            rule
            for rule in rules
            if rule.workflow_code == workflow.code
            and rule.from_status == current["code"]
            and _progress_matches(rule.when, progress)
        ),
        None,
    )
    if matched is None:
        return NOT_MATCHED
    if deal.deleted_at is not None:
        logger.warning("lms_progress_move_failed", deal_id=str(deal.id), reason="deal_deleted")
        return MOVE_FAILED
    target = next((s for s in graph["statuses"] if s["code"] == matched.to_status), None)
    if target is None:
        logger.warning(
            "lms_progress_rule_status_missing",
            workflow_code=workflow.code,
            to_status=matched.to_status,
        )
        return MOVE_FAILED

    from app.modules.crm.service import DealService
    from app.modules.integration.service import get_integration_principal

    principal = await get_integration_principal(session)
    comment = matched.comment.replace("{progress_pct}", str(progress.progress_pct or 0)) or None
    try:
        await DealService(session).transition(
            deal,
            principal,
            to_status_id=uuid.UUID(target["id"]),
            comment=comment,
            fields={},
            expected_version=deal.version,
        )
    except AppError as exc:
        logger.warning(
            "lms_progress_move_failed",
            deal_id=str(deal.id),
            workflow_code=workflow.code,
            from_status=current["code"],
            to_status=matched.to_status,
            error=exc.code.value,
        )
        raise  # SAVEPOINT вызывающего откатит частичные записи перехода
    return MOVED


async def apply_progress_rows(
    session: AsyncSession,
    rows: list[Any],
    *,
    progress_rules: list[ProgressRule] | None = None,
    course_code_map: dict[str, str] | None = None,
) -> ProgressBatchResult:
    """Применяет строки прогресса, каждую в своём SAVEPOINT: строка с ошибкой (неверный тип,
    значение вне диапазона, сбой БД) откатывает только себя и попадает в `failed`, остальные
    строки и транзакция целиком не страдают. Раньше первая же такая строка роняла всю пачку —
    push-ручка отвечала 500, pull-задача терялась до следующего тика с тем же результатом.

    Элементы, которые не объекты (`null`, числа), — мусор выгрузки: пропускаются без ошибки.

    B5-8: после успешного `upsert_progress` каждой строки — попытка движения сделки по
    `progress_rules`, в СВОЁМ отдельном SAVEPOINT (отказ перехода не должен откатывать уже
    записанный прогресс)."""
    result = ProgressBatchResult()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            logger.warning("lms_progress_row_not_object", index=index)
            result.outcomes.append(UNMATCHED)
            continue
        try:
            async with session.begin_nested():
                applied = await upsert_progress(session, row, course_code_map=course_code_map)
        except Exception as exc:  # noqa: BLE001 — одна строка не должна ронять пачку
            logger.warning(
                "lms_progress_row_failed",
                index=index,
                deal_id=str(row.get("deal_id"))[:64],
                error_type=type(exc).__name__,
                field=str(exc)[:64] if isinstance(exc, ProgressRowRejected) else None,
            )
            result.outcomes.append(FAILED)
            continue
        result.outcomes.append(APPLIED if applied is not None else UNMATCHED)
        if applied is not None and progress_rules:
            try:
                async with session.begin_nested():
                    outcome = await move_deal_by_progress(session, applied, progress_rules)
            except AppError:
                # Отказ перехода: причина уже в логе `move_deal_by_progress`, а SAVEPOINT откатил
                # всё, что переход успел записать до отказа.
                outcome = MOVE_FAILED
            except Exception as exc:  # noqa: BLE001 — движение сделки не должно ронять пачку
                logger.warning(
                    "lms_progress_move_error", index=index, error_type=type(exc).__name__
                )
                outcome = MOVE_FAILED
            if outcome == MOVED:
                result.moved += 1
            elif outcome == MOVE_FAILED:
                result.move_failed += 1
    return result


def advance_cursor(cursor: str | None, rows: list[Any], result: ProgressBatchResult) -> str | None:
    """Новое значение курсора pull-задачи: наибольший `updated_at` среди строк, но не дальше
    самой ранней строки, которую применить не удалось, — её (и всё позже неё) следующий тик
    получит снова. Чужие строки (`unmatched`) курсор не держат: LMS знает и учащихся, которых в
    CRM нет, и иначе курсор не сдвинулся бы никогда. Строка с ошибкой без `updated_at`
    определить своё место не позволяет — в этот тик курсор не двигается вовсе.

    Цена строгого правила: строка, которую не применить никогда (неверные данные в LMS), держит
    курсор, пока LMS её не исправит (новый `updated_at`); идемпотентный повтор остальных строк
    безвреден, а сама задержка видна в логе (`lms_progress_row_failed`)."""

    def mark(row: Any) -> str | None:
        value = row.get("updated_at") if isinstance(row, dict) else None
        return str(value) if value else None

    ceiling: str | None = None
    for row, outcome in zip(rows, result.outcomes, strict=True):
        if outcome != FAILED:
            continue
        failed_at = mark(row)
        if failed_at is None:
            return cursor  # место сбойной строки неизвестно: не рискуем перешагнуть её
        ceiling = failed_at if ceiling is None else min(ceiling, failed_at)

    latest = cursor
    for row, outcome in zip(rows, result.outcomes, strict=True):
        candidate = mark(row)
        if outcome == FAILED or candidate is None:
            continue
        if ceiling is not None and candidate >= ceiling:
            continue
        if latest is None or candidate > latest:
            latest = candidate
    return latest


# --- Зачисление: что уходит в LMS ------------------------------------------------------------


def _compact(values: dict[str, Any]) -> dict[str, Any]:
    """Только заполненные значения: пустое поле в исходящем зачислении не отправляется."""
    return {key: value for key, value in values.items() if value not in (None, "")}


async def build_enrollment_details(session: AsyncSession, deal_id: uuid.UUID) -> dict[str, Any]:
    """Сведения о сделке для зачисления в LMS (`LEARNING_ENROLLMENT_SENT`/
    `LEARNING_TRANSFER_REQUESTED`): номер сделки и заказа, номер потока и продукт (по первой строке
    продуктов сделки), учащийся (B2C — ФИО, email, телефон полностью) или организация (B2B —
    название и ИНН). Собирается при доставке, а не при публикации события, поэтому отражает
    актуальные данные, а не снимок на момент перехода.

    Учащийся уходит в LMS не маскированным намеренно: без ФИО и контактов зачислять некого.
    Чего нет (сделка удалена, нет продукта, контакт обезличен), того в ответе нет — доставка из-за
    неполных данных не падает."""
    deal = await session.get(Deal, deal_id)
    if deal is None or deal.deleted_at is not None:
        return {}

    details = _compact(
        {
            "deal_number": deal.number,
            "order_number": deal.order_number,
            # B5-6 (BUG-17): мок требует именно `deal_ref` — старое `deal_number` остаётся как
            # дополнение, не замена (прежние получатели зачисления не ломаются).
            "deal_ref": deal.number,
        }
    )

    line = (
        (
            await session.execute(
                select(DealProduct)
                .where(DealProduct.deal_id == deal.id)
                .order_by(DealProduct.created_at, DealProduct.id)
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    if line is not None:
        if line.stream_number is not None:
            details["stream_number"] = line.stream_number
        product = await session.get(Product, line.product_id)
        if product is not None:
            details["product"] = _compact({"code": product.code, "name": product.name})
            # B5-6 (BUG-17): мок ждёт код курса на верхнем уровне нагрузки, не только в `product`.
            details["external_course_id"] = product.code

    if deal.deal_type == "b2c" and deal.contact_id is not None:
        contact = await session.get(Contact, deal.contact_id)
        # Обезличенный контакт — это «Контакт #abc12345» без имени и связи: учащимся не является.
        if contact is not None and contact.deleted_at is None and not contact.is_anonymized:
            learner = _compact(
                {
                    "last_name": contact.last_name,
                    "first_name": contact.first_name,
                    "middle_name": contact.middle_name,
                    "email": contact.email,
                    "phone": contact.phone,
                }
            )
            if learner:
                details["learner"] = learner
                # B5-6 (BUG-17): мок требует вложенный объект `student` с именно этими тремя
                # полями (без отчества/телефона) — `learner` выше остаётся для совместимости.
                student = _compact(
                    {
                        "email": contact.email,
                        "first_name": contact.first_name,
                        "last_name": contact.last_name,
                    }
                )
                if student:
                    details["student"] = student
    elif deal.deal_type == "b2b" and deal.organization_id is not None:
        organization = await session.get(Organization, deal.organization_id)
        if organization is not None and organization.deleted_at is None:
            legal_entity = _compact({"name": organization.name, "inn": organization.inn})
            if legal_entity:
                details["organization"] = legal_entity
    return details


async def get_cursor(session: AsyncSession, *, source_code: str, resource: str) -> str | None:
    cursor = (
        await session.execute(
            select(SyncCursor).where(
                SyncCursor.source_code == source_code, SyncCursor.resource == resource
            )
        )
    ).scalar_one_or_none()
    return cursor.cursor_value if cursor else None


async def set_cursor(session: AsyncSession, *, source_code: str, resource: str, value: str) -> None:
    cursor = (
        await session.execute(
            select(SyncCursor).where(
                SyncCursor.source_code == source_code, SyncCursor.resource == resource
            )
        )
    ).scalar_one_or_none()
    if cursor is None:
        session.add(SyncCursor(source_code=source_code, resource=resource, cursor_value=value))
    else:
        cursor.cursor_value = value
    await session.flush()
